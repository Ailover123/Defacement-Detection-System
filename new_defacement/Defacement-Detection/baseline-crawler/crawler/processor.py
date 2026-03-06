"""
FILE DESCRIPTION: Global content processing pipeline handling network fetching, link extraction, and URL sanitization.
CONSOLIDATED FROM: fetcher.py, parser.py, normalizer.py, url_utils.py, throttle.py
KEY FUNCTIONS/CLASSES: LinkUtility, TrafficControl, PageFetcher, LinkExtractor
"""

import os
import requests
import time
import urllib3
import tldextract
import threading
import re
from bs4 import BeautifulSoup
from urllib.parse import urlparse, urlunparse, urljoin, quote, unquote
from playwright.sync_api import sync_playwright
from crawler.core import USER_AGENT, REQUEST_TIMEOUT, DATA_DIR, JS_GOTO_TIMEOUT, JS_WAIT_TIMEOUT, JS_STABILITY_TIME, logger

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# === LINK UTILITY ===

class LinkUtility:

    # -------------------------------
    # NETWORK NORMALIZATION (FETCH)
    # -------------------------------
    @staticmethod
    def normalize_url_for_fetch(url: str) -> str:
        """
        Normalization for FETCHING (network safe).
        Forces https:// and smart www prepending.
        """
        if not url:
            return ""

        if "://" not in url:
            url = "https://" + url

        parsed = urlparse(url)
        scheme = "https" # Always force https
        netloc = parsed.netloc.lower()

        try:
            ext = tldextract.extract(url)
            # Only add www. if it is a naked domain (no subdomain)
            if not ext.subdomain:
                netloc = "www." + netloc
        except Exception:
            # Fallback
            if not netloc.startswith("www."):
                netloc = "www." + netloc

        path = parsed.path or ""
        path = path.rstrip("/")

        query = parsed.query

        return urlunparse((
            scheme,
            netloc,
            path,
            "",
            query,
            ""
        ))

    # -------------------------------
    # STORAGE CANONICAL (DB)
    # -------------------------------
    @staticmethod
    def get_canonical_id(url: str, base: str | None = None, enforce_www: bool = False) -> str:
        if not url:
            return ""

        # 🛡️ Ensure scheme exists for correct netloc/path parsing
        # This prevents "double www" if url is already a schemeless canonical ID
        if "://" not in url and not url.startswith("/"):
            url = "https://" + url

        if  base and "://" not in url:
            url = urljoin(base,url)

        parsed = urlparse(url)

        netloc = parsed.netloc.lower()

        # 🛡️ Force/Strip www based on enforce_www
        if enforce_www:
            if not netloc.startswith("www."):
                netloc = "www." + netloc
        else:
            if netloc.startswith("www."):
                netloc = netloc[4:]

        path = parsed.path or ""
        path = path.rstrip("/")

        query = f"?{parsed.query}" if parsed.query else ""

        if not path:
            return netloc

        return f"{netloc}{path}{query}"
    
    @staticmethod
    def normalize_url(url: str, *, base: str | None = None, preference_url: str | None = None) -> str:
        """
        Normalization for FETCHING (network safe).
        Does NOT affect DB identity.
        """

        if not url:
            return ""

        url = url.strip()
        if "://" not in url and not url.startswith("/"): url = "https://" + url
        if base: url = urljoin(base, url)
        parsed = urlparse(url)
        scheme = parsed.scheme or "https"
        netloc = parsed.netloc.lower()

        # Apply branding preference (fetch-only logic)
        if preference_url:
            if "://" not in preference_url:
                preference_url = "https://" + preference_url

            pref_netloc = urlparse(preference_url).netloc.lower()

            if netloc.replace("www.", "") == pref_netloc.replace("www.", ""):
                netloc = pref_netloc

        path = parsed.path or "/"
        path = path.rstrip("/")
        if not path:
            path = "/"

        query = parsed.query

        return urlunparse((scheme, netloc, path, "", query, ""))

    @staticmethod
    def force_www_url(url: str) -> str:
        """
        Fetch-only helper.
        Forces https:// scheme and adds www prefix ONLY for naked domains.
        Does NOT affect DB canonicalization.
        """
        if not url:
            return ""

        # Normalize scheme to https
        if "://" not in url:
            url = "https://" + url
            
        parsed = urlparse(url)
        # Always force https as requested
        scheme = "https"
        netloc = parsed.netloc.lower()

        try:
            ext = tldextract.extract(url)
            # Only add www. if it is a naked domain (no subdomain)
            if not ext.subdomain:
                new_netloc = f"www.{netloc}"
            else:
                # Keep existing subdomain (www, admin, etc.)
                new_netloc = netloc
        except Exception:
            # Fallback to simple logic if tldextract fails
            new_netloc = netloc if netloc.startswith("www.") else f"www.{netloc}"

        return urlunparse((
            scheme,
            new_netloc,
            parsed.path,
            parsed.params,
            parsed.query,
            parsed.fragment,
        ))


# === TRAFFIC CONTROL ===

class TrafficControl:
    """
    FLOW: Manages domain-wide pauses and worker scaling -> Tracks 429 rate limit events -> 
    Implements thread-safe wait periods before network requests.
    """
    SITE_PAUSES = {}
    SITE_SCALE_DOWN_REQUESTS = {}
    PAUSE_LOCK = threading.Lock()

    @classmethod
    def set_pause(cls, siteid, seconds=5, url=None):
        if not siteid: return
        with cls.PAUSE_LOCK:
            now = time.time()
            if seconds > 0:
                until = now + seconds
                # Only log if we aren't already paused or if this extends it significantly
                if cls.SITE_PAUSES.get(siteid, 0) < now:
                    cls.SITE_SCALE_DOWN_REQUESTS[siteid] = True
                    url_info = f" on {url}" if url else ""
                    logger.info(f"[THROTTLE] Site {siteid} hit 429/503{url_info}. Setting DOMAIN-WIDE PAUSE for {seconds}s and requesting SCALE DOWN.")
                cls.SITE_PAUSES[siteid] = max(cls.SITE_PAUSES.get(siteid, 0), until)
            else:
                if cls.SITE_PAUSES.get(siteid, 0) > now:
                    logger.info(f"[THROTTLE] Site {siteid} pause cleared.")
                cls.SITE_PAUSES[siteid] = 0

    @classmethod
    def should_scale_down(cls, siteid):
        with cls.PAUSE_LOCK: return cls.SITE_SCALE_DOWN_REQUESTS.get(siteid, False)

    @classmethod
    def reset_scale_down(cls, siteid):
        with cls.PAUSE_LOCK: cls.SITE_SCALE_DOWN_REQUESTS[siteid] = False

    @classmethod
    def get_remaining_pause(cls, siteid):
        if not siteid: return 0
        with cls.PAUSE_LOCK:
            remaining = cls.SITE_PAUSES.get(siteid, 0) - time.time()
            return max(0, remaining)


# === PAGE FETCHER ===

class PageFetcher:
    """
    FLOW: Checks for active domain pauses -> Executes HTTP request with browser-like headers -> 
    Handles 429 retries with exponential backoff -> Returns structured response or error details.
    """
    @staticmethod
    def fetch(url, siteid=None, referer=None):
        if siteid:
            remaining = TrafficControl.get_remaining_pause(siteid)
            if remaining > 0:
                logger.info(f"[THROTTLE] Pre-fetch pause active for site {siteid}. Waiting {remaining:.1f}s...")
                time.sleep(remaining)

        max_retries = 2
        retry_delay = 5
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Cache-Control": "max-age=0",
        }
        if referer:
            headers["Referer"] = referer

        for attempt in range(max_retries + 1):
            if siteid and attempt > 0:
                remaining = TrafficControl.get_remaining_pause(siteid)
                if remaining > 0:
                    time.sleep(remaining)

            start_time = time.time()
            try:
                r = requests.get(url, timeout=REQUEST_TIMEOUT, headers=headers, verify=False, allow_redirects=True)
                fetch_time_ms = int((time.time() - start_time) * 1000)
                content_type = r.headers.get("Content-Type", "").lower()

                if r.status_code == 429 or r.status_code == 503:
                    TrafficControl.set_pause(siteid, 5, url=url)
                    if attempt < max_retries:
                        logger.warning(f"[RETRY {attempt+1}/{max_retries}] {r.status_code} Error for {url}. Waiting locally for {retry_delay}s...")
                        time.sleep(retry_delay)
                        retry_delay *= 2
                        logger.info(f"Retrying {url} now (Attempt {attempt+1}/{max_retries+1})...")
                        continue
                    else:
                        logger.error(f"{r.status_code} Error persisted for {url} after {max_retries} retries. Final 5s pause.")
                        time.sleep(5)

                if 200 <= r.status_code < 300:
                    success = "text/html" in content_type or "application/json" in content_type
                    return {
                        "success": success,
                        "response": r,
                        "final_url": r.url,
                        "fetch_time_ms": fetch_time_ms,
                        "response_size": len(r.content),
                        "content_type": content_type,
                        "error": None if success else f"ignored content type: {content_type}"
                    }
                elif r.status_code == 307:
                    # 🛡️ Sucuri / anti-bot challenge often uses 307 with JS instead of Location header
                    return {
                        "success": False, "error": f"http error: {r.status_code}",
                        "response": r, "final_url": r.url,
                        "content_type": content_type, "fetch_time_ms": fetch_time_ms,
                        "html": r.text if "text/html" in content_type else "",
                    }
                else:
                    return {
                        "success": False, "error": f"http error: {r.status_code}",
                        "response": r, "final_url": r.url,
                        "content_type": content_type, "fetch_time_ms": fetch_time_ms,
                        "html": r.text if "text/html" in content_type else "",
                    }
            except requests.exceptions.TooManyRedirects as e:
                # Capture the "Real" status of the last hop if available
                last_status = 0
                if hasattr(e, 'response') and e.response is not None:
                    last_status = e.response.status_code
                return {
                    "success": False, "error": "Too many redirects", 
                    "status_code": last_status, "final_url": url,
                    "content_type": "", "fetch_time_ms": int((time.time() - start_time) * 1000)
                }
            except Exception as e:
                if isinstance(e, (requests.exceptions.ConnectionError, requests.exceptions.Timeout)) and attempt < max_retries:
                    err_type = "Timeout" if isinstance(e, requests.exceptions.Timeout) else "Connection Error"
                    logger.warning(f"[RETRY {attempt+1}/{max_retries}] {err_type} for {url}: {e}. Waiting {retry_delay}s...")
                    time.sleep(retry_delay)
                    retry_delay *= 2
                    logger.info(f"Retrying {url} now (Attempt {attempt+1}/{max_retries+1})...")
                    continue
                
                # Base case for all other networking/connection failures
                return {
                    "success": False, "error": str(e), 
                    "status_code": 0, "final_url": url,
                    "content_type": "", "fetch_time_ms": int((time.time() - start_time) * 1000)
                }

    @staticmethod
    def fetch_rendered(url, siteid=None, save_to_tmp=False):
        """
        Optimized Playwright fetcher.
        Uses a shared browser context and blocks heavy resources for speed.
        """
        from crawler.js_engine import BrowserManager
        start_time = time.time()
        try:
            # We use a custom render call that supports resource blocking
            # Or we just use BrowserManager.render_sync and ensure it's optimized.
            html, final_url, status_code = BrowserManager.render_sync(url)
            fetch_time_ms = int((time.time() - start_time) * 1000)

            if 200 <= status_code <= 299:
                # Save to temp if requested (User snippet logic)
                if save_to_tmp and siteid:
                    try:
                        tmp_dir = DATA_DIR / "tmp"
                        tmp_dir.mkdir(parents=True, exist_ok=True)
                        filepath = tmp_dir / f"{siteid}.html"
                        filepath.write_text(html, encoding="utf-8")
                    except Exception as e:
                        logger.error(f"[PageFetcher] Failed to save tmp file: {e}")

                return {
                    "success": True,
                    "html": html,
                    "status_code": status_code,
                    "final_url": final_url,
                    "fetch_time_ms": fetch_time_ms,
                    "content_type": "text/html",
                    "response": type('obj', (object,), {
                        'status_code': status_code,
                        'text': html,
                        'content': html.encode(),
                        'url': final_url,
                        'headers': {'Content-Type': 'text/html'}
                    })
                }
            else:
                return {
                    "success": False,
                    "status_code": status_code,
                    "error": f"HTTP {status_code}",
                    "fetch_time_ms": fetch_time_ms
                }
        except Exception as e:
            logger.error(f"[Optimized Playwright Error] {e}")
            return {
                "success": False,
                "error": str(e),
                "status_code": 0,
                "fetch_time_ms": int((time.time() - start_time) * 1000)
            }
# === LINK EXTRACTOR ===

class LinkExtractor:
    """
    FLOW: Parses HTML using BeautifulSoup -> Identifies all anchors and asset links -> 
    Applies domain-boundary filters -> Classifies URLs into categories (Pagination, Static, etc.) -> 
    Returns separate lists for discovery and auditing.
    """
    @staticmethod
    def classify_url(url):
        types = set()
        url_lower = url.lower()
        path = urlparse(url).path.lower()
        if any(pat in url_lower for pat in ['/page/', '/p/', '?page=', '?p=', '/pagination/']): types.add('pagination')
        if any(pat in url_lower for pat in ['/uploads/', '/assets/', '/wp-content/uploads/', '/media/', '/files/']): types.add('assets_uploads')
        if any(path.endswith(ext) for ext in ['.pdf', '.jpg', '.jpeg', '.png', '.gif', '.svg']): types.add('assets_uploads')
        if path.endswith('.css') or path.endswith('.js'): types.add('scripts_styles')
        if 'wp-json' in url_lower or '/api/' in url_lower: types.add('api_like')
        if not types: types.add('normal_html')
        return types

    @staticmethod
    def extract_urls(html, base_url):
        soup = BeautifulSoup(html, 'html.parser')
        base_domain = urlparse(base_url).netloc
        urls, assets = [], []

        def strip_fragment(u):
            p = urlparse(u)
            return urlunparse((p.scheme, p.netloc, p.path, p.params, p.query, ""))

        for a in soup.find_all('a', href=True):
            href = a['href'].strip()
            if not href or href.startswith('#') or href.startswith('mailto:') or href.startswith('tel:'): continue
            
            # Heuristic for domain-prefixed links (e.g., allianceproit.com/services)
            if not href.startswith('/') and '://' not in href and not href.startswith('#'):
                # Extract the potential domain part, ignoring leading ./ or ../
                temp_href = href
                while temp_href.startswith('./') or temp_href.startswith('../'):
                    if temp_href.startswith('./'): temp_href = temp_href[2:]
                    else: temp_href = temp_href[3:]
                
                first_part = temp_href.split('/')[0].lower()
                if '.' in first_part and not first_part.startswith('.'):
                    current_netloc = urlparse(base_url).netloc.lower().replace('www.', '')
                    clean_cand = first_part.replace('www.', '')
                    # ✅ Refined heuristic: Only match if it's the same domain or a clear subdomain relationship
                    if clean_cand == current_netloc or clean_cand.endswith("." + current_netloc) or current_netloc.endswith("." + clean_cand):
                        href = f"{urlparse(base_url).scheme or 'https'}://{temp_href}"

            url = strip_fragment(urljoin(base_url, href))
            if "®" in url: url = url.replace("®", "&reg")
            if LinkExtractor._is_allowed_url(url, base_domain): urls.append(url)

        for img in soup.find_all('img', src=True):
            asset_url = strip_fragment(urljoin(base_url, img['src']))
            if LinkExtractor._is_allowed_url(asset_url, base_domain): assets.append(asset_url)

        for link in soup.find_all('link', href=True):
            asset_url = strip_fragment(urljoin(base_url, link['href']))
            if LinkExtractor._is_allowed_url(asset_url, base_domain): assets.append(asset_url)

        for script in soup.find_all('script', src=True):
            asset_url = strip_fragment(urljoin(base_url, script['src']))
            if LinkExtractor._is_allowed_url(asset_url, base_domain): assets.append(asset_url)

        return urls, assets

    @staticmethod
    def _is_allowed_url(url, base_domain):
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"): return False
        
        # ✅ Block recursive malformed paths (e.g. domain.com/path/domain.com/...)
        domain_part = base_domain.replace("www.", "").lower()
        path_lower = parsed.path.lower()
        if (f"/{domain_part}/" in path_lower or 
            path_lower.startswith(f"{domain_part}/") or 
            path_lower.endswith(f"/{domain_part}")):
            return False

        cand_ext = tldextract.extract(url)
        base_ext = tldextract.extract(f"https://{base_domain}")
        return cand_ext.registered_domain == base_ext.registered_domain


# === HTML NORMALIZER (FOR HASHING) ===

class ContentNormalizer:
    """
    FLOW: Strips dynamic noise (IDs, nonces, timestamps) using regex -> 
    Standardizes whitespace and tag casing -> Returns deterministic HTML for stable fingerprinting.
    """
    SKIP_PATTERNS = [
        re.compile(r'(?i)"[^"]*nonce[^"]*"\s*:\s*["\'](?:[^"\\]|\\.)*["\']'),
        re.compile(r'(?i)\b[\w:-]*nonce[\w:-]*\s*=\s*["\'](?:[^"\\]|\\.)*["\']'),
        re.compile(r'(?i)\bvalue\s*=\s*["\'](?:[^"\\]|\\.)*["\']'),
        re.compile(r'(?i)\bid\s*=\s*["\'][^"\']*["\']'),
        re.compile(r'(?i)"floatingButtonsClickTracking"\s*:\s*["\'](?:[^"\\]|\\.)*["\']'),
        re.compile(r'(?i)\baria-controls\s*=\s*["\'][^"\']*["\']'),
        re.compile(r'(?i)\baria-labelledby\s*=\s*["\'][^"\']*["\']'),
        re.compile(r'(?i)\bdata-smartmenus-id\s*=\s*["\'][^"\']*["\']'),
        re.compile(r'(?i)\bname\s*=\s*["\'][^"\']*["\']'),
        re.compile(r'(?i)\bcb\s*=\s*["\'][^"\']*["\']'),
    ]

    @classmethod
    def normalize_html(cls, html: str) -> str:
        if not html: return ""
        for pattern in cls.SKIP_PATTERNS:
            html = pattern.sub('', html)
        soup = BeautifulSoup(html, "lxml")
        for tag in soup(["noscript"]): tag.decompose()
        normalized = soup.prettify()
        return "\n".join(line.strip() for line in normalized.splitlines() if line.strip())

    @staticmethod
    def _html_to_semantic_lines(html: str) -> list[str]:
        """Convert HTML into whitespace-stable, semantic lines."""
        soup = BeautifulSoup(html or "", "lxml")
        lines: list[str] = []

        def walk(node, depth: int = 0) -> None:
            indent = "  " * depth
            from bs4 import NavigableString, Tag
            if isinstance(node, NavigableString):
                text = " ".join(str(node).split())
                if text: lines.append(indent + text)
                return
            if isinstance(node, Tag):
                attrs = " ".join(
                    f'{key}="{ " ".join(value) if isinstance(value, list) else value }"'
                    for key, value in sorted(node.attrs.items())
                )
                lines.append(indent + f"<{node.name}{(' ' + attrs) if attrs else ''}>")
                for child in node.children: walk(child, depth + 1)
                lines.append(indent + f"</{node.name}>")

        for child in soup.contents: walk(child)
        return lines

    @staticmethod
    def semantic_hash(html: str) -> str:
        """Return a SHA256 fingerprint of the semantic HTML content."""
        lines = ContentNormalizer._html_to_semantic_lines(html)
        payload = "\n".join(lines)
        import hashlib
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()