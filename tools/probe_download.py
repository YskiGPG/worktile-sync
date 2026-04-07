#!/usr/bin/env python3
"""
探测 Worktile 文件下载方案

功能:
1. 从 Worktile 页面 HTML 中提取 JS bundle URL
2. 在 JS bundle 中搜索 COS AK/SK
3. 尝试各种可能的下载 API 端点
4. 尝试 COS STS 临时凭证接口

用法:
  python tools/probe_download.py                  # 从 config.yaml 读取配置
  python tools/probe_download.py --cookie "s-xxx=yyy" --base-url "https://xxx.worktile.com"
"""

import argparse
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin

import httpx
import yaml


# 已知的 COS AK
KNOWN_AK = "AKIDjKD0b1EDyZx78B8yfIfsu1yhDXkncDds"
TEAM_ID = "695364022dd1c542de797196"
BOX_URL = "https://wt-box.worktile.com"


def load_config() -> dict:
    """从 config.yaml 加载配置"""
    config_path = Path("config.yaml")
    if not config_path.exists():
        return {}
    with open(config_path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def make_client(cookie: str, timeout: float = 15.0) -> httpx.Client:
    return httpx.Client(
        headers={"Cookie": cookie, "User-Agent": "Mozilla/5.0"},
        timeout=timeout,
        follow_redirects=False,
    )


# ── 阶段1: 从 JS bundle 中搜索 SK ──────────────────────────────


def extract_js_urls(html: str, base_url: str) -> list[str]:
    """从 HTML 中提取 JS bundle URL"""
    # 匹配 <script src="...">
    pattern = r'<script[^>]+src=["\']([^"\']*\.js[^"\']*)["\']'
    urls = re.findall(pattern, html)
    result = []
    for url in urls:
        if url.startswith("http"):
            result.append(url)
        elif url.startswith("//"):
            result.append("https:" + url)
        else:
            result.append(urljoin(base_url + "/", url.lstrip("/")))
    return result


def search_sk_in_js(js_text: str, source_url: str) -> str | None:
    """在 JS 代码中搜索 COS SecretKey"""
    if KNOWN_AK not in js_text:
        return None

    print(f"  [*] 在此文件中找到 AK!")

    # 获取 AK 附近 2000 字符的上下文
    idx = js_text.index(KNOWN_AK)
    start = max(0, idx - 2000)
    end = min(len(js_text), idx + 2000)
    context = js_text[start:end]

    # 搜索 SK 的各种模式
    sk_patterns = [
        # "secretKey": "xxxxx" 或 secretKey: "xxxxx"
        r'["\']?(?:secret[_-]?key|SecretKey|SK|cosSk|secretId|cos_sk|secret)["\']?\s*[:=]\s*["\']([A-Za-z0-9+/=]{20,60})["\']',
        # 紧跟 AK 定义后的另一个字符串常量
        r'AKIDjKD0b1EDyZx78B8yfIfsu1yhDXkncDds["\']?\s*[,;}\)]\s*["\']?(?:secret[_-]?key|SecretKey|SK|cosSk|secret)["\']?\s*[:=]\s*["\']([A-Za-z0-9+/=]{20,60})["\']',
    ]

    for pat in sk_patterns:
        matches = re.findall(pat, context, re.IGNORECASE)
        if matches:
            return matches[0]

    # 如果模式匹配失败，提取 AK 附近所有看起来像密钥的字符串
    print("  [!] 未自动匹配到 SK，以下是 AK 附近的可疑字符串:")
    # 找所有 20-60 位的 base64-like 字符串
    candidates = re.findall(r'["\']([A-Za-z0-9+/=]{20,60})["\']', context)
    candidates = [c for c in candidates if c != KNOWN_AK]
    for i, c in enumerate(candidates):
        print(f"      [{i}] {c}")

    # 同时打印 AK 附近的原始代码片段（方便人工分析）
    ak_idx = context.index(KNOWN_AK)
    snippet_start = max(0, ak_idx - 300)
    snippet_end = min(len(context), ak_idx + len(KNOWN_AK) + 300)
    print(f"\n  [!] AK 附近代码片段:\n")
    print(f"      ...{context[snippet_start:snippet_end]}...")

    return None


def phase1_find_sk(base_url: str, cookie: str) -> str | None:
    """阶段1: 从 JS bundle 中搜索 SK"""
    print("=" * 60)
    print("阶段1: 从 JS bundle 中搜索 COS SecretKey")
    print("=" * 60)

    client = make_client(cookie, timeout=30.0)

    # 1. 获取主页 HTML
    print(f"\n[1] 获取主页 HTML: {base_url}")
    try:
        resp = client.get(base_url, follow_redirects=True)
        if resp.status_code != 200:
            print(f"  获取主页失败: HTTP {resp.status_code}")
            return None
        html = resp.text
        print(f"  HTML 长度: {len(html)} 字符")
    except Exception as e:
        print(f"  获取主页失败: {e}")
        return None

    # 2. 提取 JS URL
    js_urls = extract_js_urls(html, base_url)
    print(f"\n[2] 找到 {len(js_urls)} 个 JS 文件:")
    for url in js_urls:
        name = url.split("/")[-1].split("?")[0]
        print(f"  - {name}")

    # 优先处理 app-vnext bundle（最可能包含 SK）
    priority_urls = [u for u in js_urls if "app-vnext" in u or "bundle" in u or "vendor" in u]
    other_urls = [u for u in js_urls if u not in priority_urls]
    ordered_urls = priority_urls + other_urls

    # 3. 逐个下载并搜索
    print(f"\n[3] 在 JS 文件中搜索 AK ({KNOWN_AK[:12]}...):")
    for url in ordered_urls:
        name = url.split("/")[-1].split("?")[0]
        if len(name) > 60:
            name = name[:57] + "..."
        print(f"\n  检查: {name}")
        try:
            resp = client.get(url, follow_redirects=True)
            if resp.status_code != 200:
                print(f"  跳过 (HTTP {resp.status_code})")
                continue
            js_text = resp.text
            print(f"  大小: {len(js_text)} 字符")

            sk = search_sk_in_js(js_text, url)
            if sk:
                print(f"\n  {'='*50}")
                print(f"  找到 SecretKey: {sk}")
                print(f"  {'='*50}")
                client.close()
                return sk
            elif KNOWN_AK not in js_text:
                print(f"  未找到 AK")
        except Exception as e:
            print(f"  下载失败: {e}")

    client.close()
    return None


# ── 阶段2: 探测下载 API ──────────────────────────────


def phase2_probe_apis(base_url: str, cookie: str, file_id: str, cos_key: str) -> None:
    """阶段2: 尝试各种下载 API 端点"""
    print("\n" + "=" * 60)
    print("阶段2: 探测下载 API 端点")
    print("=" * 60)

    if not file_id:
        print("  未提供 file_id，跳过 API 探测")
        print("  提示: 用 --file-id 参数提供一个文件 ID（从 list 接口获取）")
        return

    client = make_client(cookie)
    t = str(int(time.time() * 1000))

    # 主域名接口
    main_patterns = [
        (f"/api/drives/{file_id}/download", "文件下载(路径1)"),
        (f"/api/drives/{file_id}/download_url", "文件下载URL"),
        (f"/api/drives/{file_id}/url", "文件URL"),
        (f"/api/drives/{file_id}/content", "文件内容"),
        (f"/api/drives/{file_id}/signed_url", "签名URL"),
        (f"/api/drives/{file_id}/preview", "文件预览"),
        (f"/api/drives/files/{file_id}/download", "文件下载(路径2)"),
        (f"/api/drives/download/{file_id}", "文件下载(路径3)"),
        (f"/api/drives/download?file_id={file_id}&t={t}", "文件下载(查询参数)"),
        (f"/api/drives/download_url?file_id={file_id}&t={t}", "下载URL(查询参数)"),
        (f"/api/drives/batch_download", "批量下载"),
        # COS 凭证接口
        (f"/api/cos/credentials?t={t}", "COS凭证"),
        (f"/api/cos/sts?t={t}", "COS STS"),
        (f"/api/cos/token?t={t}", "COS Token"),
        (f"/api/drives/credentials?t={t}", "网盘凭证"),
        (f"/api/drives/sts?t={t}", "网盘STS"),
        (f"/api/drives/cos?t={t}", "网盘COS配置"),
        (f"/api/box/credentials?t={t}", "Box凭证"),
        (f"/api/box/sts?t={t}", "Box STS"),
        (f"/api/upload/credentials?t={t}", "上传凭证"),
        (f"/api/storage/credentials?t={t}", "存储凭证"),
        (f"/api/file/credentials?t={t}", "文件凭证"),
    ]

    print(f"\n[1] 主域名接口 ({base_url}):\n")
    for path, desc in main_patterns:
        url = f"{base_url}{path}"
        try:
            resp = client.get(url)
            code = resp.status_code
            if code == 404:
                print(f"  [404] {desc}: {path}")
            elif code == 200:
                body = resp.text[:300]
                print(f"  [200] {desc}: {path}")
                print(f"        响应: {body}")
            elif code in (301, 302, 307):
                loc = resp.headers.get("location", "N/A")
                print(f"  [{code}] {desc}: {path}")
                print(f"        跳转: {loc}")
            else:
                print(f"  [{code}] {desc}: {path}")
        except Exception as e:
            print(f"  [ERR] {desc}: {e}")

    # wt-box 接口
    box_patterns = [
        (f"/drive/download?path={cos_key}&team_id={TEAM_ID}", "Box下载(path)"),
        (f"/drive/download?key={cos_key}&team_id={TEAM_ID}", "Box下载(key)"),
        (f"/drive/download/{cos_key}?team_id={TEAM_ID}", "Box下载(路径)"),
        (f"/drive/download/{file_id}?team_id={TEAM_ID}", "Box下载(file_id)"),
        (f"/drive/file/{cos_key}?team_id={TEAM_ID}", "Box文件(cos_key)"),
        (f"/drive/file/{file_id}?team_id={TEAM_ID}", "Box文件(file_id)"),
        (f"/drive/signed_url?path={cos_key}&team_id={TEAM_ID}", "Box签名URL"),
        (f"/drive/download_url?path={cos_key}&team_id={TEAM_ID}", "Box下载URL"),
        (f"/drive/credentials?team_id={TEAM_ID}", "Box凭证"),
        (f"/drive/sts?team_id={TEAM_ID}", "Box STS"),
        (f"/drive/cos/credentials?team_id={TEAM_ID}", "Box COS凭证"),
        (f"/drive/cos/sts?team_id={TEAM_ID}", "Box COS STS"),
    ]

    print(f"\n[2] wt-box 接口 ({BOX_URL}):\n")
    box_client = httpx.Client(
        headers={"x-cookies": cookie, "User-Agent": "Mozilla/5.0"},
        timeout=15.0,
        follow_redirects=False,
    )
    for path, desc in box_patterns:
        url = f"{BOX_URL}{path}"
        try:
            resp = box_client.get(url)
            code = resp.status_code
            if code == 404:
                print(f"  [404] {desc}: {path}")
            elif code == 200:
                body = resp.text[:300]
                print(f"  [200] {desc}: {path}")
                print(f"        响应: {body}")
            elif code in (301, 302, 307):
                loc = resp.headers.get("location", "N/A")
                print(f"  [{code}] {desc}: {path}")
                print(f"        跳转: {loc}")
            else:
                print(f"  [{code}] {desc}: {path}")
        except Exception as e:
            print(f"  [ERR] {desc}: {e}")

    client.close()
    box_client.close()


# ── 阶段3: 验证签名 ──────────────────────────────


def phase3_test_signing(sk: str, cos_key: str, filename: str) -> None:
    """阶段3: 用找到的 SK 生成签名 URL 并测试下载"""
    import hashlib
    import hmac
    from urllib.parse import quote

    print("\n" + "=" * 60)
    print("阶段3: 测试 COS 签名下载")
    print("=" * 60)

    bucket_host = "app-wt-original-release-1348738073.cos.ap-shanghai.myqcloud.com"

    now = int(time.time())
    expire = now + 3600
    key_time = f"{now};{expire}"

    # Step 1: SignKey
    sign_key = hmac.new(
        sk.encode(), key_time.encode(), hashlib.sha1
    ).hexdigest()

    # Step 2: StringToSign
    encoded_filename = quote(filename, safe="")
    disposition = f"attachment; filename*=utf-8''{encoded_filename}"
    encoded_disposition = quote(disposition, safe="")

    http_string = f"get\n/{cos_key}\nresponse-content-disposition={encoded_disposition}\nhost={bucket_host}\n"
    sha1_http = hashlib.sha1(http_string.encode()).hexdigest()
    string_to_sign = f"sha1\n{key_time}\n{sha1_http}\n"

    # Step 3: Signature
    signature = hmac.new(
        sign_key.encode(), string_to_sign.encode(), hashlib.sha1
    ).hexdigest()

    # Step 4: URL
    url = (
        f"https://{bucket_host}/{cos_key}"
        f"?q-sign-algorithm=sha1"
        f"&q-ak={KNOWN_AK}"
        f"&q-sign-time={key_time}"
        f"&q-key-time={key_time}"
        f"&q-header-list=host"
        f"&q-url-param-list=response-content-disposition"
        f"&q-signature={signature}"
        f"&response-content-disposition={quote(disposition, safe='')}"
    )

    print(f"\n  生成的签名 URL:\n  {url[:120]}...\n")

    # 测试下载（只请求 HEAD）
    try:
        resp = httpx.head(url, headers={"Host": bucket_host}, timeout=10, follow_redirects=True)
        if resp.status_code == 200:
            size = resp.headers.get("content-length", "未知")
            print(f"  下载测试成功! 文件大小: {size} 字节")
        else:
            print(f"  下载测试失败: HTTP {resp.status_code}")
            print(f"  响应: {resp.text[:300]}")
    except Exception as e:
        print(f"  下载测试失败: {e}")


# ── 主程序 ──────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description="探测 Worktile 文件下载方案")
    parser.add_argument("--cookie", help="认证 Cookie 字符串")
    parser.add_argument("--base-url", help="Worktile 基础 URL")
    parser.add_argument("--file-id", help="测试用文件 ID（从 list 接口获取）", default="")
    parser.add_argument("--cos-key", help="测试用 COS key（addition.path）", default="")
    parser.add_argument("--filename", help="测试用文件名", default="test.pdf")
    parser.add_argument("--sk", help="如果已知 SK，直接测试签名", default="")
    parser.add_argument("--skip-js", action="store_true", help="跳过 JS 搜索")
    parser.add_argument("--skip-api", action="store_true", help="跳过 API 探测")
    args = parser.parse_args()

    # 从 config.yaml 加载默认值
    config = load_config()
    wt = config.get("worktile", {})

    cookie = args.cookie or wt.get("auth", {}).get("cookie", "") or wt.get("auth", {}).get("token_value", "")
    base_url = args.base_url or wt.get("base_url", "")

    if not cookie or not base_url:
        print("错误: 需要提供 --cookie 和 --base-url，或在 config.yaml 中配置")
        sys.exit(1)

    base_url = base_url.rstrip("/")
    print(f"Worktile: {base_url}")
    print(f"Cookie: {cookie[:30]}...")

    # 阶段1: 从 JS 搜索 SK
    sk = args.sk
    if not sk and not args.skip_js:
        sk = phase1_find_sk(base_url, cookie) or ""

    # 阶段2: 探测 API
    if not args.skip_api:
        phase2_probe_apis(base_url, cookie, args.file_id, args.cos_key)

    # 阶段3: 如果有 SK 和 cos_key，测试签名
    if sk and args.cos_key:
        phase3_test_signing(sk, args.cos_key, args.filename)
    elif sk:
        print(f"\n找到 SK: {sk}")
        print("提示: 用 --cos-key 和 --filename 参数可以测试实际下载")
    elif not args.skip_js:
        print("\n未能自动找到 SK。请尝试手动方法:")
        print("  1. 打开 Chrome DevTools → Sources 面板")
        print(f"  2. Ctrl+Shift+F 搜索: {KNOWN_AK}")
        print("  3. 在 AK 附近找到 SecretKey（通常是相邻的字符串常量）")
        print(f"  4. 找到后运行: python tools/probe_download.py --sk YOUR_SK --cos-key FILE_COS_KEY --filename 文件名.pdf")


if __name__ == "__main__":
    main()
