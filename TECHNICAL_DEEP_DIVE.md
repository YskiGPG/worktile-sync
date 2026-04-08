# Worktile 网盘同步工具 — 技术深度解析

> 本文档从架构设计到代码实现、从逆向工程到生产部署，完整记录了这个项目的技术细节。适合作为 SDE 学习逆向工程、双向同步算法、Docker 部署的实战参考。

---

## 目录

1. [项目背景与动机](#1-项目背景与动机)
2. [整体架构](#2-整体架构)
3. [逆向工程：从零到六个 API](#3-逆向工程从零到六个-api)
4. [认证机制深度解析](#4-认证机制深度解析)
5. [核心代码逐层拆解](#5-核心代码逐层拆解)
6. [双向同步算法](#6-双向同步算法)
7. [并发下载与线程安全](#7-并发下载与线程安全)
8. [错误处理与弹性设计](#8-错误处理与弹性设计)
9. [状态持久化与原子写入](#9-状态持久化与原子写入)
10. [监控与告警](#10-监控与告警)
11. [Docker 部署与群晖 NAS](#11-docker-部署与群晖-nas)
12. [踩过的坑与解决方案](#12-踩过的坑与解决方案)
13. [设计决策与权衡](#13-设计决策与权衡)
14. [测试策略](#14-测试策略)
15. [未来改进方向](#15-未来改进方向)

---

## 1. 项目背景与动机

### 问题

公司使用 Worktile 作为项目管理和文件存储平台。Worktile 有"网盘"功能但：

- **没有官方 API** 支持网盘文件操作（官方 API 只覆盖任务、项目等）
- **没有桌面同步客户端**（不像 Dropbox/OneDrive）
- 员工需要手动从网页版上传下载文件

### 目标

实现 Worktile 网盘 <-> 群晖 NAS 本地文件夹的自动双向同步，再通过 Synology Drive 同步到员工电脑：

```
Worktile 网盘（云端）
    ↕  本工具（Docker 容器，60秒轮询）
群晖 NAS 本地文件夹
    ↕  Synology Drive（原生同步）
员工电脑
```

### 技术挑战

1. 没有 API 文档，需要逆向工程
2. 认证机制非标准（跨域 Cookie 传递）
3. 文件下载涉及腾讯云 COS 签名
4. 中文文件名可能超出文件系统限制
5. 双向同步的冲突处理
6. 部署在低配 NAS（DS218+, 2GB RAM）上需要轻量

---

## 2. 整体架构

### 模块划分

```
src/
├── main.py      # 入口：配置加载、主循环、告警
├── api.py       # HTTP 客户端：6 个 Worktile API 的封装
├── auth.py      # 认证：Cookie 管理、跨域 Header
├── sync.py      # 核心：双向同步引擎
├── state.py     # 持久化：JSON 状态文件
├── notify.py    # 通知：邮件 + Webhook
└── utils.py     # 工具：日志、文件名截断、哈希
```

### 数据流

```
main.py (主循环, 每 60s)
  └─> SyncEngine.sync_once()
        ├─> api.list_root_folders()        # 获取根目录
        ├─> api.list_files(folder_id)      # 递归获取每个文件夹
        ├─> _sync_file() 对每个文件做决策:
        │     ├─ 远程有、本地无 → api.download_file()
        │     ├─ 本地有、远程无 → api.upload_file()
        │     ├─ 双方都有、远程更新 → download
        │     ├─ 双方都有、本地更新 → upload
        │     └─ 双方都改了 → 冲突处理
        ├─> _flush_downloads()             # 并发执行下载队列
        └─> state.save()                   # 持久化状态

main.py (循环后)
  ├─> 写入 sync_health.json
  ├─> 检查连续错误 → notifier.send()
  └─> sleep(interval)
```

### 依赖

```
httpx >= 0.27    # HTTP 客户端（支持流式下载、连接池）
pyyaml >= 6.0    # YAML 配置解析
```

为什么选 httpx 而不是 requests？
- 原生支持流式下载 (`client.stream()`)
- 内置连接池（并发下载需要）
- 更好的超时控制（读写分别设置）
- async 预留（未来可能需要）

---

## 3. 逆向工程：从零到六个 API

### 方法论

逆向一个没有 API 文档的 Web 应用，核心思路：

1. **浏览器 DevTools Network 面板** — 操作网页版，观察发出的 HTTP 请求
2. **分析请求/响应结构** — 理解参数含义和返回格式
3. **前端 JS 源码搜索** — 当 Network 面板不够时，直接读前端代码
4. **试错验证** — 用 curl/Python 脚本重放请求

### 3.1 文件列表接口（Network 面板）

最简单的逆向。在 Worktile 网盘页面刷新，Network 面板直接看到：

```
GET /api/drives/folders?parent_id=&belong=2&sort_by=updated_at&sort_type=-1&t=1775589257987
```

关键发现：
- `belong=2` 表示"企业网盘"（个人网盘是 `belong=1`）
- `t` 是毫秒时间戳，防缓存
- 根目录 `folders` 接口返回 `data` 是直接数组 `[]`
- 子目录 `list` 接口返回 `data` 是分页对象 `{value: [], count, page_count, ...}`

**注意事项**：两个接口返回结构不同！这是一个容易踩的坑。

```python
# folders 接口（根目录）
data = resp.json()
items = data.get("data", [])  # data 直接是数组

# list 接口（子目录）
data = resp.json()
page_data = data.get("data", {})
items = page_data.get("value", [])  # data 是对象，value 才是数组
```

### 3.2 文件下载接口（JS 逆向）

这是最难的部分。文件存储在腾讯云 COS 上，URL 需要签名。

**第一轮尝试：Network 面板**

点击下载按钮，看到请求：
```
GET https://app-wt-original-release-1348738073.cos.ap-shanghai.myqcloud.com/f4389d70...?sign=q-sign-algorithm%3Dsha1%26...
```

COS URL 带签名参数。签名需要 SecretKey，但 SecretKey 在哪？

**第二轮尝试：搜索 JS Bundle**

1. 从 HTML 页面提取所有 JS 文件 URL（注意：实际 JS 托管在 CDN `cdn-aliyun.worktile.com/pro/js/`，不是主域名）
2. 下载 13 个 JS Bundle（总计约 20MB 压缩代码）
3. 搜索关键词：`AKIDjKD0b1EDyZx78B8yfIfsu1yhDXkncDds`（COS SecretId）

结果：**JS 里找不到 SecretKey**。说明签名不在前端完成。

**第三轮尝试：找到 driveRealUrl**

在 Angular 前端代码中搜索 `download`、`drive` 等关键词，找到一个 Angular Pipe：

```javascript
// app-vnext.bundle.min.js (反混淆后)
driveRealUrl: function(fileId, teamId, version) {
    return boxUrl + '/drives/' + fileId + 
           '?team_id=' + teamId + 
           '&version=' + version + 
           '&action=download';
}
```

关键洞察：**前端不直接访问 COS，而是通过 wt-box.worktile.com 中转**。wt-box 服务端自动生成 COS 签名，客户端不需要 SecretKey。

最终下载 URL：
```
GET https://wt-box.worktile.com/drives/{file_id}?team_id={team_id}&version={version}&action=download
Headers: x-cookies: <完整cookie字符串>
```

### 3.3 文件上传接口（Network 面板 + JS 验证）

上传文件时抓包：

```
POST https://wt-box.worktile.com/drive/upload?belong=2&parent_id={folder_id}&team_id={team_id}
Content-Type: multipart/form-data
Headers: x-cookies: <cookie>
Body: title=文件名&file=<二进制>
```

注意跨域处理：`wt-box.worktile.com` 和主域名 `zrhubei.worktile.com` 不同源，浏览器不会自动携带 Cookie。前端通过自定义 Header `x-cookies` 传递。

### 3.4 创建文件夹接口（JS 逆向）

Network 面板看不到（测试时没有创建文件夹操作），需要从 JS 中搜索：

```javascript
// 从前端代码中找到
addFolder: function(data) {
    return http.post('/api/drive/folder', data);
}
```

**踩坑**：路径是 `/api/drive/folder`（单数），不是 `/api/drives/folders`（复数）。第一次用复数路径返回 404。

必要参数（从 JS 逆向得到）：
```json
{
    "parent_id": "父文件夹ID",
    "title": "新文件夹",
    "belong": 2,
    "visibility": 1,
    "permission": 4,
    "members": [],
    "color": "#6698FF"
}
```

### 3.5 删除文件接口（JS 逆向）

```javascript
deleteDrive: function(id) {
    return http.delete('/api/drives/' + id);
}
```

软删除（移到回收站）。还有 `removeAll` 和 `/real` 端点用于批量删除和彻底删除。

### 已确认的 6 个接口汇总

| 功能 | 方法 | 端点 | 域名 | 认证 |
|------|------|------|------|------|
| 根文件夹列表 | GET | `/api/drives/folders` | 主域名 | Cookie |
| 文件列表（分页） | GET | `/api/drives/list` | 主域名 | Cookie |
| 下载文件 | GET | `/drives/{id}` | wt-box | x-cookies |
| 上传文件 | POST | `/drive/upload` | wt-box | x-cookies |
| 创建文件夹 | POST | `/api/drive/folder` | 主域名 | Cookie |
| 删除文件 | DELETE | `/api/drives/{id}` | 主域名 | Cookie |

---

## 4. 认证机制深度解析

### 双域名认证模型

Worktile 使用两个域名，认证方式不同：

```
主域名: zrhubei.worktile.com    → 标准 Cookie Header
文件域名: wt-box.worktile.com   → 自定义 x-cookies Header
```

为什么文件域名用 `x-cookies`？因为 wt-box 是独立域名，浏览器的同源策略不允许跨域携带 Cookie。Worktile 前端的解决方案是把 Cookie 放在自定义 Header 里，由 wt-box 服务端手动解析。

### 关键 Cookie

```
s-695364022dd1c542de797196=655b2e20872e4e54ae746defaad37f83
```

格式：`s-{team_id}={session_token}`

### 代码实现

```python
class AuthManager:
    def get_headers(self) -> dict[str, str]:
        """主域名：标准 Cookie"""
        return {"Cookie": self.cookie_value}

    def get_box_headers(self) -> dict[str, str]:
        """wt-box：自定义 Header"""
        return {"x-cookies": self.cookie_value}
```

### Cookie 生命周期

- Worktile Cookie 会过期（具体时长未知，估计数天到数周）
- 过期后所有请求返回 401/403
- 没有 refresh token 机制（逆向接口，无法自动刷新）
- 只能手动从浏览器重新复制 Cookie

这是这类逆向方案的固有局限——没有 OAuth 那样的自动续期机制。

---

## 5. 核心代码逐层拆解

### 5.1 API 客户端 (api.py)

#### 数据模型

```python
@dataclass
class FileInfo:
    id: str           # Worktile _id
    name: str         # title（文件/文件夹名）
    is_folder: bool   # type==1 文件夹, type==3 文件
    size: int         # addition.size
    mtime: int        # updated_at（Unix 秒）
    parent_id: str    # parent
    cos_key: str      # addition.path（COS 文件 key）
    version: int      # addition.current_version
```

Worktile 的 `type` 字段：1 = 文件夹，3 = 文件。其他值（如 2）未知，可能是其他类型的 drive 项目。

#### 双 Client 架构

```python
class WorktileAPI:
    def __init__(self, base_url, box_url, team_id, auth):
        # 主域名 Client（元数据操作）
        self.client = httpx.Client(
            base_url=base_url,
            timeout=httpx.Timeout(30.0, read=300.0),
        )
        # 文件域名 Client（上传/下载，超时更长）
        self.box_client = httpx.Client(
            base_url=box_url,
            timeout=httpx.Timeout(30.0, read=600.0),
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        )
```

为什么分两个 Client？
- 不同的 base_url
- 不同的超时配置（文件传输需要更长时间）
- 不同的认证 Header
- 独立的连接池（并发下载不影响元数据请求）

#### 重试与指数退避

```python
MAX_RETRIES = 3
RETRY_BACKOFF = 2  # 秒

def _request(self, method, path, *, use_box=False, **kwargs):
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = client.request(method, path, headers=headers, **kwargs)
            if self.auth.is_auth_error(resp.status_code):
                self.auth.handle_auth_failure()
                raise WorktileAPIError("认证失败", resp.status_code)
            resp.raise_for_status()
            return resp
        except (httpx.HTTPStatusError, httpx.TransportError) as e:
            if attempt < MAX_RETRIES:
                wait = RETRY_BACKOFF ** attempt  # 2s, 4s
                time.sleep(wait)
    raise WorktileAPIError(f"请求失败: {last_exc}")
```

退避策略：`2^attempt` 秒 → 第一次重试等 2 秒，第二次等 4 秒。

#### 流式下载

```python
def download_file(self, file_info, save_path):
    with self.box_client.stream("GET", path, params=params, headers=headers,
                                 follow_redirects=True) as resp:
        resp.raise_for_status()
        with open(save_path, "wb") as f:
            for chunk in resp.iter_bytes(chunk_size=65536):
                f.write(chunk)
```

为什么用流式？文件可能很大（截图中最大 753MB），一次性读入内存会 OOM。`chunk_size=65536`（64KB）是网络 I/O 和内存使用的平衡点。

#### 分页处理

```python
def list_files(self, folder_id):
    all_items = []
    page = 0
    while True:
        resp = self._request("GET", "/api/drives/list", params={
            "pi": page, "ps": 200, ...
        })
        page_data = resp.json().get("data", {})
        items = page_data.get("value", [])
        if not items:
            break
        all_items.extend([self._parse_item(item) for item in items])
        if page + 1 >= page_data.get("page_count", 1):
            break
        page += 1
    return all_items
```

每页最多 200 条。如果文件夹有 500 个文件，需要 3 次请求（0-199, 200-399, 400-499）。

### 5.2 同步状态 (state.py)

#### 为什么需要状态？

没有状态文件，每次同步都是"首次同步"，无法区分：
- "这个文件是新创建的" vs "这个文件上次就同步过了"
- "远程文件被删了" vs "这个文件我还没同步过"

#### 状态模型

```python
@dataclass
class FileRecord:
    name: str              # 文件名
    remote_id: str         # Worktile _id
    remote_mtime: int      # 上次同步时的远程修改时间
    remote_size: int       # 上次同步时的远程大小
    local_mtime: float     # 上次同步时的本地修改时间
    local_size: int        # 上次同步时的本地大小
    last_sync: str         # 最后同步的 ISO 时间

@dataclass
class SyncState:
    files: dict[str, FileRecord]  # key = 相对路径
```

通过比较"当前值"和"上次同步时的值"，可以判断文件是否被修改过。

#### 原子写入

```python
def save(self, path):
    tmp_path = path.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    tmp_path.rename(path)  # rename 是原子操作
```

为什么要原子写入？如果直接 `write_text` 写到目标文件，写到一半断电/进程被杀：
- 文件内容不完整
- JSON 解析失败
- 所有同步状态丢失，回到首次同步

`rename` 在同一文件系统上是原子操作（POSIX 保证），要么完全成功，要么完全不发生。

### 5.3 文件名截断 (utils.py)

#### 问题

Linux ext4 文件系统单个文件名最长 **255 字节**。中文 UTF-8 编码每字符 3 字节，所以最多约 85 个中文字符。Worktile 上的文件名经常超过这个限制（如很长的工程项目名）。

#### 解决方案

```python
MAX_NAME_BYTES = 255

def safe_name(name: str) -> str:
    encoded = name.encode("utf-8")
    if len(encoded) <= MAX_NAME_BYTES:
        return name  # 不需要截断

    # 分离扩展名
    dot_idx = name.rfind(".")
    stem, ext = name[:dot_idx], name[dot_idx:]

    # 用 MD5 hash 避免截断后重名
    name_hash = hashlib.md5(name.encode("utf-8")).hexdigest()[:8]
    suffix = f"_{name_hash}{ext}"

    # 逐字符截断 stem 直到总长度 ≤ 255 字节
    max_stem_bytes = MAX_NAME_BYTES - len(suffix.encode("utf-8"))
    truncated = ""
    byte_count = 0
    for ch in stem:
        ch_bytes = len(ch.encode("utf-8"))
        if byte_count + ch_bytes > max_stem_bytes:
            break
        truncated += ch
        byte_count += ch_bytes

    return truncated + suffix
```

示例：
```
原名：14、招标、投标资料（包括：招标文件、资格审查报告、评标报告、...电子版.pdf（300+ 字节）
截断：14、招标、投标资料（包括：招标文件、资格审查报告_a3f8b2c1.pdf（≤ 255 字节）
```

为什么用 MD5 hash 后缀？防止不同的长文件名截断后变成相同的短名。8 位 hex = 4 字节 = 2^32 种组合，对于同一目录下的文件足够。

---

## 6. 双向同步算法

### 核心决策矩阵

同步引擎对每个文件做以下判断：

```
                远程有    远程无
本地有          Case 3    Case 2
本地无          Case 1    (不存在)
```

再结合状态文件（`prev`）：

| Case | 条件 | `prev` 存在？ | 动作 |
|------|------|:---:|------|
| 1a | 远程有、本地无 | 无 | 下载（新文件） |
| 1b | 远程有、本地无 | 有 | 本地曾有但被删了 → 删除远程（如果 sync_delete） |
| 2a | 本地有、远程无 | 无 | 上传（新文件） |
| 2b | 本地有、远程无 | 有 | 远程曾有但被删了 → 删除本地（如果 sync_delete） |
| 3a | 双方都有 | 无 | 首次同步：大小一致则跳过，否则以远程为准 |
| 3b | 双方都有 | 有 | 比较变化，决定方向（见下文） |

### Case 3b 的变化检测

```python
remote_changed = (remote.mtime != prev.remote_mtime or remote.size != prev.remote_size)
local_changed = (abs(local_ts - prev.local_mtime) > 1 or local_size != prev.local_size)
```

| remote_changed | local_changed | 动作 |
|:-:|:-:|------|
| 是 | 是 | 冲突！备份 + 以更新的为准 |
| 是 | 否 | 下载（远程更新了） |
| 否 | 是 | 上传（本地更新了） |
| 否 | 否 | 无变化，跳过 |

### 冲突处理

```python
def _handle_conflict(self, remote, local_path, folder_id, rel_path):
    # 备份本地文件为 filename.conflict.ext
    conflict_path = local_path.parent / f"{stem}.conflict{suffix}"
    shutil.copy2(local_path, conflict_path)

    # 以修改时间更新的为准
    if remote_ts >= local_ts:
        self._download(remote, local_path, rel_path)
    else:
        self._upload(folder_id, local_path, rel_path)
```

冲突策略是"最后写入胜出"（Last Write Wins），但保留了败方的备份副本。

### 首次同步的特殊处理

首次同步（无 `prev` 状态）是最容易出 bug 的地方：

```python
if not prev:
    if remote.size == local_size:
        # 大小一致 → 视为已同步，不传输
        self._record_state(rel_path, remote, local)
    else:
        # 大小不同 → 以远程为准（保守策略）
        self._download(remote, local, rel_path)
```

为什么不比较时间戳？因为本地文件的 `mtime` 可能是下载时间（比远程 `updated_at` 新），如果比较时间会误判为"本地更新了"，导致重复上传。

为什么首次以远程为准？因为 Worktile 是"源头"（source of truth），本地是副本。首次同步时应该信任远程。

### 递归遍历

```python
def _sync_folder(self, folder_id, local_path, rel_prefix):
    local_path.mkdir(parents=True, exist_ok=True)

    remote_items = self.api.list_files(folder_id)
    remote_map = {f.name: f for f in remote_items}

    local_entries = {entry.name: entry for entry in local_path.iterdir()
                     if not self._should_ignore(entry.name)}

    all_names = set(remote_map.keys()) | set(local_entries.keys())

    for name in sorted(all_names):
        if remote.is_folder:
            self._sync_folder(remote.id, sub_local, rel_path)  # 递归
        else:
            self._sync_file(...)
```

关键设计：`all_names = remote ∪ local` 确保两边的文件都被处理到。排序保证处理顺序确定性（方便调试和日志阅读）。

---

## 7. 并发下载与线程安全

### 设计思路

首次同步 1760 个文件，串行下载花了 10+ 小时。并发可以显著加速。

架构选择：**两阶段模型**

```
阶段 1: 遍历（串行）
  └─> 递归遍历所有文件夹
  └─> 对每个需要下载的文件，加入队列（不立即下载）

阶段 2: 下载（并发）
  └─> ThreadPoolExecutor 并发执行队列中的下载任务
```

为什么不在遍历过程中就并发？因为遍历涉及 API 调用（list_files），并发 API 调用可能触发 Worktile 的频率限制。把 API 调用（串行）和文件下载（并发）分开是更安全的策略。

### 实现

```python
def _download(self, remote, save_path, rel_path):
    if self.max_workers > 1:
        self._download_tasks.append((remote, save_path, rel_path))  # 入队
    else:
        self._do_download(remote, save_path, rel_path)  # 直接执行

def _flush_downloads(self):
    with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
        futures = [executor.submit(self._do_download, *task)
                   for task in self._download_tasks]
        for future in as_completed(futures):
            pass
```

### 线程安全

共享可变状态：
- `self.stats` — 计数器字典
- `self.state.files` — 状态字典

解决方案：用 `threading.Lock` 保护所有共享状态的写操作。

```python
def _do_download(self, remote, save_path, rel_path):
    try:
        self.api.download_file(remote, save_path)  # I/O 密集，不持锁
        with self._lock:  # 只在更新状态时持锁
            self._record_state(rel_path, remote, save_path)
            self.stats["downloaded"] += 1
    except Exception:
        with self._lock:
            self.stats["errors"] += 1
```

锁的粒度：只锁状态更新（几微秒），不锁网络 I/O（几秒到几分钟）。这样 Lock 几乎不会造成争用。

### httpx Client 线程安全

httpx.Client 内部使用 httpcore 连接池。连接池本身是线程安全的（有内部锁）。只要不在运行时修改 Client 的属性（headers、auth 等），多线程并发请求是安全的。

我们的 `get_box_headers()` 每次返回新 dict，不修改 Client，所以是安全的。

连接池配置：
```python
limits=httpx.Limits(max_connections=10, max_keepalive_connections=5)
```

`max_connections=10` > `max_workers=3`，确保不会因连接不够而阻塞。

---

## 8. 错误处理与弹性设计

### 错误隔离层级

```
sync_once()
├─ try/except 包裹整体
│   ├─ for folder in root_folders:
│   │   └─ try/except 包裹每个根文件夹      ← 一个根文件夹出错不影响其他
│   │       └─ _sync_folder() 递归
│   │           └─ try/except 包裹每个子文件夹  ← 一个子文件夹出错不影响同级
│   └─ _flush_downloads()
│       └─ 每个 _do_download 内部 try/except  ← 一个文件下载失败不影响其他
└─ finally: state.save()                      ← 始终保存状态
```

这个层级结构的设计原则：**错误应该被隔离到最小影响范围**。

v1 的致命问题：没有中间层的 try/except，一个深层子文件夹的错误直接终止整轮同步。

### API 重试

网络层面的瞬时错误（超时、连接断开）通过指数退避重试处理：

```python
for attempt in range(1, MAX_RETRIES + 1):
    try:
        return client.request(...)
    except (HTTPStatusError, TransportError):
        if attempt < MAX_RETRIES:
            wait = RETRY_BACKOFF ** attempt
            time.sleep(wait)
```

但认证错误（401/403）不重试——重试也没用，需要换 Cookie。

### 连续错误升级

```python
# main.py
if stats["errors"] > 0:
    consecutive_errors += 1
    if consecutive_errors >= error_threshold:
        notifier.send("Worktile 同步告警", msg)
else:
    consecutive_errors = 0
```

偶尔一次错误不告警（可能是网络抖动），连续 3 轮都有错误才发通知（大概率是 Cookie 过期或接口变化）。

---

## 9. 状态持久化与原子写入

### 状态文件位置的演进

| 版本 | 位置 | 问题 |
|------|------|------|
| v1 | `/app/sync_state.json`（容器内） | 容器删除重建后丢失 |
| v3 | `/data/worktile-sync/sync_state.json`（同步目录） | 随 Volume 持久化 |

### 监控文件同步到 Worktile

v4 的设计选择：监控文件（health/progress/state/audit）**允许同步到 Worktile**，这样用户可以在 Worktile 网页端直接预览同步状态，无需登录 NAS。

只有 `.tmp` 临时文件被忽略（防止写到一半的文件被同步）：

```python
# 只排除临时文件
INTERNAL_FILES = {"sync_state.tmp", "sync_health.tmp", "sync_progress.tmp"}

# 群晖系统目录始终忽略
NAS_SYSTEM_DIRS = {"@eaDir", "#recycle", "@tmp"}

def _should_ignore(self, name):
    return (name in INTERNAL_FILES
            or name in NAS_SYSTEM_DIRS
            or should_ignore(name, self.ignore_patterns))
```

可在 Worktile 上直接预览的文件：
- `sync_health.json` — 最后一轮同步结果 + 变更明细
- `sync_progress.json` — 实时同步进度
- `sync_state.json` — 全量文件状态记录
- `sync_audit.csv` — 历史同步统计（可用 Excel 打开）

### 状态丢失的后果与缓解

如果状态文件丢失（容器重建、手动删除）：

1. 所有文件进入"首次同步"分支
2. 对于双方都有且大小一致的文件 → 跳过（v3 修复）
3. 对于只有本地有的文件 → 上传到 Worktile
4. 如果 Worktile 已有同名文件 → 创建副本（Worktile 不覆盖同名文件）

v3 的"大小一致则跳过"逻辑大幅缓解了状态丢失的副作用（从"全量重传"到"几乎无影响"）。

---

## 10. 监控与告警

### sync_health.json

每轮同步后写入：

```json
{
  "last_sync": "2026-04-07 19:17:59",
  "stats": {
    "downloaded": 5,
    "uploaded": 0,
    "deleted_local": 0,
    "deleted_remote": 0,
    "conflicts": 0,
    "errors": 0
  },
  "status": "ok",
  "consecutive_errors": 0
}
```

外部监控系统可以定期读取这个文件，检查：
- `last_sync` 是否在预期时间范围内（超过 interval * 2 说明可能卡住了）
- `status` 是否为 `ok`
- `consecutive_errors` 是否在增长

### 通知模块

```python
class Notifier:
    def send(self, title, body):
        if self.email_cfg.get("enabled"):
            self._send_email(title, body)
        if self.webhook_cfg.get("enabled"):
            self._send_webhook(title, body)
```

Webhook 自动识别服务类型：

```python
if "sctapi.ftqq.com" in url:      # Server酱 → 推送到微信
    data = {"title": title, "desp": body}
elif "pushplus.plus" in url:        # PushPlus
    data = {"title": title, "content": body}
elif "qyapi.weixin.qq.com" in url:  # 企业微信
    data = {"msgtype": "text", "text": {"content": f"{title}\n\n{body}"}}
```

### 日志轮转

```python
RotatingFileHandler(
    log_file,
    maxBytes=10 * 1024 * 1024,  # 10MB
    backupCount=3,               # 保留 3 份历史
)
```

防止日志文件无限增长撑满磁盘。最多占用 40MB（10MB × 4 个文件）。

---

## 11. Docker 部署与群晖 NAS

### Dockerfile

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY src/ src/
CMD ["python", "-m", "src.main"]
```

精简到极致：
- `python:3.11-slim`（约 120MB）而不是完整版（约 900MB）
- `--no-cache-dir` 不缓存 pip 下载
- 只复制 `src/` 不复制测试和工具脚本

### 跨平台构建

开发机是 Apple Silicon (ARM)，NAS 是 Intel (x86)：

```bash
docker buildx build --platform linux/amd64 -t worktile-sync:v3 --load .
```

`buildx` 使用 QEMU 模拟 x86 环境编译。构建速度比原生慢但不影响运行。

### NAS 部署的特殊考虑

群晖旧版 Docker GUI（非 Container Manager）的限制：
- **没有 docker-compose**（需要 SSH 或手动操作）
- **Registry 搜索可能被墙**（国内网络无法访问 Docker Hub）
- **Volume 映射 UI 有 Add File 和 Add Folder 两种**（映射配置文件用 Add File）
- **Terminal 不是独立 shell**（attach 到容器主进程，无法交互输入命令）

解决方案：导出 `.tar` 文件 → 通过 File Station 上传到 NAS → Docker GUI 导入。

### Volume 映射

| 容器路径 | 用途 | NAS 路径示例 |
|---------|------|-------------|
| `/app/config.yaml` | 配置文件 | `docker/config.yaml` |
| `/data/worktile-sync` | 同步目录 + 状态文件 | `worktile同步文件/` |

只需要两个映射。状态文件和健康文件自动写入同步目录。

---

## 12. 踩过的坑与解决方案

### 坑 1: JS 托管在 CDN，主域名返回 403

**现象**：尝试从 `zrhubei.worktile.com/pro/js/xxx.js` 下载 JS 文件，返回 403。

**原因**：实际 JS 托管在 CDN `cdn-aliyun.worktile.com/pro/js/`。

**教训**：逆向时先看 HTML 中 `<script>` 标签的实际 URL，不要想当然。

### 坑 2: COS SecretKey 在前端找不到

**现象**：在 13 个 JS Bundle 中搜索 SecretKey，找不到。

**原因**：Worktile 的文件下载不是客户端直接访问 COS，而是通过 wt-box 服务端代理（服务端持有 SecretKey）。

**教训**：不是所有签名都在前端完成。如果前端代码里找不到密钥，说明可能有服务端中转。

### 坑 3: folders 接口和 list 接口返回结构不同

**现象**：用相同的解析逻辑处理两个接口，一个返回空数组。

**原因**：`folders` 返回 `{data: [...]}`，`list` 返回 `{data: {value: [...], count: N}}`。

**教训**：即使是同一个系统的不同接口，返回结构也可能不一致。每个接口都要单独验证。

### 坑 4: 创建文件夹的 URL 路径是单数

**现象**：`POST /api/drives/folders` 返回 404。

**原因**：正确路径是 `/api/drive/folder`（单数）。Worktile 的 URL 命名不一致。

**教训**：逆向时不要猜测 URL 模式，要从前端代码中确认实际路径。

### 坑 5: 中文文件名超过 ext4 限制

**现象**：`OSError: [Errno 36] File name too long`。

**原因**：Linux ext4 文件名最长 255 字节。中文 UTF-8 每字符 3 字节，85 个字符就到上限。工程项目文件名经常超过这个长度。

**教训**：写入文件系统前一定要验证文件名长度。跨平台时尤其注意——macOS 支持 255 UTF-16 编码单元（约 255 个字符），Linux ext4 是 255 字节。

### 坑 6: 状态丢失导致重复上传

**现象**：容器重建后，所有文件被重新上传到 Worktile，产生大量副本。

**原因**：`sync_state.json` 在容器内部（`/app/`），没有映射到 Volume。容器删除后状态丢失。首次同步逻辑比较时间戳，下载后的本地 mtime > 远程 updated_at → 误判为"本地更新了" → 上传。

**修复**：
1. 状态文件放到同步目录（有 Volume 映射）
2. 首次同步时大小一致 → 跳过不上传（不再依赖时间戳）

**教训**：持久化数据一定要在 Volume 映射的路径中。Docker 容器是临时的。

### 坑 7: 一个文件夹出错终止整轮同步

**现象**：日志显示 1760 个文件下载成功，但有 3 个根文件夹始终没有同步。

**原因**：API 按 `updated_at` 降序返回文件夹。某个文件夹深处有超长文件名 → 异常一路冒泡到 `sync_once` 的 try/except → 排在后面的根文件夹被跳过。

**修复**：在根文件夹循环和子文件夹递归处都加 try/except。

**教训**：批量操作中，单个项目的失败不应该影响整体。要在恰当的粒度上做错误隔离。

### 坑 8: Docker Hub 在国内被墙

**现象**：NAS Docker GUI 搜索 Docker Hub Registry 返回 "Failed to query registry"。

**解决**：导出 `.tar` 文件，通过 File Station 上传导入。

**教训**：面向国内部署时，不能依赖 Docker Hub。要有离线部署方案。

---

## 13. 设计决策与权衡

### 轮询 vs WebSocket

选择 60 秒轮询而不是实时推送：
- Worktile 没有暴露 WebSocket 接口
- 轮询实现简单、可靠
- 60 秒延迟对文件同步场景可接受
- 轮询的代价（API 调用）很低

### JSON 状态 vs SQLite

选择 JSON 文件而不是数据库：
- 文件数量在万级以下，JSON 性能足够
- 不需要额外依赖
- 人可读、可编辑（调试友好）
- 原子写入可以通过 rename 实现

如果文件数量到十万级，应该考虑 SQLite。

### Cookie 认证 vs OAuth

没有选择的余地——Worktile 只对内部接口用 Cookie。这意味着：
- 需要手动维护 Cookie（过期后重新复制）
- 安全性较低（Cookie 包含完整的会话信息）
- 没有细粒度的权限控制

### 保守同步策略

首次同步时大小不一致以远程为准（下载覆盖本地）：
- 这是"宁可多下载，不要误上传"的思路
- Worktile 是 source of truth
- 上传副本的代价（数据重复）远大于下载覆盖的代价

---

## 14. 测试策略

### 单元测试

```bash
pytest tests/ -v
```

测试覆盖：
- `test_state.py` — 状态序列化/反序列化、损坏文件处理
- `test_sync.py` — 同步决策逻辑的各种 Case
- `test_api.py` — API 响应解析

### 集成测试（手动）

由于依赖真实的 Worktile 环境，集成测试是手动执行的：

1. **Dry-run 模式**：`dry_run: true` 只打印不执行，验证决策逻辑
2. **小范围测试**：设置 `root_folder_id` 只同步一个测试文件夹
3. **双向验证**：在 Worktile 创建文件 → 看是否下载到本地；在本地创建文件 → 看是否上传到 Worktile

### 工具脚本

- `tools/probe_download.py` — 自动探测下载接口，验证认证是否有效
- `tools/dedup_remote.py` — 清理远程重复文件

---

## 15. v4 新增特性详解

### 三阶段同步架构

v4 将同步过程从"遍历即执行"重构为三个阶段：

```
Phase 1: Scan（扫描）
  ├─ 递归遍历远程+本地文件树
  ├─ 文件夹 updated_at 快速跳过（未变化直接跳过子树）
  ├─ 对每个文件生成 SyncAction（download/upload/delete/conflict）
  └─ 输出: list[SyncAction]

Phase 2: Plan（计划）
  ├─ 统计：下载 N 个 (X MB), 上传 M 个 (Y MB), ...
  ├─ 按 updated_at 降序排列（最近修改优先）
  ├─ 重命名检测（同大小 delete+upload 配对）
  └─ 输出: 排序后的 list[SyncAction]

Phase 3: Execute（执行）
  ├─ 先执行上传/删除/冲突（通常较快）
  ├─ 再并发执行下载（ThreadPoolExecutor）
  ├─ 每 20 个操作报告进度
  ├─ 每 50 个下载增量保存状态
  └─ 输出: stats
```

### 文件夹 updated_at 快速跳过

这是增量同步性能提升最大的优化。原理：

```python
# state 中记录每个文件夹的 updated_at
FolderRecord(remote_id="xxx", remote_mtime=1775574801, ...)

# 下一轮同步时，比较 API 返回的 mtime 与记录的 mtime
if folder_mtime == prev_folder.remote_mtime:
    # 文件夹内容未变化，跳过整个子树（不调 list_files API）
    return
```

效果：500 个文件夹 → 只扫描有变化的几个 → API 调用从 500+ 降到 ~10。

### 临时文件 + 大小校验

```python
# 下载到临时文件
tmp_path = save_path.with_suffix(".downloading")
# 下载完毕后校验大小
if actual_size != expected_size:
    tmp_path.unlink()  # 删除不完整的文件
    raise Error(...)
# 原子 rename
tmp_path.rename(save_path)
```

好处：
- 断电/crash 不会留下半成品文件
- 大小不一致自动重试
- `.downloading` 文件可以被清理

### 速率限制

```python
class RateLimiter:
    """令牌桶：每秒最多 N 次 API 调用"""
    def wait(self):
        with self._lock:
            elapsed = now - self._last_call
            if elapsed < self._min_interval:
                sleep(self._min_interval - elapsed)
```

所有 API 调用经过 `_request()`，自动限速。默认 5 次/秒。

### 配置热重载

```python
# 每轮同步前检查 config.yaml 的 mtime
if config_mtime changed:
    reload config → update auth → update notifier
```

最重要的场景：Cookie 过期后，用户更新 config.yaml 中的 Cookie，不需要重启容器。

### 审计日志

每轮同步追加一行到 `sync_audit.csv`：

```csv
timestamp,duration_sec,downloaded,uploaded,deleted_local,deleted_remote,conflicts,errors,skipped_folders
2026-04-08 12:00:00,3.2,0,0,0,0,0,0,48
2026-04-08 12:01:00,45.1,5,1,0,0,0,0,3
```

方便回溯"哪天同步了什么"，也可以用 Excel/Pandas 分析趋势。

### 本地文件监听

```python
# 使用 watchdog 库监听文件系统事件
watcher = LocalWatcher(local_dir, ignore_check)
watcher.start()

# 主循环中检测变化，提前触发同步
for _ in range(interval):
    if watcher.has_changes():
        break  # 不等 60 秒，立即同步
    sleep(1)
```

Docker 环境中 inotify 对 volume mount 的支持取决于存储驱动，不保证所有环境都能用。设为可选功能（`watch_local: true`）。

## 16. 未来改进方向

- [ ] **Cookie 自动刷新**：通过无头浏览器自动登录
- [ ] **Web 管理界面**：展示状态、手动触发、更新 Cookie
- [ ] **并发上传**：类似下载的队列模式
- [ ] **多团队支持**：同时同步多个 Worktile 团队
- [ ] **选择性同步**：通过 UI 选择要同步的文件夹
- [ ] **版本历史**：利用 Worktile 的版本机制保留历史

---

## 附：完整配置模板

```yaml
worktile:
  base_url: "https://zrhubei.worktile.com"
  box_url: "https://wt-box.worktile.com"
  team_id: "YOUR_TEAM_ID"
  auth:
    cookie: "s-YOUR_TEAM_ID=YOUR_SESSION_TOKEN"
  root_folder_id: ""

sync:
  local_dir: "/data/worktile-sync"
  interval: 60
  max_workers: 3
  rate_limit: 5.0
  watch_local: false
  sync_delete: false
  dry_run: false
  ignore_patterns:
    - ".DS_Store"
    - "Thumbs.db"
    - "*.tmp"

notification:
  enabled: true
  error_threshold: 3
  email:
    enabled: false
    smtp_host: "smtp.gmail.com"
    smtp_port: 587
    username: ""
    password: ""
    to: ""
  webhook:
    enabled: true
    url: "https://sctapi.ftqq.com/YOUR_SENDKEY.send"

logging:
  level: "INFO"
  file: "sync.log"
  max_size_mb: 10
  backup_count: 3
```
