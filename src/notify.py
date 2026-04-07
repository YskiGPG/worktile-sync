"""通知模块：邮件和 Webhook 推送"""

import json
import logging
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class Notifier:
    """统一通知管理器，支持邮件和 Webhook"""

    def __init__(self, config: dict[str, Any]) -> None:
        self.enabled = config.get("enabled", False)
        self.email_cfg = config.get("email", {})
        self.webhook_cfg = config.get("webhook", {})
        self.error_threshold = config.get("error_threshold", 3)

    def send(self, title: str, body: str) -> None:
        if not self.enabled:
            return

        if self.email_cfg.get("enabled"):
            self._send_email(title, body)
        if self.webhook_cfg.get("enabled"):
            self._send_webhook(title, body)

    def _send_email(self, title: str, body: str) -> None:
        cfg = self.email_cfg
        try:
            msg = MIMEMultipart()
            msg["From"] = cfg["username"]
            msg["To"] = cfg["to"]
            msg["Subject"] = title
            msg.attach(MIMEText(body, "plain", "utf-8"))

            with smtplib.SMTP(cfg["smtp_host"], cfg.get("smtp_port", 587)) as server:
                server.starttls()
                server.login(cfg["username"], cfg["password"])
                server.send_message(msg)

            logger.info("邮件通知已发送: %s", title)
        except Exception:
            logger.exception("发送邮件通知失败")

    def _send_webhook(self, title: str, body: str) -> None:
        """发送 Webhook 通知

        自动识别服务类型：
        - Server酱: https://sctapi.ftqq.com/KEY.send
        - PushPlus: https://www.pushplus.plus/send
        - 企业微信: https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=...
        - 通用: POST JSON {title, body}
        """
        url = self.webhook_cfg.get("url", "")
        if not url:
            return

        try:
            if "sctapi.ftqq.com" in url or "sc.ftqq.com" in url:
                data = {"title": title, "desp": body}
            elif "pushplus.plus" in url:
                data = {"title": title, "content": body}
            elif "qyapi.weixin.qq.com" in url:
                data = {"msgtype": "text", "text": {"content": f"{title}\n\n{body}"}}
            else:
                data = {"title": title, "body": body}

            with httpx.Client(timeout=10) as client:
                resp = client.post(url, json=data)
                resp.raise_for_status()

            logger.info("Webhook 通知已发送: %s", title)
        except Exception:
            logger.exception("发送 Webhook 通知失败")
