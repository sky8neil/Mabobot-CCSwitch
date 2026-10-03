"""Browser summarization helpers for summary_plus."""

import random
import re
import time
from typing import Optional
from urllib.parse import urlparse

import requests
from selenium.common.exceptions import InvalidSessionIdException, TimeoutException
from selenium.webdriver.support.ui import WebDriverWait

try:
    from websocket import WebSocketTimeoutException
except Exception:  # pragma: no cover - websocket 随 CDP 依赖提供
    WebSocketTimeoutException = None

from .content_cleaner import build_clean_stats, clean_extracted_article_text, strip_markdown

__all__ = [
    "browser_summarize",
    "detect_access_gate_page",
    "is_access_gate_silent_response",
]

ACCESS_GATE_SILENT_SENTINEL = "__SUMMARY_PLUS_SILENT_SKIP_ACCESS_GATE__"

# 连接类异常：会话陈旧或 Chrome 掉线时触发一次强制重建 + 重试。
CONNECTION_ERROR_TYPES = [
    ConnectionError,
    TimeoutError,
    requests.exceptions.ConnectionError,
    requests.exceptions.Timeout,
    TimeoutException,
    InvalidSessionIdException,
]
if WebSocketTimeoutException is not None:
    CONNECTION_ERROR_TYPES.append(WebSocketTimeoutException)
CONNECTION_EXCEPTIONS = tuple(CONNECTION_ERROR_TYPES)


WECHAT_ARTICLE_HOST = "mp.weixin.qq.com"
WECHAT_CAPTCHA_MARKER = "wappoc_appmsgcaptcha"
WECHAT_MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 "
    "MicroMessenger/8.0.40(0x18002824) NetType/WIFI Language/zh_CN"
)


def is_wechat_article_url(url: str) -> bool:
    try:
        host = (urlparse(url or "").hostname or "").lower()
    except Exception:
        return False
    return host == WECHAT_ARTICLE_HOST


def _cdp_override(driver, method: str, params: dict, logger) -> bool:
    call = getattr(driver, "_call", None)
    if callable(call):
        try:
            call(method, params, page=True)
            return True
        except TypeError:
            try:
                call(method, params)
                return True
            except Exception as e:
                logger.debug(f"CDP {method} 失败: {type(e).__name__} - {e}")
                return False
        except Exception as e:
            logger.debug(f"CDP {method} 失败: {type(e).__name__} - {e}")
            return False
    execute = getattr(driver, "execute_cdp_cmd", None)
    if callable(execute):
        try:
            execute(method, params)
            return True
        except Exception as e:
            logger.debug(f"CDP {method} 失败: {type(e).__name__} - {e}")
            return False
    return False


def apply_wechat_user_agent(driver, logger) -> bool:
    applied = _cdp_override(
        driver,
        "Emulation.setUserAgentOverride",
        {"userAgent": WECHAT_MOBILE_UA, "acceptLanguage": "zh-CN,zh;q=0.9"},
        logger,
    )
    applied = _cdp_override(
        driver,
        "Network.setUserAgentOverride",
        {"userAgent": WECHAT_MOBILE_UA},
        logger,
    ) or applied
    return applied


def is_wechat_risk_page(text: str, url: str = "", title: str = "") -> bool:
    if not text or not text.strip():
        return True
    haystack = f"{url or ''} {title or ''}".lower()
    return WECHAT_CAPTCHA_MARKER in haystack


CHALLENGE_KEYWORDS = (
    "captcha",
    "人机验证",
    "验证码",
    "向右滑动以保护您的访问",
    "请完成安全验证",
    "verify you are human",
    "are you human",
    "security check",
    "bot detection",
    "cloudflare",
    "perimeterx",
    "cf-chl",
    "px-captcha",
    "access denied",
)

ACCESS_GATE_URL_KEYWORDS = (
    "captcha",
    "challenge",
    "verify",
    "verification",
    "signin",
    "sign-in",
    "login",
    "auth/login",
)

LOGIN_GATE_PHRASES = (
    "登录后继续",
    "请先登录",
    "请登录后",
    "账号登录",
    "帐号登录",
    "扫码登录",
    "手机号登录",
    "密码登录",
    "登录即可继续",
    "login required",
    "please sign in",
    "sign in to continue",
    "authentication required",
)


def safe_get_current_url(driver):
    try:
        return driver.current_url or ""
    except Exception:
        return ""


def is_valid_content_url(url: str) -> bool:
    if not url:
        return False
    return not url.startswith(("about:blank", "chrome://", "devtools://"))


def looks_like_challenge_page(text: str) -> bool:
    low = (text or "").lower()
    if not low:
        return False
    return any(keyword.lower() in low for keyword in CHALLENGE_KEYWORDS)


def detect_access_gate_page(text: str, url: str = "", title: str = "") -> Optional[str]:
    """识别只有登录/人机验证内容、没有可摘要正文的访问门槛页面。"""
    normalized_text = re.sub(r"\s+", " ", text or "").strip()
    combined = " ".join((title or "", normalized_text)).lower()
    normalized_url = (url or "").lower()

    if looks_like_challenge_page(combined):
        return "human_verification"

    # 验证/登录专用 URL 配合短页面文本，视为访问门槛。长度限制避免正文链接
    # 或导航栏中偶然出现 login/auth 字样时误判。
    if len(normalized_text) <= 2000 and any(
        keyword in normalized_url for keyword in ACCESS_GATE_URL_KEYWORDS
    ):
        return "access_gate_url"

    if len(normalized_text) <= 2000 and any(
        phrase in combined for phrase in LOGIN_GATE_PHRASES
    ):
        return "login_required"

    # 极短的通用登录框通常只有“登录 / 账号 / 密码（或扫码）”等控件文案。
    if len(normalized_text) <= 500:
        has_login = "登录" in combined or "log in" in combined or "sign in" in combined
        has_credential = any(
            marker in combined
            for marker in ("账号", "帐号", "密码", "扫码", "手机号", "username", "password", "qr code")
        )
        if has_login and has_credential:
            return "login_form"

    return None


def is_access_gate_silent_response(text: str) -> bool:
    """模型仅返回约定哨兵时，视为语义判定的访问门槛页面。"""
    normalized = (text or "").strip().strip("`").strip()
    return normalized == ACCESS_GATE_SILENT_SENTINEL


def wait_for_page_load(driver, page_load_timeout: int, page_content_stabilize_delay: float, logger) -> None:
    max_retries = 2
    last_error = None

    for attempt in range(max_retries):
        try:
            logger.info(f"🔄 等待页面加载完成 (第 {attempt + 1}/{max_retries} 次尝试)")

            def page_fully_loaded(d):
                try:
                    ready_state = d.execute_script("return document.readyState")
                    if d.current_url.startswith("about:"):
                        return False
                    # 广告满载页可能长期停留在 interactive；视为就绪，
                    # 由正文抽取决定成败，不再死等 complete。
                    return ready_state in ("interactive", "complete")
                except Exception:
                    return False

            WebDriverWait(driver, page_load_timeout).until(page_fully_loaded)
            logger.info(
                f"✅ 页面加载完成 (第 {attempt + 1} 次尝试)，额外等待 {page_content_stabilize_delay}s 以稳定内容"
            )
            time.sleep(page_content_stabilize_delay + random.uniform(0.2, 0.8))
            return
        except TimeoutException as e:
            last_error = e
            logger.info("🔄 刷新页面并重试...")
            try:
                driver.refresh()
                time.sleep(2)
            except Exception:
                pass

    # 两次尝试都超时后不放弃：继续正文抽取，仅当正文为空才判定失败。
    if last_error:
        logger.warning(f"⚠️ 页面加载等待超时，继续尝试正文抽取: {last_error}")


def strip_summary_tag_section(text: str) -> str:
    """普通摘要模式不输出“标签”段；兼容模型偶尔仍生成标签的情况。"""
    if not text:
        return text

    lines = text.splitlines()
    kept = []
    skipping_tags = False

    tag_line_pattern = re.compile(r"^\s*(?:🏷\s*)?(?:标签|tag|tags)\s*[:：]", re.IGNORECASE)
    section_header_pattern = re.compile(r"^\s*[📖🔑📰🎯📋💡🔧]\s*[^\n]*[:：]?\s*$")

    for line in lines:
        if tag_line_pattern.match(line):
            skipping_tags = True
            continue
        if skipping_tags:
            stripped = line.strip()
            if not stripped:
                continue
            if section_header_pattern.match(line):
                skipping_tags = False
                kept.append(line)
            continue
        kept.append(line)

    return "\n".join(kept).strip()


def optimized_scroll(driver, logger) -> None:
    try:
        time.sleep(random.uniform(1.0, 2.0))
        last_height = driver.execute_script("return document.body.scrollHeight")
        max_scrolls = 8

        for i in range(max_scrolls):
            current_pos = driver.execute_script("return window.pageYOffset")
            scroll_step = random.randint(400, 900)
            driver.execute_script(f"window.scrollTo(0, {current_pos + scroll_step});")
            time.sleep(random.uniform(0.2, 0.5))

            new_height = driver.execute_script("return document.body.scrollHeight")
            if new_height == last_height and i > 1:
                break
            last_height = new_height

        driver.execute_script("window.scrollTo(0, 0);")
        time.sleep(random.uniform(0.3, 0.8))
    except Exception as e:
        logger.warning(f"⚠️ 滚动加载过程中出现异常: {e}")


def browser_summarize(
    ctx,
    url: Optional[str],
    is_link_message: bool = False,
    chat_name: str = "",
    sender: str = "",
) -> Optional[str]:
    """使用浏览器 + GPT 进行摘要"""
    max_attempts = 2
    try:
        ctx.logger.info(
            "🧭 开始浏览器摘要: "
            f"is_link_message={is_link_message}, "
            f"url={(url or '')[:180]}"
        )
        if not url or not url.startswith(("https://", "http://")):
            ctx.logger.warning("后台摘要需要已解析的 HTTP URL")
            return None

        def _fetch_once():
            # Even legacy link events must provide the resolved URL. Never reuse
            # an interactive browser tab or fall back to foreground automation.
            driver = ctx._open_background_page(url)
            ctx.logger.info("✅ 已通过 CDP 打开后台摘要页面，不激活浏览器标签页")
            page_url = url
            wechat_mode = is_wechat_article_url(url)
            try:
                if wechat_mode:
                    if apply_wechat_user_agent(driver, ctx.logger):
                        ctx.logger.info("[summary-browser] ua=wechat")
                        try:
                            driver.get(url)
                        except Exception as e:
                            ctx.logger.warning(
                                f"⚠️ 微信 UA 重新导航失败: {type(e).__name__} - {e}"
                            )
                    else:
                        ctx.logger.info("[summary-browser] ua=default")
                else:
                    ctx.logger.info("[summary-browser] ua=default")

                def _extract_current_page(fallback_url):
                    wait_for_page_load(
                        driver=driver,
                        page_load_timeout=ctx.page_load_timeout,
                        page_content_stabilize_delay=ctx.PAGE_CONTENT_STABILIZE_DELAY,
                        logger=ctx.logger,
                    )
                    optimized_scroll(driver=driver, logger=ctx.logger)

                    try:
                        current_url = safe_get_current_url(driver)
                        inner_url = current_url if current_url.startswith(("http://", "https://")) else (url or "")
                        text = driver.execute_script("return document.body && document.body.innerText || ''") or ""
                        try:
                            title = driver.title or ""
                        except Exception:
                            title = ""
                        return inner_url, text, title, None
                    except CONNECTION_EXCEPTIONS:
                        # 连接类异常交由上层重建会话后重试。
                        raise
                    except Exception as e:
                        ctx.logger.error(f"❌ 使用 innerText 提取内容时出错: {e}", exc_info=True)
                        return fallback_url, "", "", "❌ 抓取正文失败"

                page_url, main_text, page_title, failure = _extract_current_page(page_url)
                if failure:
                    return "final", failure, page_url

                if wechat_mode and is_wechat_risk_page(main_text, page_url, page_title):
                    # 风控标记通常落在 profile 的 cookie 上（非 IP 级），清 cookie 后重试。
                    _cdp_override(driver, "Network.clearBrowserCookies", {}, ctx.logger)
                    ctx.logger.info("[summary-browser] ua=wechat reason=captcha action=clear_cookies retry=yes")
                    try:
                        driver.get(url)
                    except Exception:
                        try:
                            driver.refresh()
                        except Exception:
                            pass
                    page_url, main_text, page_title, failure = _extract_current_page(page_url)
                    if failure:
                        return "final", failure, page_url

                access_gate_reason = detect_access_gate_page(
                    main_text,
                    url=page_url,
                    title=page_title,
                )
                if access_gate_reason:
                    ctx.logger.info(
                        "🔇 页面仅包含登录/人机验证，已静默跳过摘要: "
                        f"reason={access_gate_reason}, chars={len(main_text)}, "
                        f"url={page_url[:240]}"
                    )
                    return "skip", None, page_url
                if not main_text or not main_text.strip():
                    return "final", "❌ 抓取正文为空", page_url

                cleaned_text = clean_extracted_article_text(main_text)
                if cleaned_text and len(cleaned_text.strip()) >= 300:
                    stats = build_clean_stats(main_text, cleaned_text).as_dict()
                    ctx.logger.info(
                        "🧼 正文清洗完成: "
                        f"{stats['raw_chars']} -> {stats['cleaned_chars']} chars "
                        f"(reduction {stats['reduction_pct']}%)"
                    )
                    local_content = cleaned_text
                else:
                    ctx.logger.warning("⚠️ 正文清洗结果过短，回退使用原始文本")
                    local_content = main_text
                return "content", local_content[: ctx.MAX_CONTENT_LENGTH], page_url
            finally:
                driver.close()
                ctx.logger.info("✅ 已关闭本次后台摘要页面")

        def _force_rebuild_driver():
            """连接异常时强制丢弃现有会话并重建 driver/Chrome。"""
            try:
                with ctx.driver_lock:
                    if ctx.driver:
                        ctx._quit_webdriver(ctx.driver, reason="连接异常强制重建")
                        ctx.driver = None
            except Exception as e:
                ctx.logger.warning(f"⚠️ 丢弃旧 WebDriver 失败: {type(e).__name__} - {e}")
                ctx.driver = None
            try:
                return ctx._ensure_driver_available()
            except Exception as e:
                ctx.logger.warning(f"⚠️ 强制重建 WebDriver 失败: {type(e).__name__} - {e}")
                return None

        result_kind = None
        result_value = None
        fallback_url = url
        for attempt in range(1, max_attempts + 1):
            retry_flag = "no" if attempt == 1 else "yes"
            ctx.logger.info(
                f"[summary-browser] attempt={attempt} reason=stale_session retry={retry_flag}"
            )
            try:
                if not ctx._is_driver_healthy():
                    ctx._ensure_driver_available()
            except Exception as e:
                ctx.logger.warning(f"⚠️ 浏览器会话健康检查/重建失败: {type(e).__name__} - {e}")
            try:
                result_kind, result_value, fallback_url = _fetch_once()
                break
            except CONNECTION_EXCEPTIONS as e:
                ctx.logger.warning(
                    f"⚠️ 浏览器连接异常 attempt={attempt}: {type(e).__name__} - {e}"
                )
                if attempt >= max_attempts:
                    raise
                ctx.logger.info(
                    f"[summary-browser] attempt={attempt} reason=stale_session retry=yes"
                )
                _force_rebuild_driver()
                continue

        if result_kind == "skip":
            return None
        if result_kind == "final":
            return result_value
        if result_kind != "content":
            return None
        user_content = result_value

        system_prompt = ctx.prompt_summary
        messages_list = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]
        messages_list.insert(
            len(messages_list) - 1,
            {
                "role": "system",
                "content": (
                    "先判断用户内容是否只有登录界面、人机验证、验证码、安全检查或访问受限提示，"
                    "而没有可总结的正文。若是，只输出以下哨兵字符串，不得输出其他任何内容："
                    f"{ACCESS_GATE_SILENT_SENTINEL}。"
                    "若存在可总结正文，则忽略本条并正常完成摘要。"
                ),
            },
        )

        ctx.logger.info(
            "🤖 准备调用摘要 LLM: "
            f"call_type=summary, content_len={len(user_content)}, "
            f"chat={chat_name}, sender={sender}"
        )
        try:
            call_kwargs = {}
            if chat_name:
                call_kwargs["_mabobot_chat_name"] = chat_name
            response = ctx.llm_manager.call(
                plugin_name="summary_plus",
                call_type="summary",
                messages=messages_list,
                **call_kwargs,
            )
        except Exception as e:
            ctx.logger.error(f"❌ 摘要 LLM 请求失败: {e}", exc_info=True)
            return "❌ 摘要模型调用失败"
        if is_access_gate_silent_response(response):
            ctx.logger.info(
                "🔇 摘要模型判定页面仅包含登录/人机验证，已静默跳过回复: "
                f"url={(fallback_url or url or '')[:240]}"
            )
            return None
        summary_text = strip_markdown(response.strip())
        return strip_summary_tag_section(summary_text)

    except Exception as e:
        error_type = type(e).__name__
        ctx.logger.error(f"❌ 浏览器摘要失败: {error_type} - {e}", exc_info=True)
        return None
