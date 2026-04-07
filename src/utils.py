"""工具函数：日志配置、文件哈希、文件名截断等"""

import hashlib
import logging
import fnmatch
from pathlib import Path

# Linux ext4 单个文件/目录名最长 255 字节
MAX_NAME_BYTES = 255


def safe_name(name: str) -> str:
    """截断过长的文件/目录名，保证不超过文件系统限制

    中文字符 UTF-8 编码占 3 字节，255 字节约 85 个中文字符。
    截断时保留文件扩展名，中间加 hash 避免重名。
    """
    encoded = name.encode("utf-8")
    if len(encoded) <= MAX_NAME_BYTES:
        return name

    # 分离扩展名
    dot_idx = name.rfind(".")
    if dot_idx > 0 and dot_idx > len(name) - 10:
        stem, ext = name[:dot_idx], name[dot_idx:]
    else:
        stem, ext = name, ""

    # 用原名的 hash 后 8 位保证唯一性
    name_hash = hashlib.md5(name.encode("utf-8")).hexdigest()[:8]
    suffix = f"_{name_hash}{ext}"
    suffix_bytes = len(suffix.encode("utf-8"))

    # 按字符逐个截断 stem，直到总长度 <= 255 字节
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
    logging.getLogger(__name__).warning("文件名过长，已截断: %s → %s", name[:60] + "...", result[:60] + "...")
    return result


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
