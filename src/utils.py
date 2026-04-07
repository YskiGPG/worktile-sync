"""工具函数：日志配置、文件哈希等"""

import hashlib
import logging
import fnmatch
from pathlib import Path


def setup_logging(level: str = "INFO", log_file: str | None = None) -> None:
    """配置全局日志"""
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_file:
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))

    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
    )


def file_md5(path: Path, chunk_size: int = 8192) -> str:
    """计算文件的 MD5 哈希"""
    h = hashlib.md5()
    with open(path, "rb") as f:
        while chunk := f.read(chunk_size):
            h.update(chunk)
    return h.hexdigest()


def should_ignore(name: str, patterns: list[str]) -> bool:
    """检查文件名是否匹配忽略规则"""
    return any(fnmatch.fnmatch(name, p) for p in patterns)


def human_size(size_bytes: int) -> str:
    """将字节数转为可读字符串"""
    for unit in ("B", "KB", "MB", "GB"):
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024  # type: ignore[assignment]
    return f"{size_bytes:.1f} TB"
