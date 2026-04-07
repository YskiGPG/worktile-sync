# Worktile 下载接口调研记录

## 背景

Worktile 网盘文件存储在腾讯云 COS 上，下载需要带签名参数。我们已经成功抓到了文件列表、上传等接口，但下载接口始终找不到。

## 已知信息

### 文件存储位置
- COS 桶: `app-wt-original-release-1348738073.cos.ap-shanghai.myqcloud.com`
- 文件 key 来自 `list` 接口返回的 `addition.path` 字段（如 `f4389d70-d3f0-433d-a029-f48f0ed68aed`）
- COS 桶是**私有读**，直接访问返回 `AccessDenied`

### 实际下载 URL（从浏览器 Network 抓到的）
```
https://app-wt-original-release-1348738073.cos.ap-shanghai.myqcloud.com/1a155679-b17d-4478-aae4-b4cad055e796
  ?q-sign-algorithm=sha1
  &q-ak=AKIDjKD0b1EDyZx78B8yfIfsu1yhDXkncDds
  &q-sign-time=1775429319;1775432919
  &q-key-time=1775429319;1775432919
  &q-header-list=host
  &q-url-param-list=response-content-disposition
  &q-signature=b4cf340b14babb0a3c672ac1136d49ece9fa02d0
  &response-content-disposition=attachment; filename*=utf-8''文件名.rar
```

### 签名参数解析
- `q-sign-algorithm`: 固定 `sha1`
- `q-ak`: COS AccessKeyId，固定值 `AKIDjKD0b1EDyZx78B8yfIfsu1yhDXkncDds`
- `q-sign-time`: 签名有效期，格式 `{开始时间戳};{结束时间戳}`，有效期约1小时
- `q-key-time`: 同 `q-sign-time`
- `q-header-list`: 固定 `host`
- `q-url-param-list`: 固定 `response-content-disposition`
- `q-signature`: HMAC-SHA1 签名值
- `response-content-disposition`: 文件名编码

### Worktile 配置（从 /api/user/me 获取）
```json
{
  "box": {
    "baseUrl": "https://wt-box.worktile.com",
    "serviceUrl": "https://app-wt-static-release-1348738073.cos.ap-shanghai.myqcloud.com/",
    "avatarUrl": "https://app-wt-avatar-release-1348738073.cos.ap-shanghai.myqcloud.com/",
    "logoUrl": "https://app-wt-logo-release-1348738073.cos.ap-shanghai.myqcloud.com/"
  }
}
```
注意: 文件下载用的桶 `app-wt-original-release` 没有出现在配置中，配置里只有 `static`、`avatar`、`logo` 三个桶。

## 已尝试的下载接口（全部失败）

| 尝试的 URL | 结果 |
|-----------|------|
| `GET /api/drives/69d2e3aeb5176ce7c60b8217/download` | 404（被重定向到 worktile.com/404） |
| `GET /api/drives/files/69d2e3aeb5176ce7c60b8217/download` | 404（同上） |
| `GET /api/drives/download/69d2e3aeb5176ce7c60b8217` | 404 |
| `GET wt-box.worktile.com/drive/download?path={cos_key}&team_id={team_id}` | 404 |
| `GET wt-box.worktile.com/drive/download/{cos_key}?team_id={team_id}` | 404 |
| `GET wt-box.worktile.com/drive/files/{cos_key}?team_id={team_id}` | 404 |
| 直接访问 COS 不带签名 | 403 AccessDenied |

## 可用的接口

| 接口 | URL | 说明 |
|------|-----|------|
| 文件详情 | `GET /api/drives/{file_id}` | 返回 200，包含 `addition.path` 等信息，但**不含下载 URL** |
| 文件列表 | `GET /api/drives/list?parent_id={folder_id}&belong=2&...` | 返回文件和子文件夹列表 |
| 文件夹列表 | `GET /api/drives/folders?parent_id=&belong=2&...` | 返回根文件夹列表 |
| 上传 | `POST wt-box.worktile.com/drive/upload?belong=2&parent_id={}&team_id={}` | multipart 上传 |

## 浏览器端下载行为观察

1. 点击下载按钮时，Chrome Network 面板（All 模式）**没有看到任何新的 API 请求**
2. 下载按钮是 Angular 组件 `<a thyaction thyactionicon="download">`，没有 href，由 JS 事件处理
3. 点击后直接弹出系统文件保存对话框
4. 最终下载请求是直接访问 COS URL（带签名），类型是 `document`（浏览器导航）
5. 事件处理函数在 `app-vnext.bundle.min-9.62.1+202603251400.js` 中（压缩代码）

## 分析与猜测

### 可能性1: 前端 JS 直接生成签名（最可能）
签名逻辑在前端 JS 中完成。AK 已知（`AKIDjKD0b1EDyZx78B8yfIfsu1yhDXkncDds`），SK 可能也硬编码在 JS 中。
- **验证方法**: 在 `app-vnext.bundle.min` 中搜索 AK 值或 `q-signature`，找到附近的 SK
- 腾讯云 COS 签名算法是公开的: https://cloud.tencent.com/document/product/436/7778
- 如果找到 SK，我们可以在 Python 中自己生成签名

### 可能性2: 签名由 wt-box 服务端生成，但我们没找到正确的 URL
- wt-box 可能有某个接口返回签名 URL，但路径不是我们猜的那些

### 可能性3: 签名在 WebSocket 或其他非 HTTP 通道传递

### 可能性4: Worktile 做了权限限制
- 某些接口可能需要特殊权限或额外的 Header

## Debug 过程记录（2026-04-05）

### 第一阶段：自动化探测

编写了 `tools/probe_download.py` 探测脚本，分三步执行：

#### 1. 尝试从 JS bundle 中搜索 COS SecretKey

思路：既然浏览器点击下载时没有新 API 请求，签名应该在前端 JS 中生成，SK 可能硬编码在代码里。

操作：
1. 用 Cookie 认证获取 Worktile 主页 HTML
2. 从 HTML 中提取所有 `<script src="...">` 的 JS URL（共找到 13 个 JS 文件）
3. 逐个下载 JS 文件，搜索已知 AK `AKIDjKD0b1EDyZx78B8yfIfsu1yhDXkncDds`

结果：
- 主域名直接访问 JS 返回 403，但 JS 实际托管在 CDN（`cdn-aliyun.worktile.com/pro/js/`），CDN 无需认证
- **AK 字符串在所有 13 个 JS 文件中均不存在** — 推翻了"SK 硬编码在前端"的假设
- `app-vnext.bundle.min` 中有 `secretKey` 关键词，但深入分析发现全部是二次验证（2FA）相关代码，与 COS 无关

#### 2. 批量探测下载 API 端点

操作：在主域名和 wt-box 域名上分别尝试 20+ 种 URL 模式。

主域名关键结果：
| 端点 | 状态码 | 响应 |
|------|--------|------|
| `/api/drives/{file_id}/download` | 302 | 跳转到 worktile.com/404 |
| `/api/drives/download?file_id={id}` | 200 | `{"code":401,"message":"invalid id"}` |
| `/api/drives/credentials` | 200 | `{"code":401,"message":"invalid id"}` |
| `/api/drives/sts` | 200 | `{"code":401,"message":"invalid id"}` |
| `/api/cos/credentials` | 302 | 跳转到 worktile.com/404 |

分析：返回 `{"code":401,"message":"invalid id"}` 的端点其实是 `/api/drives/{id}` 的通配路由，把 `download`、`credentials` 等当作文件 ID 解析，因为格式不对所以报 `invalid id`。并非真实存在的 API。

wt-box 结果：全部 404。但注意我们当时尝试的路径是 `/drive/download`（单数），而不是 `/drives/{id}`（复数）。

#### 3. 关键突破：逆向 JS 代码

既然 AK 不在 JS 里（否定了客户端签名假设），转向搜索 JS 中的下载 URL 拼接逻辑。

**搜索策略**：在 `app-vnext.bundle.min`（8.3MB）中搜索下载相关关键词。

发现 `driveRealUrl` 关键词命中处（position 110787）的完整代码：

```javascript
class Ca {
  transform(t, e) {
    if (_.isEmpty(t)) return "";
    e = e || "";
    if (/^https*:\/\//.test(t.path)) return t.path;  // 已有完整URL，直接返回

    if (t.type === __constant.drive.type.file) {
      if (va.config.isIndependent) {
        // 独立部署模式
        return va.config.box.baseUrl + "/drives/" + t._id
          + "?team_id=" + va.me.team
          + "&version=" + t.addition.current_version
          + (t.ref_id ? "&ref_id=" + t.ref_id : "")
          + "&action=" + e;
      } else {
        // 非独立部署模式（SaaS，我们的场景）
        return va.config.box.baseUrl + "/drives/" + t._id
          + "/from-s3?team_id=" + va.me.team
          + "&version=" + t.addition.current_version
          + (t.ref_id ? "&ref_id=" + t.ref_id : "")
          + "&action=" + e;
      }
    }
    // 文件夹打包下载、页面下载等其他类型...
  }
}
// Angular pipe 注册
Ca.ɵpipe = u.UTH({ name: "driveRealUrl", type: Ca, pure: true });
```

其中 `va = window.appGlobalConfig`，`va.config.box.baseUrl` = `https://wt-box.worktile.com`。

这揭示了：下载 URL 是 `wt-box.worktile.com/drives/{file_id}` 而非 `/drive/download`，由 **wt-box 服务端** 代理 COS 签名，前端只负责拼接 URL。

#### 4. 验证

用两种路径测试：

```
# 方式1: 独立部署路径 → HTTP 200 直接返回文件内容
GET https://wt-box.worktile.com/drives/{file_id}?team_id={team_id}&version=1&action=download
Header: x-cookies: {cookie}
→ 200 OK, Content-Length: 14322157 (14.3MB PDF)

# 方式2: 非独立部署路径 → 302 跳转到 COS 签名 URL
GET https://wt-box.worktile.com/drives/{file_id}/from-s3?team_id={team_id}&version=1&action=download
Header: x-cookies: {cookie}
→ 302 → https://app-wt-original-release-1348738073.cos.ap-shanghai.myqcloud.com/{cos_key}?q-sign-algorithm=sha1&q-ak=AKID...
```

两种方式都可用。方式1更简单（直接拿到文件流），方式2暴露了 COS 签名 URL 的生成是在服务端完成的。

#### 5. 端到端验证

用完整 API 类下载 14.3MB 的 `施工合同.pdf`，字节数完全匹配：14322157/14322157。

### 关键教训

1. **路径命名的微妙差异**：上传路径是 `/drive/upload`（单数），下载路径是 `/drives/{id}`（复数）。之前手动猜测时用了 `/drive/download`，差一个 `s`
2. **不要假设签名方式**：我们最初假设"前端用硬编码 SK 签名"，但实际是服务端代理签名。AK 出现在 COS URL 中不代表 AK 在前端代码里
3. **逆向 JS 比猜 API 路径更可靠**：与其遍历所有可能的 URL 模式，不如直接找前端代码中 URL 的拼接逻辑
4. **CDN 静态资源通常无需认证**：主域名返回 403 的 JS 文件，在 CDN 域名上可以直接访问

## 最终方案（已解决 2026-04-05）

### 发现过程

通过分析 `app-vnext.bundle.min` 中的 `driveRealUrl` pipe（Angular 管道），找到下载 URL 生成逻辑：

```javascript
// 非独立部署（我们的场景）
va.config.box.baseUrl + "/drives/" + t._id + "/from-s3?team_id=" + va.me.team
  + "&version=" + t.addition.current_version + "&action=" + e

// 独立部署
va.config.box.baseUrl + "/drives/" + t._id + "?team_id=" + va.me.team
  + "&version=" + t.addition.current_version + "&action=" + e
```

### 下载接口

**不需要 COS SecretKey！** wt-box 服务端自己处理 COS 签名。

```
GET https://wt-box.worktile.com/drives/{file_id}?team_id={team_id}&version={version}&action=download
Header: x-cookies: {完整cookie}
```

- 返回 HTTP 200，直接流式返回文件内容
- `version` 来自 `addition.current_version`（通常为 1）
- `action=download` 触发下载行为

备选（302 跳转到 COS 签名 URL）:
```
GET https://wt-box.worktile.com/drives/{file_id}/from-s3?team_id={team_id}&version={version}&action=download
Header: x-cookies: {完整cookie}
```

### 为什么之前没找到

1. 路径不是 `/drive/download` 而是 `/drives/{id}`（注意 drives 有 s）
2. 认证不是通过 Cookie header 而是 `x-cookies` header
3. AK/SK 不在前端 JS 中（COS 签名由 wt-box 服务端生成）
4. 浏览器中"无新 API 请求"是因为下载 URL 由前端 JS 拼接后直接发起导航请求

## 供开发参考的腾讯云 COS 签名算法

如果拿到了 SecretKey，Python 签名代码如下:

```python
import hashlib
import hmac
import time
from urllib.parse import quote

def generate_cos_download_url(cos_key: str, filename: str) -> str:
    """生成腾讯云 COS 签名下载 URL"""
    secret_id = "AKIDjKD0b1EDyZx78B8yfIfsu1yhDXkncDds"
    secret_key = "TODO: 从JS中提取"  # ⚠️ 待填入
    
    bucket_host = "app-wt-original-release-1348738073.cos.ap-shanghai.myqcloud.com"
    
    now = int(time.time())
    expire = now + 3600  # 1小时有效期
    key_time = f"{now};{expire}"
    sign_time = key_time
    
    # Step 1: 生成 SignKey
    sign_key = hmac.new(
        secret_key.encode(), key_time.encode(), hashlib.sha1
    ).hexdigest()
    
    # Step 2: 生成 StringToSign
    disposition = quote(f"attachment; filename*=utf-8''{quote(filename)}", safe='')
    http_params = f"response-content-disposition={disposition}"
    
    http_string = f"get\n/{cos_key}\n{http_params}\nhost={bucket_host}\n"
    sha1_of_http_string = hashlib.sha1(http_string.encode()).hexdigest()
    string_to_sign = f"sha1\n{sign_time}\n{sha1_of_http_string}\n"
    
    # Step 3: 生成 Signature
    signature = hmac.new(
        sign_key.encode(), string_to_sign.encode(), hashlib.sha1
    ).hexdigest()
    
    # Step 4: 拼接 URL
    params = (
        f"q-sign-algorithm=sha1"
        f"&q-ak={secret_id}"
        f"&q-sign-time={sign_time}"
        f"&q-key-time={key_time}"
        f"&q-header-list=host"
        f"&q-url-param-list=response-content-disposition"
        f"&q-signature={signature}"
        f"&response-content-disposition={disposition}"
    )
    
    return f"https://{bucket_host}/{cos_key}?{params}"
```
