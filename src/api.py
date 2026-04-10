"""Worktile 网盘 API 封装（逆向接口）"""

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from .auth import AuthManager
from .utils import RateLimiter

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
RETRY_BACKOFF = 2


@dataclass
class FileInfo:
    """远程文件/文件夹信息"""
    id: str
    name: str
    is_folder: bool
    size: int
    mtime: int        # updated_at（Unix 秒）
    parent_id: str
    cos_key: str
    version: int


class WorktileAPIError(Exception):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class WorktileAPI:

    def __init__(
        self,
        base_url: str,
        box_url: str,
        team_id: str,
        auth: AuthManager,
        timeout: float = 30.0,
        rate_limit: float = 5.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.box_url = box_url.rstrip("/")
        self.team_id = team_id
        self.auth = auth
        self._rate_limiter = RateLimiter(rate_limit)

        self.client = httpx.Client(
            base_url=self.base_url,
            timeout=httpx.Timeout(timeout, read=300.0),
        )
        self.box_client = httpx.Client(
            base_url=self.box_url,
            timeout=httpx.Timeout(timeout, read=600.0),
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        )

    def _ts(self) -> str:
        return str(int(time.time() * 1000))

    def _request(
        self,
        method: str,
        path: str,
        *,
        use_box: bool = False,
        **kwargs: Any,
    ) -> httpx.Response:
        self._rate_limiter.wait()

        if use_box:
            headers = self.auth.get_box_headers()
            client = self.box_client
        else:
            headers = self.auth.get_headers()
            client = self.client

        headers.update(kwargs.pop("headers", {}))

        last_exc: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = client.request(method, path, headers=headers, **kwargs)

                if self.auth.is_auth_error(resp.status_code):
                    self.auth.handle_auth_failure()
                    raise WorktileAPIError("认证失败", resp.status_code)

                resp.raise_for_status()
                return resp

            except (httpx.HTTPStatusError, httpx.TransportError) as e:
                last_exc = e
                if attempt < MAX_RETRIES:
                    wait = RETRY_BACKOFF ** attempt
                    logger.warning(
                        "请求 %s %s 失败 (第%d次)，%d秒后重试: %s",
                        method, path, attempt, wait, e,
                    )
                    time.sleep(wait)
                else:
                    logger.error("请求 %s %s 失败，已达最大重试次数", method, path)

        raise WorktileAPIError(f"请求失败: {last_exc}") from last_exc

    @staticmethod
    def _parse_item(item: dict[str, Any]) -> FileInfo:
        item_type = item.get("type", 0)
        is_folder = item_type == 1
        addition = item.get("addition") or {}
        return FileInfo(
            id=item["_id"],
            name=item["title"],
            is_folder=is_folder,
            size=addition.get("size", 0) if not is_folder else 0,
            mtime=item.get("updated_at", 0),
            parent_id=item.get("parent") or "",
            cos_key=addition.get("path", "") if not is_folder else "",
            version=addition.get("current_version", 1) if not is_folder else 0,
        )

    def list_root_folders(self) -> list[FileInfo]:
        resp = self._request("GET", "/api/drives/folders", params={
            "parent_id": "",
            "belong": 2,
            "sort_by": "updated_at",
            "sort_type": -1,
            "t": self._ts(),
        })
        data = resp.json()
        items = data.get("data", [])
        folders = [self._parse_item(item) for item in items]
        logger.info("获取根目录文件夹: %d 个", len(folders))
        return folders

    def list_files(self, folder_id: str) -> list[FileInfo]:
        all_items: list[FileInfo] = []
        page = 0

        while True:
            resp = self._request("GET", "/api/drives/list", params={
                "keywords": "",
                "parent_id": folder_id,
                "belong": 2,
                "sort_by": "updated_at",
                "sort_type": -1,
                "pi": page,
                "ps": 200,
                "t": self._ts(),
            })
            data = resp.json()
            page_data = data.get("data", {})
            items = page_data.get("value", [])

            if not items:
                break

            for item in items:
                all_items.append(self._parse_item(item))

            page_count = page_data.get("page_count", 1)
            if page + 1 >= page_count:
                break
            page += 1

        logger.info("获取文件夹 %s 列表: %d 个项目", folder_id, len(all_items))
        return all_items

    def download_file(self, file_info: FileInfo, save_path: Path) -> None:
        """下载文件（临时文件 + 原子 rename + 大小校验）"""
        version = file_info.version or 1
        path = f"/drives/{file_info.id}"
        params = {
            "team_id": self.team_id,
            "version": version,
            "action": "download",
        }

        save_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = save_path.with_suffix(save_path.suffix + ".downloading")

        headers = self.auth.get_box_headers()
        last_exc: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                with self.box_client.stream(
                    "GET", path, params=params, headers=headers,
                    follow_redirects=True,
                ) as resp:
                    resp.raise_for_status()
                    with open(tmp_path, "wb") as f:
                        for chunk in resp.iter_bytes(chunk_size=65536):
                            f.write(chunk)

                # 大小校验
                actual_size = tmp_path.stat().st_size
                if file_info.size > 0 and actual_size != file_info.size:
                    tmp_path.unlink(missing_ok=True)
                    raise WorktileAPIError(
                        f"下载不完整: {file_info.name} "
                        f"(期望 {file_info.size}, 实际 {actual_size})"
                    )

                # 原子 rename
                tmp_path.rename(save_path)
                logger.info("已下载: %s (%d bytes)", save_path.name, actual_size)
                return

            except (httpx.HTTPStatusError, httpx.TransportError, WorktileAPIError) as e:
                last_exc = e
                tmp_path.unlink(missing_ok=True)
                if attempt < MAX_RETRIES:
                    wait = RETRY_BACKOFF ** attempt
                    logger.warning(
                        "下载 %s 失败 (第%d次)，%d秒后重试: %s",
                        file_info.name, attempt, wait, e,
                    )
                    time.sleep(wait)

        raise WorktileAPIError(f"下载失败: {last_exc}") from last_exc

    def upload_file(self, folder_id: str, file_path: Path) -> dict[str, Any]:
        """上传新文件到指定文件夹"""
        with open(file_path, "rb") as f:
            resp = self._request(
                "POST",
                "/drive/upload",
                use_box=True,
                params={
                    "belong": 2,
                    "parent_id": folder_id,
                    "team_id": self.team_id,
                },
                data={"title": file_path.name},
                files={"file": (file_path.name, f)},
            )

        result = resp.json()
        logger.info("已上传: %s", file_path.name)
        return result

    def update_file(self, file_id: str, file_path: Path) -> dict[str, Any]:
        """上传新版本（保留文件 ID 和版本历史）

        POST https://wt-box.worktile.com/drive/update?team_id={team_id}&id={file_id}
        """
        with open(file_path, "rb") as f:
            resp = self._request(
                "POST",
                "/drive/update",
                use_box=True,
                params={
                    "team_id": self.team_id,
                    "id": file_id,
                },
                data={"title": file_path.name},
                files={"file": (file_path.name, f)},
            )

        result = resp.json()
        version = result.get("data", {}).get("addition", {}).get("current_version", "?")
        logger.info("已更新版本: %s (v%s)", file_path.name, version)
        return result

    def delete_file(self, file_id: str) -> None:
        self._request("DELETE", f"/api/drives/{file_id}")
        logger.info("已删除远程文件: %s", file_id)

    def create_folder(self, parent_id: str, name: str) -> str:
        resp = self._request("POST", "/api/drive/folder", json={
            "parent_id": parent_id,
            "title": name,
            "belong": 2,
            "visibility": 1,
            "permission": 4,
            "members": [],
            "color": "#6698FF",
        })
        result = resp.json()
        folder_id = result.get("data", {}).get("_id", "")
        logger.info("已创建文件夹: %s (ID: %s)", name, folder_id)
        return folder_id

    def close(self) -> None:
        self.client.close()
        self.box_client.close()
