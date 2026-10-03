"""消息类型定义。"""

from __future__ import annotations

import glob
import logging
import os
import re
import shutil
import sqlite3
import tempfile
import threading
import time
import uuid
import json
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qsl, urlsplit

from mabowx.core import uia
from mabowx.core.clipboard import clear, get_text, read_files, set_text
from mabowx.core.win32 import (
    force_foreground,
    get_foreground_window,
    is_window,
    get_window_info,
    post_close_message,
    post_left_click,
    post_right_click,
)
from mabowx.core.locks import ui_transaction, uilock
from mabowx.logger import wxlog
from mabowx.param import WxParam
from mabowx.utils.tools import (
    find_content_center,
    find_quote_media_center,
    quote_media_fallback_point,
)

from .base import BaseMessage, HumanMessage
from .media_diagnostics import (
    media_download_trace,
    media_event,
    media_foreground_snapshot,
    media_ui_snapshot,
)
from .identity import (
    FILE_COLOR_MAX_DISTANCE,
    FILE_DETAIL_MAX_DISTANCE,
    FILE_MATCH_PROFILE,
    FILE_MATCH_THRESHOLDS,
    FILE_VISUAL_MAX_DISTANCE,
    MediaFileMismatchError,
    MediaIdentityError,
    compare_target_to_file,
    control_fully_visible,
    select_media_candidate,
    verify_dispatched_candidate,
    verify_stable_candidate,
)


THUMBNAIL_COPY_TIMEOUT_SEC = 3.0
THUMBNAIL_COPY_MAX_ATTEMPTS = 1
PREPARED_MEDIA_CREDENTIAL_TTL_SEC = 1.5
MEDIA_PREVIEW_OPEN_TIMEOUT_SEC = 6.0
TICKLE_CONTENT_PATTERN = re.compile(
    r"^\s*(?P<actor>.+?)\s*拍了拍\s*(?P<target>.+?)\s*$"
)
TICKLE_QUOTE_PAIRS = {
    '"': '"',
    "'": "'",
    "“": "”",
    "‘": "’",
}


def _run_ordered_media_operation(message, operation):
    """Sequence a delayed media action before entering the global UI lock."""
    with media_download_trace(message):
        media_event("queued")

        def tracked_operation():
            media_event("ui_acquired")
            return operation()

        try:
            parent = getattr(message, "parent", None)
            runner = getattr(parent, "run_ordered_media_operation", None)
            if callable(runner):
                result = runner(message, tracked_operation)
            else:
                with ui_transaction(timeout=120.0):
                    result = tracked_operation()
        except Exception as exc:
            media_event("failed", level="warning", error_type=type(exc).__name__, error=str(exc))
            raise
        media_event("completed")
        return result


class SystemMessage(BaseMessage):
    type = "system"
    attr = "system"


def _strip_tickle_quotes(value: str) -> str:
    value = str(value or "").strip()
    if len(value) >= 2 and TICKLE_QUOTE_PAIRS.get(value[0]) == value[-1]:
        return value[1:-1].strip()
    return value


def parse_tickle_content(content: str) -> dict[str, str] | None:
    """解析微信“拍一拍”系统提示，返回拍人者、被拍者及后缀。

    微信 4.1.12 群聊真机样本为 ``"Gerry" 拍了拍 "刘局"``。
    同时兼容弯引号、无引号的“拍了拍我/你/自己”和目标后的自定义后缀。
    普通文本只包含“拍了拍”但缺少两侧主体时不会命中。
    """
    match = TICKLE_CONTENT_PATTERN.fullmatch(str(content or ""))
    if match is None:
        return None
    actor = _strip_tickle_quotes(match.group("actor"))
    target_text = str(match.group("target") or "").strip()
    target = ""
    suffix = ""
    if target_text:
        closing = TICKLE_QUOTE_PAIRS.get(target_text[0])
        if closing:
            closing_index = target_text.find(closing, 1)
            if closing_index > 0:
                target = target_text[1:closing_index].strip()
                suffix = target_text[closing_index + 1 :].strip()
        if not target:
            for pronoun in ("自己", "我", "你"):
                if target_text.startswith(pronoun):
                    target = pronoun
                    suffix = target_text[len(pronoun) :].strip()
                    break
        if not target:
            target = _strip_tickle_quotes(target_text)
    if not actor or not target:
        return None
    return {"from": actor, "to": target, "suffix": suffix}


class TickleMessage(SystemMessage):
    """结构化的“拍一拍”系统消息；公开 ``type`` 仍为 ``system``。"""

    is_tickle = True

    def __init__(self, control=None, parent=None) -> None:
        super().__init__(control=control, parent=parent)
        raw = str(getattr(control, "Name", "") or "")
        parsed = parse_tickle_content(raw) or {}
        self.tickle_from = str(parsed.get("from") or "")
        self.tickle_to = str(parsed.get("to") or "")
        self.tickle_suffix = str(parsed.get("suffix") or "")


class TimeMessage(SystemMessage):
    """时间分隔条。按需求统一按系统消息处理。"""

    type = "system"
    is_time = True


class OfficialMessage(BaseMessage):
    type = "official"
    attr = "system"


def _preview_identity_is_safe(control, hwnd: int, expected_pid: int | None) -> bool:
    """Verify that a control is still the exact preview window we opened."""
    try:
        actual_hwnd = int(getattr(control, "NativeWindowHandle", 0) or 0)
        actual_pid = int(getattr(control, "ProcessId", 0) or 0)
        return bool(
            hwnd
            and actual_hwnd == hwnd
            and str(getattr(control, "ClassName", "") or "")
            == "mmui::PreviewWindow"
            and (not expected_pid or actual_pid == int(expected_pid))
        )
    except Exception:
        return False


def close_preview_window_safely(
    preview,
    expected_pid: int | None = None,
    wait_timeout: float = 2.0,
) -> bool:
    """Close only the captured media preview HWND.

    A targeted ``WM_CLOSE`` is attempted first.  ``Ctrl+W`` is allowed only if
    that exact HWND is still alive, can be rebound with the same class/PID, and
    is confirmed as the foreground window.  This prevents a delayed cleanup
    from ever closing a chat, the main WeChat window, or an unrelated browser.
    """
    try:
        hwnd = int(getattr(preview, "NativeWindowHandle", 0) or 0)
    except Exception:
        return False
    if not _preview_identity_is_safe(preview, hwnd, expected_pid):
        wxlog.warning("引用媒体预览窗口身份校验失败，已取消关闭")
        return False

    info = get_window_info(hwnd)
    if info is None or (expected_pid and info.pid != expected_pid):
        return False
    # UIA exposes mmui::PreviewWindow, while Win32 exposes Qt*WindowIcon.
    # The native class is permitted only after the exact UIA/PID/HWND proof.
    if not info.visible:
        media_event("preview_cleanup", route="already_hidden", hwnd=hwnd, outcome="closed")
        return True
    started = time.monotonic()
    native_posted = post_close_message(hwnd, {info.class_name})
    media_event("preview_cleanup", route="wm_close", hwnd=hwnd,
                native_class=info.class_name, posted=native_posted)
    if native_posted:
        deadline = time.monotonic() + max(0.0, wait_timeout)
        while time.monotonic() < deadline:
            current_info = get_window_info(hwnd)
            if not is_window(hwnd) or (current_info and not current_info.visible):
                media_event("preview_cleanup", route="wm_close", hwnd=hwnd,
                            outcome="closed", close_ms=round((time.monotonic() - started) * 1000))
                return True
            time.sleep(0.08)
    if not is_window(hwnd):
        return True

    # WM_CLOSE did not destroy the preview. Rebind and re-check identity before
    # the keyboard fallback; never send Ctrl+W to whichever window is current.
    try:
        current = uia.control_from_handle(hwnd)
    except Exception as exc:
        wxlog.warning(f"引用媒体预览窗口重新绑定失败，已取消 Ctrl+W: {exc}")
        return False
    if not _preview_identity_is_safe(current, hwnd, expected_pid):
        wxlog.warning("引用媒体预览窗口身份已变化，已取消 Ctrl+W")
        return False
    if not force_foreground(hwnd) or get_foreground_window() != hwnd:
        wxlog.warning("引用媒体预览窗口无法确认前台身份，已取消 Ctrl+W")
        return False
    try:
        current.SendKeys("{Ctrl}w", waitTime=0.5)
    except Exception as exc:
        wxlog.warning(f"引用媒体预览窗口 Ctrl+W 关闭失败: {exc}")
        return False

    deadline = time.monotonic() + max(0.5, wait_timeout)
    while time.monotonic() < deadline:
        current_info = get_window_info(hwnd)
        if not is_window(hwnd) or (current_info and not current_info.visible):
            media_event("preview_cleanup", route="ctrl_w", hwnd=hwnd, outcome="closed")
            return True
        time.sleep(0.08)
    return not is_window(hwnd)


# Accessed only while holding the cross-process UI transaction. A lease is
# never reconstructed from a persisted HWND: after restart, unknown previews
# belong to the user until proven otherwise.
_owned_media_previews: dict[int, dict] = {}
_preview_property = "Mabobot.MediaPreview." + uuid.uuid4().hex


def _preview_marker(hwnd, value=None):
    """An OS window property disappears on destruction, even if HWND is reused."""
    try:
        import ctypes
        from ctypes import wintypes
        api = ctypes.windll.user32
        if value is None:
            api.GetPropW.argtypes = [wintypes.HWND, wintypes.LPCWSTR]
            api.GetPropW.restype = wintypes.HANDLE
            return int(api.GetPropW(hwnd, _preview_property) or 0)
        if value == 0:
            api.RemovePropW.argtypes = [wintypes.HWND, wintypes.LPCWSTR]
            api.RemovePropW.restype = wintypes.HANDLE
            return int(api.RemovePropW(hwnd, _preview_property) or 0)
        api.SetPropW.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.HANDLE]
        api.SetPropW.restype = wintypes.BOOL
        return bool(api.SetPropW(hwnd, _preview_property, value))
    except Exception:
        return 0


def _preview_fingerprint(control):
    try:
        return (int(control.NativeWindowHandle), int(control.ProcessId),
                str(control.ClassName), tuple(control.GetRuntimeId()))
    except Exception:
        return None


def _remember_preview(control):
    fingerprint = _preview_fingerprint(control)
    token = (uuid.uuid4().int & 0x7fffffff) or 1
    if fingerprint and _preview_marker(fingerprint[0], token):
        _owned_media_previews[fingerprint[0]] = {
            "fingerprint": fingerprint, "active": True, "retry_at": 0.0,
            "owner_token": token,
        }


def _release_preview(control, pid):
    hwnd = int(getattr(control, "NativeWindowHandle", 0) or 0)
    lease = _owned_media_previews.get(hwnd)
    if lease and _preview_marker(hwnd) != lease['owner_token']:
        _owned_media_previews.pop(hwnd, None)
        return not is_window(hwnd)
    try:
        closed = close_preview_window_safely(control, expected_pid=pid)
    except Exception as exc:
        closed = False
        media_event("preview_cleanup", level="warning", hwnd=hwnd, error=str(exc)[:240])
    if closed:
        _preview_marker(hwnd, 0)
        _owned_media_previews.pop(hwnd, None)
    elif lease:
        lease.update(active=False, retry_at=time.monotonic() + 1)
        media_event("preview_cleanup", level="warning", hwnd=hwnd, outcome="pending_recovery")
    return closed


def _preview_baseline(pid):
    """Recover only a verified preview left by one of our finished operations."""
    controls = uia.find_top_level_controls(class_name="mmui::PreviewWindow", pid=pid, max_results=10)
    baseline = set()
    owned_busy = False
    seen = set()
    for control in controls:
        hwnd = int(getattr(control, "NativeWindowHandle", 0) or 0)
        if not hwnd:
            continue
        seen.add(hwnd)
        info = get_window_info(hwnd)
        if info is not None and not info.visible:
            continue
        lease = _owned_media_previews.get(hwnd)
        if lease and (lease["fingerprint"] != _preview_fingerprint(control)
                      or _preview_marker(hwnd) != lease['owner_token']):
            _owned_media_previews.pop(hwnd, None)
            lease = None
        if lease and not lease["active"]:
            owned_busy = True
            if time.monotonic() >= lease["retry_at"]:
                lease["retry_at"] = time.monotonic() + 5
                if _release_preview(control, pid):
                    media_event("preview_recovered", hwnd=hwnd)
                    continue
                lease["retry_at"] = time.monotonic() + 5
        baseline.add(hwnd)
    for hwnd in list(_owned_media_previews):
        if hwnd not in seen and not is_window(hwnd):
            _owned_media_previews.pop(hwnd, None)
    if baseline:
        code = "preview_cleanup_failed" if owned_busy else "preview_busy_unowned"
        media_event("preview_baseline_rejected", level="warning", baseline_hwnds=sorted(baseline), error_code=code)
        raise MediaIdentityError("图片预览窗口被占用，请关闭预览后重试", code=code)
    return baseline


def _wait_for_media_preview(
    pid: int | None,
    deadline: float,
    retry_click: Callable[[], bool] | None = None,
    retry_after: float = 2.0,
    baseline_hwnds: set[int] | None = None,
):
    """Wait for the preview and optionally repeat one identity-safe click.

    WeChat occasionally accepts a click without creating ``PreviewWindow``.
    A quote caller may therefore supply ``retry_click``; it is invoked at most
    once and is responsible for rebinding and validating the exact quote row.
    Ordinary image/video downloads keep their existing single-click behavior.
    """
    baseline = {int(hwnd or 0) for hwnd in (baseline_hwnds or set()) if int(hwnd or 0)}
    started = time.monotonic()
    polls = 0
    query_errors = 0
    last_query_error = None
    pid_mismatches = 0
    last_candidates = []
    retry_result = "not_attempted" if retry_click else "not_configured"

    def report(outcome):
        media_event(
            "preview_wait", level="info" if outcome == "opened" else "warning",
            outcome=outcome, polls=polls, wait_ms=round((time.monotonic() - started) * 1000),
            budget_ms=round(max(0.0, deadline - started) * 1000),
            baseline_hwnds=sorted(baseline), candidates=last_candidates,
            query_errors=query_errors, last_query_error=last_query_error,
            pid_mismatches=pid_mismatches,
            retry_result=retry_result,
        )

    retry_at = time.monotonic() + max(0.3, retry_after)
    retried = retry_click is None
    while time.monotonic() < deadline:
        query_diagnostics = {}
        controls = uia.find_top_level_controls(
            class_name="mmui::PreviewWindow",
            pid=pid,
            max_results=5,
            diagnostics=query_diagnostics,
        )
        polls += 1
        query_errors += query_diagnostics.get("property_errors", 0)
        pid_mismatches += query_diagnostics.get("pid_mismatches", 0)
        if query_diagnostics.get("enumeration_error"):
            query_errors += 1
        if query_diagnostics.get("last_error"):
            last_query_error = query_diagnostics["last_error"]
        last_candidates = []
        new_controls = []
        for control in controls:
            try:
                hwnd = int(getattr(control, "NativeWindowHandle", 0) or 0)
            except Exception as exc:
                last_candidates.append({"rejected": "unreadable_handle", "error": str(exc)[:240]})
                continue
            safe = bool(hwnd and hwnd not in baseline and _preview_identity_is_safe(control, hwnd, pid))
            info = get_window_info(hwnd) if safe else None
            safe = bool(safe and info is not None and info.visible and (not pid or info.pid == pid))
            last_candidates.append({"hwnd": hwnd, "baseline": hwnd in baseline, "identity_safe": safe})
            if safe:
                new_controls.append(control)
        if len(new_controls) == 1:
            report("opened")
            _remember_preview(new_controls[0])
            return new_controls[0]
        if len(new_controls) > 1:
            report("ambiguous")
            raise MediaIdentityError(
                f"点击后同时出现 {len(new_controls)} 个新媒体预览窗口，拒绝猜测"
            )
        now = time.monotonic()
        if not retried and now >= retry_at:
            retried = True
            try:
                if retry_click and retry_click():
                    retry_result = "clicked"
                    wxlog.info("引用媒体预览未出现，已对同一引用消息安全重试一次")
                else:
                    retry_result = "identity_unconfirmed"
                    wxlog.warning("引用媒体预览未出现，且无法重新确认同一引用消息")
            except Exception as exc:
                retry_result = f"{type(exc).__name__}: {exc}"[:240]
                wxlog.warning(f"引用媒体预览安全重试失败: {exc}")
        time.sleep(0.3)
    report("timeout")
    return None


def download_media_via_preview(
    message: "HumanMessage",
    dir_path: str | Path | None = None,
    timeout: int = 30,
    already_clicked: bool = False,
    retry_click: Callable[[], bool] | None = None,
    baseline_preview_hwnds: set[int] | None = None,
) -> Path:
    """左键单击媒体消息打开 PreviewWindow，然后高频右键复制路径。

    微信在打开预览窗口时开始下载媒体；加载完成前右键菜单里没有
    “复制”。因此循环：右键 -> 找“复制” -> 没有就左键关闭菜单重试。
    成功返回微信本地文件路径；若指定 dir_path 则复制到该目录。
    """
    pid = getattr(message.parent, "pid", None)
    media_event(
        "preview_baseline", download_timeout_sec=timeout,
        preview_timeout_sec=MEDIA_PREVIEW_OPEN_TIMEOUT_SEC,
        already_clicked=already_clicked, retry_enabled=retry_click is not None,
    )
    if baseline_preview_hwnds is None:
        baseline_preview_hwnds = _preview_baseline(pid)
    if baseline_preview_hwnds:
        media_event("preview_baseline_rejected", level="warning", baseline_hwnds=sorted(baseline_preview_hwnds))
        raise MediaIdentityError(
            "媒体操作开始前已有预览窗口，无法证明新预览属于当前消息"
        )

    if not already_clicked:
        media_event("prepare_target")
        prepare = getattr(message, "prepare_for_media_action", None)
        if callable(prepare):
            prepare()
        prepared_point = getattr(message, "prepared_media_action_point", None)
        point = (
            prepared_point()
            if callable(prepared_point)
            else find_content_center(message.control) or message._bias()
        )
        if point is None:
            raise RuntimeError("无法定位媒体消息")
        prepared_click = getattr(message, "click_prepared_media", None)
        media_event(
            "dispatch_click", point=point,
            route="prepared_hwnd" if callable(prepared_click) else "screen",
            prepared_row_rect=getattr(message, "_prepared_media_row_rect", None),
        )
        if callable(prepared_click):
            prepared_click(point, right=False)
            media_event("click_returned", foreground=media_foreground_snapshot())
            time.sleep(0.6)
        else:
            uia.click_screen(point[0], point[1], wait=0.6)
            media_event("click_returned", foreground=media_foreground_snapshot())

    deadline = time.monotonic() + timeout
    # Creating PreviewWindow should be quick even when the media itself still
    # needs time to load.  Do not hold the global WeChat UI transaction for the
    # full download timeout when the click did not open any preview at all.
    preview_deadline = min(
        deadline,
        time.monotonic() + MEDIA_PREVIEW_OPEN_TIMEOUT_SEC,
    )
    preview = _wait_for_media_preview(
        pid,
        preview_deadline,
        retry_click=retry_click,
        baseline_hwnds=baseline_preview_hwnds,
    )
    if preview is None:
        media_event("preview_timeout_snapshot", level="warning", snapshot=media_ui_snapshot(message))
        raise MediaIdentityError("媒体预览窗口未打开", code="preview_open_timeout")

    try:
        media_event("validate_preview_origin")
        validate_origin = getattr(message, "validate_dispatched_media_origin", None)
        if callable(validate_origin):
            validate_origin()
        preview_hwnd = int(getattr(preview, "NativeWindowHandle", 0) or 0)

        # WeChat 4.x exposes media copying through the preview toolbar's
        # “更多” popup. The popup is a top-level mmui::XMenu whose native owner
        # remains the preview HWND; it is not a descendant of PreviewWindow and
        # it never becomes the Win32 foreground window. Prefer that proven
        # route before retaining the older preview right-click fallback.
        more_deadline = min(
            deadline,
            time.monotonic() + min(10.0, max(2.0, (deadline - time.monotonic()) * 0.5)),
        )
        media_event("copy_more_menu", preview_hwnd=preview_hwnd)
        try:
            return _copy_media_from_preview_more_menu(
                message,
                preview,
                preview_hwnd=preview_hwnd,
                expected_pid=pid,
                deadline=more_deadline,
                dir_path=dir_path,
            )
        except MediaIdentityError:
            raise
        except Exception as exc:
            wxlog.warning(f"预览工具栏复制失败，回退预览右键: {exc}")
            media_event("copy_context_menu", level="warning", error_type=type(exc).__name__, error=str(exc))

        while time.monotonic() < deadline:
            if not _preview_identity_is_safe(preview, preview_hwnd, pid):
                raise MediaIdentityError("媒体预览窗口身份在下载期间发生变化")
            rect = preview.BoundingRectangle
            center_x = int(rect.left + (rect.right - rect.left) // 2)
            center_y = int(rect.top + (rect.bottom - rect.top) * 0.55)
            clear()
            uia.right_click_screen(center_x, center_y, wait=0.5)
            time.sleep(0.7)
            copy_items = uia.find_controls(
                preview,
                control_type="MenuItemControl",
                name="复制",
                max_results=5,
            )
            if copy_items:
                copy_items[0].Click(simulateMove=False, waitTime=0.5)
                time.sleep(0.7)
                paths = read_files()
                if paths:
                    src = Path(paths[0])
                    if src.exists():
                        if dir_path is None:
                            return src
                        target_dir = Path(dir_path)
                        target_dir.mkdir(parents=True, exist_ok=True)
                        target = target_dir / src.name
                        shutil.copy2(src, target)
                        return target
            # 左键点击预览区域关闭右键菜单，准备下一轮重试。
            try:
                uia.click_screen(center_x, center_y, wait=0.3)
            except Exception:
                pass
            time.sleep(0.4)
        raise RuntimeError("媒体加载完成前未能复制到路径")
    finally:
        if not _release_preview(preview, pid):
            wxlog.warning("引用/媒体预览窗口未确认关闭；未向其他窗口发送快捷键")


def _copy_media_from_preview_more_menu(
    message: "HumanMessage",
    preview,
    *,
    preview_hwnd: int,
    expected_pid: int | None,
    deadline: float,
    dir_path: str | Path | None = None,
) -> Path:
    """Copy one previewed media file through its owner-bound “更多” menu."""

    from mabowx.ui.component import Menu

    if not _preview_identity_is_safe(preview, preview_hwnd, expected_pid):
        raise MediaIdentityError("媒体预览窗口身份无法确认")
    more_buttons = uia.find_controls(
        preview,
        control_type="ButtonControl",
        name="更多",
        max_results=3,
        max_nodes=500,
    )
    visible_buttons = []
    for button in more_buttons:
        try:
            rect = button.BoundingRectangle
            if (
                button.Exists(0)
                and not bool(getattr(button, "IsOffscreen", False))
                and int(rect.right) > int(rect.left)
                and int(rect.bottom) > int(rect.top)
            ):
                visible_buttons.append(button)
        except Exception:
            continue
    if len(visible_buttons) != 1:
        raise RuntimeError(f"媒体预览“更多”按钮数量异常: {len(visible_buttons)}")

    more_button = visible_buttons[0]
    last_error = ""
    while time.monotonic() < deadline:
        if not _preview_identity_is_safe(preview, preview_hwnd, expected_pid):
            raise MediaIdentityError("媒体预览窗口身份在复制期间发生变化")
        if get_foreground_window() != preview_hwnd:
            if not force_foreground(preview_hwnd) or get_foreground_window() != preview_hwnd:
                raise MediaIdentityError("媒体预览窗口无法安全切到前台")

        try:
            rect = more_button.BoundingRectangle
            anchor = (
                (int(rect.left) + int(rect.right)) // 2,
                (int(rect.top) + int(rect.bottom)) // 2,
            )
        except Exception as exc:
            raise RuntimeError(f"无法定位媒体预览“更多”按钮: {exc}") from exc

        baseline_hwnds: set[int] = set()
        for existing in uia.find_top_level_controls(
            class_name="mmui::XMenu",
            pid=expected_pid,
            max_results=10,
        ):
            try:
                hwnd = int(getattr(existing, "NativeWindowHandle", 0) or 0)
            except Exception:
                hwnd = 0
            if hwnd:
                baseline_hwnds.add(hwnd)

        clear()
        more_button.Click(simulateMove=False, waitTime=0.2)
        menu = Menu(
            message.parent,
            timeout=min(1.2, max(0.1, deadline - time.monotonic())),
            anchor=anchor,
            baseline_hwnds=baseline_hwnds,
            require_new=True,
            expected_owner_hwnd=preview_hwnd,
        )
        try:
            if not _preview_identity_is_safe(preview, preview_hwnd, expected_pid):
                raise MediaIdentityError("打开复制菜单后媒体预览身份发生变化")
            response = menu.select("复制")
            if not response.is_success:
                last_error = response["message"]
            else:
                clipboard_deadline = min(deadline, time.monotonic() + 1.5)
                while time.monotonic() < clipboard_deadline:
                    paths = read_files()
                    if paths:
                        src = Path(paths[0])
                        if src.exists():
                            if dir_path is None:
                                return src
                            target_dir = Path(dir_path)
                            target_dir.mkdir(parents=True, exist_ok=True)
                            target = target_dir / src.name
                            try:
                                if src.resolve() == target.resolve():
                                    return src
                            except Exception:
                                pass
                            shutil.copy2(src, target)
                            return target
                        last_error = f"路径不存在: {src}"
                        break
                    time.sleep(0.05)
                if not last_error:
                    last_error = "复制后剪贴板未出现文件路径"
        finally:
            try:
                menu.close()
            except Exception:
                pass
        time.sleep(0.2)

    raise RuntimeError(f"预览菜单获取媒体文件失败: {last_error or '超时'}")


def download_via_copy_menu(
    message: "HumanMessage",
    dir_path: str | Path | None = None,
    timeout: int = 30,
    max_attempts: int | None = None,
) -> Path:
    """右键媒体消息 -> 复制，从 CF_HDROP 剪贴板读取路径并复制文件。"""
    from mabowx.ui.component import Menu

    deadline = time.monotonic() + timeout
    last_error = ""
    attempts = 0
    while time.monotonic() < deadline and (
        max_attempts is None or attempts < max(1, int(max_attempts))
    ):
        attempts += 1
        prepare = getattr(message, "prepare_for_media_action", None)
        if callable(prepare):
            prepare()
        else:
            activate = getattr(message, "_activate_source_window", None)
            if callable(activate) and not activate():
                raise RuntimeError("媒体消息所属聊天窗口未能安全激活")
        clear()
        prepared_point = getattr(message, "prepared_media_action_point", None)
        strict_media_menu = callable(prepared_point)
        point = (
            prepared_point()
            if strict_media_menu
            else find_content_center(message.control) or message._bias()
        )
        if point is None:
            raise RuntimeError("无法定位媒体消息")
        baseline_hwnds: set[int] = set()
        if strict_media_menu:
            parent_pid = getattr(message.parent, "pid", None)
            try:
                existing_menus = uia.find_top_level_controls(
                    class_name="mmui::XMenu",
                    pid=parent_pid,
                    max_results=10,
                )
            except Exception:
                existing_menus = []
            for existing in existing_menus:
                try:
                    hwnd = int(getattr(existing, "NativeWindowHandle", 0) or 0)
                except Exception:
                    hwnd = 0
                if hwnd:
                    baseline_hwnds.add(hwnd)
        prepared_click = getattr(message, "click_prepared_media", None)
        if callable(prepared_click):
            prepared_click(point, right=True)
            time.sleep(0.4)
        else:
            uia.right_click_screen(point[0], point[1], wait=0.4)
        time.sleep(0.2)
        menu_kwargs = (
            {
                "baseline_hwnds": baseline_hwnds,
                "require_new": True,
                "expected_owner_hwnd": int(
                    message._target_identity().chat_hwnd or 0
                ),
            }
            if strict_media_menu
            else {}
        )
        menu = Menu(
            message.parent,
            timeout=min(0.8, max(0.1, deadline - time.monotonic())),
            anchor=point,
            **menu_kwargs,
        )
        try:
            validate_origin = getattr(message, "validate_dispatched_media_origin", None)
            if callable(validate_origin):
                validate_origin()
            response = menu.select("复制")
        except Exception:
            try:
                menu.close()
            except Exception:
                pass
            raise
        if not response.is_success:
            last_error = response["message"]
            try:
                menu.close()
            except Exception:
                pass
            time.sleep(0.2)
            continue
        last_error = ""
        clipboard_deadline = min(deadline, time.monotonic() + 1.2)
        while time.monotonic() < clipboard_deadline:
            paths = read_files()
            if paths:
                src = Path(paths[0])
                if src.exists():
                    # 微信的“复制”已经把缓存文件的真实路径放入
                    # CF_HDROP。调用方没有指定目录时直接返回该路径，
                    # 与预览下载路径保持一致，也避免无意义的二次复制。
                    if dir_path is None:
                        return src
                    target_dir = Path(dir_path)
                    target_dir.mkdir(parents=True, exist_ok=True)
                    target = target_dir / src.name
                    try:
                        if src.resolve() == target.resolve():
                            return src
                    except Exception:
                        pass
                    shutil.copy2(src, target)
                    return target
                last_error = f"路径不存在: {src}"
                break
            time.sleep(0.05)
        if not last_error:
            last_error = "复制后剪贴板未出现文件路径"
        time.sleep(0.2)
    raise RuntimeError(f"获取媒体文件失败: {last_error or '超时'}")


class TextMessage(HumanMessage):
    type = "text"


class VoiceMessage(HumanMessage):
    type = "voice"


QUOTE_PATTERN = re.compile(
    r"^(.*)\s*引用\s+(.+?)\s+的消息\s*[:：]\s*(.*)$",
    re.DOTALL,
)


class QuoteMessage(HumanMessage):
    type = "quote"

    def __init__(self, control=None, parent=None) -> None:
        super().__init__(control=control, parent=parent)
        raw = str(getattr(control, "Name", "") or "")
        match = QUOTE_PATTERN.match(raw)
        if match:
            self.content = match.group(1).strip()
            self.quote_nickname = match.group(2).strip()
            self.quote_content = match.group(3).strip()
        else:
            self.content = raw
            self.quote_nickname = ""
            self.quote_content = ""

    @staticmethod
    def _control_is_clickable(control) -> bool:
        if control is None:
            return False
        try:
            rect = control.BoundingRectangle
            if int(rect.right - rect.left) <= 0 or int(rect.bottom - rect.top) <= 0:
                return False
            return bool(control.Exists(0))
        except Exception:
            return False

    def _same_quote(self, candidate) -> bool:
        return bool(
            getattr(candidate, "type", "") == self.type
            and getattr(candidate, "content", "") == self.content
            and getattr(candidate, "quote_nickname", "") == self.quote_nickname
            and getattr(candidate, "quote_content", "") == self.quote_content
            and (
                not getattr(self, "sender", "")
                or not getattr(candidate, "sender", "")
                or getattr(candidate, "sender", "") == self.sender
            )
        )

    def _refresh_visible_control(self) -> bool:
        """Rebind a delayed quote download to the exact visible quote row.

        Message controls are virtualized by WeChat.  If the cached control is
        stale, bind only when the visible quote is unambiguous; choosing the
        latest similar quote could download the wrong image.
        """
        self._quote_lookup_failure = ""
        getter = getattr(self.parent, "get_messages", None)
        if not callable(getter):
            return self._control_is_clickable(self.control)
        try:
            candidates = [
                candidate
                for candidate in getter()
                if self._same_quote(candidate)
                and self._control_is_clickable(getattr(candidate, "control", None))
            ]
        except Exception as exc:
            self._quote_lookup_failure = f"visible_read_error:{type(exc).__name__}"
            wxlog.warning(f"刷新引用消息控件失败: {exc}")
            return False
        if not candidates:
            self._quote_lookup_failure = "quote_not_visible_or_not_clickable"
            return False

        original_id = str(getattr(self, "id", "") or "")
        exact_id = [
            candidate
            for candidate in candidates
            if original_id and str(getattr(candidate, "id", "") or "") == original_id
        ]
        if len(exact_id) == 1:
            chosen = exact_id[0]
        elif len(candidates) == 1:
            chosen = candidates[0]
        else:
            self._quote_lookup_failure = f"ambiguous_quote:count={len(candidates)}"
            wxlog.warning(
                "可见区存在多条无法区分的相同引用消息，已拒绝猜测引用图片: "
                f"count={len(candidates)} content={self.content[:60]!r}"
            )
            return False
        self.control = chosen.control
        self.parent = chosen.parent
        self.control_class_name = getattr(chosen, "control_class_name", "")
        self.direction = getattr(chosen, "direction", self.direction)
        return True

    def click_quote(self) -> bool:
        """点击引用消息，打开被引用图片/视频的预览窗口。"""
        if not self._refresh_visible_control():
            wxlog.warning(
                f"引用媒体定位失败: reason={getattr(self, '_quote_lookup_failure', 'unknown')} "
                f"delivery_id={getattr(self, 'delivery_id', None)} runtime_id={self.id}"
            )
            return False
        if not self._activate_source_window():
            raise RuntimeError("引用消息所属聊天窗口未能安全激活，已取消点击")
        point = find_quote_media_center(self.control, self.direction)
        source = "thumbnail"
        if point is None:
            point = quote_media_fallback_point(self.control, self.direction)
            source = "geometry"
        if point is None:
            self._quote_lookup_failure = "quote_click_point_unavailable"
            wxlog.warning(f"引用媒体点击位置不可用: runtime_id={self.id} direction={self.direction}")
            return False
        wxlog.debug(
            f"点击引用媒体: source={source} direction={self.direction} point={point}"
        )
        uia.click_screen(point[0], point[1], wait=0.6)
        return True

    def download_quote_media(self, dir_path: str | Path | None = None, timeout: int = 30) -> Path:
        """下载引用消息中的图片/视频。"""
        return _run_ordered_media_operation(
            self,
            lambda: self._download_quote_image_unlocked(
                dir_path=dir_path,
                timeout=timeout,
            ),
        )

    def download_quote_image(self, dir_path: str | Path | None = None, timeout: int = 30) -> Path:
        """保留旧公开 API；底层与引用视频共用媒体下载路径。"""
        return self.download_quote_media(dir_path=dir_path, timeout=timeout)

    def _download_quote_image_unlocked(
        self,
        dir_path: str | Path | None = None,
        timeout: int = 30,
    ) -> Path:
        pid = getattr(self.parent, "pid", None)
        baseline = _preview_baseline(pid)
        if not self.click_quote():
            raise RuntimeError(f"无法点击引用消息: {getattr(self, '_quote_lookup_failure', '') or 'unknown'}")
        return download_media_via_preview(
            self,
            dir_path=dir_path,
            timeout=timeout,
            already_clicked=True,
            retry_click=self.click_quote,
            baseline_preview_hwnds=baseline,
        )


class ImageMessage(HumanMessage):
    type = "image"

    def _target_identity(self):
        target = getattr(self, "media_target_identity", None)
        if target is not None:
            return target
        reason = str(getattr(self, "media_target_error", "") or "").strip()
        if reason:
            raise MediaIdentityError(f"接收时图片身份快照失败: {reason}")
        raise MediaIdentityError(
            "图片消息没有监听阶段生成的身份快照，拒绝对缓存 UIA 控件执行点击"
        )

    def _fresh_visible_messages(self) -> list:
        getter = getattr(self.parent, "get_messages", None)
        if not callable(getter):
            raise MediaIdentityError("图片所属聊天不支持重新枚举消息")
        try:
            return list(getter(resolve_group_senders=False, probe_avatar_direction=False) or [])
        except TypeError:
            return list(getter() or [])
        except Exception as exc:
            raise MediaIdentityError(f"重新枚举图片消息失败: {exc}") from exc

    def _bind_fresh_candidate(self):
        target = self._target_identity()
        current_chat = str(getattr(self.parent, "who", "") or "").strip()
        if target.chat_name and current_chat and current_chat != target.chat_name:
            raise MediaIdentityError(
                f"聊天窗口已从 {target.chat_name!r} 变为 {current_chat!r}"
            )
        visible = self._fresh_visible_messages()
        candidate = select_media_candidate(target, visible)
        self.control = candidate.control
        self.parent = candidate.parent
        self.control_class_name = getattr(candidate, "control_class_name", "")
        self.direction = getattr(candidate, "direction", self.direction)
        return candidate

    def prepare_for_media_action(self) -> bool:
        """Rebind and double-check the exact thumbnail immediately before click."""

        target = self._target_identity()
        candidate = self._bind_fresh_candidate()
        message_list = getattr(self.parent, "message_list", None)
        if not control_fully_visible(candidate.control, message_list):
            try:
                pattern = candidate.control.GetScrollItemPattern()
                pattern.ScrollIntoView()
            except Exception as exc:
                raise MediaIdentityError(f"目标图片未完整可见且无法安全滚入: {exc}") from exc
            time.sleep(0.12)
            candidate = self._bind_fresh_candidate()
            message_list = getattr(self.parent, "message_list", None)
            if not control_fully_visible(candidate.control, message_list):
                raise MediaIdentityError("目标图片滚动后仍未完整位于消息视口")

        if not self._activate_source_window():
            raise MediaIdentityError("目标图片所属聊天窗口未能安全切到前台")

        # Bringing the chat to the foreground can rebuild RecyclerListView.
        # Enumerate once more, then require two stable visual/geometry samples.
        candidate = self._bind_fresh_candidate()
        message_list = getattr(self.parent, "message_list", None)
        if not control_fully_visible(candidate.control, message_list):
            raise MediaIdentityError("窗口激活后目标图片离开了完整可见区域")
        visual = verify_stable_candidate(target, candidate)
        if visual.rect is None:
            raise MediaIdentityError("点击前没有取得缩略图的绝对矩形")
        try:
            row = candidate.control.BoundingRectangle
            row_rect = (
                int(row.left),
                int(row.top),
                int(row.right),
                int(row.bottom),
            )
        except Exception as exc:
            raise MediaIdentityError(f"点击前无法读取图片消息矩形: {exc}") from exc
        media_rect = tuple(int(value) for value in visual.rect)
        if not (
            row_rect[0] <= media_rect[0] < media_rect[2] <= row_rect[2]
            and row_rect[1] <= media_rect[1] < media_rect[3] <= row_rect[3]
        ):
            raise MediaIdentityError("缩略图矩形已离开目标消息行")
        self.control = candidate.control
        self._prepared_media_point = (
            (media_rect[0] + media_rect[2]) // 2,
            (media_rect[1] + media_rect[3]) // 2,
        )
        self._prepared_media_row_rect = row_rect
        self._prepared_media_at = time.monotonic()
        return True

    def prepared_media_action_point(self) -> tuple[int, int]:
        """Consume the just-validated thumbnail point unless its row moved."""

        target = self._target_identity()
        point = getattr(self, "_prepared_media_point", None)
        expected_row = getattr(self, "_prepared_media_row_rect", None)
        prepared_at = float(getattr(self, "_prepared_media_at", 0.0) or 0.0)
        if (
            point is None
            or expected_row is None
            or time.monotonic() - prepared_at > PREPARED_MEDIA_CREDENTIAL_TTL_SEC
        ):
            raise MediaIdentityError("缩略图点击凭据不存在或已经过期")
        try:
            if not self.control.Exists(0):
                raise MediaIdentityError("缩略图控件在点击前已失效")
            row = self.control.BoundingRectangle
            current_row = (
                int(row.left),
                int(row.top),
                int(row.right),
                int(row.bottom),
            )
            top = self.control.GetTopLevelControl()
            current_hwnd = int(getattr(top, "NativeWindowHandle", 0) or 0)
        except MediaIdentityError:
            raise
        except Exception as exc:
            raise MediaIdentityError(f"点击前最终布局校验失败: {exc}") from exc
        if current_row != expected_row:
            delta = tuple(
                current - expected
                for current, expected in zip(current_row, expected_row)
            )
            raise MediaIdentityError(
                "缩略图消息行在校验后发生滚动或重排: "
                f"expected={expected_row} current={current_row} delta={delta}", code="target_moved"
            )
        if target.chat_hwnd and current_hwnd != target.chat_hwnd:
            raise MediaIdentityError("缩略图控件在点击前离开原聊天窗口")
        if target.chat_hwnd and get_foreground_window() != target.chat_hwnd:
            raise MediaIdentityError("原聊天窗口在点击前失去前台焦点")
        if not (
            current_row[0] <= point[0] < current_row[2]
            and current_row[1] <= point[1] < current_row[3]
        ):
            raise MediaIdentityError("缩略图点击点已离开目标消息行")
        return int(point[0]), int(point[1])

    def validate_dispatched_media_origin(self) -> bool:
        """Confirm the exact menu/preview still originates from the target row."""

        target = self._target_identity()
        point = getattr(self, "_prepared_media_point", None)
        expected_row = getattr(self, "_prepared_media_row_rect", None)
        if point is None or expected_row is None:
            raise MediaIdentityError("点击投递后缺少原消息几何凭据")
        try:
            if not self.control.Exists(0):
                raise MediaIdentityError("点击投递后原缩略图控件已失效")
            row = self.control.BoundingRectangle
            current_row = (
                int(row.left),
                int(row.top),
                int(row.right),
                int(row.bottom),
            )
        except MediaIdentityError:
            raise
        except Exception as exc:
            raise MediaIdentityError(f"点击投递后无法校验原消息矩形: {exc}") from exc
        if current_row != expected_row:
            delta = tuple(
                current - expected
                for current, expected in zip(current_row, expected_row)
            )
            raise MediaIdentityError(
                "点击投递后原消息行发生滚动或重排: "
                f"expected={expected_row} current={current_row} delta={delta}", code="target_moved"
            )
        visual = verify_dispatched_candidate(target, self)
        media_rect = visual.rect
        if media_rect is None or not (
            media_rect[0] <= int(point[0]) < media_rect[2]
            and media_rect[1] <= int(point[1]) < media_rect[3]
        ):
            raise MediaIdentityError("点击投递后原点击点不再属于目标缩略图")
        return True

    def click_prepared_media(self, point: tuple[int, int], *, right: bool) -> None:
        """Dispatch the validated click only to the captured chat HWND."""

        verified_point = self.prepared_media_action_point()
        normalized_point = int(point[0]), int(point[1])
        if normalized_point != verified_point:
            raise MediaIdentityError("缩略图点击点在投递前发生变化")
        target = self._target_identity()
        if not target.chat_hwnd:
            raise MediaIdentityError("缩略图目标缺少可定向投递的聊天 HWND")
        poster = post_right_click if right else post_left_click
        if not poster(target.chat_hwnd, verified_point[0], verified_point[1]):
            raise MediaIdentityError("无法把缩略图点击定向投递给原聊天窗口")

    def _verify_downloaded_path(self, path: str | Path) -> Path:
        target = self._target_identity()
        target_visual = target.visual
        self._last_media_candidate_path = str(path)
        try:
            result = compare_target_to_file(target, path)
        except Exception as exc:
            raise MediaIdentityError(f"下载图片无法完成身份校验: {exc}") from exc
        self._last_media_verification = {
            "matched": result.matched,
            "phash_distance": result.phash_distance,
            "color_distance": result.color_distance,
            "detail_distance": result.detail_distance,
            "variant": result.variant,
            "match_rule": result.match_rule,
            "match_profile": FILE_MATCH_PROFILE,
            "match_thresholds": FILE_MATCH_THRESHOLDS,
            "delivery_id": target.delivery_id,
            "raw_message_id": target.raw_message_id,
            "route": str(getattr(self, "_last_media_route", "") or ""),
            "target_width": getattr(target_visual, "width", None),
            "target_height": getattr(target_visual, "height", None),
            "target_aspect_ratio": getattr(target_visual, "aspect_ratio", None),
            "target_variance": getattr(target_visual, "variance", None),
            "target_rect": getattr(target_visual, "rect", None),
            "phash_threshold": FILE_VISUAL_MAX_DISTANCE,
            "color_threshold": FILE_COLOR_MAX_DISTANCE,
            "detail_threshold": FILE_DETAIL_MAX_DISTANCE,
            "variant_metrics": [
                {
                    "variant": name,
                    "phash_distance": phash_distance,
                    "color_distance": color_distance,
                    "detail_distance": detail_distance,
                }
                for name, phash_distance, color_distance, detail_distance
                in result.variant_metrics
            ],
        }
        if not result.matched:
            raise MediaFileMismatchError(
                "下载结果与接收时缩略图不一致，拒绝绑定: "
                f"phash_distance={result.phash_distance} "
                f"color_distance={result.color_distance} "
                f"detail_distance={result.detail_distance} variant={result.variant}"
            )
        return Path(path)

    def download(self, dir_path: str | Path | None = None, timeout: int = 30) -> Path:
        """One quick thumbnail copy, then a newly identified preview window."""
        return _run_ordered_media_operation(
            self,
            lambda: self._download_unlocked(dir_path=dir_path, timeout=timeout),
        )

    def _download_unlocked(
        self,
        dir_path: str | Path | None = None,
        timeout: int = 30,
    ) -> Path:
        self._last_media_candidate_path = ""
        self._last_media_verification = {}
        # In WeChat 4.x the thumbnail context menu's “复制” action places image
        # pixels on the clipboard, not a CF_HDROP file path. The supported file
        # route is the newly opened preview's owner-bound “更多 → 复制” menu.
        self._last_media_route = "preview"
        deadline = time.monotonic() + max(1.0, float(timeout))
        for attempt in range(2):
            try:
                path = download_media_via_preview(self, dir_path=dir_path,
                                                  timeout=max(1.0, deadline - time.monotonic()))
                break
            except MediaIdentityError as exc:
                if attempt or exc.code != "target_moved" or time.monotonic() + 1.2 >= deadline:
                    raise
                media_event("target_rebind_retry", reason=exc.code)
                # Re-enumerate and verify the exact thumbnail after cleanup;
                # never reuse coordinates from the click that just failed.
                time.sleep(1.1)
        media_event("verify_downloaded_file")
        return self._verify_downloaded_path(path)


class VideoMessage(HumanMessage):
    type = "video"

    def download(self, dir_path: str | Path | None = None, timeout: int = 30) -> Path:
        """单击视频打开预览窗口，点“保存”后从剪贴板获取微信本地路径。"""
        return _run_ordered_media_operation(
            self,
            lambda: download_media_via_preview(
                self,
                dir_path=dir_path,
                timeout=timeout,
            ),
        )


class FileMessage(HumanMessage):
    type = "file"

    def _refresh_visible_control(self, chat_name: str = "") -> bool:
        """窗口/消息 UIA 重绘后重新绑定同一张可见文件卡片。"""
        original_content = str(getattr(self, "content", "") or "").strip()
        original_direction = str(getattr(self, "direction", "") or "").strip()

        def bind_from(chatbox) -> bool:
            getter = getattr(chatbox, "get_messages", None)
            if not callable(getter):
                return False
            try:
                messages = getter() or []
            except Exception:
                return False
            for candidate in reversed(messages):
                if str(getattr(candidate, "type", "") or "") != self.type:
                    continue
                if str(getattr(candidate, "content", "") or "").strip() != original_content:
                    continue
                candidate_direction = str(
                    getattr(candidate, "direction", "") or ""
                ).strip()
                if (
                    original_direction
                    and candidate_direction
                    and candidate_direction != original_direction
                ):
                    continue
                control = getattr(candidate, "control", None)
                if find_content_center(control) is None:
                    continue
                self.control = control
                self.parent = getattr(candidate, "parent", None) or chatbox
                return True
            return False

        current_parent = getattr(self, "parent", None)
        if bind_from(current_parent):
            return True

        # 右键菜单消失时微信可能重建独立聊天窗口。旧消息仍指向旧 ChatBox，
        # 因此从 WeChatSubWnd.root 回到主窗口，按进入下载前保存的聊天名
        # 强制枚举新子窗口，再在新 ChatBox 中精确匹配文件名和消息方向。
        sub_root = getattr(current_parent, "root", None)
        main_root = getattr(sub_root, "root", None)
        get_sub_wnd = getattr(main_root, "get_sub_wnd", None)
        if not callable(get_sub_wnd):
            return False
        target_chat = str(chat_name or "").strip()
        if not target_chat:
            try:
                target_chat = str(getattr(current_parent, "who", "") or "").strip()
            except Exception:
                target_chat = ""
        if not target_chat:
            return False
        try:
            fresh_chat = get_sub_wnd(target_chat, force_refresh=True)
            fresh_core = getattr(fresh_chat, "core", None)
            if fresh_core is None or not fresh_core.exists():
                return False
            return bind_from(fresh_core.get_chatbox())
        except Exception:
            return False

    def _copy_path_to_clipboard(self, timeout: float = 10.0) -> str | None:
        """右键文件卡片 -> 复制，从 CF_HDROP 剪贴板读取微信本地路径。"""
        from mabowx.ui.component import Menu

        deadline = time.monotonic() + timeout
        started_at = time.monotonic()
        last_error = ""
        attempt = 0
        try:
            chat_name = str(getattr(self.parent, "who", "") or "").strip()
        except Exception:
            chat_name = ""
        while time.monotonic() < deadline:
            attempt += 1
            # 防止“复制”未真正更新剪贴板时误读上一次文件操作留下的
            # CF_HDROP，从而把错误文件归档到当前消息。
            clear()
            # 文件消息的 ListItem 覆盖整行。截图算法会把时间标签、滚动条
            # 等离散像素也算入 bbox，真机上曾得到整行中心的空白坐标，右键
            # 因而完全不弹菜单。消息方向偏移点稳定落在实际文件卡片内，应
            # 优先使用；只有方向暂不可判定时才用截图中心兜底。
            point = self._bias() or find_content_center(self.control)
            if point is None:
                if self._refresh_visible_control(chat_name):
                    point = self._bias() or find_content_center(self.control)
                    if point is not None:
                        wxlog.debug(
                            f"文件消息控件已重新绑定: attempt={attempt} chat={chat_name!r}"
                        )
            if point is None:
                last_error = "文件消息控件暂时不可用"
                wxlog.debug(
                    f"文件消息暂时无法定位，等待窗口重绑: attempt={attempt} "
                    f"chat={chat_name!r}"
                )
                time.sleep(0.25)
                continue
            uia.right_click_screen(point[0], point[1], wait=0.4)
            time.sleep(0.2)
            menu = Menu(
                self.parent,
                timeout=min(0.8, max(0.1, deadline - time.monotonic())),
                anchor=point,
            )
            response = menu.select("复制")
            if not response.is_success:
                last_error = response["message"]
                wxlog.debug(
                    f"文件菜单暂不能复制: attempt={attempt} error={last_error}"
                )
                # “复制”在文件尚未下载完成时可能不存在。必须先关闭本轮
                # 菜单再重试，否则下一次右键可能落在旧菜单上。
                try:
                    menu.close()
                except Exception:
                    pass
                time.sleep(0.2)
                continue

            # 微信写入 CF_HDROP 可能略晚于菜单关闭；短轮询比固定等待更快，
            # 同时不会把剪贴板尚未就绪误判为失败。
            last_error = ""
            clipboard_deadline = min(deadline, time.monotonic() + 1.2)
            while time.monotonic() < clipboard_deadline:
                paths = read_files()
                if paths:
                    candidate = Path(paths[0])
                    if candidate.exists():
                        wxlog.info(
                            "文件菜单复制成功: "
                            f"attempt={attempt} elapsed={time.monotonic() - started_at:.3f}s "
                            f"name={candidate.name!r}"
                        )
                        return str(candidate)
                    last_error = f"路径不存在: {candidate}"
                time.sleep(0.08)
            if not last_error:
                last_error = "复制后剪贴板未出现文件路径"
            time.sleep(0.15)

        wxlog.warning(
            "文件菜单复制超时: "
            f"attempts={attempt} elapsed={time.monotonic() - started_at:.3f}s "
            f"last_error={last_error or '未知'}"
        )
        return None

    @uilock
    def download(self, dir_path: str | Path | None = None, force_click: bool = False, timeout: int = 30) -> Path:
        """下载文件消息并在同一 UI 事务内恢复文本剪贴板。"""
        if not self.exists():
            raise RuntimeError("文件消息已失效")
        try:
            original_clipboard = get_text()
        except Exception:
            original_clipboard = ""
        try:
            source = self._copy_path_to_clipboard(timeout=timeout)
        finally:
            try:
                set_text(original_clipboard)
            except Exception as exc:
                wxlog.warning(f"恢复文件下载前的文本剪贴板失败: {exc}")
        if not source:
            raise RuntimeError("获取文件路径失败，请确认微信已开启文件自动下载")
        src = Path(source)
        if not src.exists():
            raise RuntimeError(f"微信文件路径不存在: {src}")
        target_dir = Path(dir_path) if dir_path else Path(WxParam.DEFAULT_SAVE_PATH)
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / src.name
        try:
            shutil.copy2(src, target)
        except Exception as exc:
            raise RuntimeError(f"复制文件失败: {exc}") from exc
        return target


class LocationMessage(HumanMessage):
    type = "location"


_HISTORY_URL_POLL_INTERVAL_SEC = 0.5
_HISTORY_URL_POLL_TIMEOUT_SEC = 8.0
# 实测：xiaoheihe 卡片 17:37:10 点击 → 17:38:29 才写入 History（79s），
# 原 25s 合并轮询 + 12s 迟写入宽限（合计约 37s）必然耗尽。这里把合并轮询
# 放宽到 100s、迟写入宽限放宽到 15s，使整体窗口可覆盖 79s 迟到写入，
# 总预算 _LINK_URL_TOTAL_BUDGET_SEC 仍是最终上限。
_HISTORY_URL_MERGE_POLL_TIMEOUT_SEC = 100.0
_LINK_URL_TOTAL_BUDGET_SEC = 120.0
_LINK_LATE_WRITE_GRACE_SEC = 15.0
_LINK_LATE_WRITE_RECHECK_INTERVAL_SEC = 1.0
_LINK_CLICK_MAX_ATTEMPTS = 3
_LINK_CLICK_RETRY_INTERVAL_SEC = 4.0
# 空标题接受分支的噪声过滤：API 主机与明显跳转中间页不能作为解析结果。
_HISTORY_RESULT_NOISE_HOST_PREFIXES = ("api.",)
_HISTORY_RESULT_NOISE_PATH_MARKERS = ("/redirect", "/jump", "/web/share", "/app/bbs/link")
_HISTORY_RESULT_NOISE_QUERY_KEYS = ("url", "target", "redirect", "goto")
# 用于在单次解析内只记录一次 no_new_url，避免 0.5s 轮询刷屏。
_HISTORY_NO_URL_LOG_KEY = "\x00no_new_url"
_LINK_BRIDGE_LOGGER_NAME = "wx_bot"
_HISTORY_URL_PREFIX = "mp.weixin.qq.com/s/"
_HISTORY_WEIXIN_HOST = "mp.weixin.qq.com"
_HISTORY_URL_LOOKUP_TIMEOUT_SEC = 10.0
_HISTORY_FILE_READ_TIMEOUT_SEC = 1.5
_HISTORY_TITLE_WINDOW_USEC = 7 * 24 * 60 * 60 * 1_000_000
_HISTORY_TITLE_KEY_LENGTH = 24
_HISTORY_TITLE_MIN_MATCH_LEN = 12
_HISTORY_TITLE_MIN_LINE_KEY_LEN = 4
_HISTORY_TITLE_MAX_ROWS = 200
_HISTORY_EPOCH_OFFSET_SEC = 11_644_473_600
_HISTORY_TITLE_STRIP_PATTERN = re.compile(r"[\W_]+", re.UNICODE)
_HISTORY_TITLE_TAG_PATTERN = re.compile(r"^(?:\[[^\]]*\]|分享|[\s:：])+", re.UNICODE)
_HISTORY_TITLE_META_LINE_PATTERN = re.compile(
    r"^(?:UP主|up主|播放|时长|作者|来源|点赞|评论|收藏|发布|封面)\s*[:：]",
    re.UNICODE,
)
_HISTORY_TITLE_NUMERIC_LINE_PATTERN = re.compile(r"^\d+(?:\.\d+)?\s*万?$", re.UNICODE)


def _history_now_usec() -> int:
    """当前时间换算为 Chromium last_visit_time（1601 起微秒）。"""
    return int((time.time() + _HISTORY_EPOCH_OFFSET_SEC) * 1_000_000)


def _normalize_history_title(value: str) -> str:
    """归一化标题/内容：去空白与标点，取前 N 字作为匹配 key。"""
    try:
        return _HISTORY_TITLE_STRIP_PATTERN.sub("", str(value or ""))[
            :_HISTORY_TITLE_KEY_LENGTH
        ]
    except Exception:
        return ""


def _history_title_keys(value: str) -> list[str]:
    """从卡片文本派生多个候选标题 key（每个归一化后截 24 字）。

    卡片 content 常以 ``[链接]``/``[图片]`` 等行首标记开头，且常见结构为
    ``标题\\n摘要\\n来源``。单一 key 会因标记或尾部差异漏配，因此同时产出
    去标记正文、最长一行、原文保底三个候选，并按顺序去重。
    """
    text = str(value or "")
    candidates: list[str] = []
    stripped = text
    try:
        stripped = _HISTORY_TITLE_TAG_PATTERN.sub("", text)
    except Exception:
        stripped = text
    candidates.append(_normalize_history_title(stripped))
    try:
        lines = [line.strip() for line in stripped.splitlines() if line.strip()]
        if lines:
            candidates.append(_normalize_history_title(max(lines, key=len)))
    except Exception:
        pass
    candidates.append(_normalize_history_title(text))
    # 逐行候选：富卡片首行常是真实标题，逐行加入可避免最长行/原文截断漏配；
    # 跳过 UP主/播放 等明显元信息行与纯数字（含 N万）行。
    try:
        for line in stripped.splitlines():
            line = line.strip()
            if not line:
                continue
            if _HISTORY_TITLE_META_LINE_PATTERN.match(line):
                continue
            if _HISTORY_TITLE_NUMERIC_LINE_PATTERN.match(line):
                continue
            normalized = _normalize_history_title(line)
            if len(normalized) >= _HISTORY_TITLE_MIN_LINE_KEY_LEN:
                candidates.append(normalized)
    except Exception:
        pass
    result: list[str] = []
    for candidate in candidates:
        if candidate and candidate not in result:
            result.append(candidate)
    return result


def _title_matches_key(raw_title: str, key: str) -> bool:
    """标题与 key 互为包含、或 key 前缀命中即视为同一篇文章。"""
    if not key:
        return False
    normalized = _normalize_history_title(raw_title)
    if not normalized:
        return False
    if key in normalized or normalized in key:
        return True
    prefix = key[:_HISTORY_TITLE_MIN_MATCH_LEN]
    return bool(prefix) and prefix in normalized


def _history_url_is_noise(url: str) -> bool:
    """判断空标题接受分支里应跳过的噪声 URL。

    xiaoheihe 一次点击会写入三条记录（API 端点、跳转中间页、最终文章页），
    只有最终文章页可作为解析结果。过滤规则保持保守：host 以 ``api.`` 开头、
    路径命中明显的跳转中间页标记、或查询参数里内嵌 http(s) 目标地址时才判为
    噪声；解析失败一律按噪声处理（宁可不接受也不误取）。
    """
    try:
        text = str(url or "").strip()
        if not text:
            return True
        parsed = urlsplit(text)
        host = (parsed.hostname or "").lower()
        if not host:
            return True
        for prefix in _HISTORY_RESULT_NOISE_HOST_PREFIXES:
            if host.startswith(prefix):
                return True
        path = (parsed.path or "").lower()
        for marker in _HISTORY_RESULT_NOISE_PATH_MARKERS:
            if marker in path:
                return True
        try:
            for key, value in parse_qsl(parsed.query or "", keep_blank_values=True):
                if key.lower() in _HISTORY_RESULT_NOISE_QUERY_KEYS and str(
                    value
                ).lower().startswith(("http://", "https://")):
                    return True
        except Exception:
            pass
        return False
    except Exception:
        return True


def _history_pick_empty_title_url(pool) -> str | None:
    """空标题接受分支取该批最后一条非噪声 URL；全为噪声时返回 None。"""
    candidate = None
    for url, _title, _last_visit in pool:
        if _history_url_is_noise(url):
            continue
        candidate = url
    return candidate


def _link_title_source(message) -> str:
    """按 UIA Name → content → title 取第一个非空的标题来源。"""
    control = getattr(message, "control", None)
    if control is not None:
        try:
            name = str(getattr(control, "Name", "") or "").strip()
            if name:
                return name
        except Exception:
            pass
    content = str(getattr(message, "content", "") or "").strip()
    if content:
        return content
    return str(getattr(message, "title", "") or "").strip()


def _history_profile_files() -> list[str]:
    """只读定位 WMPF(Chromium) 未加密 History 文件。"""
    try:
        appdata = os.environ.get("APPDATA") or ""
        if not appdata:
            return []
        pattern = os.path.join(
            appdata, "Tencent", "xwechat", "radium", "web", "profiles", "*", "History"
        )
        return [path for path in glob.glob(pattern) if os.path.isfile(path)]
    except Exception:
        return []


def _query_history_urls(path: str, since: int, limit: int = 20) -> list[tuple[str, str, int]]:
    """复制 History 到临时目录后再查询，绝不写微信目录。"""
    rows: list[tuple[str, str, int]] = []
    temp_dir = None
    try:
        temp_dir = tempfile.mkdtemp(prefix="mabowx_history_")
        copy_path = os.path.join(temp_dir, "History")
        shutil.copy2(path, copy_path)
        connection = sqlite3.connect(copy_path)
        try:
            cursor = connection.execute(
                "select url, title, last_visit_time from urls "
                "where last_visit_time > ? "
                "order by last_visit_time desc limit ?",
                (int(since), int(limit)),
            )
            for url, title, last_visit in cursor.fetchall():
                if not url:
                    continue
                rows.append((str(url), str(title or ""), int(last_visit or 0)))
        finally:
            connection.close()
    except Exception:
        return []
    finally:
        if temp_dir:
            try:
                shutil.rmtree(temp_dir, ignore_errors=True)
            except Exception:
                pass
    return rows


def _query_history_urls_limited(
    path: str,
    since: int,
    deadline: float,
    limit: int = 20,
) -> list[tuple[str, str, int]]:
    """带限时的单文件只读查询；超时或异常都返回已取得的结果。"""
    rows: list[tuple[str, str, int]] = []
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return rows
    budget = min(_HISTORY_FILE_READ_TIMEOUT_SEC, remaining)
    finished = threading.Event()

    def worker() -> None:
        try:
            rows.extend(_query_history_urls(path, since, limit))
        except Exception:
            pass
        finally:
            finished.set()

    try:
        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
    except Exception:
        return []
    finished.wait(budget)
    return list(rows)


def _history_lookup_by_titles(keys, deadline: float) -> tuple[str | None, str]:
    """只读按多个候选标题匹配 History（7 天窗），返回 (url, 命中的 key)。

    候选按顺序依次尝试，任一候选命中即返回；每个候选取该候选下
    last_visit_time 最新的公众号文章 URL。异常全部吞掉。
    """
    try:
        key_list: list[str] = []
        for key in keys:
            if key and key not in key_list:
                key_list.append(key)
        if not key_list:
            return None, ""
        cutoff = _history_now_usec() - _HISTORY_TITLE_WINDOW_USEC
        rows: list[tuple[str, str, int]] = []
        for path in _history_profile_files():
            if time.monotonic() >= deadline:
                break
            rows.extend(
                _query_history_urls_limited(
                    path, cutoff, deadline, limit=_HISTORY_TITLE_MAX_ROWS
                )
            )
        for key in key_list:
            best: tuple[int, str] | None = None
            for url, title, last_visit in rows:
                if _HISTORY_WEIXIN_HOST not in url:
                    continue
                if not _title_matches_key(title, key):
                    continue
                if best is None or last_visit > best[0]:
                    best = (last_visit, url)
            if best:
                return best[1], key
        return None, ""
    except Exception:
        return None, ""


def _history_lookup_by_title(key: str, deadline: float) -> str | None:
    """只读按标题匹配 History（7 天窗），返回最新的公众号文章 URL。"""
    return _history_lookup_by_titles([key], deadline)[0]


def _history_visit_baseline() -> int:
    """点击前记录所有 profile 的 max(last_visit_time) 作为基线。"""
    baseline = 0
    for path in _history_profile_files():
        rows = _query_history_urls(path, 0, limit=1)
        if rows and rows[0][2] > baseline:
            baseline = rows[0][2]
    return baseline


def _select_history_url(rows: list[tuple[str, str, int]]) -> str | None:
    """优先公众号文章 URL，否则任意 http(s)；取 last_visit_time 最新一条。"""
    weixin_rows = [row for row in rows if _HISTORY_URL_PREFIX in row[0]]
    pool = weixin_rows or [
        row for row in rows if row[0].startswith(("http://", "https://"))
    ]
    if not pool:
        return None
    pool.sort(key=lambda row: row[2], reverse=True)
    return pool[0][0]


def _history_new_url_once(
    baseline: int,
    keys=(),
    mismatch_seen: set | None = None,
) -> str | None:
    """单轮读取【新出现】的 History 行，按标题校验后选出文章 URL。

    新行必须与候选 key 之一标题匹配（复用 ``_title_matches_key``）才会
    返回；新但不匹配的行写 ``stage=history_new reason=title_mismatch`` 的
    INFO 后继续等待，避免取到并发的无关文章。未提供 keys 时退回旧「取最新
    一条」语义。异常全部吞掉。
    """
    try:
        key_list: list[str] = []
        for key in keys or ():
            if key and key not in key_list:
                key_list.append(key)
        rows: list[tuple[str, str, int]] = []
        for path in _history_profile_files():
            rows.extend(_query_history_urls(path, baseline))
        weixin_rows = [row for row in rows if _HISTORY_URL_PREFIX in row[0]]
        pool = weixin_rows or [
            row for row in rows if row[0].startswith(("http://", "https://"))
        ]
        if not pool:
            return None
        pool.sort(key=lambda row: row[2], reverse=True)
        if not key_list:
            # 无候选标题时沿用「取最新」语义，但同样跳过 API/跳转噪声。
            for url, _title, _last_visit in pool:
                if not _history_url_is_noise(url):
                    return url
            return pool[0][0]
        for url, title, _last_visit in pool:
            hit_key = next(
                (key for key in key_list if _title_matches_key(title, key)), ""
            )
            if hit_key:
                try:
                    logging.getLogger(_LINK_BRIDGE_LOGGER_NAME).info(
                        f"[link-click] stage=history_new reason=title_match key={hit_key[:20]}"
                    )
                except Exception:
                    pass
                return url
            normalized = _normalize_history_title(title)
            if not normalized:
                continue
            if mismatch_seen is not None:
                if normalized in mismatch_seen:
                    continue
                mismatch_seen.add(normalized)
            _link_click_log(
                "title_mismatch",
                stage="history_new",
                title=normalized[:12],
            )
        # 空标题接受分支：本轮新行标题全为空（无法参与标题校验）时，按规范
        # 过滤噪声后取该批最后一条 URL（真机 xiaoheihe 空标题证据）。
        if all(not _normalize_history_title(row[1]) for row in pool):
            candidate = _history_pick_empty_title_url(pool)
            if candidate:
                _link_click_log(
                    "empty_title_accept",
                    None,
                    stage="history_new",
                    url=candidate,
                )
                return candidate
        if pool and (
            mismatch_seen is None or _HISTORY_NO_URL_LOG_KEY not in mismatch_seen
        ):
            if mismatch_seen is not None:
                mismatch_seen.add(_HISTORY_NO_URL_LOG_KEY)
            _link_click_log("no_new_url", None, stage="history_new")
        return None
    except Exception:
        return None


def _get_url_from_history(baseline: int, deadline: float) -> str | None:
    """轮询【新出现】的 History 行，返回文章 URL；异常全部吞掉返回 None。"""
    try:
        while time.monotonic() < deadline:
            url = _history_new_url_once(baseline)
            if url:
                return url
            time.sleep(_HISTORY_URL_POLL_INTERVAL_SEC)
    except Exception:
        return None
    return None


def _link_click_log(
    reason: str,
    rect=None,
    started: float | None = None,
    stage: str = "click",
    title: str = "",
    url: str = "",
) -> None:
    """把链接点击/清理的每步结果写入桥主 logger（落 wx_bot.log）。

    形如 ``[link-click] stage=click reason=clicked rect=(...) elapsed=..s``；
    带 ``title`` 时追加 ``title=<前12字>``，带 ``url`` 时追加 ``url=<前缀>``
    （只打前缀，避免完整 URL 泄漏会话参数）。任何日志异常都必须吞掉，
    绝不能影响链接解析本身。
    """
    try:
        elapsed = time.monotonic() - (started if started is not None else time.monotonic())
        if rect is None:
            rect_text = "None"
        else:
            try:
                rect_text = "(" + ",".join(str(int(value)) for value in rect) + ")"
            except Exception:
                rect_text = str(rect)
        title_text = f" title={title}" if title else ""
        url_text = f" url={url[:64]}" if url else ""
        message = (
            f"[link-click] stage={stage} reason={reason}{title_text}{url_text} "
            f"rect={rect_text} elapsed={elapsed:.3f}s"
        )
        logging.getLogger(_LINK_BRIDGE_LOGGER_NAME).info(message)
    except Exception:
        pass


def _control_rect(control) -> tuple[int, int, int, int] | None:
    """读取控件绝对矩形；控件无效或读取异常时返回 None。"""
    if control is None:
        return None
    try:
        rect = control.BoundingRectangle
        left, top, right, bottom = (
            int(rect.left),
            int(rect.top),
            int(rect.right),
            int(rect.bottom),
        )
    except Exception:
        return None
    if right <= left or bottom <= top:
        return None
    return (left, top, right, bottom)


_LINK_BROWSER_LOCK = threading.Lock()


class LinkMessage(HumanMessage):
    type = "link"


class CardMessage(HumanMessage):
    """链接/公众号卡片消息。

    微信 4.1.12 中，普通链接卡片和公众号卡片使用相同的 UIA 结构。
    对外类型对齐为 ``link``，Mabobot 的 internal API 会据此发布
    ``LINK_MESSAGE_RECEIVED`` 事件，summary_plus 才能处理。
    """

    type = "link"
    mtype = "link"

    @staticmethod
    def _control_is_clickable(control) -> bool:
        if control is None:
            return False
        try:
            rect = control.BoundingRectangle
            if int(rect.right - rect.left) <= 0 or int(rect.bottom - rect.top) <= 0:
                return False
            return bool(control.Exists(0))
        except Exception:
            return False

    def _bind_visible_candidate(self, candidate) -> None:
        self.control = candidate.control
        self.parent = candidate.parent
        self.control_class_name = getattr(candidate, "control_class_name", "")

    def _refresh_visible_control(self) -> bool:
        """按内容重新绑定当前可见卡片，避免持久缓存失效 UIA 对象。

        微信 4.x 的消息列表会虚拟化并复用 RuntimeId。卡片收到后若又有
        新消息进入，回调时保存的 UIA control 可能已变成 0x0，甚至一次
        ``Exists`` 调用就阻塞十几秒。执行动作前必须从当前可见消息重新
        找到同一张卡片，不能继续点击旧对象。

        先精确一致匹配；失败后用归一化标题（复用 A5 的
        ``_normalize_history_title``，各截前 24 字）互相包含做模糊匹配，
        再绑定一次。两种情况都不命中时返回 False，由调用方兜底。
        """
        self._link_locate_reason = ""
        getter = getattr(self.parent, "get_messages", None)
        if not callable(getter):
            # 兼容手工构造的消息对象；正常监听消息都有 ChatBox parent。
            return self.exists()
        try:
            # Rebinding geometry must not re-identify every sender in the
            # viewport. Identity was resolved when this message was delivered.
            messages = getter(resolve_group_senders=False, probe_avatar_direction=False)
        except Exception as exc:
            wxlog.warning(f"刷新链接卡片控件失败: {exc}")
            return False
        candidates = [candidate for candidate in messages if (
                getattr(candidate, "type", "") == self.type
                and getattr(candidate, "content", "") == self.content
                and self._control_is_clickable(getattr(candidate, "control", None))
            )]
        if len(candidates) == 1:
            self._link_locate_reason = "exact_ok"
            self._bind_visible_candidate(candidates[0])
            return True
        if len(candidates) > 1:
            wxlog.warning(f"可见区存在多张相同链接卡片，已拒绝猜测: count={len(candidates)}")
            return False

        # 精确匹配失败：用归一化前 24 字互相包含做模糊匹配。
        key = _normalize_history_title(self.content)
        fuzzy = []
        if key:
            for candidate in messages:
                if getattr(candidate, "type", "") != self.type:
                    continue
                if not self._control_is_clickable(getattr(candidate, "control", None)):
                    continue
                other = _normalize_history_title(getattr(candidate, "content", ""))
                if other and (key in other or other in key):
                    fuzzy.append(candidate)
        if len(fuzzy) == 1:
            self._link_locate_reason = "fuzzy_ok"
            self._bind_visible_candidate(fuzzy[0])
            wxlog.info(f"链接卡片模糊匹配成功: key={key[:16]!r}")
            return True
        if fuzzy:
            wxlog.warning(f"可见区存在多张模糊匹配的链接卡片，已拒绝猜测: count={len(fuzzy)}")
        return False

    def _visible_click_point(self) -> tuple[int, int] | None:
        """把方向点击点约束到消息列表与卡片行的真实可见交集。"""
        point = self._bias()
        if point is None:
            return None
        try:
            row = self.control.BoundingRectangle
            message_list = getattr(self.parent, "message_list", None)
            if message_list is None:
                return point
            viewport = message_list.BoundingRectangle
            left = max(int(row.left), int(viewport.left))
            top = max(int(row.top), int(viewport.top))
            right = min(int(row.right), int(viewport.right))
            bottom = min(int(row.bottom), int(viewport.bottom))
            if right <= left or bottom <= top:
                return None
            margin_x = min(6, max(0, (right - left - 1) // 3))
            margin_y = min(6, max(0, (bottom - top - 1) // 3))
            return (
                min(max(int(point[0]), left + margin_x), right - 1 - margin_x),
                min(max(int(point[1]), top + margin_y), bottom - 1 - margin_y),
            )
        except Exception:
            return point

    def _log_card_ui(self, stage: str, *, point=None) -> None:
        """只读、尽力记录点击证据；诊断失败不能影响原有操作。"""
        try:
            from mabowx.core.win32 import get_window_geometry

            def probe(operation):
                try:
                    return operation()
                except Exception as exc:
                    return {"probe_error": str(exc)[:160]}

            def rect(control):
                value = control.BoundingRectangle
                return tuple(int(getattr(value, key)) for key in ("left", "top", "right", "bottom"))

            parent = getattr(self, "parent", None)
            window = getattr(parent, "root", None)
            hwnd = probe(lambda: int(window.HWND))
            details = {
                "stage": stage,
                "raw_runtime_id": getattr(self, "id", None),
                "delivery_id": getattr(self, "delivery_id", None),
                "point": point,
                "row_rect": probe(lambda: rect(self.control)),
                "viewport_rect": probe(lambda: rect(parent.message_list)),
                "chat_hwnd": hwnd,
                "chat_geometry": probe(lambda: get_window_geometry(hwnd)) if isinstance(hwnd, int) and hwnd else None,
                "foreground": media_foreground_snapshot(),
            }
            wxlog.info(f"链接卡片 UI 诊断: {details}")
        except Exception:
            pass

    def _control_center_point(
        self,
    ) -> tuple[tuple[int, int] | None, tuple[int, int, int, int] | None]:
        """最后一招：直接取 ``self.control`` 的 BoundingRectangle 中心。

        优先取与消息视口的可见交集中心；交集为空时退回控件自身中心。
        返回 ``(point, rect)``；控件矩形不可读时 point 为 None。
        """
        rect = _control_rect(getattr(self, "control", None))
        if rect is None:
            return None, None
        left, top, right, bottom = rect
        clamp_left, clamp_top, clamp_right, clamp_bottom = left, top, right, bottom
        message_list = getattr(self.parent, "message_list", None)
        if message_list is not None:
            try:
                viewport = message_list.BoundingRectangle
                clamp_left = max(left, int(viewport.left))
                clamp_top = max(top, int(viewport.top))
                clamp_right = min(right, int(viewport.right))
                clamp_bottom = min(bottom, int(viewport.bottom))
            except Exception:
                clamp_left, clamp_top, clamp_right, clamp_bottom = left, top, right, bottom
        if clamp_right > clamp_left and clamp_bottom > clamp_top:
            return (
                (clamp_left + clamp_right) // 2,
                (clamp_top + clamp_bottom) // 2,
            ), rect
        return ((left + right) // 2, (top + bottom) // 2), rect

    @uilock
    def _click_visible_card(self) -> tuple[str, tuple[int, int, int, int] | None]:
        """激活聊天后点击卡片；定位失败时用控件矩形兜底点击。"""
        started = time.monotonic()
        self._log_card_ui("before_activate")
        if not self._activate_source_window():
            raise RuntimeError("卡片所属聊天窗口未能安全激活")
        point = self._visible_click_point()
        rect = _control_rect(getattr(self, "control", None))
        reason = "clicked"
        if point is None:
            # 最后一招：不再静默放弃，直接用 self.control 的矩形点击。
            point, fallback_rect = self._control_center_point()
            if point is None:
                _link_click_log("not_found", rect or fallback_rect, started)
                raise RuntimeError("卡片当前没有可点击的可见区域")
            rect = fallback_rect or rect
            reason = "control_fallback"
            _link_click_log("control_fallback", rect, started)
        self._log_card_ui("before_click", point=point)
        uia.click_screen(point[0], point[1], wait=0.5)
        self._log_card_ui("after_click", point=point)
        _link_click_log("clicked", rect, started)
        return reason, rect

    def _signal_link_stop(self) -> None:
        event = getattr(self, "_link_stop_event", None)
        if event is not None:
            try:
                event.set()
            except Exception:
                pass

    def _close_link_browser(self) -> None:
        """关闭本次解析新打开的微信内置浏览器窗口。"""
        if not getattr(self, "_link_card_clicked", False):
            return
        started = time.monotonic()
        deadline = started + 5.0
        before = getattr(self, "_link_browser_windows_before", set())
        if not getattr(self, "_link_browser_snapshot_ok", False):
            wxlog.warning("链接浏览器收尾跳过 reason=no_snapshot")
            _link_click_log(
                "cleanup skipped reason=no_snapshot",
                None,
                started,
                stage="browser_close",
            )
            return
        closed = set()
        remaining = set()
        after = None
        owned_path = Path("data/link_browser_owned.json")
        try:
            owned_path.parent.mkdir(parents=True, exist_ok=True)
            owned = {int(hwnd) for hwnd in json.loads(owned_path.read_text(encoding="utf-8")) if int(hwnd) > 0}
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            owned = set()
        owned_before = len(owned)
        WeChatBrowser = None
        try:
            from mabowx.ui.component import WeChatBrowser

            for attempt in range(3):
                if time.monotonic() >= deadline:
                    break
                after = WeChatBrowser.enumerate_embedded_browser_hwnds()
                if after is None:
                    wxlog.warning("链接浏览器收尾跳过 reason=enumeration_failed")
                    remaining = set()
                    break
                targets = (after - before) | (owned & after)
                owned.update(after - before)
                remaining = targets - closed
                _link_click_log(
                    f"scan attempt={attempt} before={len(before)} after={len(after)} targets={len(targets)}",
                    None,
                    started,
                    stage="browser_close",
                )
                for hwnd in targets:
                    if time.monotonic() >= deadline:
                        break
                    try:
                        ok = WeChatBrowser.close_embedded_browser_hwnd(
                            hwnd, timeout=min(4.0, max(0.1, deadline - time.monotonic()))
                        )
                        _link_click_log(
                            f"close hwnd={hwnd} ok={ok}",
                            None,
                            started,
                            stage="browser_close",
                        )
                        if ok:
                            closed.add(hwnd)
                    except Exception:
                        pass
                if attempt < 2 and time.monotonic() < deadline:
                    time.sleep(min(0.3, max(0, deadline - time.monotonic())))
            if time.monotonic() < deadline:
                after = WeChatBrowser.enumerate_embedded_browser_hwnds()
                if after is not None:
                    remaining = ((after - before) | (owned & after)) - closed
                else:
                    remaining = set()
        except Exception as exc:
            wxlog.warning(f"链接浏览器收尾失败: {exc}")
        finally:
            try:
                current = WeChatBrowser.enumerate_embedded_browser_hwnds()
                alive_owned = owned & current if current is not None else owned - closed
                owned_path.write_text(json.dumps(sorted(alive_owned)), encoding="utf-8")
            except Exception:
                alive_owned = owned - closed
            _link_click_log(
                f"cleanup closed={len(closed)} remaining={len(remaining)} "
                f"before={len(before) if isinstance(before, set) else -1} "
                f"after={len(after) if isinstance(after, set) else -1} "
                f"owned_before={owned_before} owned_closed={len(closed)} leftover={len(alive_owned)} "
                f"elapsed={time.monotonic() - started:.1f}s",
                None,
                started,
                stage="browser_close",
            )

    def get_url(self, timeout: float = 15.0) -> str:
        """Serialize link requests separately from short desktop transactions."""
        timeout = max(0.1, float(timeout))
        if not _LINK_BROWSER_LOCK.acquire(timeout=timeout):
            _link_click_log("lock_timeout", None, time.monotonic())
            raise TimeoutError("等待链接解析任务超时")
        try:
            self._link_browser_windows_before = set()
            self._link_browser_snapshot_ok = False
            try:
                from mabowx.ui.component import WeChatBrowser

                owned_path = Path("data/link_browser_owned.json")
                try:
                    owned = {int(hwnd) for hwnd in json.loads(owned_path.read_text(encoding="utf-8")) if int(hwnd) > 0}
                except (OSError, ValueError, TypeError, json.JSONDecodeError):
                    owned = set()
                live = WeChatBrowser.enumerate_embedded_browser_hwnds()
                if live is not None:
                    owned.intersection_update(live)
                    for hwnd in list(owned):
                        WeChatBrowser.close_embedded_browser_hwnd(hwnd, timeout=5.0)
                    live_after = WeChatBrowser.enumerate_embedded_browser_hwnds()
                    if live_after is not None:
                        owned.intersection_update(live_after)
                    owned_path.parent.mkdir(parents=True, exist_ok=True)
                    owned_path.write_text(json.dumps(sorted(owned)), encoding="utf-8")

                snapshot = WeChatBrowser.enumerate_embedded_browser_hwnds()
                if snapshot is not None:
                    self._link_browser_windows_before = snapshot
                    self._link_browser_snapshot_ok = True
                else:
                    wxlog.warning("链接浏览器窗口快照失败: enumeration_failed")
            except Exception as exc:
                wxlog.warning(f"链接浏览器窗口快照失败: {exc}")
            try:
                url = self._get_url_serialized(timeout)
            except BaseException:
                self._signal_link_stop()
                raise
            # 命中后立即通知守护点击线程停止重试，再清理打开的浏览器。
            self._signal_link_stop()
            return url
        finally:
            self._signal_link_stop()
            try:
                self._close_link_browser()
            except Exception:
                pass
            _LINK_BROWSER_LOCK.release()

    def _trigger_daemon_click(
        self,
        deadline: float,
        stop_event: threading.Event | None = None,
    ) -> None:
        """在守护线程中触发卡片点击，绝不阻塞主线程历史轮询。

        `ui_transaction(timeout=...)` 只限制取锁，不限制 with 体内的 UIA 调用；
        真机上 UIA 调用可能阻塞数分钟。因此点击整体移入守护线程并立即返回，
        线程内先初始化 UIA/COM，异常一律吞掉，主线程照常轮询。

        点击最多重试 ``_LINK_CLICK_MAX_ATTEMPTS`` 次（间隔约 4s）；主线程一旦
        命中 URL 会 set ``stop_event``，重试立即停止。
        """
        stop_event = stop_event or threading.Event()

        def click_worker() -> None:
            try:
                initializer = getattr(uia, "InitializeUIAutomationInCurrentThread", None)
                if not callable(initializer):
                    auto_mod = getattr(uia, "auto", None)
                    initializer = getattr(
                        auto_mod, "InitializeUIAutomationInCurrentThread", None
                    )
                if callable(initializer):
                    initializer()
            except Exception as exc:
                wxlog.warning(f"卡片点击线程初始化 UIA 失败: {exc}")
            for attempt in range(1, _LINK_CLICK_MAX_ATTEMPTS + 1):
                if stop_event.is_set() or time.monotonic() >= deadline:
                    break
                attempt_started = time.monotonic()
                try:
                    remaining = max(0.0, deadline - time.monotonic())
                    with ui_transaction(timeout=min(20.0, remaining)):
                        if stop_event.is_set():
                            break
                        self._log_card_ui("click_card")
                        if not self._refresh_visible_control():
                            # 定位失败不放弃：交给 _click_visible_card 用控件矩形兜底。
                            _link_click_log(
                                "not_found",
                                _control_rect(getattr(self, "control", None)),
                                attempt_started,
                            )
                        elif getattr(self, "_link_locate_reason", "") == "fuzzy_ok":
                            _link_click_log(
                                "fuzzy_ok",
                                _control_rect(getattr(self, "control", None)),
                                attempt_started,
                            )
                        if self.roll_into_view():
                            time.sleep(0.15)
                            self._refresh_visible_control()
                        self._click_visible_card()
                except TimeoutError as exc:
                    _link_click_log("lock_timeout", None, attempt_started)
                    wxlog.warning(
                        f"卡片点击未取得 UI 锁，稍后重试: attempt={attempt} error={exc}"
                    )
                except Exception as exc:
                    wxlog.warning(
                        f"卡片点击未完成 attempt={attempt}/{_LINK_CLICK_MAX_ATTEMPTS} "
                        f"error={exc}"
                    )
                if stop_event.is_set():
                    break
                if attempt < _LINK_CLICK_MAX_ATTEMPTS:
                    stop_event.wait(_LINK_CLICK_RETRY_INTERVAL_SEC)

        try:
            thread = threading.Thread(target=click_worker, daemon=True)
            thread.start()
        except Exception as exc:
            wxlog.warning(f"卡片点击线程启动失败: {exc}")

    def _get_url_serialized(self, timeout: float) -> str:
        started_at = time.monotonic()
        deadline = started_at + timeout
        stage = "history_title"
        title_keys = _history_title_keys(_link_title_source(self))
        # 新入库但不匹配的行只记一次 title_mismatch，避免每次轮询重复刷日志。
        mismatch_seen: set[str] = set()
        # 命中后用于通知守护点击线程停止重试；clicked 标记是否真正点过卡片，
        # 只有点过才需要在成功后清理微信内置浏览器。
        self._link_stop_event = threading.Event()
        self._link_card_clicked = False

        def history_cap():
            return min(deadline, time.monotonic() + _HISTORY_URL_LOOKUP_TIMEOUT_SEC)

        def lookup_title(cap):
            # 多候选依次尝试，任一命中即返回；返回 (url, 命中的 key)。
            return _history_lookup_by_titles(title_keys, cap())

        def log_hit(source, url, stage_started, key=""):
            wxlog.info(
                f"卡片URL来源: source={source} prefix={url[:64]} "
                f"title_key={key[:16]} "
                f"stage_elapsed={time.monotonic() - stage_started:.3f}s "
                f"elapsed={time.monotonic() - started_at:.3f}s"
            )

        try:
            # ① 只读按标题匹配：点开前先看 History 是否已有该文章（无副作用）。
            stage = "history_title"
            stage_started = time.monotonic()
            title_url, title_hit = lookup_title(history_cap)
            if title_url:
                log_hit("history_title", title_url, stage_started, title_hit)
                return title_url

            # WeChat 4.x 无「复制链接」：点击前记录 WMPF History 基线，点开后
            # 读取其写入的文章 URL。全过程只读。
            history_baseline = _history_visit_baseline()

            # ② 点击卡片改由守护线程触发，主线程立即进入轮询；UIA 阻塞不影响返回。
            stage = "click_card"
            self._link_card_clicked = True
            self._trigger_daemon_click(deadline, self._link_stop_event)

            # ③ 主线程双判轮询：≤25s、间隔 0.5s，新入库行优先，标题次之。
            stage = "poll_history"
            stage_started = time.monotonic()
            poll_deadline = min(
                deadline,
                started_at + _LINK_URL_TOTAL_BUDGET_SEC,
                time.monotonic() + _HISTORY_URL_MERGE_POLL_TIMEOUT_SEC,
            )
            while time.monotonic() < poll_deadline:
                new_url = _history_new_url_once(
                    history_baseline, title_keys, mismatch_seen
                )
                if new_url:
                    log_hit("history_new", new_url, stage_started)
                    return new_url
                title_url, title_hit = lookup_title(
                    lambda: min(
                        poll_deadline,
                        time.monotonic() + _HISTORY_URL_LOOKUP_TIMEOUT_SEC,
                    )
                )
                if title_url:
                    log_hit("history_title", title_url, stage_started, title_hit)
                    return title_url
                time.sleep(_HISTORY_URL_POLL_INTERVAL_SEC)

            # ④ 轮询超时后再做一次标题匹配；仍未命中直接失败。
            stage = "history_poll"
            stage_started = time.monotonic()
            title_url, title_hit = lookup_title(
                lambda: min(
                    started_at + _LINK_URL_TOTAL_BUDGET_SEC,
                    time.monotonic() + _HISTORY_URL_LOOKUP_TIMEOUT_SEC,
                )
            )
            if title_url:
                log_hit("history_poll", title_url, stage_started, title_hit)
                return title_url

            # ⑤ 轮询结束后的迟写入宽限：微信可能在轮询窗口关闭后才写入
            # History（真机证据：13:36:48 入库，轮询 13:36:42 已结束，差 6s）。
            # 再等 _LINK_LATE_WRITE_GRACE_SEC 秒，每 1s 复查新行与标题；
            # 宽限期内命中同样视为成功，并沿用既有成功路径关闭内置浏览器。
            stage = "history_late"
            stage_started = time.monotonic()
            grace_deadline = min(
                deadline,
                started_at + _LINK_URL_TOTAL_BUDGET_SEC,
                time.monotonic() + _LINK_LATE_WRITE_GRACE_SEC,
            )
            while time.monotonic() < grace_deadline:
                new_url = _history_new_url_once(
                    history_baseline, title_keys, mismatch_seen
                )
                if new_url:
                    _link_click_log("hit", None, stage_started, stage="history_late")
                    log_hit("history_late", new_url, stage_started)
                    return new_url
                title_url, title_hit = lookup_title(
                    lambda: min(
                        grace_deadline,
                        time.monotonic() + _HISTORY_URL_LOOKUP_TIMEOUT_SEC,
                    )
                )
                if title_url:
                    _link_click_log("hit", None, stage_started, stage="history_late")
                    log_hit("history_late", title_url, stage_started, title_hit)
                    return title_url
                time.sleep(_LINK_LATE_WRITE_RECHECK_INTERVAL_SEC)
            _link_click_log("miss", None, stage_started, stage="history_late")

            raise TimeoutError(f"链接解析超时: stage={stage}")
        except Exception as exc:
            self._log_card_ui(f"failed:{stage}")
            wxlog.warning(
                f"链接卡片解析失败: stage={stage} timeout={timeout} "
                f"elapsed={time.monotonic() - started_at:.3f}s error={exc}"
            )
            raise
        finally:
            self._log_card_ui("after_cleanup")
            wxlog.info(f"链接卡片解析结束: stage={stage} elapsed={time.monotonic() - started_at:.3f}s")


class EmotionMessage(HumanMessage):
    type = "emotion"


class MergeMessage(HumanMessage):
    type = "merge"


class PersonalCardMessage(HumanMessage):
    type = "personal_card"


class NoteMessage(HumanMessage):
    type = "note"


class MiniAppMessage(HumanMessage):
    type = "miniapp"


class OtherMessage(HumanMessage):
    type = "other"
