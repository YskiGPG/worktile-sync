# Worktile 网盘同步工具

将 Worktile 团队网盘与本地文件夹（群晖 NAS）自动双向同步。

Worktile 官方 API 不提供网盘文件操作接口，本工具通过逆向网页版内部接口实现。

## 功能

- **双向同步**：Worktile 网盘 <-> 本地文件夹，每 60 秒自动轮询
- **递归同步**：自动遍历所有子文件夹
- **冲突处理**：双方都修改时以更新时间为准，冲突文件自动备份
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
# 在本地构建并导出
docker buildx build --platform linux/amd64 -t worktile-sync:latest --load .
docker save worktile-sync:latest -o worktile-sync.tar
```

然后在群晖 Docker GUI 中：
1. **Image → Add → Add From File**，选择 `worktile-sync.tar`
2. **Launch** 启动容器，配置 Volume 映射：

| 本地路径 | 容器路径 |
|---------|---------|
| `docker/config.yaml` | `/app/config.yaml` |
| 你的同步目录 | `/data/worktile-sync` |

3. 勾选 **Enable auto-restart**

**方式二：docker-compose**

```bash
# 在 NAS 上
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

## 注意事项

- **Cookie 过期**：Worktile Cookie 会过期，届时日志会出现认证失败告警，需要重新从浏览器复制 Cookie 并更新 `config.yaml`，然后重启容器
- **逆向接口风险**：Worktile 版本更新后接口可能变化，需重新抓包适配
- **首次同步**：建议先设 `dry_run: true` 跑一次确认文件列表正确，再改为 `false`
- **NAS 存储空间**：首次同步会下载全部文件，确保 NAS 有足够空间
