# Worktile 网盘同步工具

## 项目概述

开发一个后台同步程序，实现 Worktile 网盘与本地文件夹（群晖 NAS）的自动双向同步。
Worktile 官方 API 不提供网盘文件操作接口，因此需要通过逆向网页版内部接口实现。

## 技术栈

- **语言**: Python 3.11+
- **HTTP 客户端**: `requests` 或 `httpx`
- **部署环境**: 群晖 NAS (DS218+, 可升级) Docker 容器
- **目标同步链路**: Worktile 网盘 ↔ 同步脚本(Docker) ↔ 群晖本地文件夹 ↔ Synology Drive ↔ 用户电脑

## 项目结构

```
worktile-sync/
├── CLAUDE.md              # 本文件
├── TODO.md                # 任务跟踪
├── README.md              # 使用说明
├── config.example.yaml    # 配置文件模板
├── config.yaml            # 实际配置（不提交 git）
├── src/
│   ├── __init__.py
│   ├── main.py            # 入口，主循环
│   ├── api.py             # Worktile 接口封装（列表/下载/上传/删除）
│   ├── auth.py            # 认证管理（Cookie/Token 刷新）
│   ├── sync.py            # 核心同步逻辑（双向同步、冲突处理）
│   ├── state.py           # 同步状态持久化（JSON）
│   └── utils.py           # 工具函数（日志、文件哈希等）
├── tests/
│   ├── test_api.py
│   ├── test_sync.py
│   └── test_state.py
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── .gitignore
```

## Worktile 接口（已确认）

### 基础信息

- **主域名**: `https://zrhubei.worktile.com`
- **文件存储域名**: `https://wt-box.worktile.com`（上传用）
- **COS 域名**: `https://app-wt-original-release-1348738073.cos.ap-shanghai.myqcloud.com`（下载用）
- **Team ID**: `695364022dd1c542de797196`

### 认证方式

使用 Cookie 认证。关键 Cookie:
```
s-695364022dd1c542de797196=655b2e20872e4e54ae746defaad37f83
```
Cookie 键名格式为 `s-{team_id}`，值为 session token。

上传接口（`wt-box.worktile.com`）是跨域的，Cookie 通过自定义 Header `x-cookies` 传递。

### 接口详情

#### 1. 获取根文件夹列表

```
GET /api/drives/folders?parent_id=&belong=2&sort_by=updated_at&sort_type=-1&t={timestamp}
```

参数:
- `parent_id`: 空 = 根目录，填文件夹 ID = 子目录
- `belong`: 2（团队网盘）
- `sort_by`: 排序字段
- `sort_type`: -1 降序
- `t`: 当前时间戳（防缓存）

返回结构（注意: `data` 直接是数组，不是分页对象）:
```json
{
  "code": 200,
  "data": [
    {
      "_id": "697318d64f80a8a75dea1261",
      "type": 1,
      "parent": null,
      "parents": [],
      "title": "05-其它咨询",
      "updated_at": 1775094897,
      "children_count": 4
    }
  ]
}
```

#### 2. 获取文件列表（文件+子文件夹混合）

```
GET /api/drives/list?keywords=&parent_id={folder_id}&belong=2&sort_by=updated_at&sort_type=-1&pi=0&ps=200&t={timestamp}
```

参数:
- `parent_id`: 文件夹 ID（必填）
- `pi`: 页码，从 0 开始
- `ps`: 每页数量，默认 200
- 其他同上

返回结构:
```json
{
  "code": 200,
  "data": {
    "value": [
      {
        "_id": "69d2e3aeb5176ce7c60b8217",
        "type": 3,
        "parent": "698040a14056a7ba49006e6f",
        "parents": ["697317...", "698016..."],
        "title": "施工合同.pdf",
        "updated_at": 1775428526,
        "addition": {
          "ext": "pdf",
          "size": 14322157,
          "path": "f4389d70-d3f0-433d-a029-f48f0ed68aed",
          "current_version": 1
        }
      }
    ],
    "count": 5,
    "page_count": 1,
    "page_index": 0,
    "page_size": 200
  }
}
```

**重要**: `list` 接口返回文件和子文件夹混合结果。通过 `type` 区分:
- `type: 1` = 文件夹（没有 `addition.path`）
- `type: 3` = 文件（有 `addition.path`、`addition.size`、`addition.ext`）

#### 3. 上传文件

```
POST https://wt-box.worktile.com/drive/upload?belong=2&parent_id={folder_id}&team_id={team_id}
Content-Type: multipart/form-data
```

Headers（注意不是标准 Cookie，而是自定义 Header）:
```
x-cookies: s-695364022dd1c542de797196=655b2e20872e4e54ae746defaad37f83;（完整cookie字符串）
```

Form Data:
- `title`: 文件名（如 `合同.pdf`）
- `file`: 文件二进制内容，filename 为文件名

#### 4. 下载文件（已确认）

通过 `wt-box.worktile.com` 下载，服务端自动处理 COS 签名:

```
GET https://wt-box.worktile.com/drives/{file_id}?team_id={team_id}&version={current_version}&action=download
```

Headers（跨域，同上传接口）:
```
x-cookies: s-695364022dd1c542de797196=655b2e20872e4e54ae746defaad37f83;（完整cookie字符串）
```

参数:
- `file_id`: 文件的 `_id`
- `team_id`: 团队 ID
- `version`: `addition.current_version`（通常为 1）
- `action`: `download`

返回: HTTP 200，直接流式返回文件二进制内容。

发现方式: 从前端 JS `driveRealUrl` Angular pipe 中逆向提取。

#### 5. 删除文件（已确认）

软删除（移到回收站）:
```
DELETE /api/drives/{file_id}
```

返回: `{"code": 200}`

彻底删除:
```
DELETE /api/drives/{file_id}/real
```

批量删除:
```
DELETE /api/drives/remove
Body: { "drive_ids": ["id1", "id2"] }
```

发现方式: 从前端 JS `deleteDrive`/`delDrive`/`removeAll` 方法中提取。

#### 6. 创建文件夹（已确认）

注意路径是 `drive/folder`（都是单数），不是 `drives/folders`。

```
POST /api/drive/folder
Body: {
  "parent_id": "父文件夹ID",
  "title": "新文件夹",
  "belong": 2,
  "visibility": 1,
  "permission": 4,
  "members": [],
  "color": "#6698FF"
}
```

返回: `{"code": 200, "data": {"_id": "新文件夹ID", ...}}`

批量创建路径（上传文件夹时用）:
```
POST /api/drive/folders
Body: { "parent_id": "xxx", "paths": ["path1", "path2"], "belong": 2, "visibility": 1, "color": "#6698FF" }
```

发现方式: 从前端 JS `addFolder`/`createFolderPath` 方法中提取。

## 数据模型映射

| Worktile 字段 | 同步脚本字段 | 说明 |
|--------------|------------|------|
| `_id` | `remote_id` | 文件/文件夹唯一 ID |
| `title` | `name` | 文件名 |
| `type` | `is_folder` | 1=文件夹, 3=文件 |
| `updated_at` | `remote_mtime` | Unix 时间戳（秒） |
| `addition.size` | `remote_size` | 文件大小（字节） |
| `addition.path` | `cos_key` | COS 上的文件 key |
| `addition.ext` | - | 文件扩展名 |
| `parent` | `parent_id` | 父文件夹 ID |

## 核心逻辑

### 同步策略
1. 轮询模式，默认每 60 秒检查一次
2. 通过对比文件修改时间 + 大小判断文件是否变化
3. 用 JSON 文件持久化上一次同步状态
4. 冲突策略：以修改时间更新的一方为准，冲突文件备份为 `filename.conflict.ext`

### 同步方向
- **远程 → 本地**: 远程有新文件或更新 → 下载到本地
- **本地 → 远程**: 本地有新文件或更新 → 上传到 Worktile
- **删除同步**: 一方删除的文件，另一方也删除（可配置是否启用）

### 文件列表获取逻辑

由于 `list` 接口返回文件和子文件夹混合结果:
1. 只用 `list` 接口即可，通过 `type` 字段区分文件和文件夹
2. 递归遍历: 对 `type=1` 的子文件夹，递归调用 `list` 接口
3. 分页处理: 如果 `page_count > 1`，需要翻页获取全部文件（`pi=0,1,2...`）
4. 根目录需先调用 `folders` 接口获取顶层文件夹列表

### 认证处理
- Cookie 认证，关键 Cookie 为 `s-{team_id}={session_token}`
- 需要处理 Token 过期自动告警
- 上传接口使用 `x-cookies` Header（跨域）

## 编码规范

- 所有日志使用 `logging` 模块，不要用 `print`
- 配置使用 YAML 格式，`config.yaml` 不提交 git
- 敏感信息（token、cookie）只存在 config.yaml 中
- 函数和变量用英文命名，注释可以用中文
- 错误处理：网络请求必须有重试机制（最多 3 次，指数退避）
- 类型注解：所有函数参数和返回值加 type hints

## 常用命令

```bash
# 安装依赖
pip install -r requirements.txt

# 运行同步
python -m src.main

# 运行测试
pytest tests/ -v

# Docker 构建
docker build -t worktile-sync .

# Docker 运行
docker-compose up -d
```

## 注意事项

- 这是逆向接口方案，Worktile 更新版本后接口可能变化，需要重新抓包适配
- 同步工具只做文件的读写操作，不会修改 Worktile 的项目/任务等其他数据
- 首次同步前建议先做一次完整的文件列表对比，用 dry-run 模式确认无误再执行
- 群晖 NAS 上 Docker 运行时注意文件权限问题（uid/gid 映射）
- `list` 接口每页最多 200 条，文件多的文件夹需要分页处理
