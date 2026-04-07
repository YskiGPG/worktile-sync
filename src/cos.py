"""腾讯云 COS 预签名 URL 生成

参考文档: https://cloud.tencent.com/document/product/436/7778
"""

import hashlib
import hmac
import logging
import time
from urllib.parse import quote

logger = logging.getLogger(__name__)

# Worktile 使用的 COS 桶
DEFAULT_BUCKET_HOST = "app-wt-original-release-1348738073.cos.ap-shanghai.myqcloud.com"
DEFAULT_EXPIRE_SECONDS = 3600  # 签名有效期 1 小时


class COSSigner:
    """腾讯云 COS 请求签名器"""

    def __init__(
        self,
        secret_id: str,
        secret_key: str,
        bucket_host: str = DEFAULT_BUCKET_HOST,
        expire_seconds: int = DEFAULT_EXPIRE_SECONDS,
    ) -> None:
        self.secret_id = secret_id
        self.secret_key = secret_key
        self.bucket_host = bucket_host
        self.expire_seconds = expire_seconds

    def generate_download_url(self, cos_key: str, filename: str) -> str:
        """生成 COS 文件下载的预签名 URL

        Args:
            cos_key: 文件在 COS 上的 key（即 addition.path）
            filename: 下载时的文件名（用于 Content-Disposition）

        Returns:
            带签名的完整下载 URL
        """
        now = int(time.time())
        expire = now + self.expire_seconds
        key_time = f"{now};{expire}"

        # Step 1: 生成 SignKey = HMAC-SHA1(SecretKey, KeyTime)
        sign_key = hmac.new(
            self.secret_key.encode("utf-8"),
            key_time.encode("utf-8"),
            hashlib.sha1,
        ).hexdigest()

        # Step 2: 构造 HttpString 和 StringToSign
        # response-content-disposition 参数需要在签名中包含
        encoded_filename = quote(filename, safe="")
        disposition = f"attachment; filename*=utf-8''{encoded_filename}"
        encoded_disposition = quote(disposition, safe="")

        # HttpString = Method\nUriPathname\nHttpParameters\nHttpHeaders\n
        http_parameters = f"response-content-disposition={encoded_disposition}"
        http_headers = f"host={self.bucket_host}"
        http_string = f"get\n/{cos_key}\n{http_parameters}\n{http_headers}\n"

        # SHA1Hash(HttpString)
        sha1_http_string = hashlib.sha1(http_string.encode("utf-8")).hexdigest()

        # StringToSign = Algorithm\nSignTime\nSHA1Hash(HttpString)\n
        string_to_sign = f"sha1\n{key_time}\n{sha1_http_string}\n"

        # Step 3: 生成 Signature = HMAC-SHA1(SignKey, StringToSign)
        signature = hmac.new(
            sign_key.encode("utf-8"),
            string_to_sign.encode("utf-8"),
            hashlib.sha1,
        ).hexdigest()

        # Step 4: 拼接完整 URL
        url = (
            f"https://{self.bucket_host}/{cos_key}"
            f"?q-sign-algorithm=sha1"
            f"&q-ak={self.secret_id}"
            f"&q-sign-time={key_time}"
            f"&q-key-time={key_time}"
            f"&q-header-list=host"
            f"&q-url-param-list=response-content-disposition"
            f"&q-signature={signature}"
            f"&response-content-disposition={encoded_disposition}"
        )

        logger.debug("生成 COS 签名 URL: cos_key=%s, 有效期至 %d", cos_key, expire)
        return url
