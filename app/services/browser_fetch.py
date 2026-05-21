"""Windowed-Chromium HTML fetcher used to bypass eBay's 403 bot block.

eBay fingerprints httpx by TLS handshake, header set, and missing JavaScript
execution; even a perfect User-Agent on httpx still gets `403 Access Denied`.
A real Chromium with JS executes the same fingerprinting handshake browsers
do and gets through.

Design notes:
  - Runs **non-headless** by default. Connor wants to see the window so he can
    solve captchas / sign-ins / "are you human" prompts when they appear.
  - Uses `launch_persistent_context` against a profile dir under `~/.cardscanner`
    so cookies, localStorage, and any human-solved captcha state survive across
    fetches AND across uvicorn restarts. Subsequent fetches reuse the same
    window — no fresh-session captcha storm.
  - When a captcha/block page is detected, logs a prominent warning and polls
    the page for up to 90s, giving Connor time to solve it manually. Once the
    block clears, the fetch returns the real HTML.

If Playwright isn't installed (or chromium binary is missing), `is_available()`
returns False so the caller can fall back to httpx.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import httpx

from app.config import settings
from app.utils import logger as _log

log = _log.get(__name__)

# One Chrome-on-macOS UA — must stay consistent across the persistent profile,
# since cookies and fingerprint history were established against this identity.
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_SEC_CH_UA = (
    '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"'
)

# Removes the most obvious automation tells from the browser fingerprint.
# Akamai (eBay's edge) blocks Chromium on these signals even in headed mode.
_STEALTH_INIT_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
window.chrome = window.chrome || {runtime: {}};
"""

_CHROMIUM_LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-features=IsolateOrigins,site-per-process",
    "--no-sandbox",
]

# Persistent profile lives under the user's home dir so it survives uvicorn
# restarts and venv rebuilds. Override with $CARDSCANNER_PW_PROFILE if needed.
_PROFILE_DIR = Path(
    os.environ.get("CARDSCANNER_PW_PROFILE",
                   str(Path.home() / ".cardscanner" / "playwright-profile"))
)

# Headless can be forced on (for CI / smoke tests) via env var.
_HEADLESS = os.environ.get("CARDSCANNER_PW_HEADLESS", "0") == "1"

# Strings that indicate the page is a bot-check / block page, not real content.
_BLOCK_SIGNALS = (
    "access denied",
    "pardon our interruption",
    "verify you are human",
    "please verify yourself",
    "/sec/captcha/",
    "px-captcha",
    "g-recaptcha",
    "are you a robot",
)

_pw_instance = None
_pw_context = None  # BrowserContext from launch_persistent_context
_pw_lock = asyncio.Lock()
_pw_available: Optional[bool] = None


def is_available() -> bool:
    """Cheap check: can we import playwright?"""
    global _pw_available
    if _pw_available is not None:
        return _pw_available
    try:
        import playwright  # noqa: F401
        _pw_available = True
    except ImportError:
        _pw_available = False
    return _pw_available


async def _get_context():
    """Reuse one persistent Chromium context for the life of the server."""
    global _pw_instance, _pw_context
    if _pw_context is not None:
        return _pw_context
    async with _pw_lock:
        if _pw_context is not None:
            return _pw_context
        from playwright.async_api import async_playwright
        _pw_instance = await async_playwright().start()
        _PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        _pw_context = await _pw_instance.chromium.launch_persistent_context(
            str(_PROFILE_DIR),
            headless=_HEADLESS,
            args=_CHROMIUM_LAUNCH_ARGS,
            user_agent=_USER_AGENT,
            viewport={"width": 1280, "height": 1000},
            locale="en-US",
            timezone_id="America/New_York",
            extra_http_headers={
                "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
                           "image/avif,image/webp,*/*;q=0.8"),
                "Accept-Language": "en-US,en;q=0.9",
                "Sec-Ch-Ua": _SEC_CH_UA,
                "Sec-Ch-Ua-Mobile": "?0",
                "Sec-Ch-Ua-Platform": '"macOS"',
                "Upgrade-Insecure-Requests": "1",
            },
        )
        await _pw_context.add_init_script(_STEALTH_INIT_JS)
        log.info("playwright: chromium launched",
                 extra={"headless": _HEADLESS, "profile": str(_PROFILE_DIR)})
    return _pw_context


def _looks_blocked(html: str) -> bool:
    """True if the page looks like a bot-check or block page, not real content."""
    if not html or len(html) < 5000:
        # Genuine eBay search pages are >100kB. Anything tiny is a block stub.
        return True
    lc = html.lower()
    return any(sig in lc for sig in _BLOCK_SIGNALS)


async def _notify_captcha(page, url: str) -> None:
    """Send a push notification via ntfy with a screenshot of the captcha page."""
    if not settings.ntfy_topic:
        return
    try:
        screenshot = await page.screenshot(type="png")
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(
                f"{settings.ntfy_server.rstrip('/')}/{settings.ntfy_topic}",
                content=screenshot,
                headers={
                    "Title": "CardScanner: Captcha needed",
                    "Message": f"Solve at http://{settings.host}:{settings.port}/captcha",
                    "Tags": "robot,warning",
                    "Filename": "captcha.png",
                    "Priority": "high",
                    "Click": f"http://{settings.host}:{settings.port}/captcha",
                },
            )
        log.info("captcha notification sent via ntfy")
    except Exception as e:
        log.warning("ntfy notification failed", extra={"error": str(e)})


# Shared state for the remote captcha-solving page
_captcha_page = None  # the Playwright page waiting for captcha solve
_captcha_url: Optional[str] = None
_captcha_event: Optional[asyncio.Event] = None


def get_captcha_state() -> dict:
    """Return current captcha state for the /captcha endpoint."""
    return {
        "waiting": _captcha_page is not None,
        "url": _captcha_url,
    }


async def get_captcha_screenshot() -> Optional[bytes]:
    """Take a fresh screenshot of the captcha page."""
    if _captcha_page is None:
        return None
    try:
        return await _captcha_page.screenshot(type="png")
    except Exception:
        return None


async def send_captcha_click(x: int, y: int) -> bool:
    """Click at (x, y) on the captcha page."""
    if _captcha_page is None:
        return False
    try:
        await _captcha_page.mouse.click(x, y)
        await _captcha_page.wait_for_timeout(500)
        return True
    except Exception:
        return False


async def send_captcha_keys(text: str) -> bool:
    """Type text into the captcha page."""
    if _captcha_page is None:
        return False
    try:
        await _captcha_page.keyboard.type(text)
        return True
    except Exception:
        return False


async def _wait_for_unblock(page, url: str, *, max_wait_s: int = 180) -> str:
    """Poll the page until the block indicator goes away, or give up.

    In headless mode: sends a push notification and exposes the page for
    remote solving via /captcha. In headed mode: logs a warning for manual
    solving in the visible window.
    """
    global _captcha_page, _captcha_url, _captcha_event

    log.warning(
        "⚠️ CAPTCHA / BLOCK DETECTED — will wait up to %ds. URL: %s",
        max_wait_s, url,
    )

    # Notify and expose for remote solving
    await _notify_captcha(page, url)
    _captcha_page = page
    _captcha_url = url
    _captcha_event = asyncio.Event()

    try:
        deadline = asyncio.get_event_loop().time() + max_wait_s
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(2)
            try:
                html = await page.content()
            except Exception:
                continue
            if not _looks_blocked(html):
                log.info("playwright: block cleared, continuing")
                return html
        log.warning("playwright: block did not clear in %ds, returning anyway",
                    max_wait_s)
        try:
            return await page.content()
        except Exception:
            return ""
    finally:
        _captcha_page = None
        _captcha_url = None
        _captcha_event = None


async def fetch_html(url: str, *, timeout_ms: int = 25000,
                     wait_settle_ms: int = 2000) -> Optional[str]:
    """Fetch a URL via Chromium with bot-evasion + captcha-relay.

    Returns HTML text or None on failure. When the target is an eBay search
    URL and we don't yet hold an eBay session cookie, hits the homepage first
    so the request looks like a real session — Akamai's edge is far more
    permissive when there's prior navigation history.
    """
    if not is_available():
        return None
    try:
        ctx = await _get_context()
    except Exception as e:
        log.warning("playwright launch failed", extra={"error": str(e)})
        return None

    page = None
    try:
        page = await ctx.new_page()

        host = urlparse(url).hostname or ""
        if "ebay.com" in host and "/sch/" in url:
            # Only do the warmup once per session — once we have eBay cookies,
            # subsequent search fetches are ~2-3s instead of ~9s.
            cookies = await ctx.cookies("https://www.ebay.com/")
            has_session = any(
                c["name"] in ("dp1", "ds1", "ds2", "s", "ebay", "nonsession")
                for c in cookies
            )
            if not has_session:
                try:
                    await page.goto("https://www.ebay.com/",
                                    wait_until="domcontentloaded",
                                    timeout=timeout_ms)
                    await page.wait_for_timeout(1200)
                except Exception as e:
                    log.warning("playwright warmup warning",
                                extra={"error": type(e).__name__})

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        except Exception as e:
            log.warning("playwright goto warning",
                        extra={"url": url[:120], "error": type(e).__name__})
        await page.wait_for_timeout(wait_settle_ms)
        html = await page.content()

        if _looks_blocked(html):
            html = await _wait_for_unblock(page, url)

        return html
    except Exception as e:
        log.warning("playwright fetch error",
                    extra={"error": str(e), "url": url[:120]})
        return None
    finally:
        if page:
            try:
                await page.close()
            except Exception:
                pass


async def shutdown() -> None:
    """Best-effort cleanup of the Playwright context/instance."""
    global _pw_instance, _pw_context
    try:
        if _pw_context:
            await _pw_context.close()
    except Exception:
        pass
    try:
        if _pw_instance:
            await _pw_instance.stop()
    except Exception:
        pass
    _pw_context = None
    _pw_instance = None
