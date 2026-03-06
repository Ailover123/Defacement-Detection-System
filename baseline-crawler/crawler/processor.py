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
import compare_utils
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

import requests
from crawler.js_engine import BrowserManager, JSIntelligence
from crawler.core import USER_AGENT, logger


class PageFetcher:

    TIMEOUT = 15

    # Class-level WAF bypass state (set via configure())
    _current_waf_ip = None
    _current_primary_host = None

    @classmethod
    def configure(cls, domain, waf_ip):
        """
        Set the WAF bypass IP for HTTP fetching.
        When set, HTTP requests route to the IP directly with a Host header.
        Pass None to disable bypass.
        """
        cls._current_waf_ip = waf_ip if waf_ip else None
        cls._current_primary_host = domain if domain else None
        if waf_ip:
            logger.info(f"[FETCH] HTTP bypass configured: {domain} -> {waf_ip}")
        else:
            logger.info("[FETCH] HTTP bypass disabled (standard routing).")

    @staticmethod
    def _is_invalid_render_result(final_url: str, html: str) -> bool:
        final = (final_url or "").lower()
        body = (html or "").lower()

        if final.startswith("chrome-error://") or final.startswith("devtools://"):
            return True

        if "chrome-error://chromewebdata" in body or "net::err_" in body:
            return True

        return False

    @staticmethod
    def fetch(url: str, siteid=None):
        """
        FAST HTTPS-first fetch.
        If PageFetcher._current_waf_ip is set and the URL matches the primary host,
        routes via WAF IP directly (IP as URL host + Host header).
        Handles redirects manually to maintain WAF IP routing.
        """
        from urllib.parse import urlparse, urlunparse, urljoin
        
        waf_ip = PageFetcher._current_waf_ip
        primary_host = PageFetcher._current_primary_host
        
        current_url = url
        max_redirects = 10
        redirect_count = 0
        
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        }

        try:
            while redirect_count < max_redirects:
                fetch_url = current_url
                original_netloc = None
                
                parsed = urlparse(current_url)
                if waf_ip and primary_host:
                    url_host = parsed.netloc.lower().replace("www.", "")
                    target_host = primary_host.lower().replace("www.", "")
                    if url_host == target_host:
                        # Replace host with WAF IP, keep domain in Host header
                        ip_url = urlunparse(parsed._replace(netloc=waf_ip))
                        fetch_url = ip_url
                        headers["Host"] = parsed.netloc
                        original_netloc = parsed.netloc

                response = requests.get(
                    fetch_url,
                    headers=headers,
                    timeout=PageFetcher.TIMEOUT,
                    allow_redirects=False,  # 🛡️ Manual control
                    verify=False,
                )

                # Check for redirect
                if response.status_code in (301, 302, 303, 307, 308):
                    location = response.headers.get("Location")
                    if not location:
                        break
                    
                    # Resolve relative redirects
                    next_url = urljoin(current_url, location)
                    if next_url == current_url: # Infinite loop protection
                        break
                        
                    current_url = next_url
                    redirect_count += 1
                    continue
                else:
                    # Not a redirect, we're done
                    # Restore final_url if we used IP
                    final_url = response.url
                    if original_netloc:
                        resp_parsed = urlparse(response.url)
                        final_url = urlunparse(resp_parsed._replace(netloc=original_netloc))
                    
                    return {
                        "success": True,
                        "status_code": response.status_code,
                        "html": response.text or "",
                        "final_url": final_url,
                        "content_type": response.headers.get("Content-Type", ""),
                    }

            return {
                "success": False,
                "status_code": 0,
                "html": "",
                "final_url": current_url,
                "error": f"Max redirects ({max_redirects}) exceeded" if redirect_count >= max_redirects else "Redirect loop detected",
            }

        except Exception as e:
            logger.warning(f"[FETCH] HTTP fetch failed for {url}: {e}")
            return {
                "success": False,
                "status_code": 0,
                "html": "",
                "final_url": current_url,
                "error": str(e),
            }

    # ------------------------------------------------------------
    # SMART FETCH (HTTP + OPTIONAL JS)
    # ------------------------------------------------------------

    @staticmethod
    def fetch_rendered(url: str, siteid=None, save_to_tmp=False, force_js=False):
        """
        Smart fetch pipeline:

        1. HTTP fetch (fast)
        2. Detect if JS rendering is required
        3. Use Playwright only when necessary
        """

        start = time.time()
        http_result = PageFetcher.fetch(url, siteid)

        html = http_result.get("html", "")
        http_html = html
        http_final_url = http_result.get("final_url", url)
        http_status_code = http_result.get("status_code", 0)
        http_content_type = http_result.get("content_type", "")

        # ------------------------------------------------------
        # If HTTP failed → fallback to JS rendering
        # ------------------------------------------------------

        if not http_result["success"]:

            logger.info(f"[FETCH] HTTP failed → using JS render: {url}")

            try:
                _wip = PageFetcher._current_waf_ip
                _ph = PageFetcher._current_primary_host
                html, final_url, status = BrowserManager.render_sync(url, waf_ip=_wip, primary_host=_ph)
                # Retry once if truncated (no <body>)
                if html and "<body" not in html.lower():
                    logger.warning(f"[FETCH] Truncated JS render (no <body>), retrying: {url}")
                    time.sleep(2)
                    html, final_url, status = BrowserManager.render_sync(url, waf_ip=_wip, primary_host=_ph)
                return {
                    "success": True,
                    "html": html,
                    "status_code": status,
                    "final_url": final_url,
                    "content_type": "text/html",
                    "fetch_time_ms": int((time.time() - start) * 1000),
                }

            except Exception as e:

                return {
                    "success": False,
                    "error": str(e),
                    "html": "",
                    "status_code": 0,
                    "final_url": url,
                    "content_type": "",
                    "fetch_time_ms": int((time.time() - start) * 1000),
                }

        # ------------------------------------------------------
        # Check if page requires JS rendering
        # ------------------------------------------------------

        if force_js or JSIntelligence.needs_js_rendering(html):

            logger.info(f"[FETCH] JS rendering required: {url}")

            try:
                _wip = PageFetcher._current_waf_ip
                _ph = PageFetcher._current_primary_host
                html, final_url, status = BrowserManager.render_sync(url, waf_ip=_wip, primary_host=_ph)

                # Retry once if truncated (no <body>)
                if html and "<body" not in html.lower():
                    logger.warning(f"[FETCH] Truncated JS render (no <body>), retrying: {url}")
                    time.sleep(2)
                    html, final_url, status = BrowserManager.render_sync(url, waf_ip=_wip, primary_host=_ph)

                if PageFetcher._is_invalid_render_result(final_url, html):
                    err = f"Invalid JS render result for {url}: final_url={final_url}"
                    logger.warning(f"[FETCH] {err}")
                    return {
                        "success": False,
                        "error": err,
                        "html": "",
                        "status_code": 0,
                        "final_url": url,
                        "content_type": "",
                        "fetch_time_ms": int((time.time() - start) * 1000),
                    }

                return {
                    "success": True,
                    "html": html,
                    "status_code": status,
                    "final_url": final_url,
                    "content_type": "text/html",
                    "fetch_time_ms": int((time.time() - start) * 1000),
                }

            except Exception as e:

                logger.warning(f"[FETCH] JS render failed for {url}: {e}")

                # Keep crawl progress when HTTP already returned usable HTML.
                if http_html:
                    logger.warning(
                        f"[FETCH] Falling back to HTTP HTML after JS failure: {url}"
                    )
                    return {
                        "success": True,
                        "html": http_html,
                        "status_code": http_status_code,
                        "final_url": http_final_url,
                        "content_type": http_content_type,
                        "fetch_time_ms": int((time.time() - start) * 1000),
                    }

                return {
                    "success": False,
                    "error": str(e),
                    "html": "",
                    "status_code": 0,
                    "final_url": url,
                    "content_type": "",
                    "fetch_time_ms": int((time.time() - start) * 1000),
                }

        # ------------------------------------------------------
        # No JS needed → return HTTP HTML
        # ------------------------------------------------------

        logger.debug(f"[FETCH] Using HTTP HTML (no JS needed): {url}")

        return {
            "success": True,
            "html": html,
            "status_code": http_result["status_code"],
            "final_url": http_result["final_url"],
            "content_type": http_result.get("content_type", ""),
            "fetch_time_ms": int((time.time() - start) * 1000),
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
    def semantic_hash(html: str) -> str:
        """Return a SHA256 fingerprint of the semantic HTML content using unified rules."""
        return compare_utils.semantic_hash(html)