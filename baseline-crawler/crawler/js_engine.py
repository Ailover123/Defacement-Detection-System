"""
PRODUCTION STABLE JS ENGINE
- Multi-stage navigation fallback
- DOM stabilization
- Safe content extraction
- Worker self-healing
- No networkidle usage
- Stable for CMS / WordPress / CDN
"""

import threading
import queue
import time
import hashlib
import re
from playwright.sync_api import sync_playwright
from crawler.core import USER_AGENT, logger


# ============================================================
# JS INTELLIGENCE
# ============================================================

class JSIntelligence:

    @staticmethod
    def needs_js_rendering(html: str) -> bool:
        if not html:
            return True

        h = html.lower()

        if (
            '<div id="root"' in h or
            '<div id="app"' in h or
            '<app-root' in h or
            '<div id="__next"' in h
        ):
            return True

        if "<body" in h:
            body = h[h.find("<body"):]
            if (
                "<a " not in body and
                "<p" not in body and
                "<main" not in body and
                "<article" not in body and
                "<section" not in body
            ):
                return True

        return False

    @staticmethod
    def is_404_content(html: str) -> bool:
        if not html:
            return False

        h = html.lower()
        patterns = [
            "page not found",
            "404 not found",
            "error 404",
            "doesn't exist",
        ]

        title_match = re.search(
            r'<title[^>]*>(.*?)</title>',
            h,
            re.IGNORECASE | re.DOTALL
        )

        if title_match:
            title_text = title_match.group(1).strip()
            if any(p in title_text for p in patterns):
                return True

        return False


# ============================================================
# RENDER CACHE
# ============================================================

class RenderCache:
    CACHE_TTL_SECONDS = 60 * 60 * 12
    _cache = {}
    _lock = threading.Lock()

    @staticmethod
    def _key(url: str) -> str:
        return hashlib.sha256(url.encode()).hexdigest()

    @classmethod
    def get(cls, url):
        key = cls._key(url)
        with cls._lock:
            entry = cls._cache.get(key)
            if not entry:
                return None
            html, ts = entry
            if time.time() - ts > cls.CACHE_TTL_SECONDS:
                del cls._cache[key]
                return None
            return html

    @classmethod
    def set(cls, url, html):
        key = cls._key(url)
        with cls._lock:
            cls._cache[key] = (html, time.time())


# ============================================================
# RENDER RESULT
# ============================================================

class RenderResult:
    def __init__(self, content=None, final_url=None, status_code=0, error=None):
        self.content = content
        self.final_url = final_url
        self.status_code = status_code
        self.error = error


# ============================================================
# BROWSER MANAGER
# ============================================================

class BrowserManager:

    _num_workers = 5
    _workers = []
    _queues = []
    _init_lock = threading.Lock()
    _rr_index = 0

    # --------------------------------------------------------
    # Worker Thread
    # --------------------------------------------------------

    class _Worker(threading.Thread):

        def __init__(self, wid, task_queue):
            super().__init__(daemon=True, name=f"RenderWorker-{wid}")
            self.wid = wid
            self.task_queue = task_queue
            self.start()

        def run(self):

            while True:
                try:
                    with sync_playwright() as p:

                        browser = p.chromium.launch(
                            headless=True,
                            args=[
                                "--disable-gpu",
                                "--no-sandbox",
                                "--disable-dev-shm-usage",
                                "--disable-extensions",
                                "--disable-background-networking",
                                "--disable-renderer-backgrounding",
                                "--disable-blink-features=AutomationControlled",
                            ],
                        )

                        context = browser.new_context(
                            user_agent=USER_AGENT,
                            viewport={"width": 1280, "height": 900},
                            java_script_enabled=True,
                        )

                        # Block heavy resources (keep JS)
                        def route_handler(route):
                            if route.request.resource_type in (
                                "image",
                                "font",
                                "media",
                            ):
                                return route.abort()
                            return route.continue_()

                        context.route("**/*", route_handler)

                        page = context.new_page()

                        logger.info(f"[JS-ENGINE] Worker-{self.wid} ready")

                        while True:
                            item = self.task_queue.get()
                            if item is None:
                                return

                            url, result_q = item

                            try:
                                status_code = 0
                                html = ""
                                nav_error = None

                                # ====================================================
                                # STAGE 1: DOMContentLoaded attempt
                                # ====================================================
                                try:
                                    response = page.goto(
                                        url,
                                        wait_until="domcontentloaded",
                                        timeout=15000,
                                    )
                                    if response:
                                        status_code = response.status
                                except Exception as e:
                                    nav_error = e
                                    response = None

                                # ====================================================
                                # STAGE 2: Commit fallback if DOM failed
                                # ====================================================
                                if not response:
                                    try:
                                        response = page.goto(
                                            url,
                                            wait_until="commit",
                                            timeout=15000,
                                        )
                                        if response:
                                            status_code = response.status
                                    except Exception as e:
                                        nav_error = e
                                        logger.warning(
                                            f"[JS-ENGINE] Navigation fallback failed for {url}: {e}"
                                        )

                                # If navigation never produced a response, treat it as a hard failure.
                                if not response:
                                    raise RuntimeError(
                                        f"Navigation failed for {url}: {nav_error or 'no response from browser'}"
                                    )

                                # Browser error pages must not be processed as crawlable HTML.
                                final_url = page.url or ""
                                if final_url.lower().startswith("chrome-error://"):
                                    raise RuntimeError(
                                        f"Browser error page returned for {url}: {final_url}"
                                    )

                                # ====================================================
                                # STAGE 3: Stabilization
                                # ====================================================
                                try:
                                    page.wait_for_load_state("load", timeout=5000)
                                except:
                                    pass

                                try:
                                    page.wait_for_function(
                                        "document.readyState === 'complete'",
                                        timeout=5000
                                    )
                                except:
                                    pass

                                page.wait_for_timeout(500)

                                # ====================================================
                                # STAGE 4: Safe content extraction
                                # ====================================================
                                try:
                                    html = page.content()
                                except:
                                    page.wait_for_timeout(400)
                                    html = page.content()

                                result_q.put(
                                    RenderResult(
                                        html,
                                        final_url,
                                        status_code,
                                    )
                                )

                                # Fast reset
                                try:
                                    page.goto("about:blank", timeout=1500)
                                except:
                                    pass

                            except Exception as e:
                                logger.warning(
                                    f"[JS-ENGINE] Worker-{self.wid} page error: {e}"
                                )

                                try:
                                    page.close()
                                except:
                                    pass

                                try:
                                    page = context.new_page()
                                except:
                                    raise

                                result_q.put(RenderResult(error=e))

                            finally:
                                self.task_queue.task_done()

                except Exception as fatal:
                    logger.error(
                        f"[JS-ENGINE] Worker-{self.wid} crashed. Restarting... {fatal}"
                    )
                    time.sleep(2)
                    continue

    # --------------------------------------------------------
    # Initialization
    # --------------------------------------------------------

    @classmethod
    def _ensure_running(cls):
        with cls._init_lock:
            if (
                len(cls._workers) == cls._num_workers and
                len(cls._queues) == cls._num_workers
            ):
                return

            cls._workers = []
            cls._queues = []
            cls._rr_index = 0

            for i in range(cls._num_workers):
                q = queue.Queue()
                worker = cls._Worker(i, q)
                cls._queues.append(q)
                cls._workers.append(worker)

    # --------------------------------------------------------
    # Public API
    # --------------------------------------------------------

    @classmethod
    def render_sync(cls, url: str):
        cls._ensure_running()

        result_q = queue.Queue()

        worker_index = cls._rr_index % len(cls._queues)
        cls._rr_index += 1

        cls._queues[worker_index].put((url, result_q))

        result = result_q.get()

        if result.error:
            raise result.error

        return result.content, result.final_url, result.status_code


# ============================================================
# JS RENDER WORKER
# ============================================================

class JSRenderWorker(threading.Thread):

    def __init__(self):
        super().__init__(daemon=True)
        self.queue = queue.Queue()
        self.start()

    def run(self):
        while True:
            url, event = self.queue.get()
            try:
                html, final_url, status = BrowserManager.render_sync(url)
                event["html"] = html.strip() if html else ""
                event["final_url"] = final_url
                event["status_code"] = status
            except Exception as e:
                event["error"] = e
            finally:
                event["done"].set()
                self.queue.task_done()

    def render(self, url: str, timeout: int = 35):
        event = {
            "done": threading.Event(),
            "html": None,
            "final_url": None,
            "status_code": 0,
            "error": None,
        }

        self.queue.put((url, event))

        if not event["done"].wait(timeout):
            raise TimeoutError(f"JS rendering timeout for {url}")

        if event["error"]:
            raise event["error"]

        return event["html"], event["final_url"], event["status_code"]