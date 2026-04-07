# Worktile 网盘同步工具 - 任务清单

## 阶段一：接口抓取与验证

- [x] 抓取「获取文件夹列表」接口 → `GET /api/drives/folders`
- [x] 抓取「获取文件列表」接口 → `GET /api/drives/list`
- [x] 抓取「上传文件」接口 → `POST wt-box.worktile.com/drive/upload`
- [x] 确认认证方式 → Cookie (`s-{team_id}={session}`)，上传/下载用 `x-cookies` Header
- [x] 确认文件数据结构 → `_id`, `title`, `type`, `updated_at`, `addition.path/size/ext/current_version`
- [x] 抓取「下载文件」接口 → `GET wt-box.worktile.com/drives/{file_id}?team_id={team_id}&version={version}&action=download`
  - wt-box 服务端自动生成 COS 签名，不需要客户端持有 SK
  - 从 JS 中 `driveRealUrl` pipe 逆向发现
- [x] 抓取「删除文件」接口 → `DELETE /api/drives/{file_id}`（软删除到回收站）
- [x] 抓取「创建文件夹」接口 → `POST /api/drive/folder`（注意 drive/folder 都是单数）
  - 需要 `permission: 4, visibility: 1, members: [], color: "#6698FF"` 等完整参数

## 阶段二：核心开发

### 项目初始化
- [x] 创建项目目录结构
- [x] 编写 `requirements.txt`（httpx, pyyaml）
- [x] 编写 `config.example.yaml` 配置模板
- [x] 编写 `.gitignore`

### API 封装 (`src/api.py`)
- [x] 实现 `WorktileAPI` 类
- [x] `list_files(folder_id)` — 获取文件列表（处理分页、type 区分）
- [x] `list_root_folders()` — 获取根目录文件夹
- [x] `download_file(file_info, save_path)` — 通过 wt-box 下载文件
- [x] `upload_file(folder_id, file_path)` — 上传本地文件（用 x-cookies）
- [x] `delete_file(file_id)` — 删除远程文件
- [x] `create_folder(parent_id, name)` — 创建文件夹
- [x] 所有请求加重试机制（3次，指数退避）

### 认证管理 (`src/auth.py`)
- [x] 实现 Cookie 注入（主域名用 Cookie，wt-box 用 x-cookies）
- [x] 实现 Token 过期检测（HTTP 401/403 判断）
- [x] 过期后的处理：日志告警

### 同步状态 (`src/state.py`)
- [x] 定义 `SyncState` 数据结构
- [x] 实现状态文件读写（JSON 持久化）
- [x] 记录每个文件的：文件名、远程ID、cos_key、修改时间、大小、同步时间

### 核心同步逻辑 (`src/sync.py`)
- [x] 实现 `sync_once()` 主流程
- [x] 实现 dry-run 模式
- [x] 子文件夹递归同步

### 主入口 (`src/main.py`)
- [x] 加载配置、主循环、优雅退出

## 阶段三：测试与调试

- [x] 远程新增文件 → 本地自动下载（上传测试文件后同步下载，44 bytes 匹配）
- [x] 本地新增文件 → 远程自动上传（upload_file 接口验证通过）
- [x] 中文文件名、特殊字符文件名（dry-run 遍历大量中文/特殊字符文件无报错）
- [ ] 大文件（>100MB）— 待实际环境验证
- [ ] 分页场景（文件数 >200）— 待实际环境验证
- [x] dry-run 模式验证（递归遍历项目资料文件夹，0 错误）

## 阶段四：Docker 部署到群晖

- [x] 编写 Dockerfile
- [x] 编写 docker-compose.yml
- [ ] 群晖 Docker 部署并验证完整链路

---

*最后更新：2026-04-06*
