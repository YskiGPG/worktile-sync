"""测试 API 封装（使用 mock）"""

from unittest.mock import patch, MagicMock
from pathlib import Path

from src.auth import AuthManager
from src.api import WorktileAPI, FileInfo


def _make_api() -> WorktileAPI:
    auth = AuthManager({"type": "cookie", "header_name": "Cookie", "token_value": "test=1"})
    return WorktileAPI(
        base_url="https://example.worktile.com",
        auth=auth,
        endpoints={
            "file_list": "/api/drive/files",
            "file_download": "/api/drive/files/{file_id}/download",
            "file_upload": "/api/drive/files/upload",
            "file_delete": "/api/drive/files/{file_id}",
            "folder_create": "/api/drive/folders",
        },
    )


def test_list_files_parses_response() -> None:
    api = _make_api()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "data": [
            {"id": "f1", "name": "test.txt", "is_folder": False, "size": 100, "updated_at": "2026-04-01T00:00:00Z"},
        ],
        "total": 1,
    }
    mock_resp.raise_for_status = MagicMock()

    with patch.object(api.client, "request", return_value=mock_resp):
        files = api.list_files("root")

    assert len(files) == 1
    assert files[0].name == "test.txt"
    assert files[0].size == 100
    api.close()


def test_auth_headers_injected() -> None:
    api = _make_api()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"data": [], "total": 0}
    mock_resp.raise_for_status = MagicMock()

    with patch.object(api.client, "request", return_value=mock_resp) as mock_req:
        api.list_files("root")
        call_kwargs = mock_req.call_args
        headers = call_kwargs.kwargs.get("headers") or call_kwargs[1].get("headers", {})
        assert headers.get("Cookie") == "test=1"

    api.close()
