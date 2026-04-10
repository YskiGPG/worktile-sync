# Worktile 网盘同步工具 - 任务清单

## 已完成

### v1-v3: 基础同步

- [x] 逆向 6 个 Worktile API（列表/下载/上传/删除/创建文件夹）
- [x] 双向同步引擎 + 冲突处理
- [x] Docker 部署到群晖 NAS
- [x] 文件名截断（ext4 255字节限制）
- [x] 错误隔离（单文件/文件夹失败不中断整体）
- [x] Server酱微信通知

### v4: 三阶段架构 + 全面优化

- [x] 三阶段模型：Scan → Plan → Execute
- [x] 逆向第 7 个 API：`/drive/update`（版本上传）
- [x] 文件夹 `updated_at` 跳过（增量同步秒级完成）
- [x] 并发下载（ThreadPoolExecutor）
- [x] 临时文件下载 + 大小校验
- [x] API 速率限制（令牌桶）
- [x] 增量状态保存（每 50 个文件保存一次）
- [x] Hash 校验（mtime 变但 size 不变时算 MD5）
- [x] 重命名检测（remote_id 匹配）
- [x] 本地文件监听（watchdog）
- [x] 配置热重载
- [x] 审计日志 + 进度文件
- [x] Unicode NFC 规范化
- [x] 启动/停止/错误/小时汇总通知

---

## 当前任务: WebSocket 实时推送

### 背景

当前同步每 20 秒轮询 Worktile API。2026-04-10 从浏览器 DevTools 发现 Worktile 使用 Socket.IO：

```
wss://zrhubei.worktile.com/socket.io/?token=841c104b6f614ed7b7f173f386adde48&...
```

实现后可将云端→本地同步从 20 秒延迟降到秒级。

### Phase 1: 逆向 WebSocket 协议

- [ ] 抓取完整连接参数（token 来源、query params）
- [ ] 分析 Socket.IO 握手过程
- [ ] 在 Worktile 上操作文件，记录 WebSocket 收到的消息
- [ ] 确认文件变更事件的格式（event name、payload 结构）
- [ ] 在 Angular 前端 JS 中搜索 Socket.IO 事件注册代码
- [ ] 确认 token 生命周期（是否随 Cookie 过期）

### Phase 2: 实现 WebSocket 客户端

- [ ] Python Socket.IO 客户端（python-socketio 库）
- [ ] 连接认证（token 获取）
- [ ] 消息解析（过滤文件变更事件）
- [ ] 断线重连 + 心跳保活
- [ ] 与同步引擎集成

### Phase 3: 混合同步模式

- [ ] WebSocket 事件触发精准同步（单文件/文件夹级别）
- [ ] 保留轮询作为 fallback（断线降级）
- [ ] 定期全量校验兜底（每小时一次）

### 技术要点

```
目标架构:
  WebSocket listener ──(文件变更事件)──→ 精准同步单个文件
       ↓ (断线)
  fallback polling ──(每小时)──→ 全量 sync_once() 兜底
```

**关键问题：**
1. token 从哪来？可能从 Cookie session 派生或单独认证接口
2. 哪些事件与网盘相关？需要过滤出文件变更事件
3. 事件 payload 结构？file_id / folder_id / 操作类型
4. 消息可靠性？需要兜底机制

### 第一步（需要用户操作）

在浏览器中：
1. 打开 Worktile 网盘 → DevTools → Network → WS 标签
2. 找到 socket.io 连接，点击查看 Messages
3. 在 Worktile 上传/删除/重命名一个文件
4. 截图或复制 WebSocket 收到的消息内容

---

## 未来

- [ ] Cookie 自动刷新（无头浏览器）
- [ ] Web 管理界面
- [ ] 并发上传
- [ ] 多团队支持

*最后更新：2026-04-10*
