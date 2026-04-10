"""探测 Worktile 文件版本上传 API

Worktile 网页版有"上传新版本"功能，本脚本尝试找到对应的 API。
需要先在 Worktile 上创建一个测试文件，获取其 file_id。

用法:
    python tools/probe_version_upload.py <file_id>

例如:
    python tools/probe_version_upload.py 69d5ef280213cb2d248a720c
"""

import sys
import time
import logging
import tempfile
from pathlib import Path

import yaml
import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.auth import AuthManager

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def main() -> None:
    if len(sys.argv) < 2:
        print("用法: python tools/probe_version_upload.py <file_id>")
        print("请在 Worktile 上传一个测试文件，然后从 API 获取其 _id")
        sys.exit(1)

    file_id = sys.argv[1]

    with open("config.yaml", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    wt = config["worktile"]
    auth = AuthManager(wt["auth"])
    team_id = wt.get("team_id", "")
    box_url = wt.get("box_url", "https://wt-box.worktile.com")
    base_url = wt["base_url"]

    # 创建测试文件
    test_content = f"version test {time.time()}\n".encode()
    tmp = tempfile.NamedTemporaryFile(suffix=".txt", delete=False, prefix="version_test_")
    tmp.write(test_content)
    tmp.close()
    test_path = Path(tmp.name)

    logger.info("测试文件: %s (file_id: %s)", test_path.name, file_id)

    headers_cookie = auth.get_headers()
    headers_xcookie = auth.get_box_headers()

    # 方案 1: wt-box upload 加 drive_id 参数
    logger.info("=" * 50)
    logger.info("方案 1: POST wt-box/drive/upload?drive_id={file_id}")
    try:
        with open(test_path, "rb") as f:
            resp = httpx.post(
                f"{box_url}/drive/upload",
                params={"belong": 2, "drive_id": file_id, "team_id": team_id},
                headers=headers_xcookie,
                data={"title": test_path.name},
                files={"file": (test_path.name, f)},
                timeout=30,
            )
        logger.info("状态码: %d", resp.status_code)
        logger.info("响应: %s", resp.text[:500])
    except Exception as e:
        logger.error("失败: %s", e)

    # 方案 2: wt-box upload 加 file_id 参数
    logger.info("=" * 50)
    logger.info("方案 2: POST wt-box/drive/upload?file_id={file_id}")
    try:
        with open(test_path, "rb") as f:
            resp = httpx.post(
                f"{box_url}/drive/upload",
                params={"belong": 2, "file_id": file_id, "team_id": team_id},
                headers=headers_xcookie,
                data={"title": test_path.name},
                files={"file": (test_path.name, f)},
                timeout=30,
            )
        logger.info("状态码: %d", resp.status_code)
        logger.info("响应: %s", resp.text[:500])
    except Exception as e:
        logger.error("失败: %s", e)

    # 方案 3: POST /api/drive/{file_id}/version
    logger.info("=" * 50)
    logger.info("方案 3: POST /api/drive/{file_id}/version (主域名)")
    try:
        with open(test_path, "rb") as f:
            resp = httpx.post(
                f"{base_url}/api/drive/{file_id}/version",
                headers=headers_cookie,
                files={"file": (test_path.name, f)},
                timeout=30,
            )
        logger.info("状态码: %d", resp.status_code)
        logger.info("响应: %s", resp.text[:500])
    except Exception as e:
        logger.error("失败: %s", e)

    # 方案 4: POST /api/drives/{file_id}/version
    logger.info("=" * 50)
    logger.info("方案 4: POST /api/drives/{file_id}/version")
    try:
        with open(test_path, "rb") as f:
            resp = httpx.post(
                f"{base_url}/api/drives/{file_id}/version",
                headers=headers_cookie,
                files={"file": (test_path.name, f)},
                timeout=30,
            )
        logger.info("状态码: %d", resp.status_code)
        logger.info("响应: %s", resp.text[:500])
    except Exception as e:
        logger.error("失败: %s", e)

    # 方案 5: wt-box PUT /drives/{file_id}
    logger.info("=" * 50)
    logger.info("方案 5: PUT wt-box/drives/{file_id}")
    try:
        with open(test_path, "rb") as f:
            resp = httpx.put(
                f"{box_url}/drives/{file_id}",
                params={"team_id": team_id},
                headers=headers_xcookie,
                data={"title": test_path.name},
                files={"file": (test_path.name, f)},
                timeout=30,
            )
        logger.info("状态码: %d", resp.status_code)
        logger.info("响应: %s", resp.text[:500])
    except Exception as e:
        logger.error("失败: %s", e)

    # 方案 6: wt-box upload 加 id 参数（Angular 前端可能用的字段名）
    logger.info("=" * 50)
    logger.info("方案 6: POST wt-box/drive/upload?id={file_id}")
    try:
        with open(test_path, "rb") as f:
            resp = httpx.post(
                f"{box_url}/drive/upload",
                params={"belong": 2, "id": file_id, "team_id": team_id},
                headers=headers_xcookie,
                data={"title": test_path.name},
                files={"file": (test_path.name, f)},
                timeout=30,
            )
        logger.info("状态码: %d", resp.status_code)
        logger.info("响应: %s", resp.text[:500])
    except Exception as e:
        logger.error("失败: %s", e)

    test_path.unlink(missing_ok=True)
    logger.info("=" * 50)
    logger.info("探测完成。请检查上方结果，找到返回 200 且 version > 1 的方案。")


if __name__ == "__main__":
    main()
