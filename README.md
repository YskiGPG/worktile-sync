# Worktile 网盘同步工具

将 Worktile 团队网盘与本地文件夹（群晖 NAS）自动双向同步。

Worktile 官方 API 不提供网盘文件操作接口，本工具通过逆向网页版内部接口实现。

## 功能

- **双向同步**：Worktile 网盘 <-> 本地文件夹，每 60 秒自动轮询
- **三阶段架构**：扫描 → 计划 → 执行，同步前预览操作计划
- **智能跳过**：未变化的文件夹自动跳过（基于 `updated_at`），增量同步秒级完成
- **并发下载**：`max_workers` 多线程下载，首次同步提速 3-5 倍
- **冲突处理**：双方都修改时以更新时间为准，冲突文件自动备份
- **本地监听**：检测本地文件变化立即触发同步（可选，需 watchdog）
- **告警通知**：连续错误自动推送微信/邮件（Server酱/PushPlus/企业微信）
- **配置热重载**：Cookie 更新后自动生效，无需重启容器
- **审计日志**：每轮同步统计写入 CSV，方便回溯
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
| **Port Settings** | 不需要端口映射，直接 Next |
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

## 项目结构

```
├── src/
│   ├── main.py    # 入口，主循环
│   ├── api.py     # Worktile 接口封装（列表/下载/上传/删除/创建文件夹）
│   ├── auth.py    # 认证管理（Cookie / x-cookies）
│   ├── sync.py    # 双向同步引擎
│   ├── state.py   # 同步状态持久化（JSON）
│   └── utils.py   # 工具函数
├── tools/
│   └── probe_download.py  # 下载接口探测脚本
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
| 下载文件 | GET | `wt-box.worktile.com/drives/{file_id}?team_id={id}&version={ver}&action=download` |
| 上传文件 | POST | `wt-box.worktile.com/drive/upload?belong=2&parent_id={id}&team_id={id}` |
| 创建文件夹 | POST | `/api/drive/folder` |
| 删除文件 | DELETE | `/api/drives/{file_id}` |

- 主域名接口使用标准 `Cookie` Header
- wt-box 跨域接口使用自定义 `x-cookies` Header
- 详细接口文档见 [CLAUDE.md](CLAUDE.md) 和 [DOWNLOAD_INVESTIGATION.md](DOWNLOAD_INVESTIGATION.md)

## 升级容器

当有新版本时：

1. 停止旧容器（Container → 选中 → Stop）
2. 导入新镜像（Image → Add From File）
3. 用新镜像 Launch 新容器，Volume 映射和之前一样
4. 删除旧容器（可选）

> 升级不会丢失已同步的文件，新容器会复用同步目录中已有的文件。
> 建议升级前删除容器内的 `sync_state.json`（如果有的话），让新版本重新扫描一次。

## 注意事项

- **首次同步耗时**：文件数量多时首次全量同步可能需要数小时（串行下载），后续增量同步每轮通常几秒完成
- **Cookie 过期**：Worktile Cookie 会过期，届时日志会出现认证失败告警，需要重新从浏览器复制 Cookie 并更新 `config.yaml`，然后重启容器
- **逆向接口风险**：Worktile 版本更新后接口可能变化，需重新抓包适配
- **首次同步**：建议先设 `dry_run: true` 跑一次确认文件列表正确，再改为 `false`
- **NAS 存储空间**：首次同步会下载全部文件，确保 NAS 有足够空间
- **超长文件名**：中文文件名超过 85 个字符（255 字节）会自动截断并加 hash 后缀，不影响同步
- **错误容忍**：单个文件/文件夹同步失败不会中断整体同步，错误会记录在日志中
- **健康监控**：每轮同步后会写入 `sync_health.json`，连续 3 轮报错会在日志中输出 CRITICAL 告警
