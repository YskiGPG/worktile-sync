"""入口：加载配置、主循环、通知"""

import json
import logging
import signal
import sys
import time
from pathlib import Path
from typing import Any

import yaml

from .api import WorktileAPI
from .auth import AuthManager
from .notify import Notifier
from .sync import SyncEngine
from .utils import setup_logging

logger = logging.getLogger(__name__)

_running = True


def _handle_signal(signum: int, frame: Any) -> None:
    global _running
    logger.info("收到信号 %s，准备退出...", signal.Signals(signum).name)
    _running = False


def load_config(path: str = "config.yaml") -> dict[str, Any]:
    config_path = Path(path)
    if not config_path.exists():
        logger.error("配置文件 %s 不存在，请从 config.example.yaml 复制并填写", path)
        sys.exit(1)

    with open(config_path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _write_health(health_file: Path, data: dict) -> None:
    """原子写入健康状态文件"""
    tmp = health_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.rename(health_file)


def main() -> None:
    config = load_config()

    # 日志配置
    log_cfg = config.get("logging", {})
    setup_logging(
        level=log_cfg.get("level", "INFO"),
        log_file=log_cfg.get("file"),
        max_size_mb=log_cfg.get("max_size_mb", 10),
        backup_count=log_cfg.get("backup_count", 3),
    )

    wt = config["worktile"]
    sync_cfg = config["sync"]
    logger.info("=" * 50)
    logger.info("Worktile 网盘同步工具启动")
    logger.info("  远程: %s (文件夹 ID: %s)", wt["base_url"], wt.get("root_folder_id", "全部"))
    logger.info("  本地: %s", sync_cfg["local_dir"])
    logger.info("  同步间隔: %d 秒", sync_cfg["interval"])
    logger.info("  并发线程: %d", sync_cfg.get("max_workers", 1))
    logger.info("  删除同步: %s", "启用" if sync_cfg.get("sync_delete") else "禁用")
    logger.info("  Dry-run: %s", "启用" if sync_cfg.get("dry_run") else "禁用")
    logger.info("=" * 50)

    # 初始化组件
    auth = AuthManager(wt["auth"])
    api = WorktileAPI(
        base_url=wt["base_url"],
        box_url=wt.get("box_url", "https://wt-box.worktile.com"),
        team_id=wt.get("team_id", ""),
        auth=auth,
    )

    local_dir = Path(sync_cfg["local_dir"])
    local_dir.mkdir(parents=True, exist_ok=True)

    # 状态和健康文件放在同步目录下（自动忽略不上传）
    state_path = local_dir / "sync_state.json"
    health_file = local_dir / "sync_health.json"

    # 通知
    notifier = Notifier(config.get("notification", {}))

    engine = SyncEngine(
        api=api,
        local_dir=local_dir,
        root_folder_id=wt["root_folder_id"],
        state_path=state_path,
        sync_delete=sync_cfg.get("sync_delete", False),
        dry_run=sync_cfg.get("dry_run", False),
        ignore_patterns=sync_cfg.get("ignore_patterns", []),
        max_workers=sync_cfg.get("max_workers", 1),
    )

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    # 主循环
    interval = sync_cfg.get("interval", 60)
    consecutive_errors = 0

    try:
        while _running:
            stats = engine.sync_once()

            # 健康状态
            health = {
                "last_sync": time.strftime("%Y-%m-%d %H:%M:%S"),
                "stats": stats,
                "status": "error" if stats["errors"] > 0 else "ok",
                "consecutive_errors": consecutive_errors,
            }
            _write_health(health_file, health)

            # 错误告警与通知
            if stats["errors"] > 0:
                consecutive_errors += 1
                if consecutive_errors >= notifier.error_threshold:
                    msg = (
                        f"连续 {consecutive_errors} 轮同步出现错误！\n\n"
                        f"统计: {json.dumps(stats, ensure_ascii=False)}\n"
                        f"时间: {health['last_sync']}\n\n"
                        f"请检查：Cookie 是否过期？网络是否正常？"
                    )
                    logger.critical(msg)
                    notifier.send("Worktile 同步告警", msg)
            else:
                if consecutive_errors >= notifier.error_threshold:
                    # 从错误中恢复，发送恢复通知
                    notifier.send(
                        "Worktile 同步恢复",
                        f"同步已恢复正常（此前连续 {consecutive_errors} 轮错误）",
                    )
                consecutive_errors = 0

            for _ in range(interval):
                if not _running:
                    break
                time.sleep(1)
    finally:
        api.close()
        logger.info("同步工具已停止")


if __name__ == "__main__":
    main()
