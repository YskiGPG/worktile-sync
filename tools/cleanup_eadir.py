"""清理 Worktile 网盘中的 @eaDir 文件夹（群晖缩略图目录）

用法: python tools/cleanup_eadir.py
"""

import sys
import time
import logging
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.api import WorktileAPI, FileInfo
from src.auth import AuthManager

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def find_eadir(api: WorktileAPI, folder_id: str, path: str, targets: list) -> None:
    items = api.list_files(folder_id)
    for item in items:
        if not item.is_folder:
            continue
        full = f"{path}/{item.name}" if path else item.name
        if item.name == "@eaDir":
            targets.append((item.id, full))
            logger.info("找到: %s", full)
        else:
            find_eadir(api, item.id, full, targets)


def main() -> None:
    with open("config.yaml", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    wt = config["worktile"]
    auth = AuthManager(wt["auth"])
    api = WorktileAPI(
        base_url=wt["base_url"],
        box_url=wt.get("box_url", "https://wt-box.worktile.com"),
        team_id=wt.get("team_id", ""),
        auth=auth,
    )

    folder_id = wt.get("root_folder_id", "")
    targets: list[tuple[str, str]] = []

    logger.info("扫描 @eaDir 文件夹...")
    if folder_id:
        find_eadir(api, folder_id, "", targets)
    else:
        for f in api.list_root_folders():
            find_eadir(api, f.id, f.name, targets)

    logger.info("共找到 %d 个 @eaDir 文件夹", len(targets))

    for fid, path in targets:
        try:
            api.delete_file(fid)
            logger.info("已删除: %s", path)
            time.sleep(0.3)
        except Exception:
            logger.exception("删除失败: %s", path)

    api.close()
    logger.info("完成")


if __name__ == "__main__":
    main()
