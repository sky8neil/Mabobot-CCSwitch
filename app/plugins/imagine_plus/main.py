"""imagine_plus 关键词直连出图插件。

在任何被授权该插件的聊天中，只要消息 @了机器人 且命中生图关键词，
插件直接调用宿主图像通道出图并把图片发回该聊天，命中即消费该消息。
"""

import logging
import re
import string
import time
from datetime import datetime
from pathlib import Path
from typing import Any, List, Optional, Tuple

from app.core.event_bus import Event, EventType
from app.services.host_image_service import HostImageService
from app.utils.plugin_config import get_config

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

_LOG_PREFIX = "🎨 imagine_plus: "
_PLUGIN_NAME = "imagine_plus"

# 默认生图关键词，子串匹配，ASCII 大小写不敏感
_DEFAULT_KEYWORDS = [
    "生图",
    "生成图片",
    "生成一张图",
    "生成图",
    "文生图",
    "画一张",
    "画一张图",
    "画个",
    "画一幅",
    "帮我画",
    "帮忙画",
    "出图",
    "做个图",
    "配张图",
    "配图",
]

# 默认句式（正则）匹配，与子串关键词并存；命名组 desc 保留描述中间部分
_DEFAULT_PATTERNS = (
    r"(?:生成个|生成张|来张|来一张|来一幅|画个|画一张|画一幅|做个|做张)\s*(?P<desc>.{0,24}?)\s*(?:图片|照片|相片|图|画)",
    r"(?:生成|画|做|来)\s*(?:个|张|一张|一幅|一副)\s*(?P<desc>.{1,24}?)\s*(?:图片|照片|相片|图)",
)

_DEFAULT_HINT_TEXT = "🎨 想画什么？把描述发我，例如：@我 画一只在雨里奔跑的柯基"
_DEFAULT_OUTPUT_DIR = "data/plugins/imagine_plus/out"

# mime 到文件扩展名
_MIME_EXT = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
}

# 首尾需要去掉的标点（含中英文）
_STRIP_PUNCTUATION = string.punctuation + "，。！？、；：""''（）【】《》〈〉…—～·「」『』"

# 失败/拒绝回复文案：模型或接口拒绝时，只外发这一条。
# 必须同时包含：拒绝说明 + 模型原始报错（原文照登）+ 换提示词建议。
_FAILURE_REPLY_HEADER = "⚠️ 出图失败：模型/接口拒绝了这次请求，换个提示词再试一次。"
_FAILURE_REPLY_ERROR_LABEL = "原始报错（原文照登）："
# 原文照登，仅在极端超长时截断，远大于旧的 200 字符上限。
_FAILURE_REPLY_MAX_ERROR_CHARS = 4000


def _format_failure_reply(error: Any) -> str:
    """把异常整理成唯一的一条失败回复（拒绝说明 + 原始报错 + 换词建议）。"""
    # 原文照登：不做 strip/转义，避免改动模型返回的字符。
    raw = str(error) if error is not None else ""
    if not raw.strip():
        raw = "（未提供报错内容）"
    if len(raw) > _FAILURE_REPLY_MAX_ERROR_CHARS:
        raw = raw[:_FAILURE_REPLY_MAX_ERROR_CHARS]
    return f"{_FAILURE_REPLY_HEADER}\n{_FAILURE_REPLY_ERROR_LABEL}\n{raw}"


def _to_int(value: Any, default: int) -> int:
    """尽量把配置值转换为整数，失败时使用默认值。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class ImaginePlusPlugin:
    """关键词直接出图插件主体。"""

    def __init__(self, context: Any = None):
        self.context = context
        self.keywords = self._load_keywords()
        self.patterns = self._load_patterns()
        self.max_count = max(1, _to_int(self._cfg("max_count", 4), 4))
        self.default_count = _to_int(self._cfg("default_count", 1), 1)
        self.default_count = max(1, min(self.default_count, self.max_count))
        self.hint_text = str(self._cfg("hint_text", _DEFAULT_HINT_TEXT) or _DEFAULT_HINT_TEXT)
        self.output_dir = str(self._cfg("output_dir", _DEFAULT_OUTPUT_DIR) or _DEFAULT_OUTPUT_DIR)
        self.generate_timeout_seconds = max(1, _to_int(self._cfg("generate_timeout_seconds", 300), 300))
        self.send_retries = max(1, _to_int(self._cfg("send_retries", 3), 3))

    # ------------------------------------------------------------------
    # 配置
    # ------------------------------------------------------------------
    def _cfg(self, key: str, default: Any) -> Any:
        return get_config(key, default, plugin_name=_PLUGIN_NAME)

    def _load_keywords(self) -> List[str]:
        raw = self._cfg("keywords", _DEFAULT_KEYWORDS)
        if not isinstance(raw, list):
            raw = _DEFAULT_KEYWORDS
        keywords = [str(item).strip() for item in raw if str(item or "").strip()]
        return keywords or list(_DEFAULT_KEYWORDS)

    def _load_patterns(self) -> List[str]:
        raw = self._cfg("patterns", _DEFAULT_PATTERNS)
        if not isinstance(raw, list) or not raw:
            raw = _DEFAULT_PATTERNS
        patterns = [str(item).strip() for item in raw if str(item or "").strip()]
        return patterns or list(_DEFAULT_PATTERNS)

    # ------------------------------------------------------------------
    # 文本匹配与提示词提取
    # ------------------------------------------------------------------
    def _match_keyword(self, message: str) -> Optional[str]:
        """返回第一个命中的关键词或 pattern:...，未命中返回 None。"""
        lowered = message.lower()
        for keyword in self.keywords:
            if keyword.lower() in lowered:
                return keyword
        for pattern in self.patterns:
            try:
                if re.search(pattern, message, re.IGNORECASE):
                    return f"pattern:{pattern}"
            except re.error:
                logger.warning("%s忽略无效句式正则: %s", _LOG_PREFIX, pattern)
        return None

    def _extract_prompt(self, message: str) -> Tuple[str, int]:
        """提取提示词与出图数量。"""
        text = re.sub(r"@\S+", " ", message)
        # 先剥离句式并保留命名组 desc 的描述内容，再剥离纯子串关键词。
        for pattern in self.patterns:
            try:
                text = re.sub(
                    pattern,
                    lambda m: " " + (m.group("desc") or "") + " ",
                    text,
                    flags=re.IGNORECASE,
                )
            except re.error:
                logger.warning("%s忽略无效句式正则: %s", _LOG_PREFIX, pattern)
        for keyword in self.keywords:
            text = re.sub(re.escape(keyword), " ", text, flags=re.IGNORECASE)
        text = re.sub(r"\s+", " ", text).strip()
        text = text.strip(_STRIP_PUNCTUATION).strip()

        count = self.default_count
        match = re.search(r"(\d+)\s*[张幅]", message)
        if match:
            count = _to_int(match.group(1), self.default_count)
        count = max(1, min(count, self.max_count))
        return text, count

    @staticmethod
    def _is_group_chat(data: Any) -> bool:
        """优先使用 is_group，缺失时按 chat_type 推断。"""
        is_group = data.get("is_group")
        if is_group is None:
            return str(data.get("chat_type") or "").strip().lower() == "group"
        return bool(is_group)

    # ------------------------------------------------------------------
    # 落盘与发送
    # ------------------------------------------------------------------
    @staticmethod
    def _chat_marker(chat_name: str) -> str:
        marker = re.sub(r"[\\/:*?\"<>|\s]+", "_", str(chat_name or "")).strip("_")
        return marker[:40] or "chat"

    def _save_image(self, data: bytes, mime: str, chat_name: str, index: int, stamp: str) -> Path:
        ext = _MIME_EXT.get(str(mime or "").lower(), "bin")
        out_dir = Path(self.output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{self._chat_marker(chat_name)}_{stamp}_{index}.{ext}"
        path.write_bytes(data)
        return path

    def _send_message(self, wx: Any, chat_name: str, text: str) -> bool:
        if wx is None:
            logger.warning("%swx 缺失，无法发送消息: chat=%s text=%s", _LOG_PREFIX, chat_name, text)
            return False
        try:
            return bool(wx.send_message(chat_name, text))
        except Exception as exc:
            logger.error("%s发送消息失败: %s", _LOG_PREFIX, exc)
            return False

    def _send_image_files(self, wx: Any, chat_name: str, paths: List[str]) -> bool:
        """发送图片，失败重试 send_retries 次，间隔 attempt*2 秒。"""
        if wx is None:
            logger.warning("%swx 缺失，无法发送图片: chat=%s", _LOG_PREFIX, chat_name)
            return False

        retries = max(1, int(self.send_retries))
        last_error: Optional[Exception] = None
        for attempt in range(1, retries + 1):
            try:
                sent = False
                if hasattr(wx, "send_files"):
                    sent = bool(wx.send_files(chat_name, list(paths)))
                if not sent and hasattr(wx, "SendFiles"):
                    sent = True
                    for one in paths:
                        if not wx.SendFiles(str(one), chat_name):
                            sent = False
                            break
                if sent:
                    return True
            except Exception as exc:  # noqa: BLE001 - 发送容错，保持重试
                last_error = exc
                logger.warning("%s发送图片异常(第%s次): %s", _LOG_PREFIX, attempt, exc)
            if attempt < retries:
                time.sleep(attempt * 2)
        logger.error("%s发送图片失败: chat=%s error=%s", _LOG_PREFIX, chat_name, last_error)
        return False

    # ------------------------------------------------------------------
    # 事件处理
    # ------------------------------------------------------------------
    def handle_text_message(self, event: Event) -> bool:
        data = getattr(event, "data", None) or {}
        context = getattr(event, "context", None) or {}
        chat_name = str(data.get("chat_name") or "")
        wx = context.get("wx") if hasattr(context, "get") else None

        try:
            message = str(data.get("message") or "")
            if not self._match_keyword(message):
                return False

            bot_mentioned = bool(data.get("bot_mentioned"))
            if self._is_group_chat(data) and not bot_mentioned:
                return False

            prompt, count = self._extract_prompt(message)
            if len(prompt) < 2:
                logger.info("%s提示词为空，发送引导语: chat=%s", _LOG_PREFIX, chat_name)
                self._send_message(wx, chat_name, self.hint_text)
                return True

            service = HostImageService()
            if not service.available():
                logger.warning("%s宿主图像通道不可用: chat=%s", _LOG_PREFIX, chat_name)
                self._send_message(wx, chat_name, "⚠️ 当前未配置宿主图像通道")
                return True

            # 兼容旧版 host_image_service._endpoint：base 若已带版本段则先剥掉，
            # 新旧实现都会补唯一 /v1 前缀，避免出现 /v1/v1 导致 404。
            base = str(getattr(service, "api_base", "") or "").rstrip("/")
            if re.search(r"/v\d+$", base):
                service.api_base = base[: base.rfind("/")]
                logger.info("%s已规范化出图 base: %s", _LOG_PREFIX, service.api_base)

            logger.info(
                "%s开始出图: chat=%s count=%s endpoint=%s prompt=%s",
                _LOG_PREFIX,
                chat_name,
                count,
                service._endpoint(),
                prompt,
            )
            results = service.generate(prompt, count=count)
            if not results:
                raise RuntimeError("图像通道未返回图片")

            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            paths: List[str] = []
            for index, item in enumerate(results, start=1):
                try:
                    img_data, mime = item[0], item[1]
                except Exception:
                    continue
                if not img_data:
                    continue
                paths.append(str(self._save_image(img_data, mime, chat_name, index, stamp)))

            if not paths:
                raise RuntimeError("图像通道未返回可用图片")

            if not self._send_image_files(wx, chat_name, paths):
                logger.error("%s图片发送失败，文件已保存: chat=%s files=%s", _LOG_PREFIX, chat_name, paths)
            return True

        except Exception as exc:  # noqa: BLE001 - 兜底，绝不向上抛异常
            logger.error("%s处理失败: %s", _LOG_PREFIX, exc, exc_info=True)
            try:
                self._send_message(wx, chat_name, _format_failure_reply(exc))
            except Exception:
                pass
            return True


_plugin: Optional[ImaginePlusPlugin] = None


def handle_text_message(event: Event) -> bool:
    """文本消息事件入口。"""
    if _plugin is None:
        return False
    return _plugin.handle_text_message(event)


def register(event_bus, subscribe, context):
    """插件注册函数。"""
    global _plugin
    logger.info("%s注册插件...", _LOG_PREFIX)
    _plugin = ImaginePlusPlugin(context)
    if context is not None and hasattr(context, "register_cleanup"):
        context.register_cleanup(unregister)
    subscribe(event_type=EventType.TEXT_MESSAGE_RECEIVED, handler=handle_text_message)
    try:
        from app.services.host_image_service import HostImageService, host_image_config

        _cfg = host_image_config()
        logger.info(
            "%s出图配置自检: base=%s model=%s quality=%s keylen=%s endpoint=%s",
            _LOG_PREFIX,
            _cfg.get("api_base"),
            _cfg.get("model"),
            _cfg.get("quality"),
            len(_cfg.get("api_key") or ""),
            HostImageService()._endpoint(),
        )
    except Exception as exc:  # pragma: no cover - 诊断信息不应影响注册
        logger.warning("%s出图配置自检失败: %s", _LOG_PREFIX, exc)
    logger.info("%s插件注册成功", _LOG_PREFIX)


def unregister():
    """取消注册插件。"""
    global _plugin
    logger.info("%s取消注册插件...", _LOG_PREFIX)
    _plugin = None
