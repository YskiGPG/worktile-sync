"""同步状态持久化：记录每个文件和文件夹的同步状态"""

import json
import logging
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class FileRecord:
    """单个文件的同步记录"""
    name: str
    remote_id: str
    remote_mtime: int
    remote_size: int
    local_mtime: float
    local_size: int
    last_sync: str
    cos_key: str = ""
    local_hash: str = ""  # MD5 hash（懒计算，仅 mtime 变但 size 不变时触发）


@dataclass
class FolderRecord:
    """文件夹的同步记录（用于跳过未变化的文件夹）"""
    remote_id: str
    remote_mtime: int
    last_sync: str


@dataclass
class SyncState:
    """完整的同步状态"""
    files: dict[str, FileRecord] = field(default_factory=dict)
    folders: dict[str, FolderRecord] = field(default_factory=dict)

    def save(self, path: Path) -> None:
        """持久化状态到 JSON 文件（原子写入）"""
        data = {
            "files": {k: asdict(v) for k, v in self.files.items()},
            "folders": {k: asdict(v) for k, v in self.folders.items()},
        }
        tmp_path = path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_path.rename(path)
        logger.debug("同步状态已保存到 %s (%d 个文件, %d 个文件夹)",
                      path, len(self.files), len(self.folders))

    @classmethod
    def load(cls, path: Path) -> "SyncState":
        """从 JSON 文件加载状态"""
        if not path.exists():
            logger.info("未找到状态文件，将进行首次同步")
            return cls()

        try:
            raw: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))

            # 兼容旧格式（v1/v2 没有 folders 字段，files 直接是顶层 dict）
            if "files" in raw and isinstance(raw["files"], dict):
                files_raw = raw["files"]
                folders_raw = raw.get("folders", {})
            else:
                # 旧格式：整个 JSON 就是 files
                files_raw = raw
                folders_raw = {}

            files = {k: FileRecord(**v) for k, v in files_raw.items()}
            folders = {k: FolderRecord(**v) for k, v in folders_raw.items()}
            return cls(files=files, folders=folders)
        except (json.JSONDecodeError, TypeError) as e:
            logger.warning("状态文件损坏，将重新同步: %s", e)
            return cls()
