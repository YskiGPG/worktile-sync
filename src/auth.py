"""认证管理：Cookie 注入、Token 过期检测"""

import logging
from typing import Any

logger = logging.getLogger(__name__)


class AuthManager:
    """管理 Worktile API 认证

    主域名 (zrhubei.worktile.com): 使用标准 Cookie header
    上传域名 (wt-box.worktile.com): 跨域，使用 x-cookies header
    """

    def __init__(self, auth_config: dict[str, Any]) -> None:
        self.cookie_value: str = auth_config.get("cookie", "")

        if not self.cookie_value:
            logger.warning("认证 Cookie 为空，请在 config.yaml 中配置")

    def get_headers(self) -> dict[str, str]:
        """主域名请求的 Header（标准 Cookie）"""
        return {"Cookie": self.cookie_value}

    def get_box_headers(self) -> dict[str, str]:
        """wt-box 跨域请求的 Header（x-cookies）"""
        return {"x-cookies": self.cookie_value}

    def is_auth_error(self, status_code: int) -> bool:
        """判断响应是否为认证失败"""
        return status_code in (401, 403)

    def handle_auth_failure(self) -> None:
        """处理认证失败：记录告警"""
        logger.error(
            "认证失败！Cookie 可能已过期。"
            "请重新从浏览器获取 Cookie 并更新 config.yaml 中的 auth.cookie"
        )
