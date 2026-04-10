# Worktile 网盘同步工具 — 开发指南

## 项目概述

Worktile 网盘 ↔ 群晖 NAS 自动双向同步工具。Worktile 无官方 API，通过逆向网页版内部接口实现。
当前版本 v4，三阶段同步架构（Scan → Plan → Execute）。

详细技术文档见 [TECHNICAL_DEEP_DIVE.md](TECHNICAL_DEEP_DIVE.md)。

## 技术栈

- Python 3.11+, httpx, Docker
- 部署: 群晖 NAS Docker 容器
- 通知: Server酱 (微信推送)

## 工作流程

每次代码修改后，自动执行以下步骤，无需提醒：

1. **自查**: 重新阅读代码，检查逻辑错误、边界情况、变量未定义
2. **验证**: `python3 -c "from src.main import main; print('OK')"` 确认 import 无误
3. **构建**: `docker buildx build --platform linux/amd64 -t worktile-sync:v4 --load .`
4. **导出**: `docker save worktile-sync:v4 -o ~/Desktop/worktile-sync-v4.tar`
5. **提交**: `git add → git commit（不带 Co-Authored-By）→ git push`
6. **文档**: 更新 README.md 和 TECHNICAL_DEEP_DIVE.md 中受影响的部分

发现问题直接修复，不要等指出。以资深工程师标准审查代码。

## 代码规范

- 所有日志用 `logging`，不用 `print`
- 函数参数和返回值加 type hints
- 网络请求必须有重试（3 次，指数退避）
- 错误处理：单个文件/文件夹失败不影响整体同步
- 不留 TODO 或占位代码

## 已逆向的 API（7 个）

| 功能 | 方法 | 端点 | 域名 | 认证 |
|------|------|------|------|------|
| 根文件夹列表 | GET | /api/drives/folders | 主域名 | Cookie |
| 文件列表(分页) | GET | /api/drives/list | 主域名 | Cookie |
| 下载文件 | GET | /drives/{id}?action=download | wt-box | x-cookies |
| 上传新文件 | POST | /drive/upload | wt-box | x-cookies |
| 上传新版本 | POST | /drive/update?id={file_id} | wt-box | x-cookies |
| 创建文件夹 | POST | /api/drive/folder | 主域名 | Cookie |
| 删除文件 | DELETE | /api/drives/{id} | 主域名 | Cookie |

## 已知限制

- Cookie 需手动更新（更新 config.yaml 后自动热重载，不用重启容器）

## 关键设计决策

- **监控文件**: INTERNAL_FILES 排除，不上传到 Worktile（preview 不可用）
- **业务文件更新**: 用 `/drive/update`（版本更新，保留历史）
- **监控文件更新**: 用 delete + `/drive/upload`（不创版本）— 但目前已排除同步
- **文件夹跳过**: 记录 folder updated_at，未变化跳过整个子树
- **首次同步**: 双方都有且大小一致 → 跳过；大小不同 → 远程为准
- **冲突处理**: Last Write Wins + 备份 .conflict 文件
- **重命名检测**: 通过 remote_id 匹配，本地 rename 替代重新下载
