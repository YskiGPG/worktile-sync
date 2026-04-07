"""同步状态持久化：记录每个文件的同步状态"""

import json
import logging
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

STATE_FILE = "sync_state.json"


@dataclass
class FileRecord:
    """单个文件的同步记录"""
    name: str
    remote_id: str
    remote_mtime: int  # Worktile updated_at Unix 时间戳（秒）
    remote_size: int
    local_mtime: float  # os.path.getmtime 返回的 timestamp
    local_size: int
    last_sync: str  # 最后一次同步的 ISO 时间
    cos_key: str = ""  # COS 文件 key


@dataclass
class SyncState:
    """完整的同步状态"""
    # key = 相对路径（如 "docs/readme.txt"）
    files: dict[str, FileRecord] = field(default_factory=dict)

    def save(self, path: Path) -> None:
        """持久化状态到 JSON 文件"""
        data = {k: asdict(v) for k, v in self.files.items()}
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.debug("同步状态已保存到 %s", path)

    @classmethod
    def load(cls, path: Path) -> "SyncState":
        """从 JSON 文件加载状态"""
        if not path.exists():
            logger.info("未找到状态文件，将进行首次同步")
            return cls()

        try:
            data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
            files = {k: FileRecord(**v) for k, v in data.items()}
            return cls(files=files)
        except (json.JSONDecodeError, TypeError) as e:
            logger.warning("状态文件损坏，将重新同步: %s", e)
            return cls()
