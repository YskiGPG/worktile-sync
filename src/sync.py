"""核心同步引擎：三阶段模型（扫描 → 计划 → 执行）"""

import logging
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .api import WorktileAPI, FileInfo
from .state import SyncState, FileRecord, FolderRecord
from .utils import should_ignore, safe_name, normalize_name, human_size, file_md5

logger = logging.getLogger(__name__)

# 只排除临时文件（正式监控文件通过"删旧传新"机制同步到 Worktile）
INTERNAL_FILES = {
    "sync_state.tmp", "sync_health.tmp", "sync_progress.tmp",
    "sync_audit.csv.old",
}

# 群晖系统目录，始终忽略
NAS_SYSTEM_DIRS = {"@eaDir", "#recycle", "@tmp"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class SyncAction:
    """一个待执行的同步操作"""
    type: str           # download, upload, delete_local, delete_remote, conflict
    rel_path: str
    remote: FileInfo | None = None
    local_path: Path | None = None
    folder_id: str = ""
    remote_mtime: int = 0  # 用于按更新时间排序


class SyncEngine:
    """三阶段同步引擎: Scan → Plan → Execute"""

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
        self._progress_file = local_dir / "sync_progress.json"
        self._sync_start = 0.0
        self._total_actions = 0
        self._recent_changes: list[dict] = []
        self._folders_scanned = 0
        self._folders_skipped = 0

        self.stats: dict[str, int] = {
            "downloaded": 0, "uploaded": 0, "deleted_local": 0,
            "deleted_remote": 0, "conflicts": 0, "errors": 0,
            "skipped_folders": 0,
        }

    def _should_ignore(self, name: str) -> bool:
        return (name in INTERNAL_FILES
                or name in NAS_SYSTEM_DIRS
                or should_ignore(name, self.ignore_patterns))

    # ── Phase 1: Scan ──────────────────────────────────────────────

    def _scan_folder(
        self,
        folder_id: str,
        local_path: Path,
        rel_prefix: str,
        actions: list[SyncAction],
        folder_mtime: int = 0,
    ) -> None:
        """递归扫描文件夹，收集同步动作"""
        folder_key = rel_prefix or "/"
        prev_folder = self.state.folders.get(folder_key)

        # 文件夹 updated_at 跳过：未变化则跳过整个子树
        if prev_folder and folder_mtime > 0 and folder_mtime == prev_folder.remote_mtime:
            self.stats["skipped_folders"] += 1
            self._folders_skipped += 1
            logger.debug("文件夹未变化，跳过: %s", rel_prefix or "/")
            return

        local_path.mkdir(parents=True, exist_ok=True)

        self._folders_scanned += 1
        if self._folders_scanned % 10 == 0:
            self._write_progress(
                "scanning",
                0,
                f"已扫描 {self._folders_scanned} 个文件夹, "
                f"跳过 {self._folders_skipped} 个 — {rel_prefix or '/'}",
            )

        remote_items = self.api.list_files(folder_id)
        remote_map: dict[str, FileInfo] = {}
        for f in remote_items:
            remote_map[normalize_name(f.name)] = f

        local_entries: dict[str, Path] = {}
        if local_path.exists():
            for entry in local_path.iterdir():
                if not self._should_ignore(entry.name):
                    local_entries[normalize_name(entry.name)] = entry

        all_names = set(remote_map.keys()) | set(local_entries.keys())

        # 按远程更新时间降序排（最近更新的优先）
        def sort_key(name: str) -> int:
            r = remote_map.get(name)
            return -(r.mtime if r else 0)

        for name in sorted(all_names, key=sort_key):
            if self._should_ignore(name):
                continue

            local_name = safe_name(name)
            rel_path = f"{rel_prefix}/{local_name}" if rel_prefix else local_name
            remote = remote_map.get(name)
            local = local_entries.get(name) or local_entries.get(normalize_name(local_name))

            # 递归子文件夹
            if remote and remote.is_folder:
                sub_local = local_path / local_name
                try:
                    self._scan_folder(remote.id, sub_local, rel_path, actions, remote.mtime)
                except Exception:
                    logger.exception("扫描子文件夹失败: %s", rel_path)
                    self.stats["errors"] += 1
                continue

            if local and local.is_dir() and not remote:
                new_id = self._create_remote_folder(folder_id, name)
                if new_id:
                    try:
                        self._scan_folder(new_id, local, rel_path, actions)
                    except Exception:
                        logger.exception("扫描新建远程文件夹失败: %s", rel_path)
                        self.stats["errors"] += 1
                continue

            # 生成文件同步动作
            action = self._decide_action(local_name, rel_path, folder_id, local_path, remote, local)
            if action:
                actions.append(action)

        # 记录文件夹状态
        effective_mtime = folder_mtime
        if not effective_mtime and remote_items:
            effective_mtime = max(f.mtime for f in remote_items)
        self.state.folders[folder_key] = FolderRecord(
            remote_id=folder_id,
            remote_mtime=effective_mtime,
            last_sync=_now_iso(),
        )

    def _decide_action(
        self,
        name: str,
        rel_path: str,
        folder_id: str,
        local_path: Path,
        remote: FileInfo | None,
        local: Path | None,
    ) -> SyncAction | None:
        """对单个文件决定同步动作"""
        prev = self.state.files.get(rel_path)

        # Case 1: 远程有，本地无
        if remote and not local:
            if prev:
                if self.sync_delete:
                    return SyncAction("delete_remote", rel_path, remote=remote,
                                      remote_mtime=remote.mtime)
                return None
            return SyncAction("download", rel_path, remote=remote,
                              local_path=local_path / name, folder_id=folder_id,
                              remote_mtime=remote.mtime)

        # Case 2: 本地有，远程无
        if local and not remote:
            if prev:
                if self.sync_delete:
                    return SyncAction("delete_local", rel_path, local_path=local)
                return None
            return SyncAction("upload", rel_path, local_path=local,
                              folder_id=folder_id)

        # Case 3: 双方都有
        if remote and local and local.is_file():
            local_ts = local.stat().st_mtime
            local_size = local.stat().st_size

            remote_changed = prev and (
                remote.mtime != prev.remote_mtime or remote.size != prev.remote_size
            )

            # 本地变化检测：时间变了 + 大小也变了 → 确定变了
            # 时间变了但大小没变 → 算 hash 确认内容是否真的变了
            local_changed = False
            if prev:
                mtime_diff = abs(local_ts - prev.local_mtime) > 1
                size_diff = local_size != prev.local_size
                if size_diff:
                    local_changed = True
                elif mtime_diff and not size_diff:
                    # 时间变了但大小没变 → hash 校验
                    current_hash = file_md5(local)
                    if prev.local_hash and current_hash == prev.local_hash:
                        local_changed = False  # 内容没变（只是 touch 了）
                        logger.debug("mtime 变化但 hash 一致，跳过: %s", rel_path)
                    else:
                        local_changed = True

            if not prev:
                # 首次同步：大小一致视为已同步
                if remote.size == local_size:
                    self._record_state(rel_path, remote, local)
                    return None
                # 大小不同，以远程为准
                return SyncAction("download", rel_path, remote=remote,
                                  local_path=local, folder_id=folder_id,
                                  remote_mtime=remote.mtime)
            elif remote_changed and local_changed:
                return SyncAction("conflict", rel_path, remote=remote,
                                  local_path=local, folder_id=folder_id,
                                  remote_mtime=remote.mtime)
            elif remote_changed:
                return SyncAction("download", rel_path, remote=remote,
                                  local_path=local, folder_id=folder_id,
                                  remote_mtime=remote.mtime)
            elif local_changed:
                # 传入 remote 信息，上传时先删旧版（Worktile 不覆盖同名文件）
                return SyncAction("upload", rel_path, remote=remote, local_path=local,
                                  folder_id=folder_id)

        return None

    # ── Phase 2: Plan ──────────────────────────────────────────────

    def _plan(self, actions: list[SyncAction]) -> None:
        """分析、排序、报告同步计划"""
        downloads = [a for a in actions if a.type == "download"]
        uploads = [a for a in actions if a.type == "upload"]
        del_local = [a for a in actions if a.type == "delete_local"]
        del_remote = [a for a in actions if a.type == "delete_remote"]
        conflicts = [a for a in actions if a.type == "conflict"]

        dl_size = sum(a.remote.size for a in downloads if a.remote)
        ul_size = sum(
            a.local_path.stat().st_size for a in uploads
            if a.local_path and a.local_path.exists()
        )

        logger.info(
            "同步计划 — 下载:%d (%s) 上传:%d (%s) 删除(本地):%d 删除(远程):%d 冲突:%d 跳过文件夹:%d",
            len(downloads), human_size(dl_size),
            len(uploads), human_size(ul_size),
            len(del_local), len(del_remote), len(conflicts),
            self.stats["skipped_folders"],
        )

        # 按更新时间排序（最近更新的优先）
        actions.sort(key=lambda a: -a.remote_mtime)

        # 重命名检测（通过 remote_id 匹配）
        self._handle_renames(actions)

    def _handle_renames(self, actions: list[SyncAction]) -> None:
        """通过 remote_id 检测重命名，用本地 rename 替代 download+delete

        Worktile 上重命名文件 → _id 不变，title 变
        → 旧路径消失(download new)，state 中旧路径的 remote_id 与新路径匹配
        → 直接本地 rename，避免重新下载
        """
        # remote_id → 旧路径 映射（从 state 中获取）
        id_to_old_path: dict[str, str] = {}
        for path, rec in self.state.files.items():
            if rec.remote_id:
                id_to_old_path[rec.remote_id] = path

        # 找出 download 动作中 remote_id 已在 state 中的（说明是重命名）
        to_remove: list[SyncAction] = []
        for action in actions:
            if action.type != "download" or not action.remote:
                continue
            old_path = id_to_old_path.get(action.remote.id)
            if not old_path or old_path == action.rel_path:
                continue

            # 找到了！remote_id 一样但路径不同 → Worktile 端重命名
            old_local = self.local_dir / old_path
            new_local = action.local_path

            if old_local.exists():
                logger.info("检测到远程重命名: %s → %s (跳过重新下载)", old_path, action.rel_path)
                try:
                    new_local.parent.mkdir(parents=True, exist_ok=True)
                    old_local.rename(new_local)
                    # 更新 state
                    self.state.files.pop(old_path, None)
                    self._record_state(action.rel_path, action.remote, new_local)
                    to_remove.append(action)
                    # 同时移除旧路径的 delete/upload 动作
                    for other in actions:
                        if other.rel_path == old_path:
                            to_remove.append(other)
                except Exception:
                    logger.exception("本地重命名失败: %s → %s", old_path, action.rel_path)

        for a in to_remove:
            if a in actions:
                actions.remove(a)

    # ── Phase 3: Execute ───────────────────────────────────────────

    def _execute(self, actions: list[SyncAction]) -> None:
        """执行所有同步动作"""
        if not actions:
            return

        total = len(actions)
        downloads = [a for a in actions if a.type == "download"]
        others = [a for a in actions if a.type != "download"]

        completed = 0

        # 先执行非下载操作（上传、删除、冲突 — 通常较快）
        for action in others:
            if self.dry_run:
                logger.info("[DRY-RUN] %s: %s", action.type, action.rel_path)
            else:
                self._exec_one(action)
            completed += 1
            if completed % 20 == 0:
                logger.info("进度: %d/%d (%.0f%%)", completed, total, completed / total * 100)
            self._write_progress("uploading", completed, action.rel_path)

        # 执行下载（可并发）
        if downloads:
            if self.dry_run:
                for a in downloads:
                    logger.info("[DRY-RUN] download: %s", a.rel_path)
                    completed += 1
            elif self.max_workers > 1 and len(downloads) > 1:
                logger.info("开始并发下载 %d 个文件 (workers=%d)",
                            len(downloads), self.max_workers)
                with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                    futures = {executor.submit(self._exec_download, a): a for a in downloads}
                    for future in as_completed(futures):
                        action = futures[future]
                        completed += 1
                        if completed % 20 == 0:
                            logger.info("进度: %d/%d (%.0f%%)",
                                        completed, total, completed / total * 100)
                        self._write_progress("downloading", completed, action.rel_path)
                        self._maybe_save_state()
            else:
                for action in downloads:
                    self._exec_download(action)
                    completed += 1
                    if completed % 20 == 0:
                        logger.info("进度: %d/%d (%.0f%%)",
                                    completed, total, completed / total * 100)
                    self._write_progress("downloading", completed, action.rel_path)
                    self._maybe_save_state()

    def _maybe_save_state(self) -> None:
        """增量保存状态（每 50 个下载保存一次）"""
        with self._lock:
            if self.stats["downloaded"] > 0 and self.stats["downloaded"] % 50 == 0:
                self.state.save(self.state_path)
                logger.debug("增量保存状态: %d 个文件", self.stats["downloaded"])

    def _write_progress(self, phase: str, completed: int, current: str = "") -> None:
        """实时写入同步进度文件（保留每个阶段最近 20 条历史）"""
        import json, time as _time
        elapsed = _time.monotonic() - self._sync_start
        total = self._total_actions
        pct = (completed / total * 100) if total > 0 else 0

        snapshot = {
            "phase": phase,
            "current_file": current,
            "completed": completed,
            "total": total,
            "percent": round(pct, 1),
            "downloaded": self.stats["downloaded"],
            "uploaded": self.stats["uploaded"],
            "errors": self.stats["errors"],
            "elapsed_sec": round(elapsed, 1),
            "updated_at": _time.strftime("%Y-%m-%d %H:%M:%S"),
        }

        try:
            # 读取现有进度文件
            existing = {}
            if self._progress_file.exists():
                try:
                    existing = json.loads(self._progress_file.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    pass

            history = existing.get("history", {})
            phase_history = history.get(phase, [])
            phase_history.insert(0, snapshot)  # 最新在前
            phase_history = phase_history[:20]  # 保留 20 条
            history[phase] = phase_history

            data = {
                "status": "syncing",
                "current": snapshot,
                "history": history,
            }

            tmp = self._progress_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.rename(self._progress_file)
        except Exception:
            pass

    def _exec_one(self, action: SyncAction) -> None:
        if action.type == "upload":
            self._exec_upload(action)
        elif action.type == "delete_local":
            self._exec_delete_local(action)
        elif action.type == "delete_remote":
            self._exec_delete_remote(action)
        elif action.type == "conflict":
            self._exec_conflict(action)

    def _exec_download(self, action: SyncAction) -> None:
        try:
            self.api.download_file(action.remote, action.local_path)
            with self._lock:
                self._record_state(action.rel_path, action.remote, action.local_path)
                self.stats["downloaded"] += 1
                self._recent_changes.append({
                    "action": "download", "direction": "worktile → 本地",
                    "file": action.rel_path, "size": action.remote.size,
                })
        except Exception:
            logger.exception("下载失败: %s", action.rel_path)
            with self._lock:
                self.stats["errors"] += 1

    def _exec_upload(self, action: SyncAction) -> None:
        try:
            # Worktile 上传不覆盖同名文件 → 先删旧版再传新版
            if action.remote and action.remote.id:
                try:
                    self.api.delete_file(action.remote.id)
                except Exception:
                    logger.warning("删除旧版本失败（继续上传）: %s", action.rel_path)

            file_size = action.local_path.stat().st_size
            result = self.api.upload_file(action.folder_id, action.local_path)
            data = result.get("data", result)
            remote_info = FileInfo(
                id=data.get("_id", ""),
                name=action.local_path.name,
                is_folder=False,
                size=file_size,
                mtime=data.get("updated_at", int(datetime.now().timestamp())),
                parent_id=action.folder_id,
                cos_key=data.get("addition", {}).get("path", ""),
                version=data.get("addition", {}).get("current_version", 1),
            )
            self._record_state(action.rel_path, remote_info, action.local_path)
            self.stats["uploaded"] += 1
            self._recent_changes.append({
                "action": "upload", "direction": "本地 → worktile",
                "file": action.rel_path, "size": file_size,
            })
        except Exception:
            logger.exception("上传失败: %s", action.rel_path)
            self.stats["errors"] += 1

    def _exec_delete_local(self, action: SyncAction) -> None:
        try:
            if action.local_path.is_dir():
                shutil.rmtree(action.local_path)
            else:
                action.local_path.unlink()
            self.state.files.pop(action.rel_path, None)
            self.stats["deleted_local"] += 1
            self._recent_changes.append({
                "action": "delete_local", "direction": "删除本地",
                "file": action.rel_path, "size": 0,
            })
            logger.info("已删除本地: %s", action.rel_path)
        except Exception:
            logger.exception("删除本地失败: %s", action.rel_path)
            self.stats["errors"] += 1

    def _exec_delete_remote(self, action: SyncAction) -> None:
        try:
            self.api.delete_file(action.remote.id)
            self.state.files.pop(action.rel_path, None)
            self.stats["deleted_remote"] += 1
            self._recent_changes.append({
                "action": "delete_remote", "direction": "删除远程",
                "file": action.rel_path, "size": 0,
            })
        except Exception:
            logger.exception("删除远程失败: %s", action.rel_path)
            self.stats["errors"] += 1

    def _exec_conflict(self, action: SyncAction) -> None:
        self.stats["conflicts"] += 1
        remote_ts = float(action.remote.mtime)
        local_ts = action.local_path.stat().st_mtime

        stem = action.local_path.stem
        suffix = action.local_path.suffix
        conflict_path = action.local_path.parent / f"{stem}.conflict{suffix}"

        if remote_ts >= local_ts:
            logger.info("冲突(远程更新): %s → 备份 %s", action.rel_path, conflict_path.name)
            shutil.copy2(action.local_path, conflict_path)
            self._exec_download(action)
        else:
            logger.info("冲突(本地更新): %s → 上传", action.rel_path)
            self._exec_upload(action)

    # ── Orchestrator ───────────────────────────────────────────────

    def sync_once(self) -> dict[str, int]:
        """执行一次完整的三阶段同步"""
        self.stats = {k: 0 for k in self.stats}
        actions: list[SyncAction] = []

        import time as _time
        self._sync_start = _time.monotonic()
        self._recent_changes = []
        self._folders_scanned = 0
        self._folders_skipped = 0

        # Phase 1: Scan
        logger.info("开始扫描...")
        self._write_progress("scanning", 0, "遍历文件夹...")
        try:
            if self.root_folder_id:
                self._scan_folder(self.root_folder_id, self.local_dir, "", actions)
            else:
                root_folders = self.api.list_root_folders()
                for folder in root_folders:
                    if self._should_ignore(folder.name):
                        continue
                    sub_local = self.local_dir / folder.name
                    try:
                        self._scan_folder(folder.id, sub_local, folder.name, actions, folder.mtime)
                    except Exception:
                        logger.exception("扫描根文件夹失败: %s", folder.name)
                        self.stats["errors"] += 1
        except Exception:
            logger.exception("扫描阶段发生错误")
            self.stats["errors"] += 1

        # Phase 2: Plan
        self._plan(actions)

        if not actions:
            logger.info("无需同步")
            self._write_progress("idle", 0, "无需同步")
        else:
            self._total_actions = len(actions)
            # Phase 3: Execute
            self._execute(actions)

        # 始终保存状态
        self.state.save(self.state_path)

        logger.info(
            "同步完成 — 下载:%d 上传:%d 删除(本地):%d 删除(远程):%d 冲突:%d 错误:%d",
            self.stats["downloaded"], self.stats["uploaded"],
            self.stats["deleted_local"], self.stats["deleted_remote"],
            self.stats["conflicts"], self.stats["errors"],
        )

        result = dict(self.stats)
        result["recent_changes"] = self._recent_changes[-50:]  # 最近 50 条
        return result

    # ── Helpers ────────────────────────────────────────────────────

    def _create_remote_folder(self, parent_id: str, name: str) -> str:
        if self.dry_run:
            logger.info("[DRY-RUN] 创建远程文件夹: %s", name)
            return ""
        try:
            return self.api.create_folder(parent_id, name)
        except Exception:
            logger.exception("创建远程文件夹失败: %s", name)
            self.stats["errors"] += 1
            return ""

    def _record_state(self, rel_path: str, remote: FileInfo, local_path: Path) -> None:
        stat = local_path.stat() if local_path.exists() else None
        # 小文件（<50MB）存 hash，大文件跳过（算 hash 太慢）
        local_hash = ""
        if stat and stat.st_size < 50 * 1024 * 1024:
            try:
                local_hash = file_md5(local_path)
            except Exception:
                pass
        self.state.files[rel_path] = FileRecord(
            name=remote.name,
            remote_id=remote.id,
            remote_mtime=remote.mtime,
            remote_size=remote.size,
            local_mtime=stat.st_mtime if stat else 0.0,
            local_size=stat.st_size if stat else 0,
            last_sync=_now_iso(),
            local_hash=local_hash,
        )
