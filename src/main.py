"""入口：主循环、配置热重载、审计日志、文件监听"""

import csv
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
        logger.error("配置文件 %s 不存在", path)
        sys.exit(1)
    with open(config_path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _write_health(health_file: Path, data: dict) -> None:
    tmp = health_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.rename(health_file)


def _write_audit(audit_file: Path, stats: dict, duration: float) -> None:
    """追加同步审计记录到 CSV"""
    header_needed = not audit_file.exists()
    with open(audit_file, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if header_needed:
            writer.writerow([
                "timestamp", "duration_sec", "downloaded", "uploaded",
                "deleted_local", "deleted_remote", "conflicts", "errors",
                "skipped_folders",
            ])
        writer.writerow([
            time.strftime("%Y-%m-%d %H:%M:%S"),
            f"{duration:.1f}",
            stats.get("downloaded", 0), stats.get("uploaded", 0),
            stats.get("deleted_local", 0), stats.get("deleted_remote", 0),
            stats.get("conflicts", 0), stats.get("errors", 0),
            stats.get("skipped_folders", 0),
        ])


def main() -> None:
    config = load_config()
    config_path = Path("config.yaml")
    config_mtime = config_path.stat().st_mtime

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
    logger.info("Worktile 网盘同步工具 v4 启动")
    logger.info("  远程: %s (文件夹 ID: %s)", wt["base_url"], wt.get("root_folder_id", "全部"))
    logger.info("  本地: %s", sync_cfg["local_dir"])
    logger.info("  同步间隔: %d 秒", sync_cfg["interval"])
    logger.info("  并发线程: %d", sync_cfg.get("max_workers", 1))
    logger.info("  删除同步: %s", "启用" if sync_cfg.get("sync_delete") else "禁用")
    logger.info("  Dry-run: %s", "启用" if sync_cfg.get("dry_run") else "禁用")
    logger.info("  本地监听: %s", "启用" if sync_cfg.get("watch_local") else "禁用")
    logger.info("=" * 50)

    auth = AuthManager(wt["auth"])
    api = WorktileAPI(
        base_url=wt["base_url"],
        box_url=wt.get("box_url", "https://wt-box.worktile.com"),
        team_id=wt.get("team_id", ""),
        auth=auth,
        rate_limit=sync_cfg.get("rate_limit", 5.0),
    )

    local_dir = Path(sync_cfg["local_dir"])
    local_dir.mkdir(parents=True, exist_ok=True)

    state_path = local_dir / "sync_state.json"
    health_file = local_dir / "sync_health.json"
    audit_file = local_dir / "sync_audit.csv"

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

    # 可选：本地文件监听
    watcher = None
    if sync_cfg.get("watch_local", False):
        try:
            from .watcher import LocalWatcher
            watcher = LocalWatcher(local_dir, engine._should_ignore)
            watcher.start()
        except ImportError:
            logger.warning("watchdog 未安装，本地文件监听禁用。pip install watchdog 可启用")

    interval = sync_cfg.get("interval", 60)
    consecutive_errors = 0

    try:
        while _running:
            # 配置热重载（检测 Cookie 更新等）
            try:
                current_mtime = config_path.stat().st_mtime
                if current_mtime != config_mtime:
                    logger.info("检测到配置文件变化，重新加载...")
                    config = load_config()
                    wt = config["worktile"]
                    new_auth = AuthManager(wt["auth"])
                    api.auth = new_auth
                    notifier = Notifier(config.get("notification", {}))
                    interval = config["sync"].get("interval", 60)
                    config_mtime = current_mtime
                    logger.info("配置已重新加载")
            except Exception:
                logger.exception("重新加载配置失败")

            # 执行同步
            start_time = time.monotonic()
            stats = engine.sync_once()
            duration = time.monotonic() - start_time

            # 健康状态
            health = {
                "last_sync": time.strftime("%Y-%m-%d %H:%M:%S"),
                "duration_sec": round(duration, 1),
                "stats": stats,
                "status": "error" if stats["errors"] > 0 else "ok",
                "consecutive_errors": consecutive_errors,
            }
            _write_health(health_file, health)

            # 审计日志
            _write_audit(audit_file, stats, duration)

            # 错误告警
            if stats["errors"] > 0:
                consecutive_errors += 1
                if consecutive_errors >= notifier.error_threshold:
                    msg = (
                        f"连续 {consecutive_errors} 轮同步出现错误！\n\n"
                        f"统计: {json.dumps(stats, ensure_ascii=False)}\n"
                        f"时间: {health['last_sync']}\n"
                        f"耗时: {duration:.1f}s\n\n"
                        f"请检查：Cookie 是否过期？网络是否正常？"
                    )
                    logger.critical(msg)
                    notifier.send("Worktile 同步告警", msg)
            else:
                if consecutive_errors >= notifier.error_threshold:
                    notifier.send(
                        "Worktile 同步恢复",
                        f"同步已恢复正常（此前连续 {consecutive_errors} 轮错误）",
                    )
                consecutive_errors = 0

            # 等待下一轮（支持中途退出 + 本地文件变化提前触发）
            for _ in range(interval):
                if not _running:
                    break
                if watcher and watcher.has_changes():
                    changes = watcher.get_changes()
                    logger.info("检测到 %d 个本地文件变化，提前开始同步", len(changes))
                    break
                time.sleep(1)
    finally:
        if watcher:
            watcher.stop()
        api.close()
        logger.info("同步工具已停止")


if __name__ == "__main__":
    main()
