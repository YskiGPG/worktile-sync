"""核心同步逻辑：双向同步、冲突处理、并发下载"""

import logging
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from .api import WorktileAPI, FileInfo
from .state import SyncState, FileRecord
from .utils import should_ignore, safe_name

logger = logging.getLogger(__name__)

# 内部文件，自动忽略不参与同步
INTERNAL_FILES = {"sync_state.json", "sync_state.tmp", "sync_health.json", "sync_health.tmp"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class SyncEngine:
    """双向同步引擎"""

    def __init__(
        self,
        api: WorktileAPI,
        local_dir: Path,
        root_folder_id: str,
        state_path: Path,
        sync_delete: bool = False,
        dry_run: bool = False,
        ignore_patterns: list[str] | None = None,
        max_workers: int = 1,
    ) -> None:
        self.api = api
        self.local_dir = local_dir
        self.root_folder_id = root_folder_id
        self.state_path = state_path
        self.sync_delete = sync_delete
        self.dry_run = dry_run
        self.ignore_patterns = ignore_patterns or []
        self.max_workers = max(1, max_workers)
        self.state = SyncState.load(state_path)
        self._lock = threading.Lock()

        self.stats: dict[str, int] = {
            "downloaded": 0, "uploaded": 0, "deleted_local": 0,
            "deleted_remote": 0, "conflicts": 0, "errors": 0,
        }

    def _should_ignore(self, name: str) -> bool:
        """检查是否应忽略（用户模式 + 内部文件）"""
        return name in INTERNAL_FILES or should_ignore(name, self.ignore_patterns)

    def sync_once(self) -> dict[str, int]:
        """执行一次完整同步，返回统计信息"""
        self.stats = {k: 0 for k in self.stats}
        self._download_tasks: list[tuple[FileInfo, Path, str]] = []
        logger.info("开始同步...")

        try:
            if self.root_folder_id:
                self._sync_folder(self.root_folder_id, self.local_dir, "")
            else:
                root_folders = self.api.list_root_folders()
                for folder in root_folders:
                    if self._should_ignore(folder.name):
                        continue
                    sub_local = self.local_dir / folder.name
                    try:
                        self._sync_folder(folder.id, sub_local, folder.name)
                    except Exception:
                        logger.exception("同步根文件夹失败，跳过: %s", folder.name)
                        self.stats["errors"] += 1

            # 执行并发下载队列
            self._flush_downloads()

        except Exception:
            logger.exception("同步过程中发生错误")
            self.stats["errors"] += 1
        finally:
            # 始终保存状态（即使有错误）
            self.state.save(self.state_path)

        logger.info(
            "同步完成 — 下载:%d 上传:%d 删除(本地):%d 删除(远程):%d 冲突:%d 错误:%d",
            self.stats["downloaded"], self.stats["uploaded"],
            self.stats["deleted_local"], self.stats["deleted_remote"],
            self.stats["conflicts"], self.stats["errors"],
        )

        return dict(self.stats)

    def _flush_downloads(self) -> None:
        """执行所有待下载任务（并发模式）"""
        if not self._download_tasks:
            return

        count = len(self._download_tasks)
        logger.info("开始并发下载 %d 个文件 (workers=%d)", count, self.max_workers)

        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures = [
                executor.submit(self._do_download, *task)
                for task in self._download_tasks
            ]
            for future in as_completed(futures):
                pass  # 错误已在 _do_download 中处理

        self._download_tasks.clear()

    def _sync_folder(self, folder_id: str, local_path: Path, rel_prefix: str) -> None:
        """同步一个文件夹（递归）"""
        local_path.mkdir(parents=True, exist_ok=True)

        # 1. 获取远程文件列表
        remote_items = self.api.list_files(folder_id)
        remote_map: dict[str, FileInfo] = {f.name: f for f in remote_items}

        # 2. 扫描本地文件
        local_entries: dict[str, Path] = {}
        if local_path.exists():
            for entry in local_path.iterdir():
                if not self._should_ignore(entry.name):
                    local_entries[entry.name] = entry

        all_names = set(remote_map.keys()) | set(local_entries.keys())

        for name in sorted(all_names):
            if self._should_ignore(name):
                continue

            local_name = safe_name(name)
            rel_path = f"{rel_prefix}/{local_name}" if rel_prefix else local_name
            remote = remote_map.get(name)
            local = local_entries.get(name) or local_entries.get(local_name)

            # 递归处理子文件夹
            if remote and remote.is_folder:
                sub_local = local_path / local_name
                try:
                    self._sync_folder(remote.id, sub_local, rel_path)
                except Exception:
                    logger.exception("同步子文件夹失败，跳过: %s", rel_path)
                    self.stats["errors"] += 1
                continue

            if local and local.is_dir() and not remote:
                new_id = self._create_remote_folder(folder_id, name)
                if new_id:
                    try:
                        self._sync_folder(new_id, local, rel_path)
                    except Exception:
                        logger.exception("同步新建远程文件夹失败，跳过: %s", rel_path)
                        self.stats["errors"] += 1
                continue

            # 处理文件同步
            self._sync_file(
                name=local_name,
                rel_path=rel_path,
                folder_id=folder_id,
                local_path=local_path,
                remote=remote,
                local=local,
            )

    def _sync_file(
        self,
        name: str,
        rel_path: str,
        folder_id: str,
        local_path: Path,
        remote: FileInfo | None,
        local: Path | None,
    ) -> None:
        """同步单个文件"""
        prev = self.state.files.get(rel_path)

        # Case 1: 远程有，本地没有
        if remote and not local:
            if prev:
                if self.sync_delete:
                    self._delete_remote(remote, rel_path)
                else:
                    logger.debug("本地已删除但未启用删除同步，跳过: %s", rel_path)
            else:
                self._download(remote, local_path / name, rel_path)
            return

        # Case 2: 本地有，远程没有
        if local and not remote:
            if prev:
                if self.sync_delete:
                    self._delete_local(local, rel_path)
                else:
                    logger.debug("远程已删除但未启用删除同步，跳过: %s", rel_path)
            else:
                self._upload(folder_id, local, rel_path)
            return

        # Case 3: 双方都有
        if remote and local and local.is_file():
            remote_ts = float(remote.mtime)
            local_ts = local.stat().st_mtime
            local_size = local.stat().st_size

            remote_changed = prev and (
                remote.mtime != prev.remote_mtime or remote.size != prev.remote_size
            )
            local_changed = prev and (
                abs(local_ts - prev.local_mtime) > 1 or local_size != prev.local_size
            )

            if not prev:
                # 首次同步：双方都有且大小一致 → 视为已同步，直接记录状态
                if remote.size == local_size:
                    logger.debug("首次同步，文件大小一致，跳过: %s", rel_path)
                    self._record_state(rel_path, remote, local)
                elif remote_ts > local_ts:
                    self._download(remote, local, rel_path)
                else:
                    # 本地更新或时间相同但大小不同 → 以远程为准（保守策略）
                    self._download(remote, local, rel_path)
            elif remote_changed and local_changed:
                self._handle_conflict(remote, local, folder_id, rel_path)
            elif remote_changed:
                self._download(remote, local, rel_path)
            elif local_changed:
                self._upload(folder_id, local, rel_path)

    def _download(self, remote: FileInfo, save_path: Path, rel_path: str) -> None:
        """下载文件（串行直接执行，并发加入队列）"""
        if self.dry_run:
            logger.info("[DRY-RUN] 将下载: %s", rel_path)
            return

        if self.max_workers > 1:
            self._download_tasks.append((remote, save_path, rel_path))
        else:
            self._do_download(remote, save_path, rel_path)

    def _do_download(self, remote: FileInfo, save_path: Path, rel_path: str) -> None:
        """执行实际下载"""
        try:
            self.api.download_file(remote, save_path)
            with self._lock:
                self._record_state(rel_path, remote, save_path)
                self.stats["downloaded"] += 1
        except Exception:
            logger.exception("下载失败: %s", rel_path)
            with self._lock:
                self.stats["errors"] += 1

    def _upload(self, folder_id: str, file_path: Path, rel_path: str) -> None:
        if self.dry_run:
            logger.info("[DRY-RUN] 将上传: %s", rel_path)
            return

        try:
            result = self.api.upload_file(folder_id, file_path)
            data = result.get("data", result)
            remote_info = FileInfo(
                id=data.get("_id", ""),
                name=file_path.name,
                is_folder=False,
                size=file_path.stat().st_size,
                mtime=data.get("updated_at", int(datetime.now().timestamp())),
                parent_id=folder_id,
                cos_key=data.get("addition", {}).get("path", ""),
                version=data.get("addition", {}).get("current_version", 1),
            )
            self._record_state(rel_path, remote_info, file_path)
            self.stats["uploaded"] += 1
        except Exception:
            logger.exception("上传失败: %s", rel_path)
            self.stats["errors"] += 1

    def _delete_local(self, path: Path, rel_path: str) -> None:
        if self.dry_run:
            logger.info("[DRY-RUN] 将删除本地: %s", rel_path)
            return
        try:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
            self.state.files.pop(rel_path, None)
            self.stats["deleted_local"] += 1
            logger.info("已删除本地文件: %s", rel_path)
        except Exception:
            logger.exception("删除本地文件失败: %s", rel_path)
            self.stats["errors"] += 1

    def _delete_remote(self, remote: FileInfo, rel_path: str) -> None:
        if self.dry_run:
            logger.info("[DRY-RUN] 将删除远程: %s", rel_path)
            return
        try:
            self.api.delete_file(remote.id)
            self.state.files.pop(rel_path, None)
            self.stats["deleted_remote"] += 1
        except Exception:
            logger.exception("删除远程文件失败: %s", rel_path)
            self.stats["errors"] += 1

    def _handle_conflict(
        self, remote: FileInfo, local_path: Path, folder_id: str, rel_path: str
    ) -> None:
        self.stats["conflicts"] += 1
        remote_ts = float(remote.mtime)
        local_ts = local_path.stat().st_mtime

        stem = local_path.stem
        suffix = local_path.suffix
        conflict_name = f"{stem}.conflict{suffix}"
        conflict_path = local_path.parent / conflict_name

        if remote_ts >= local_ts:
            logger.info("冲突：远程更新，备份本地文件: %s -> %s", rel_path, conflict_name)
            if not self.dry_run:
                shutil.copy2(local_path, conflict_path)
                self._download(remote, local_path, rel_path)
        else:
            logger.info("冲突：本地更新，上传本地文件: %s", rel_path)
            self._upload(folder_id, local_path, rel_path)

    def _create_remote_folder(self, parent_id: str, name: str) -> str:
        if self.dry_run:
            logger.info("[DRY-RUN] 将创建远程文件夹: %s", name)
            return ""
        try:
            return self.api.create_folder(parent_id, name)
        except Exception:
            logger.exception("创建远程文件夹失败: %s", name)
            self.stats["errors"] += 1
            return ""

    def _record_state(
        self, rel_path: str, remote: FileInfo, local_path: Path
    ) -> None:
        stat = local_path.stat() if local_path.exists() else None
        self.state.files[rel_path] = FileRecord(
            name=remote.name,
            remote_id=remote.id,
            remote_mtime=remote.mtime,
            remote_size=remote.size,
            local_mtime=stat.st_mtime if stat else 0.0,
            local_size=stat.st_size if stat else 0,
            last_sync=_now_iso(),
        )
