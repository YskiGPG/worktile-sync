"""本地文件变化监听（可选功能，需要 watchdog）"""

import logging
import threading
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
    HAS_WATCHDOG = True
except ImportError:
    HAS_WATCHDOG = False
    # Dummy base class when watchdog not installed
    class FileSystemEventHandler:  # type: ignore[no-redef]
        pass


class _ChangeCollector(FileSystemEventHandler):
    """收集文件变化事件"""

    def __init__(self, ignore_check: Callable[[str], bool]) -> None:
        super().__init__()
        self._changed: set[str] = set()
        self._lock = threading.Lock()
        self._ignore = ignore_check

    def _on_change(self, event: object) -> None:
        if getattr(event, "is_directory", False):
            return
        src = getattr(event, "src_path", "")
        name = Path(src).name
        if self._ignore(name):
            return
        with self._lock:
            self._changed.add(src)

    on_created = _on_change
    on_modified = _on_change
    on_deleted = _on_change
    on_moved = _on_change

    def has_changes(self) -> bool:
        return bool(self._changed)

    def get_and_clear(self) -> set[str]:
        with self._lock:
            paths = self._changed.copy()
            self._changed.clear()
            return paths


class LocalWatcher:
    """监听本地目录文件变化，检测到变化时可提前触发同步。

    使用 watchdog 库，Docker 环境中需要 volume mount 支持 inotify。
    """

    def __init__(self, watch_dir: Path, ignore_check: Callable[[str], bool]) -> None:
        if not HAS_WATCHDOG:
            raise ImportError("watchdog 未安装: pip install watchdog")
        self._collector = _ChangeCollector(ignore_check)
        self._observer = Observer()
        self._observer.schedule(self._collector, str(watch_dir), recursive=True)

    def start(self) -> None:
        self._observer.start()
        logger.info("本地文件监听已启动")

    def stop(self) -> None:
        self._observer.stop()
        self._observer.join()
        logger.info("本地文件监听已停止")

    def has_changes(self) -> bool:
        return self._collector.has_changes()

    def get_changes(self) -> set[str]:
        return self._collector.get_and_clear()
