"""入口：加载配置、主循环"""

import logging
import signal
import sys
import time
from pathlib import Path
from typing import Any

import yaml

from .api import WorktileAPI
from .auth import AuthManager
from .sync import SyncEngine
from .utils import setup_logging

logger = logging.getLogger(__name__)

_running = True


def _handle_signal(signum: int, frame: Any) -> None:
    global _running
    logger.info("收到信号 %s，准备退出...", signal.Signals(signum).name)
    _running = False


def load_config(path: str = "config.yaml") -> dict[str, Any]:
    """加载 YAML 配置文件"""
    config_path = Path(path)
    if not config_path.exists():
        logger.error("配置文件 %s 不存在，请从 config.example.yaml 复制并填写", path)
        sys.exit(1)

    with open(config_path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def main() -> None:
    config = load_config()

    # 日志配置
    log_cfg = config.get("logging", {})
    setup_logging(
        level=log_cfg.get("level", "INFO"),
        log_file=log_cfg.get("file"),
    )

    # 打印同步配置摘要
    wt = config["worktile"]
    sync_cfg = config["sync"]
    logger.info("=" * 50)
    logger.info("Worktile 网盘同步工具启动")
    logger.info("  远程: %s (文件夹 ID: %s)", wt["base_url"], wt.get("root_folder_id", "全部"))
    logger.info("  本地: %s", sync_cfg["local_dir"])
    logger.info("  同步间隔: %d 秒", sync_cfg["interval"])
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
    state_path = Path("sync_state.json")

    engine = SyncEngine(
        api=api,
        local_dir=local_dir,
        root_folder_id=wt["root_folder_id"],
        state_path=state_path,
        sync_delete=sync_cfg.get("sync_delete", False),
        dry_run=sync_cfg.get("dry_run", False),
        ignore_patterns=sync_cfg.get("ignore_patterns", []),
    )

    # 注册信号处理
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    # 主循环
    interval = sync_cfg.get("interval", 60)
    try:
        while _running:
            engine.sync_once()
            # 等待下一轮，支持中途退出
            for _ in range(interval):
                if not _running:
                    break
                time.sleep(1)
    finally:
        api.close()
        logger.info("同步工具已停止")


if __name__ == "__main__":
    main()
