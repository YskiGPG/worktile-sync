# Worktile 网盘同步工具

将 Worktile 团队网盘与本地文件夹（群晖 NAS）自动双向同步。

Worktile 官方 API 不提供网盘文件操作接口，本工具通过逆向网页版内部接口实现。

## 功能

- **双向同步**：Worktile 网盘 <-> 本地文件夹，每 60 秒自动轮询
- **三阶段架构**：扫描 → 计划 → 执行，同步前预览操作计划
- **智能跳过**：远程未变化的文件夹跳过 API 调用（基于 `updated_at`），但仍扫描本地文件系统检测新增/修改，增量同步秒级完成
- **并发下载**：`max_workers` 多线程下载，首次同步提速 3-5 倍
- **冲突处理**：双方都修改时以更新时间为准，冲突文件自动备份
- **本地监听**：检测本地文件变化立即触发同步（可选，需 watchdog）
- **告警通知**：连续错误自动推送微信/邮件（Server酱/PushPlus/企业微信）
- **配置热重载**：Cookie 更新后自动生效，无需重启容器
- **审计日志**：每轮同步统计写入 CSV，方便回溯
- **HTTP 状态面板**：内置 HTTP 服务器（默认端口 9090），浏览器查看同步状态、进度、日志
- **Dry-run 模式**：首次运行可先预览，确认无误再执行
- **Docker 部署**：适配群晖 NAS Docker 环境

## 同步链路

```
Worktile 网盘（云端）
    ↕  本工具（Docker 容器，60秒轮询）
群晖 NAS 本地文件夹
    ↕  Synology Drive（自动同步）
用户电脑
```

## 快速开始

### 1. 获取认证 Cookie

1. 用 Chrome 打开你的 Worktile 团队网盘
2. F12 打开 DevTools → Network 面板
3. 刷新页面，点击任意请求，在 Request Headers 中复制完整的 `Cookie` 值

### 2. 配置

```bash
cp config.example.yaml config.yaml
```

编辑 `config.yaml`，填入：

```yaml
worktile:
  base_url: "https://你的团队.worktile.com"
  box_url: "https://wt-box.worktile.com"
  team_id: "你的团队ID"  # Cookie 中 s-{这里就是team_id}=xxx
  auth:
    cookie: "粘贴完整的 Cookie 字符串"
  root_folder_id: ""  # 留空=同步全部，填文件夹ID=只同步指定文件夹

sync:
  local_dir: "/data/worktile-sync"  # Docker 容器内路径
  interval: 60
  sync_delete: false  # 是否同步删除操作
  dry_run: false
  status_port: 9090  # HTTP 状态面板端口，设为 0 禁用
```

### 3. 部署到群晖 NAS

**方式一：导入预构建镜像（推荐）**

```bash
# 在本地构建并导出（Apple Silicon 需指定 amd64 平台）
docker buildx build --platform linux/amd64 -t worktile-sync:latest --load .
docker save worktile-sync:latest -o worktile-sync.tar
```

将 `worktile-sync.tar` 传输到 NAS（通过 File Station 上传、USB、或网络共享均可）。

#### 群晖 Docker GUI 部署步骤

**第 1 步：导入镜像**

Image → Add → **Add From File** → 选择 `worktile-sync.tar`，等待导入完成。

**第 2 步：准备配置文件**

在 NAS 的 `docker` 共享文件夹中放置 `config.yaml`：

```
/volume1/docker/config.yaml
```

可通过 File Station 上传，或用 SSH 复制。

**第 3 步：创建容器**

在 Image 列表中选择导入的镜像，点击 **Launch**，按以下步骤配置：

| 页面 | 设置 |
|------|------|
| **Network** | 保持默认 `bridge` → Next |
| **General Settings** | Container Name: `worktile-sync`<br>勾选 **Enable auto-restart** → Next |
| **Port Settings** | 添加端口映射 `9090:9090`（状态面板）→ Next |
| **Volume Settings** | 添加以下两个映射（见下表）→ Next |
| **Summary** | 确认无误 → Apply |

Volume 映射配置（**最关键**）：

| 操作 | NAS 路径 | 容器路径（Mount Path） |
|------|---------|----------------------|
| Add File | `docker/config.yaml` | `/app/config.yaml` |
| Add Folder | 你的同步目录（如 `worktile同步文件`） | `/data/worktile-sync` |

> **注意**: Volume Settings 页面有两个按钮：**Add File**（映射单个文件）和 **Add Folder**（映射文件夹）。config.yaml 用 Add File，同步目录用 Add Folder。

**第 4 步：验证运行**

容器启动后，在 Container 列表中点击容器名 → **Terminal** 标签页可看到实时日志。正常运行会显示：

```
[INFO] src.main - Worktile 网盘同步工具 v4 启动
[INFO] src.sync - 开始扫描...
[INFO] src.sync - 同步计划 — 下载:10 (45.2 MB) 上传:0 (0.0 B) 删除(本地):0 删除(远程):0 冲突:0 跳过文件夹:48
[INFO] src.sync - 开始并发下载 10 个文件 (workers=3)
[INFO] src.sync - 进度: 10/10 (100%)
[INFO] src.sync - 同步完成 — 下载:10 上传:0 删除(本地):0 删除(远程):0 冲突:0 错误:0
```

也可以通过 **Log** 标签页查看历史日志。

**方式二：Docker Hub 拉取**

如果 NAS 能访问 Docker Hub：

```
Registry → 搜索 yskigpg/worktile-sync → Download → 选择 tag: v2
```

然后按上面第 3-4 步创建容器。

> 国内网络可能无法访问 Docker Hub，此时请使用方式一。

**方式三：docker-compose（SSH）**

```bash
# 在 NAS 上通过 SSH
docker-compose up -d
```

`docker-compose.yml` 中默认映射 `/volume1/SynologyDrive/Worktile`，按需修改。

### 4. 本地运行（不用 Docker）

```bash
pip install -r requirements.txt
python -m src.main
```

## 同步规则

| 场景 | 行为 |
|------|------|
| 远程新文件，本地没有 | 下载到本地 |
| 本地新文件，远程没有 | 上传到 Worktile |
| 远程文件有更新 | 下载覆盖本地 |
| 本地文件有更新 | 上传覆盖远程 |
| 双方都修改了同一文件 | 以修改时间更新的为准，另一份备份为 `文件名.conflict.扩展名` |
| 一方删除（`sync_delete: true`） | 另一方也删除 |
| 一方删除（`sync_delete: false`） | 不处理，保留另一方的文件 |

## 监控文件

同步目录下会自动生成以下监控文件。这些文件属于 `INTERNAL_FILES`，仅存在于 NAS 本地，**不同步到 Worktile**。原因：通过 `/drive/upload` API 上传的文件缺少 `version` 元数据，在 Worktile 网页端无法预览/下载；且员工不应看到这些运维文件。

| 文件 | 说明 | 更新频率 |
|------|------|---------|
| `sync_health.json` | 最后一轮同步结果、统计、变更明细 | 每轮同步结束 |
| `sync_progress.json` | 实时同步进度（阶段、已完成/总数、当前文件） | 扫描/下载过程中每 10 个文件夹 |
| `sync_state.json` | 所有文件的同步状态记录 | 每轮同步结束 + 增量保存 |
| `sync_audit.csv` | 历史同步统计（CSV 格式，可用 Excel 打开） | 每轮追加一行 |

查看方式有两种：
1. **HTTP 状态面板**（推荐）：浏览器访问 `http://NAS-IP:9090`，见下文
2. **NAS File Station**：直接打开同步目录中的 JSON/CSV 文件

### sync_health.json 示例

```json
{
  "last_sync": "2026-04-08 19:30:00",
  "duration_sec": 3.2,
  "status": "ok",
  "stats": {"downloaded": 2, "uploaded": 1, "errors": 0, ...},
  "recent_changes": [
    {"action": "download", "direction": "worktile → 本地", "file": "合同.pdf", "size": 1234567},
    {"action": "upload", "direction": "本地 → worktile", "file": "报告.docx", "size": 45678}
  ]
}
```

### sync_progress.json 示例（同步进行中）

```json
{
  "status": "syncing",
  "phase": "downloading",
  "current_file": "已扫描 350 个文件夹, 跳过 280 个",
  "completed": 15,
  "total": 20,
  "percent": 75.0,
  "downloaded": 13,
  "elapsed_sec": 45.2
}
```

## HTTP 状态面板

容器内运行轻量 HTTP 服务器（默认端口 9090），提供 Web 界面和 JSON API，方便在浏览器或监控系统中查看同步状态。

**访问地址**：`http://NAS-IP:9090`

| 端点 | 说明 |
|------|------|
| `/` | HTML 仪表盘（自动刷新 10 秒），展示同步状态、统计、最近变更 |
| `/health` | sync_health.json 原始内容 |
| `/status` | 合并 health + progress 的 JSON（适合监控系统拉取） |
| `/progress` | sync_progress.json 原始内容 |
| `/audit` | 最近 50 条审计 CSV 记录 |
| `/log` | 最近 100 行运行日志 |

**配置**（可选，在 `config.yaml` 中）：

```yaml
sync:
  status_port: 9090  # 默认 9090，设为 0 禁用
```

**Docker 部署需要端口映射**：

```
Port Settings: 本地端口 9090 → 容器端口 9090
```

docker-compose 方式：

```yaml
ports:
  - "9090:9090"
```

## 项目结构

```
├── src/
│   ├── main.py      # 入口：主循环、配置热重载、审计、通知
│   ├── api.py       # Worktile API 封装（速率限制、临时文件下载、大小校验）
│   ├── auth.py      # 认证管理（Cookie / x-cookies）
│   ├── sync.py      # 三阶段同步引擎（扫描 → 计划 → 执行）
│   ├── state.py     # 同步状态持久化（原子写入、文件夹缓存）
│   ├── notify.py    # 通知（邮件 + Webhook）
│   ├── status.py    # HTTP 状态面板（轻量 HTTP 服务器）
│   ├── watcher.py   # 本地文件监听（可选，watchdog）
│   └── utils.py     # 工具函数（日志轮转、文件名截断、速率限制）
├── tools/
│   ├── probe_download.py   # 下载接口探测脚本
│   ├── dedup_remote.py     # 远程重复文件清理
│   └── cleanup_eadir.py    # @eaDir 清理
├── config.example.yaml
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## 已逆向的 Worktile 接口

| 功能 | 方法 | 端点 |
|------|------|------|
| 根文件夹列表 | GET | `/api/drives/folders?parent_id=&belong=2` |
| 文件/子文件夹列表 | GET | `/api/drives/list?parent_id={id}&belong=2&pi=0&ps=200` |
| 下载文件 | GET | `wt-box/drives/{file_id}?team_id={id}&version={ver}&action=download` |
| 上传新文件 | POST | `wt-box/drive/upload?belong=2&parent_id={id}&team_id={id}` |
| 上传新版本 | POST | `wt-box/drive/update?team_id={id}&id={file_id}` |
| 创建文件夹 | POST | `/api/drive/folder` |
| 删除文件 | DELETE | `/api/drives/{file_id}` |

- 主域名接口使用标准 `Cookie` Header
- wt-box 跨域接口使用自定义 `x-cookies` Header
- 上传新文件（`/drive/upload`）创建新条目；更新已有文件（`/drive/update`）保留文件 ID 和版本历史
- 详细接口文档见 [CLAUDE.md](CLAUDE.md) 和 [TECHNICAL_DEEP_DIVE.md](TECHNICAL_DEEP_DIVE.md)

## 升级容器

当有新版本时：

1. 停止旧容器（Container → 选中 → Stop）
2. 导入新镜像（Image → Add From File）
3. 用新镜像 Launch 新容器，Volume 映射和之前一样
4. 删除旧容器（可选）

> 升级不会丢失已同步的文件，新容器会复用同步目录中已有的文件。
> 建议升级前删除容器内的 `sync_state.json`（如果有的话），让新版本重新扫描一次。

## 注意事项

- **首次同步耗时**：文件数量多时首次全量同步可能需要数小时，后续增量同步通常几秒（文件夹跳过优化）
- **首次扫描慢**：v4 首次运行需要扫描所有文件夹建立缓存（~4000 个文件夹约 30 分钟），之后未变化的文件夹自动跳过
- **Cookie 过期**：更新 `config.yaml` 中的 Cookie 后自动生效（配置热重载），无需重启容器
- **逆向接口风险**：Worktile 版本更新后接口可能变化，需重新抓包适配
- **NAS 存储空间**：首次同步会下载全部文件，确保 NAS 有足够空间
- **超长文件名**：中文文件名超过 85 个字符（255 字节）会自动截断并加 hash 后缀
- **群晖系统目录**：`@eaDir`、`#recycle` 等群晖系统目录自动忽略
- **监控文件**：`sync_health.json`、`sync_progress.json`、`sync_state.json`、`sync_audit.csv` 属于 `INTERNAL_FILES`，仅存在 NAS 本地，不同步到 Worktile（API 上传的文件缺少 `version` 元数据无法预览，且员工不应看到运维文件）。通过 HTTP 状态面板（`http://NAS-IP:9090`）或 File Station 查看
- **变更通知**：报错立即推送 + 每小时汇总；容器启动/停止通知
- **版本历史**：业务文件更新时使用 `/drive/update` API，保留 Worktile 上的版本历史
