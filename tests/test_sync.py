"""测试同步逻辑"""

from pathlib import Path
from unittest.mock import MagicMock

from src.api import FileInfo
from src.sync import SyncEngine


def _make_engine(tmp_path: Path, dry_run: bool = False) -> SyncEngine:
    api = MagicMock()
    api.list_files.return_value = []
    local_dir = tmp_path / "local"
    local_dir.mkdir()
    return SyncEngine(
        api=api,
        local_dir=local_dir,
        root_folder_id="root",
        state_path=tmp_path / "state.json",
        dry_run=dry_run,
    )


def test_new_remote_file_downloaded(tmp_path: Path) -> None:
    """远程有新文件 → 应下载到本地"""
    engine = _make_engine(tmp_path)
    engine.api.list_files.return_value = [
        FileInfo(id="f1", name="hello.txt", is_folder=False, size=5, mtime="2026-04-01T00:00:00Z", parent_id="root"),
    ]
    engine.sync_once()
    engine.api.download_file.assert_called_once()


def test_new_local_file_uploaded(tmp_path: Path) -> None:
    """本地有新文件 → 应上传到远程"""
    engine = _make_engine(tmp_path)
    # 创建本地文件
    test_file = engine.local_dir / "new.txt"
    test_file.write_text("content")
    engine.api.upload_file.return_value = {"id": "f2", "updated_at": "2026-04-05T00:00:00Z"}

    engine.sync_once()
    engine.api.upload_file.assert_called_once()


def test_dry_run_no_side_effects(tmp_path: Path) -> None:
    """dry-run 模式不应执行实际操作"""
    engine = _make_engine(tmp_path, dry_run=True)
    engine.api.list_files.return_value = [
        FileInfo(id="f1", name="hello.txt", is_folder=False, size=5, mtime="2026-04-01T00:00:00Z", parent_id="root"),
    ]
    engine.sync_once()
    engine.api.download_file.assert_not_called()
    engine.api.upload_file.assert_not_called()
