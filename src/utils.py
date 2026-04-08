"""工具函数：日志、文件名处理、速率限制、Unicode 规范化"""

import hashlib
import logging
import fnmatch
import threading
import time
import unicodedata
from pathlib import Path

MAX_NAME_BYTES = 255


def normalize_name(name: str) -> str:
    """Unicode NFC 规范化（macOS 用 NFD，Linux 用 NFC，统一为 NFC）"""
    return unicodedata.normalize("NFC", name)


def safe_name(name: str) -> str:
    """截断过长的文件/目录名，保证不超过文件系统 255 字节限制"""
    encoded = name.encode("utf-8")
    if len(encoded) <= MAX_NAME_BYTES:
        return name

    dot_idx = name.rfind(".")
    if dot_idx > 0 and dot_idx > len(name) - 10:
        stem, ext = name[:dot_idx], name[dot_idx:]
    else:
        stem, ext = name, ""

    name_hash = hashlib.md5(name.encode("utf-8")).hexdigest()[:8]
    suffix = f"_{name_hash}{ext}"
    suffix_bytes = len(suffix.encode("utf-8"))

    max_stem_bytes = MAX_NAME_BYTES - suffix_bytes
    truncated = ""
    byte_count = 0
    for ch in stem:
        ch_bytes = len(ch.encode("utf-8"))
        if byte_count + ch_bytes > max_stem_bytes:
            break
        truncated += ch
        byte_count += ch_bytes

    result = truncated + suffix
    logging.getLogger(__name__).warning(
        "文件名过长，已截断: %s → %s", name[:60] + "...", result[:60] + "..."
    )
    return result


def setup_logging(
    level: str = "INFO",
    log_file: str | None = None,
    max_size_mb: int = 10,
    backup_count: int = 3,
) -> None:
    """配置全局日志（带文件轮转）"""
    from logging.handlers import RotatingFileHandler

    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_file:
        handlers.append(RotatingFileHandler(
            log_file,
            maxBytes=max_size_mb * 1024 * 1024,
            backupCount=backup_count,
            encoding="utf-8",
        ))

    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
    )


def file_md5(path: Path, chunk_size: int = 8192) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        while chunk := f.read(chunk_size):
            h.update(chunk)
    return h.hexdigest()


def should_ignore(name: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(name, p) for p in patterns)


def human_size(size_bytes: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024  # type: ignore[assignment]
    return f"{size_bytes:.1f} TB"


class RateLimiter:
    """令牌桶速率限制器"""

    def __init__(self, calls_per_second: float = 5.0) -> None:
        self._min_interval = 1.0 / calls_per_second if calls_per_second > 0 else 0
        self._last_call = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        if self._min_interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_call
            if elapsed < self._min_interval:
                time.sleep(self._min_interval - elapsed)
            self._last_call = time.monotonic()
