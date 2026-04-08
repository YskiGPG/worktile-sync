"""清理 Worktile 网盘中的重复文件

同步工具因状态丢失导致重复上传时使用。
逻辑：同一文件夹内同名文件，保留最早的（原件），删除后来上传的副本。

用法：
    # 预览（不删除）
    python tools/dedup_remote.py

    # 确认删除
    python tools/dedup_remote.py --delete
"""

import sys
import time
import logging
from collections import defaultdict
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.api import WorktileAPI, FileInfo
from src.auth import AuthManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def find_duplicates(api: WorktileAPI, folder_id: str, folder_name: str, depth: int = 0) -> list[FileInfo]:
    """递归查找文件夹中的重复文件，返回应删除的副本列表"""
    indent = "  " * depth
    duplicates: list[FileInfo] = []

    items = api.list_files(folder_id)

    # 按名称分组
    by_name: dict[str, list[FileInfo]] = defaultdict(list)
    subfolders: list[FileInfo] = []

    for item in items:
        if item.is_folder:
            subfolders.append(item)
        else:
            by_name[item.name].append(item)

    # 找出重复的（同名 > 1 个）
    # 2026-04-07 00:00:00 CST (UTC+8) 的 Unix 时间戳
    APR7_START = 1775491200

    for name, copies in by_name.items():
        if len(copies) > 1:
            # 按 mtime 升序排，保留最早的
            copies.sort(key=lambda f: f.mtime)
            original = copies[0]
            # 只删除 4月7日及之后上传的副本
            dupes = [c for c in copies[1:] if c.mtime >= APR7_START]
            if dupes:
                logger.info(
                    "%s[重复] %s/%s: 保留原件(mtime=%d), 删除 %d 个4月7日副本",
                    indent, folder_name, name, original.mtime, len(dupes),
                )
                duplicates.extend(dupes)

    # 递归子文件夹
    for sub in subfolders:
        duplicates.extend(
            find_duplicates(api, sub.id, f"{folder_name}/{sub.name}", depth + 1)
        )

    return duplicates


def main() -> None:
    delete_mode = "--delete" in sys.argv

    config_path = Path("config.yaml")
    if not config_path.exists():
        logger.error("找不到 config.yaml")
        sys.exit(1)

    with open(config_path, encoding="utf-8") as f:
        config = yaml.safe_load(f)

    wt = config["worktile"]
    auth = AuthManager(wt["auth"])
    api = WorktileAPI(
        base_url=wt["base_url"],
        box_url=wt.get("box_url", "https://wt-box.worktile.com"),
        team_id=wt.get("team_id", ""),
        auth=auth,
    )

    logger.info("=" * 50)
    logger.info("Worktile 网盘重复文件清理工具")
    logger.info("模式: %s", "删除" if delete_mode else "预览（加 --delete 执行删除）")
    logger.info("=" * 50)

    # 获取根文件夹
    root_folder_id = wt.get("root_folder_id", "")
    all_duplicates: list[FileInfo] = []

    if root_folder_id:
        all_duplicates = find_duplicates(api, root_folder_id, "根目录")
    else:
        root_folders = api.list_root_folders()
        for folder in root_folders:
            dupes = find_duplicates(api, folder.id, folder.name)
            all_duplicates.extend(dupes)

    logger.info("=" * 50)
    logger.info("共发现 %d 个重复文件", len(all_duplicates))

    if not all_duplicates:
        logger.info("没有重复文件，无需清理")
        api.close()
        return

    # 统计大小
    total_size = sum(f.size for f in all_duplicates)
    logger.info("重复文件总大小: %.1f MB", total_size / 1024 / 1024)

    if delete_mode:
        logger.info("开始删除...")
        deleted = 0
        errors = 0
        for f in all_duplicates:
            try:
                api.delete_file(f.id)
                deleted += 1
                if deleted % 10 == 0:
                    logger.info("已删除 %d / %d", deleted, len(all_duplicates))
            except Exception:
                logger.exception("删除失败: %s (id=%s)", f.name, f.id)
                errors += 1
            time.sleep(0.2)  # 避免请求过快

        logger.info("删除完成: 成功 %d, 失败 %d", deleted, errors)
    else:
        logger.info("以上为预览，加 --delete 参数执行实际删除")

    api.close()


if __name__ == "__main__":
    main()
