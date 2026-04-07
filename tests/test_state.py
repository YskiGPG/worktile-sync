"""测试同步状态持久化"""

from pathlib import Path

from src.state import SyncState, FileRecord


def test_save_and_load(tmp_path: Path) -> None:
    state = SyncState()
    state.files["docs/readme.txt"] = FileRecord(
        name="readme.txt",
        remote_id="abc123",
        remote_mtime="2026-04-01T10:00:00+00:00",
        remote_size=1024,
        local_mtime=1711958400.0,
        local_size=1024,
        last_sync="2026-04-05T00:00:00+00:00",
    )

    state_file = tmp_path / "state.json"
    state.save(state_file)

    loaded = SyncState.load(state_file)
    assert "docs/readme.txt" in loaded.files
    rec = loaded.files["docs/readme.txt"]
    assert rec.remote_id == "abc123"
    assert rec.remote_size == 1024


def test_load_missing_file(tmp_path: Path) -> None:
    state = SyncState.load(tmp_path / "nonexistent.json")
    assert state.files == {}


def test_load_corrupt_file(tmp_path: Path) -> None:
    bad_file = tmp_path / "bad.json"
    bad_file.write_text("not json at all")
    state = SyncState.load(bad_file)
    assert state.files == {}
