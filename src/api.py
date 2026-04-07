"""Worktile 网盘 API 封装（逆向接口）"""

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from .auth import AuthManager

logger = logging.getLogger(__name__)

# 重试配置
MAX_RETRIES = 3
RETRY_BACKOFF = 2  # 指数退避基数（秒）


@dataclass
class FileInfo:
    """远程文件/文件夹信息"""
    id: str           # Worktile _id
    name: str         # title
    is_folder: bool   # type==1 为文件夹, type==3 为文件
    size: int         # addition.size，文件夹为 0
    mtime: int        # updated_at（Unix 时间戳，秒）
    parent_id: str    # parent
    cos_key: str      # addition.path，文件夹为空
    version: int      # addition.current_version


class WorktileAPIError(Exception):
    """API 调用异常"""
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class WorktileAPI:
    """Worktile 网盘 API 客户端"""

    def __init__(
        self,
        base_url: str,
        box_url: str,
        team_id: str,
        auth: AuthManager,
        timeout: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.box_url = box_url.rstrip("/")
        self.team_id = team_id
        self.auth = auth
        self.client = httpx.Client(
            base_url=self.base_url,
            timeout=httpx.Timeout(timeout, read=300.0),
        )
        # 上传/下载用单独的 client（跨域，用 x-cookies）
        self.box_client = httpx.Client(
            base_url=self.box_url,
            timeout=httpx.Timeout(timeout, read=600.0),
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        )

    def _ts(self) -> str:
        """当前毫秒时间戳（防缓存参数）"""
        return str(int(time.time() * 1000))

    def _request(
        self,
        method: str,
        path: str,
        *,
        use_box: bool = False,
        **kwargs: Any,
    ) -> httpx.Response:
        """带重试和认证的通用请求方法"""
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
        """将 API 返回的 item 解析为 FileInfo"""
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
        """获取团队网盘根目录文件夹列表

        GET /api/drives/folders?parent_id=&belong=2&sort_by=updated_at&sort_type=-1&t=...
        """
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
        """获取指定文件夹下的文件和子文件夹列表（处理分页）

        GET /api/drives/list?parent_id={folder_id}&belong=2&pi=0&ps=200&...
        返回文件和子文件夹混合，通过 type 区分
        """
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

            # 检查是否有更多页
            page_count = page_data.get("page_count", 1)
            if page + 1 >= page_count:
                break
            page += 1

        logger.info("获取文件夹 %s 列表: %d 个项目", folder_id, len(all_items))
        return all_items

    def download_file(self, file_info: FileInfo, save_path: Path) -> None:
        """下载文件到指定路径

        通过 wt-box 服务端下载（服务端自动生成 COS 签名）:
        GET https://wt-box.worktile.com/drives/{file_id}?team_id={team_id}&version={version}&action=download
        """
        version = file_info.version or 1
        path = f"/drives/{file_info.id}"
        params = {
            "team_id": self.team_id,
            "version": version,
            "action": "download",
        }

        save_path.parent.mkdir(parents=True, exist_ok=True)

        headers = self.auth.get_box_headers()
        last_exc: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                with self.box_client.stream(
                    "GET", path, params=params, headers=headers,
                    follow_redirects=True,
                ) as resp:
                    resp.raise_for_status()
                    with open(save_path, "wb") as f:
                        for chunk in resp.iter_bytes(chunk_size=65536):
                            f.write(chunk)

                logger.info(
                    "已下载: %s (%d bytes)", save_path.name, save_path.stat().st_size
                )
                return

            except (httpx.HTTPStatusError, httpx.TransportError) as e:
                last_exc = e
                if attempt < MAX_RETRIES:
                    wait = RETRY_BACKOFF ** attempt
                    logger.warning(
                        "下载 %s 失败 (第%d次)，%d秒后重试: %s",
                        file_info.name, attempt, wait, e,
                    )
                    time.sleep(wait)

        raise WorktileAPIError(f"下载失败: {last_exc}") from last_exc

    def upload_file(self, folder_id: str, file_path: Path) -> dict[str, Any]:
        """上传本地文件到指定文件夹

        POST https://wt-box.worktile.com/drive/upload?belong=2&parent_id=...&team_id=...
        跨域请求，使用 x-cookies Header
        """
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

    def delete_file(self, file_id: str) -> None:
        """删除远程文件（软删除，移到回收站）

        DELETE /api/drives/{file_id}
        """
        self._request("DELETE", f"/api/drives/{file_id}")
        logger.info("已删除远程文件: %s", file_id)

    def create_folder(self, parent_id: str, name: str) -> str:
        """创建文件夹，返回新文件夹 ID

        POST /api/drive/folder（注意 drive 和 folder 都是单数）
        """
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
        """关闭 HTTP 客户端"""
        self.client.close()
        self.box_client.close()
