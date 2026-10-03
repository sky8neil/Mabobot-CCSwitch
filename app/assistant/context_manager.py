"""Token-budgeted context helpers for the core Assistant.

This module renders bounded context. Durable raw messages live in
``app.history.store`` and are retrieved on demand.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Tuple

# Per-message hard limit applied before rendering reply context (R-A2).
MESSAGE_CONTENT_LIMIT = 600
MESSAGE_TRUNCATION_MARKER = "…（内容过长已截断）"

# A message is treated as deictic only when it is short and points back at
# something already in the room (R-B1).
DEICTIC_MAX_CHARS = 40
DEICTIC_PRONOUNS = (
    "这个",
    "这条",
    "这篇",
    "这则",
    "这张",
    "这新闻",
    "这消息",
    "这报道",
    "这段",
    "这些",
    "此事",
    "这事",
    "上面",
    "上述",
    "上文",
    "刚才那",
    "刚才这",
    "刚刚那",
    "刚刚这",
    "前面那",
)
BOT_SUMMARY_MARKERS = ("摘要", "一句话总结", "思维导图")
_ADJACENT_LINK_BLOCK_HEADER = "【紧邻上文链接内容】"
_URL_PATTERN = re.compile(r"https?://[^\s，。、）)】\]\}＞>\"'「」]+", re.IGNORECASE)
_TITLE_PATTERN = re.compile(r"标题[:：]\s*(.+)")


class ChatContextManager:
    """Estimate, trim and render chat context without owning memory state."""

    def estimate_tokens(self, text: Any) -> int:
        """Conservative token estimate that works well for mixed Chinese text."""
        if text is None:
            return 0
        value = str(text)
        if not value:
            return 0

        cjk_chars = sum(1 for ch in value if "\u4e00" <= ch <= "\u9fff")
        other_chars = len(value) - cjk_chars
        return max(1, cjk_chars + (other_chars + 3) // 4)

    def estimate_message_tokens(self, message: Dict[str, Any]) -> int:
        sender = message.get("sender", "")
        content = message.get("content", "")
        time_str = message.get("time", "")
        return self.estimate_tokens(f"[{time_str}] [{sender}]: {content}") + 4

    def truncate_text_to_budget(
        self,
        text: str,
        token_budget: int,
        notice: str = "内容因上下文预算限制已截断",
    ) -> str:
        if not text or token_budget <= 0:
            return ""
        if self.estimate_tokens(text) <= token_budget:
            return text

        low, high = 0, len(text)
        while low < high:
            mid = (low + high + 1) // 2
            if self.estimate_tokens(text[:mid]) <= token_budget:
                low = mid
            else:
                high = mid - 1

        trimmed = text[:low].rstrip()
        if len(trimmed) < 20:
            return f"（{notice}）"
        return trimmed + f"\n\n（{notice}）"

    def truncate_message_content(
        self,
        text: Any,
        max_chars: int = MESSAGE_CONTENT_LIMIT,
    ) -> str:
        """Keep the head of one message and append an explicit omission marker."""
        value = str(text or "")
        if max_chars <= 0 or len(value) <= max_chars:
            return value
        return value[:max_chars].rstrip() + MESSAGE_TRUNCATION_MARKER

    def truncate_messages(
        self,
        messages: List[Dict[str, Any]],
        max_chars: int = MESSAGE_CONTENT_LIMIT,
    ) -> List[Dict[str, Any]]:
        """Return shallow copies whose per-message content respects ``max_chars``."""
        trimmed: List[Dict[str, Any]] = []
        for message in messages or []:
            if not isinstance(message, dict):
                trimmed.append(message)
                continue
            if "content" not in message:
                trimmed.append(message)
                continue
            copy = dict(message)
            copy["content"] = self.truncate_message_content(
                message.get("content"), max_chars
            )
            trimmed.append(copy)
        return trimmed

    def is_deictic_message(
        self,
        text: Any,
        max_chars: int = DEICTIC_MAX_CHARS,
    ) -> bool:
        """Whether a short message uses a pronoun that points at nearby context."""
        value = str(text or "").strip()
        if not value or len(value) > max_chars:
            return False
        return any(pronoun in value for pronoun in DEICTIC_PRONOUNS)

    @staticmethod
    def _extract_url(text: str) -> str:
        match = _URL_PATTERN.search(text or "")
        return match.group(0).rstrip("。，、)）]】>") if match else ""

    @staticmethod
    def _extract_title(text: str, url: str) -> str:
        body = str(text or "")
        titled = _TITLE_PATTERN.search(body)
        if titled:
            title = titled.group(1).strip()
            if title:
                return title[:120]
        body = body.replace(url, " ")
        for raw_line in body.splitlines():
            line = re.sub(
                r"^[\[【（(]?\s*(?:链接|分享|网页|新闻|文章|视频)\s*[\]】）)]?\s*",
                "",
                raw_line.strip(),
            )
            line = line.strip().strip("【】[]()（）《》<>\"'").strip()
            if not line:
                continue
            if line.lower().startswith(
                ("http", "摘要", "一句话总结", "关键要点", "📖", "🔑")
            ):
                continue
            return line[:120]
        return ""

    def build_adjacent_link_block(
        self,
        messages: List[Dict[str, Any]],
        lookback: int = 10,
    ) -> str:
        """Render a citable block for the nearest link/summary message (R-B1).

        Returns an empty string when the recent window has neither a link nor a
        bot summary, so ordinary chat is never polluted.
        """
        if not messages:
            return ""
        window = list(messages)[-max(1, int(lookback or 1)):] if lookback else []
        url = ""
        title = ""
        summary = ""
        for message in reversed(window):
            content = str(message.get("content") or "").strip()
            if not content:
                continue
            found_url = self._extract_url(content)
            is_summary = bool(message.get("is_bot")) and any(
                marker in content for marker in BOT_SUMMARY_MARKERS
            )
            if is_summary and not summary:
                summary = content
                if not title:
                    titled = _TITLE_PATTERN.search(content)
                    if titled and titled.group(1).strip():
                        title = titled.group(1).strip()[:120]
            if found_url and not url:
                url = found_url
                if not title:
                    title = self._extract_title(content, found_url)
        if not (url or summary):
            return ""
        parts = [_ADJACENT_LINK_BLOCK_HEADER]
        if title:
            parts.append(f"标题: {title}")
        if url:
            parts.append(f"URL: {url}")
        if summary:
            parts.append(f"摘要: {self.truncate_message_content(summary)}")
        return " / ".join(parts)

    def format_messages(self, messages: List[Dict[str, Any]]) -> str:
        if not messages:
            return "（暂无近期原始聊天记录）"

        lines = []
        for message in messages:
            sender = message.get("sender", "User")
            content = message.get("content", "")
            time_str = message.get("time", "")
            cursor = message.get("_log_cursor")
            prefix = f"[#{cursor}] " if cursor else ""
            if time_str:
                lines.append(f"{prefix}[{time_str}] [{sender}]: {content}")
            else:
                lines.append(f"{prefix}[{sender}]: {content}")
        return "\n".join(lines)

    def select_recent_messages(
        self,
        messages: List[Dict[str, Any]],
        token_budget: int,
    ) -> Tuple[List[Dict[str, Any]], int]:
        """Take the newest messages that fit while preserving their order."""
        if token_budget <= 0 or not messages:
            return [], 0

        selected: List[Dict[str, Any]] = []
        used = 0
        for message in reversed(messages):
            cost = self.estimate_message_tokens(message)
            if selected and used + cost > token_budget:
                break
            selected.append(message)
            used += cost
            if used >= token_budget:
                break

        selected.reverse()
        return selected, used

    def select_prefix_messages(
        self,
        messages: List[Dict[str, Any]],
        token_budget: int,
    ) -> Tuple[List[Dict[str, Any]], int]:
        """Take the oldest contiguous prefix for cursor-based ingestion."""
        if token_budget <= 0 or not messages:
            return [], 0

        selected: List[Dict[str, Any]] = []
        used = 0
        for message in messages:
            cost = self.estimate_message_tokens(message)
            if selected and used + cost > token_budget:
                break
            selected.append(message)
            used += cost
            if used >= token_budget:
                break
        return selected, used
