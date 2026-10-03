#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""SPEC-B2 空闲清理守护。

每轮（默认 1800s）只在链路空闲时执行：
  1) 关闭微信内置浏览器(WeChatAppEx)的独立顶层窗口（窗口标题=页面标题）；
     默认受保护标题（微信/Weixin/WeChat/WeChatAppEx/微信输入法/登录）与空标题一律跳过；
  2) 关闭插件 Chrome(CFT, 调试端口 19223) 的标签页，默认关闭所有标签页，
     仅排除内部协议（devtools://、chrome://、chrome-extension://）；
     只统计 type=page 的 target，about:blank / chrome-untrusted://new-tab-page*
     / chrome://newtab* 为永不关闭的哨兵页，无哨兵页时一个都不关（reason=no_sentinel），
     关闭前后各探测一次 /json/version 以确保绝不把浏览器本身关掉；
     可用 chrome_keep_hosts / wechat_keep_titles 配白名单。

忙闲判定：查桥 + app 的 /api/status；任一 busy，或 last_activity 距今小于
空闲宽限（默认 120s）=> 本轮跳过，等下一轮。关闭只在空闲且非 dry-run 时执行，
单轮最多关闭 20 个，失败只告警。

仅使用 Python 标准库；Windows 专有部分用 ctypes 懒加载实现，非 Windows
平台会记录 unsupported_platform 并安全跳过清理 ①。

用法：
  python cleanup_idle_service.py --once --dry-run        # 单轮试跑，不执行关闭
  python cleanup_idle_service.py --once                 # 单轮真实清理
  python cleanup_idle_service.py                        # 常驻，每 1800s 一轮
  python cleanup_idle_service.py --interval 600 --disabled
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
import time
import urllib.error
import urllib.request

DEFAULT_BRIDGE_STATUS_URL = "http://127.0.0.1:5555/api/status"
DEFAULT_APP_STATUS_URL = "http://127.0.0.1:21889/api/status"
DEFAULT_CHROME_DEBUG_PORT = 19223
DEFAULT_LOG_PATH = r"C:\Users\mabobot\Mabobot\logs\cleanup.log"
DEFAULT_INTERVAL_SEC = 1800
DEFAULT_IDLE_GRACE_SEC = 120.0
DEFAULT_MAX_CLOSE_PER_RUN = 20
DEFAULT_REQUEST_TIMEOUT_SEC = 3.0
# 清理② 关闭模式：
#   "all"      —— 关闭 CFT Chrome 中所有标签页，仅排除内部协议（默认，SPEC-batch2-FIX F2）；
#   "internal" —— 仅关闭历史遗留的空白/内部页（旧行为，保留以便回退）。
CHROME_CLOSE_MODE_ALL = "all"
CHROME_CLOSE_MODE_INTERNAL = "internal"
DEFAULT_CHROME_CLOSE_MODE = CHROME_CLOSE_MODE_ALL
CHROME_CLOSE_MODES = (CHROME_CLOSE_MODE_ALL, CHROME_CLOSE_MODE_INTERNAL)
# "all" 模式下唯一排除项：浏览器内部协议页，绝不关闭。
CHROME_INTERNAL_URL_PREFIXES = (
    "devtools://",
    "chrome://",
    "chrome-extension://",
)
# "internal" 模式复刻旧行为（含 about:blank / edge://），供回退时对齐。
CHROME_LEGACY_RESIDUAL_URL_PREFIXES = (
    "about:blank",
    "devtools://",
    "chrome://",
    "chrome-extension://",
    "edge://",
)
# F6：哨兵页（永不可关闭）——存在任一哨兵页时才允许关闭其他真实页，用于保证
# Chrome 绝不因标签页归零而退出。2026-09-26 实测证明 F4 的 total-1 口径会因
# /json/list 中 service_worker/background_page 等非 page target 被计入而失效。
CHROME_SENTINEL_URL_PREFIXES = (
    "about:blank",
    "chrome-untrusted://new-tab-page",
    "chrome://newtab",
)
# F6：只统计 type == "page" 的 target；非 page 类型既不计入 total 也不关。
CHROME_TARGET_TYPE_PAGE = "page"
# 清理① 目标进程：微信内置浏览器的页面是 WeChatAppEx 的独立顶层窗口，
# 窗口标题即页面标题。绝不关闭 WeChat 主进程窗口。
WECHAT_BROWSER_PROCESS = "wechatappex.exe"
# 清理② 默认白名单（host），空表示关闭所有非内部协议标签页。
DEFAULT_CHROME_KEEP_HOSTS: tuple = ()
# 清理② 标签页硬上限：每轮结束时最多保留的 page 标签页数（2026-10-01 用户要求：
# 每半小时检查一次 Chrome，只保留一个标签页）。<=0 表示不启用。
DEFAULT_CHROME_MAX_TABS = 1
# P5（2026-10-01 用户要求）：桥/app 状态源不可达时是否跳过整轮（保守优先，默认跳过）。
DEFAULT_SKIP_IF_STATUS_UNKNOWN = True
# 清理① 默认标题白名单（窗口标题），空表示关闭所有 WeChatAppEx 页面窗口。
DEFAULT_WECHAT_KEEP_TITLES: tuple = ()
# 清理① 默认受保护标题（精确匹配、大小写不敏感），命中绝不关闭。
# 微信 4.x 主界面窗口也挂在 WeChatAppEx 进程上，仅按进程名过滤不安全
# （2026-09-26 实测：清理①曾对微信主窗口投递 WM_CLOSE，title=微信）。
DEFAULT_WECHAT_PROTECTED_TITLES = (
    "微信",
    "Weixin",
    "WeChat",
    "WeChatAppEx",
    "微信输入法",
    "登录",
)
_PROTECTED_WECHAT_TITLES_LOWER = frozenset(
    title.lower() for title in DEFAULT_WECHAT_PROTECTED_TITLES
)
CONFIG_FILENAME = "cleanup_idle_service.config.json"


def _default_config_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), CONFIG_FILENAME)


def load_config(cli_args: argparse.Namespace) -> dict:
    """合并配置文件与命令行；命令行优先。缺失文件时使用内置默认值。"""
    config = {
        "enabled": True,
        "bridge_status_url": DEFAULT_BRIDGE_STATUS_URL,
        "app_status_url": DEFAULT_APP_STATUS_URL,
        "chrome_debug_port": DEFAULT_CHROME_DEBUG_PORT,
        "interval_seconds": DEFAULT_INTERVAL_SEC,
        "idle_grace_seconds": DEFAULT_IDLE_GRACE_SEC,
        "max_close_per_run": DEFAULT_MAX_CLOSE_PER_RUN,
        "request_timeout_seconds": DEFAULT_REQUEST_TIMEOUT_SEC,
        "log_file": DEFAULT_LOG_PATH,
        "chrome_close_mode": DEFAULT_CHROME_CLOSE_MODE,
        "chrome_keep_hosts": list(DEFAULT_CHROME_KEEP_HOSTS),
        "chrome_max_tabs": DEFAULT_CHROME_MAX_TABS,
        "skip_if_status_unknown": DEFAULT_SKIP_IF_STATUS_UNKNOWN,
        "wechat_keep_titles": list(DEFAULT_WECHAT_KEEP_TITLES),
    }
    config_path = cli_args.config or _default_config_path()
    if config_path and os.path.isfile(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as handle:
                loaded = json.load(handle)
            if isinstance(loaded, dict):
                config.update(loaded)
        except Exception:
            pass
    if cli_args.interval is not None:
        config["interval_seconds"] = int(cli_args.interval)
    if cli_args.idle_grace is not None:
        config["idle_grace_seconds"] = float(cli_args.idle_grace)
    if cli_args.max_close is not None:
        config["max_close_per_run"] = int(cli_args.max_close)
    if cli_args.log_file is not None:
        config["log_file"] = cli_args.log_file
    if cli_args.chrome_port is not None:
        config["chrome_debug_port"] = int(cli_args.chrome_port)
    if cli_args.disabled:
        config["enabled"] = False
    if cli_args.enabled:
        config["enabled"] = True
    if cli_args.bridge_status_url is not None:
        config["bridge_status_url"] = cli_args.bridge_status_url
    if cli_args.app_status_url is not None:
        config["app_status_url"] = cli_args.app_status_url
    if cli_args.chrome_close_mode is not None:
        config["chrome_close_mode"] = cli_args.chrome_close_mode
    if cli_args.chrome_max_tabs is not None:
        config["chrome_max_tabs"] = int(cli_args.chrome_max_tabs)
    if cli_args.allow_unknown_status:
        config["skip_if_status_unknown"] = False
    if cli_args.chrome_keep_host:
        config["chrome_keep_hosts"] = list(config.get("chrome_keep_hosts") or []) + list(
            cli_args.chrome_keep_host
        )
    if cli_args.wechat_keep_title:
        config["wechat_keep_titles"] = list(config.get("wechat_keep_titles") or []) + list(
            cli_args.wechat_keep_title
        )
    return config


def _resolve_log_path(log_file: str) -> str:
    """非 Windows 测试主机上把 Windows 默认路径落到临时目录，避免生成怪名文件。"""
    target = str(log_file)
    if os.name != "nt" and "\\" in target and "/" not in target:
        return os.path.join(tempfile.gettempdir(), "mabobot_cleanup.log")
    return target


def build_logger(log_file: str) -> logging.Logger:
    logger = logging.getLogger("mabobot.cleanup")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if logger.handlers:
        return logger
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    stream = logging.StreamHandler(stream=sys.stdout)
    stream.setFormatter(fmt)
    logger.addHandler(stream)
    target = _resolve_log_path(log_file)
    try:
        parent = os.path.dirname(target)
        if parent:
            os.makedirs(parent, exist_ok=True)
        file_handler = logging.FileHandler(target, encoding="utf-8")
    except Exception:
        fallback = os.path.join(tempfile.gettempdir(), "mabobot_cleanup.log")
        logger.warning("log_file_unavailable path=%s fallback=%s", log_file, fallback)
        file_handler = logging.FileHandler(fallback, encoding="utf-8")
        file_handler.baseFilename = fallback
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)
    return logger


def _http_get_json(url: str, timeout: float):
    """GET JSON；任何失败都返回 None（不抛异常）。"""
    try:
        request = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if not 200 <= int(response.status) < 300:
                return None
            payload = response.read().decode("utf-8", errors="replace")
        return json.loads(payload)
    except Exception:
        return None


def _http_put(url: str, timeout: float) -> bool:
    try:
        request = urllib.request.Request(url, method="PUT")
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return 200 <= int(response.status) < 300
    except Exception:
        return False


def _normalize_status_activity(value) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def evaluate_idle(config: dict, now: float, logger: logging.Logger) -> tuple[bool, list, dict]:
    """返回 (busy, reasons, signals)。busy=True 表示本轮应跳过。"""
    timeout = float(config.get("request_timeout_seconds") or DEFAULT_REQUEST_TIMEOUT_SEC)
    grace = float(config.get("idle_grace_seconds") or DEFAULT_IDLE_GRACE_SEC)
    signals = {
        "bridge": _http_get_json(str(config.get("bridge_status_url")), timeout),
        "app": _http_get_json(str(config.get("app_status_url")), timeout),
    }
    reasons: list[str] = []
    for name, signal in signals.items():
        if not isinstance(signal, dict):
            continue
        if signal.get("busy"):
            inflight = signal.get("inflight")
            reasons.append(f"{name}:busy={inflight}")
        last_activity = _normalize_status_activity(signal.get("last_activity"))
        if last_activity > 0 and (now - last_activity) < grace:
            reasons.append(f"{name}:recent={now - last_activity:.1f}s")
    busy = bool(reasons)
    logger.info(
        "idle_check busy=%s reasons=%s bridge=%s app=%s",
        busy,
        reasons or "none",
        _signal_summary(signals.get("bridge")),
        _signal_summary(signals.get("app")),
    )
    return busy, reasons, signals


def _signal_summary(signal) -> str:
    if not isinstance(signal, dict):
        return "unavailable"
    return (
        f"busy={bool(signal.get('busy'))},"
        f"last_activity={_normalize_status_activity(signal.get('last_activity')):.0f}"
    )


def _unknown_status_sources(signals: dict) -> list:
    """P5：返回不可达/无法解析的状态源名字（桥/app）。"""
    return [name for name, signal in signals.items() if not isinstance(signal, dict)]


# --------------------------------------------------------------------------
# 清理 ②：插件 Chrome(CFT) 残留标签页（/json/list + PUT /json/close/{id}）
# --------------------------------------------------------------------------
def _chrome_version_ok(config: dict) -> bool:
    """F6：静默探测 GET /json/version，只返回可达性，不写日志（供关闭后复检）。"""
    port = int(config.get("chrome_debug_port") or DEFAULT_CHROME_DEBUG_PORT)
    timeout = float(config.get("request_timeout_seconds") or DEFAULT_REQUEST_TIMEOUT_SEC)
    return isinstance(_http_get_json(f"http://127.0.0.1:{port}/json/version", timeout), dict)


def _chrome_available(config: dict, logger: logging.Logger) -> bool:
    """F4：清理②开始前先探测 GET /json/version；不可达则记 chrome_unavailable。"""
    if _chrome_version_ok(config):
        return True
    port = int(config.get("chrome_debug_port") or DEFAULT_CHROME_DEBUG_PORT)
    logger.info("chrome_unavailable port=%s", port)
    return False


def _chrome_targets(config: dict, logger: logging.Logger) -> list:
    port = int(config.get("chrome_debug_port") or DEFAULT_CHROME_DEBUG_PORT)
    timeout = float(config.get("request_timeout_seconds") or DEFAULT_REQUEST_TIMEOUT_SEC)
    payload = _http_get_json(f"http://127.0.0.1:{port}/json/list", timeout)
    if not isinstance(payload, list):
        logger.info("chrome_list unavailable port=%s", port)
        return []
    return [item for item in payload if isinstance(item, dict)]


def _chrome_keep_hosts(config: dict) -> list:
    hosts = config.get("chrome_keep_hosts") or []
    if isinstance(hosts, str):
        hosts = [hosts]
    return [str(host).strip().lower() for host in hosts if str(host).strip()]


def _chrome_host_kept(url: str, keep_hosts: list) -> bool:
    if not keep_hosts:
        return False
    try:
        from urllib.parse import urlsplit

        host = (urlsplit(url).hostname or "").strip().lower()
    except Exception:
        return False
    if not host:
        return False
    for keep in keep_hosts:
        if host == keep or host.endswith("." + keep):
            return True
    return False


def _chrome_url_close_allowed(url: str, keep_hosts: list) -> bool:
    """判断某标签页 URL 在“all”模式下是否应关闭（内部协议与白名单除外）。"""
    lowered = url.lower()
    if lowered.startswith(tuple(prefix.lower() for prefix in CHROME_INTERNAL_URL_PREFIXES)):
        return False
    if _chrome_host_kept(url, keep_hosts):
        return False
    return True


def _chrome_url_close_allowed_legacy(url: str) -> bool:
    """旧行为：只关空白/内部页。"""
    return url.lower().startswith(
        tuple(prefix.lower() for prefix in CHROME_LEGACY_RESIDUAL_URL_PREFIXES)
    )


def _chrome_target_is_page(item: dict) -> bool:
    """F6：只把 type == "page" 的 target 计入统计与关闭范围。"""
    return str(item.get("type") or "").strip().lower() == CHROME_TARGET_TYPE_PAGE


def _chrome_url_is_sentinel(url: str) -> bool:
    """F6：哨兵页绝不关闭（保留一个“保命页”，防止 Chrome 因标签页归零退出）。"""
    lowered = str(url or "").strip().lower()
    return lowered.startswith(tuple(prefix.lower() for prefix in CHROME_SENTINEL_URL_PREFIXES))


def cleanup_chrome_tabs(config: dict, budget: int, dry_run: bool, logger: logging.Logger) -> list:
    """按 chrome_close_mode 关闭标签页，返回已处理 URL 列表（dry-run 也记录）。

    F4：清理前先探测 /json/version，不可达则记 chrome_unavailable 并跳过本轮清理②。
    F6：只统计 type == "page" 的 target；about:blank / chrome-untrusted://new-tab-page*
    / chrome://newtab* 为哨兵页，永不关闭；不存在哨兵页时一个都不关
    （skip reason=no_sentinel），宁可少关，绝不把浏览器关掉。
    """
    processed: list[str] = []
    if budget <= 0:
        return processed
    if not _chrome_available(config, logger):
        return processed
    mode = str(config.get("chrome_close_mode") or DEFAULT_CHROME_CLOSE_MODE).strip().lower()
    if mode not in CHROME_CLOSE_MODES:
        logger.warning("chrome_close_mode_unknown mode=%s fallback=%s", mode, CHROME_CLOSE_MODE_ALL)
        mode = CHROME_CLOSE_MODE_ALL
    keep_hosts = _chrome_keep_hosts(config)
    port = int(config.get("chrome_debug_port") or DEFAULT_CHROME_DEBUG_PORT)
    timeout = float(config.get("request_timeout_seconds") or DEFAULT_REQUEST_TIMEOUT_SEC)

    targets = _chrome_targets(config, logger)
    # F6：非 page 类型（service_worker/background_page 等）既不计入也不关。
    pages = [item for item in targets if _chrome_target_is_page(item)]
    # F6：仅当存在哨兵页时才考虑关闭其他真实页。
    if not any(_chrome_url_is_sentinel(str(item.get("url") or "")) for item in pages):
        logger.info("chrome_cleanup_skipped reason=no_sentinel total=%s", len(pages))
        return processed

    closeable: list = []
    for item in pages:
        url = str(item.get("url") or "")
        target_id = str(item.get("id") or "")
        if not target_id or not url:
            continue
        if _chrome_url_is_sentinel(url):
            continue
        if mode == CHROME_CLOSE_MODE_INTERNAL:
            allowed = _chrome_url_close_allowed_legacy(url)
        else:
            allowed = _chrome_url_close_allowed(url, keep_hosts)
        if allowed:
            closeable.append((target_id, url))
    if not closeable:
        return processed
    # F6：关闭上限 = min(可关真实页数, budget, page 总数 - 1)；有哨兵页兜底不会归零。
    limit = min(len(closeable), budget, len(pages) - 1)
    if limit <= 0:
        logger.info(
            "chrome_cleanup_skipped reason=no_closeable total=%s closeable=%s",
            len(pages),
            len(closeable),
        )
        return processed
    for target_id, url in closeable[:limit]:
        if dry_run:
            logger.info("chrome_close_dry_run target_id=%s url=%s", target_id, url)
            processed.append(url)
            continue
        ok = _http_put(f"http://127.0.0.1:{port}/json/close/{target_id}", timeout)
        logger.info("chrome_close target_id=%s url=%s ok=%s", target_id, url, ok)
        processed.append(url)
    # F6：关闭后复检 /json/version，若变为不可达则 WARN，便于排查（整轮仍算 done）。
    if not dry_run and processed and not _chrome_version_ok(config):
        logger.warning("chrome_lost_after_close=true port=%s closed=%s", port, len(processed))
    return processed


def enforce_chrome_tab_cap(
    config: dict, budget: int, dry_run: bool, logger: logging.Logger
) -> list:
    """F7：每轮结束时把 Chrome 标签页收敛到 chrome_max_tabs（默认 1）。

    与 cleanup_chrome_tabs 的区别：后者"关真实页、留哨兵页"，仍可能残留多个
    哨兵页；本函数是硬上限——最终只剩 cap 个 page 标签页（2026-10-01 用户要求：
    每半小时检查一次 Chrome，只保留一个标签页）。
    保留顺序：优先第一个哨兵页（保证浏览器不因标签归零而退出），否则列表第一个；
    cap<=0 表示不启用；预算不足时本轮少关、下轮继续，绝不关到 0。
    """
    processed: list = []
    raw_cap = config.get("chrome_max_tabs")
    cap = DEFAULT_CHROME_MAX_TABS if raw_cap is None else int(raw_cap)
    if cap <= 0 or budget <= 0:
        return processed
    if not _chrome_available(config, logger):
        return processed
    pages = [item for item in _chrome_targets(config, logger) if _chrome_target_is_page(item)]
    if len(pages) <= cap:
        logger.info("chrome_tab_cap ok tabs=%s cap=%s", len(pages), cap)
        return processed
    keeper = None
    for item in pages:
        if _chrome_url_is_sentinel(str(item.get("url") or "")):
            keeper = item
            break
    if keeper is None:
        keeper = pages[0]
    keeper_id = str(keeper.get("id") or "")
    extras = [item for item in pages if str(item.get("id") or "") != keeper_id]
    port = int(config.get("chrome_debug_port") or DEFAULT_CHROME_DEBUG_PORT)
    timeout = float(config.get("request_timeout_seconds") or DEFAULT_REQUEST_TIMEOUT_SEC)
    for item in extras[: min(len(extras), budget)]:
        target_id = str(item.get("id") or "")
        url = str(item.get("url") or "")
        if dry_run:
            logger.info("chrome_tab_cap_dry_run target_id=%s url=%s", target_id, url)
            processed.append(url)
            continue
        ok = _http_put(f"http://127.0.0.1:{port}/json/close/{target_id}", timeout)
        logger.info("chrome_tab_cap_close target_id=%s url=%s ok=%s", target_id, url, ok)
        processed.append(url)
    logger.info(
        "chrome_tab_cap before=%s keep=%s closed=%s cap=%s",
        len(pages), keeper_id, len(processed), cap,
    )
    if not dry_run and processed and not _chrome_version_ok(config):
        logger.warning("chrome_lost_after_cap=true port=%s closed=%s", port, len(processed))
    return processed


# --------------------------------------------------------------------------
# 清理 ①：微信内置浏览器(WeChatAppEx)独立顶层窗口
# --------------------------------------------------------------------------
def _process_image_name(pid: int) -> str:
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32
        process_query_limited_information = 0x1000
        handle = kernel32.OpenProcess(process_query_limited_information, False, int(pid))
        if not handle:
            return ""
        try:
            size = wintypes.DWORD(32768)
            buffer = ctypes.create_unicode_buffer(size.value)
            if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                return buffer.value
            return ""
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return ""


def _is_wechat_appex_image(image_path: str) -> bool:
    """进程映像是否为 WeChatAppEx（大小写不敏感）；WeChat 主进程返回 False。"""
    name = str(image_path).replace("\\", "/").rsplit("/", 1)[-1].strip().lower()
    return name == WECHAT_BROWSER_PROCESS


def _enum_wechat_browser_windows() -> list:
    """枚举 WeChatAppEx 顶层窗口，返回 [(hwnd, title), ...]。

    F3：微信内置浏览器的页面是 WeChatAppEx 的独立顶层窗口，窗口标题=页面标题。
    按进程名（大小写不敏感）匹配，绝不匹配 WeChat 主进程。
    """
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        results: list = []
        callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

        def _callback(hwnd, _lparam):
            try:
                if not user32.IsWindowVisible(hwnd):
                    return True
                length = user32.GetWindowTextLengthW(hwnd)
                buffer_size = length + 1 if length > 0 else 1
                title_buffer = ctypes.create_unicode_buffer(buffer_size)
                user32.GetWindowTextW(hwnd, title_buffer, buffer_size)
                title = title_buffer.value.strip()
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if _is_wechat_appex_image(_process_image_name(int(pid.value))):
                    results.append((int(hwnd), title))
            except Exception:
                pass
            return True

        user32.EnumWindows(callback_type(_callback), 0)
        return results
    except Exception:
        return []


def _close_window_wm_close(hwnd: int, logger: logging.Logger) -> bool:
    """向 WeChatAppEx 顶层窗口投递 WM_CLOSE；返回窗口是否已消失。"""
    try:
        import ctypes

        user32 = ctypes.windll.user32
        wm_close = 0x0010
        user32.PostMessageW(int(hwnd), wm_close, 0, 0)
        time.sleep(0.4)
        return not bool(user32.IsWindow(int(hwnd)))
    except Exception as exc:
        logger.warning("wechat_close_error hwnd=%s error=%s", hwnd, exc)
        return False


def _wechat_keep_titles(config: dict) -> list:
    titles = config.get("wechat_keep_titles") or []
    if isinstance(titles, str):
        titles = [titles]
    return [str(title).strip().lower() for title in titles if str(title).strip()]


def _wechat_title_is_protected(title: str) -> bool:
    """默认受保护标题（精确匹配、大小写不敏感）：微信主窗口、输入法等绝不关闭。"""
    return str(title or "").strip().lower() in _PROTECTED_WECHAT_TITLES_LOWER


def cleanup_wechat_articles(config: dict, budget: int, dry_run: bool, logger: logging.Logger) -> list:
    """关闭微信内置浏览器(WeChatAppEx)的独立顶层窗口，返回已处理描述列表。"""
    processed: list[str] = []
    if budget <= 0:
        return processed
    if os.name != "nt":
        logger.info("wechat_cleanup_skipped reason=unsupported_platform")
        return processed
    keep_titles = _wechat_keep_titles(config)
    windows = _enum_wechat_browser_windows()
    if not windows:
        logger.info("wechat_cleanup_skipped reason=no_browser_window")
        return processed
    for hwnd, title in windows:
        if len(processed) >= budget:
            logger.info("wechat_close_budget_reached closed=%s", len(processed))
            break
        normalized = str(title or "").strip().lower()
        if not normalized:
            logger.info("wechat_skip reason=empty_title hwnd=%s", hwnd)
            continue
        if _wechat_title_is_protected(title):
            logger.info("wechat_skip reason=protected_title hwnd=%s title=%s", hwnd, title)
            continue
        if normalized in keep_titles:
            logger.info("wechat_close_skipped reason=keep_title hwnd=%s title=%s", hwnd, title)
            continue
        if dry_run:
            logger.info("wechat_close_dry_run hwnd=%s title=%s", hwnd, title)
            processed.append(f"hwnd={hwnd} title={title}")
            continue
        ok = _close_window_wm_close(hwnd, logger)
        logger.info("wechat_close hwnd=%s title=%s ok=%s", hwnd, title, ok)
        processed.append(f"hwnd={hwnd} title={title}")
    return processed


# --------------------------------------------------------------------------
# 单轮编排
# --------------------------------------------------------------------------
def run_once(config: dict, logger: logging.Logger, dry_run: bool) -> dict:
    started = time.monotonic()
    now = time.time()
    budget = int(config.get("max_close_per_run") or DEFAULT_MAX_CLOSE_PER_RUN)
    busy, reasons, _signals = evaluate_idle(config, now, logger)
    if busy:
        logger.info("round_result status=skip reason=busy detail=%s elapsed=%.3fs",
                    reasons or "unknown", time.monotonic() - started)
        return {"status": "skip", "reasons": reasons, "wechat": [], "chrome": [],
                "chrome_capped": []}
    if config.get("skip_if_status_unknown", DEFAULT_SKIP_IF_STATUS_UNKNOWN):
        unknown = _unknown_status_sources(_signals)
        # 2026-10-01（用户要求放宽，并修正方向）：只有**桥**能报 busy/last_activity
        # （app 的 /api/system/status 只返回系统指标，没有这两个字段），
        # 所以硬闸门只认 bridge；app 不可达仅记一条 note，不影响本轮清理。
        blocking = [name for name in unknown if name == "bridge"]
        if unknown and not blocking:
            logger.info(
                "status_note app_unavailable_not_blocking detail=%s elapsed=%.3fs",
                ",".join(unknown),
                time.monotonic() - started,
            )
        if blocking:
            logger.info(
                "round_result status=skip reason=status_unknown detail=%s elapsed=%.3fs",
                ",".join(blocking),
                time.monotonic() - started,
            )
            return {"status": "skip", "reasons": ["status_unknown:" + ",".join(blocking)],
                    "wechat": [], "chrome": [], "chrome_capped": []}

    wechat_closed = cleanup_wechat_articles(config, budget, dry_run, logger)
    chrome_closed = cleanup_chrome_tabs(config, budget - len(wechat_closed), dry_run, logger)
    chrome_capped = enforce_chrome_tab_cap(config, budget, dry_run, logger)
    logger.info(
        "round_result status=done dry_run=%s wechat=%s chrome=%s chrome_capped=%s elapsed=%.3fs",
        dry_run,
        wechat_closed,
        chrome_closed,
        chrome_capped,
        time.monotonic() - started,
    )
    return {
        "status": "done",
        "dry_run": dry_run,
        "wechat": wechat_closed,
        "chrome": chrome_closed,
        "chrome_capped": chrome_capped,
    }


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Mabobot idle cleanup service")
    parser.add_argument("--once", action="store_true", help="run a single round then exit")
    parser.add_argument("--dry-run", action="store_true", help="log only, never close")
    parser.add_argument("--interval", type=int, default=None, help="seconds between rounds")
    parser.add_argument("--idle-grace", type=float, default=None, help="idle grace seconds")
    parser.add_argument("--max-close", type=int, default=None, help="max closes per round")
    parser.add_argument("--log-file", default=None, help="log file path")
    parser.add_argument("--config", default=None, help="optional JSON config path")
    parser.add_argument("--chrome-port", type=int, default=None, help="Chrome debug port")
    parser.add_argument("--chrome-close-mode", default=None, choices=list(CHROME_CLOSE_MODES),
                        help="chrome close mode: all (default) or internal")
    parser.add_argument("--chrome-keep-host", action="append", default=None,
                        help="host to keep when closing chrome tabs (repeatable)")
    parser.add_argument("--wechat-keep-title", action="append", default=None,
                        help="WeChatAppEx window title to keep (repeatable)")
    parser.add_argument("--chrome-max-tabs", type=int, default=None,
                        help="max chrome tabs kept after each round (default 1, 0=disable)")
    parser.add_argument("--allow-unknown-status", action="store_true", default=False,
                        help="run the round even if bridge/app status is unavailable")
    parser.add_argument("--bridge-status-url", default=None)
    parser.add_argument("--app-status-url", default=None)
    parser.add_argument("--enabled", action="store_true", help="force enable")
    parser.add_argument("--disabled", action="store_true", help="force disable")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    config = load_config(args)
    logger = build_logger(str(config.get("log_file")))
    logger.info(
        "cleanup_service_start enabled=%s once=%s dry_run=%s interval=%s bridge=%s app=%s",
        config.get("enabled"),
        args.once,
        args.dry_run,
        config.get("interval_seconds"),
        config.get("bridge_status_url"),
        config.get("app_status_url"),
    )
    if not config.get("enabled"):
        logger.info("cleanup_service_disabled: no cleanup will run")
        return 0
    if args.once:
        run_once(config, logger, args.dry_run)
        return 0
    interval = int(config.get("interval_seconds") or DEFAULT_INTERVAL_SEC)
    while True:
        try:
            run_once(config, logger, args.dry_run)
        except Exception as exc:
            logger.exception("round_error error=%s", exc)
        try:
            time.sleep(max(1, interval))
        except KeyboardInterrupt:
            logger.info("cleanup_service_stop reason=keyboard_interrupt")
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
