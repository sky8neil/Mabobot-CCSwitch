"""宿主侧第三方 OpenAI 兼容图像通道服务。

仅使用标准库调用 `POST {BASE}/v1/images/generations`，用于在非官方
Codex Profile（例如 cc-switch 管理的第三方 API Key）下完成生图/改图。
"""

import base64
import json
import logging
import os
import re
from typing import List, Optional, Tuple
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

try:
    from app.services.config_service import get_setting
except ImportError:  # pragma: no cover - 独立导入时只读环境变量
    def get_setting(key, default=None):
        return default


HOST_IMAGE_ENV_BASE = "MABOBOT_IMAGE_API_BASE"
HOST_IMAGE_ENV_KEY = "MABOBOT_IMAGE_API_KEY"
HOST_IMAGE_ENV_MODEL = "MABOBOT_IMAGE_MODEL"
HOST_IMAGE_ENV_QUALITY = "MABOBOT_IMAGE_QUALITY"
HOST_IMAGE_ENV_TIMEOUT = "MABOBOT_IMAGE_TIMEOUT"
DEFAULT_HOST_IMAGE_MODEL = "grok-imagine-image-2.0"
DEFAULT_HOST_IMAGE_QUALITY = "medium"
DEFAULT_HOST_IMAGE_BASE = "https://subapi.803866.xyz"
DEFAULT_HOST_IMAGE_TIMEOUT = 300


class HostImageError(RuntimeError):
    """第三方图像通道错误。"""


def _mask_secret(text: str, key: str) -> str:
    """把文本中出现过的 key 替换为 <key>。"""
    if not text:
        return text
    if key and key in text:
        return text.replace(key, "<key>")
    return text


def _lookup(name: str, default: str = "") -> str:
    """配置查找：settings 表 -> 环境变量 -> 默认值。"""
    value = None
    try:
        value = get_setting(name)
    except Exception:
        value = None
    if value is None or (isinstance(value, str) and not value.strip()):
        value = os.getenv(name)
    if value is None or (isinstance(value, str) and not value.strip()):
        return default
    if isinstance(value, str):
        return value.strip()
    return value


def _sniff_mime(data: bytes) -> str:
    """按图片头嗅探 mime，未知按 image/png。"""
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG"):
        return "image/png"
    if data.startswith(b"GIF8"):
        return "image/gif"
    if data.startswith(b"BM"):
        return "image/bmp"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/png"


def _to_data_url(data: bytes, mime: str) -> str:
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def host_image_config() -> dict:
    """返回 {'api_base','api_key','model','quality','timeout'}。

    配置查找顺序：settings 表 -> 环境变量 -> 兜底默认值。
    api_base 依次尝试 MABOBOT_IMAGE_API_BASE -> SUBAPI_API_BASE -> DEFAULT_HOST_IMAGE_BASE；
    api_key  依次尝试 MABOBOT_IMAGE_API_KEY  -> SUBAPI_API_KEY  -> ""。
    取不到 api_key 时返回空字符串，不抛异常。
    """
    api_base = (
        _lookup(HOST_IMAGE_ENV_BASE)
        or _lookup("SUBAPI_API_BASE")
        or DEFAULT_HOST_IMAGE_BASE
    )
    api_key = _lookup(HOST_IMAGE_ENV_KEY) or _lookup("SUBAPI_API_KEY") or ""
    model = _lookup(HOST_IMAGE_ENV_MODEL) or DEFAULT_HOST_IMAGE_MODEL
    quality = _lookup(HOST_IMAGE_ENV_QUALITY) or DEFAULT_HOST_IMAGE_QUALITY
    timeout_raw = _lookup(HOST_IMAGE_ENV_TIMEOUT)
    try:
        timeout = int(timeout_raw) if timeout_raw else DEFAULT_HOST_IMAGE_TIMEOUT
    except (TypeError, ValueError):
        timeout = DEFAULT_HOST_IMAGE_TIMEOUT
    if timeout <= 0:
        timeout = DEFAULT_HOST_IMAGE_TIMEOUT
    return {
        "api_base": api_base,
        "api_key": api_key,
        "model": model,
        "quality": quality,
        "timeout": timeout,
    }


def host_image_available() -> bool:
    """api_base 与 api_key 都非空 = True。"""
    config = host_image_config()
    return bool(config.get("api_base")) and bool(config.get("api_key"))


class HostImageService:
    """第三方 OpenAI 兼容图像接口客户端。"""

    def __init__(self, config: Optional[dict] = None):
        self._explicit_config = config is not None
        self.config = config if config is not None else host_image_config()
        self.api_base = str(self.config.get("api_base") or "").strip()
        self.api_key = str(self.config.get("api_key") or "").strip()
        self.model = self.config.get("model") or DEFAULT_HOST_IMAGE_MODEL
        self.quality = self.config.get("quality") or DEFAULT_HOST_IMAGE_QUALITY
        try:
            self.timeout = int(self.config.get("timeout") or DEFAULT_HOST_IMAGE_TIMEOUT)
        except (TypeError, ValueError):
            self.timeout = DEFAULT_HOST_IMAGE_TIMEOUT
        if self.timeout <= 0:
            self.timeout = DEFAULT_HOST_IMAGE_TIMEOUT

    def available(self) -> bool:
        if not self._explicit_config:
            return host_image_available()
        return bool(self.api_base) and bool(self.api_key)

    def describe(self) -> str:
        """安全描述，不含 key。"""
        return f"{self.model}@{self.quality} via {self.api_base}"

    def _mask(self, text: str) -> str:
        return _mask_secret(text, self.api_key)

    def _endpoint(self) -> str:
        """拼接图像端点；base 已带 /v1 之类版本段时不重复追加。"""
        base = self.api_base.rstrip("/")
        if not re.search(r"/v\d+$", base):
            base += "/v1"
        return base + "/images/generations"

    def _download_url(self, url: str) -> bytes:
        request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return response.read()

    def generate(
        self,
        prompt: str,
        images: Optional[List[bytes]] = None,
        count: int = 1,
    ) -> List[Tuple[bytes, str]]:
        """调用第三方接口生成/编辑图片，返回 [(图片字节, mime), ...]。"""
        if not prompt or not prompt.strip():
            raise HostImageError("提示词为空")
        if not self.api_base:
            raise HostImageError("第三方图像接口地址为空")
        if not self.api_key:
            raise HostImageError("第三方图像接口密钥为空")
        try:
            count = max(1, int(count))
        except (TypeError, ValueError):
            count = 1

        body = {
            "model": self.model,
            "prompt": prompt,
            "n": count,
            "quality": self.quality,
            "response_format": "b64_json",
        }
        if images:
            data_urls = [_to_data_url(data, _sniff_mime(data)) for data in images]
            body["image"] = data_urls[0] if len(data_urls) == 1 else data_urls

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0",
        }
        request = urllib.request.Request(
            self._endpoint(),
            data=json.dumps(body).encode("utf-8"),
            headers=headers,
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:500]
            except Exception:
                detail = ""
            raise HostImageError(self._mask(
                f"第三方图像接口返回 HTTP {exc.code}: {detail}"
            ))
        except urllib.error.URLError as exc:
            raise HostImageError(self._mask(f"第三方图像接口请求失败: {exc.reason}"))
        except Exception as exc:
            raise HostImageError(self._mask(f"第三方图像接口请求异常: {exc}"))

        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception:
            raise HostImageError("第三方图像接口返回内容无法解析为 JSON")

        items = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(items, list) or not items:
            raise HostImageError(self._mask(
                "第三方图像接口未返回图片数据: "
                + json.dumps(payload, ensure_ascii=False)[:300]
            ))

        results: List[Tuple[bytes, str]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            data = None
            if item.get("b64_json"):
                try:
                    data = base64.b64decode(item["b64_json"])
                except Exception:
                    continue
            elif item.get("url"):
                try:
                    data = self._download_url(item["url"])
                except urllib.error.HTTPError as exc:
                    raise HostImageError(self._mask(
                        f"下载第三方图像返回 HTTP {exc.code}"
                    ))
                except urllib.error.URLError as exc:
                    raise HostImageError(self._mask(f"下载第三方图像失败: {exc.reason}"))
                except Exception as exc:
                    raise HostImageError(self._mask(f"下载第三方图像异常: {exc}"))
            if not data:
                continue
            mime = item.get("mime_type") or _sniff_mime(data)
            results.append((data, mime))

        if not results:
            raise HostImageError("第三方图像接口未返回可用图片")
        return results
