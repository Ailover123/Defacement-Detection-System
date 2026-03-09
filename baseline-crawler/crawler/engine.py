"""
FILE DESCRIPTION: Global orchestration module managing worker threads, crawl frontier, and execution lifecycle.
CONSOLIDATED FROM: worker.py, frontier.py, queue.py
KEY FUNCTIONS/CLASSES: CrawlerWorker, Frontier, ExecutionPolicy
"""

import threading
import time
import re
from collections import defaultdict, deque
from urllib.parse import urlparse
from datetime import datetime
from queue import Queue, Empty

from crawler.core import CRAWL_DELAY, logger
from crawler.processor import LinkUtility, PageFetcher, LinkExtractor, TrafficControl
from crawler.js_engine import JSIntelligence, RenderCache, JSRenderWorker
from crawler.storage.db import insert_crawl_page
from crawler.storage.baseline_store import save_baseline
from crawler.compare_engine import CompareEngine

# === EXECUTION POLICY ===

class ExecutionPolicy:
    """
    FLOW: Defines static skip rules for URLs (Assets, Tags, Params) -> 
    Implements strict domain boundary checks -> Classifies if a URL should be enqueued.
    """
    PATH_SKIP_RULES = {
        "TAG_PAGE": r"^/(product-)?tag/",
        "AUTHOR_PAGE": r"^/author/",
        "PAGINATION": r"/page/\d*/?$",
        "ASSET_DIRECTORY": r"^/(assets|static|media|uploads|images|img|css|js)/",
    }
    QUERY_SKIP_RULES = {
        "PAGINATION": r"(^|&)(page|paged|p)=",
        "SORTING": r"(orderby|sort|order|filter_|display)=",
        "ACTIONS": r"(add-to-cart|add_to_wishlist|action=yith-woocompare|remove_item)",
        "SITE_QUERY": r"(^|&)site=",
        "GL_TRACKING": r"(^|&)_gl=",
        "UTM_MARKETING": r"(^|&)utm_",
    }
    STATIC_EXTENSIONS = (
        ".css", ".js", ".png", ".jpg", ".jpeg", ".webp",
        ".gif", ".svg", ".ico", ".woff", ".woff2",
        ".ttf", ".eot", ".pdf", ".zip", ".xlsx",
        ".xls", ".docx", ".doc", ".gz", ".tar",
        ".ppt", ".pptx", ".mp3"
    )
    @staticmethod
    def is_recursion(url):
        """Detects infinite recursion in paths (e.g. /a/b/a/b or repeated domain segments)"""
        parsed = urlparse(url)
        path = parsed.path.strip('/')
        if not path: return False
        segments = [s.lower() for s in path.split('/') if s]
        if len(segments) < 3: return False
        
        # 1. Simple count of identical segments
        counts = defaultdict(int)
        for s in segments:
            if len(s) < 3: continue
            counts[s] += 1
            if counts[s] > 2: return True
            
        # 2. Check for consecutive repeating sequences (e.g. /foo/bar/foo/bar)
        for n in range(1, len(segments)//2 + 1):
            for i in range(len(segments) - 2*n + 1):
                if segments[i:i+n] == segments[i+n:i+2*n]:
                    return True
        return False

    @classmethod
    def classify_skip(cls, url: str):
        parsed = urlparse(url)
        path = parsed.path or ""
        if path.lower().endswith(cls.STATIC_EXTENSIONS): return "STATIC"
        if parsed.query: return "QUERY_PARAM"
        for k, r in cls.PATH_SKIP_RULES.items():
            if re.search(r, path.lower()): return k
        for k, r in cls.QUERY_SKIP_RULES.items():
            if re.search(r, parsed.query.lower()): return k
        return None

    @staticmethod
    def is_allowed_domain(seed_url: str, candidate_url: str, current_url: str = None) -> bool:
        # 🔒 Robust scheme handling for urlparse
        temp_seed = seed_url if "://" in seed_url else "https://" + seed_url
        temp_cand = candidate_url if "://" in candidate_url else "https://" + candidate_url
        
        s_netloc = urlparse(temp_seed).netloc.lower().split(":")[0]
        c_netloc = urlparse(temp_cand).netloc.lower().split(":")[0]
        
        if s_netloc == c_netloc: return True
        if current_url:
            temp_curr = current_url if "://" in current_url else "https://" + current_url
            curr_netloc = urlparse(temp_curr).netloc.lower().split(":")[0]
            if curr_netloc == c_netloc: return True
            
        s_base = s_netloc[4:] if s_netloc.startswith("www.") else s_netloc
        c_base = c_netloc[4:] if c_netloc.startswith("www.") else c_netloc
        return s_base == c_base and s_base != ""


# === FRONTIER MANAGEMENT ===

class Frontier:
    """
    FLOW: Manages thread-safe discovery sets (visited, in-progress) -> 
    Implements duplicate prevention -> Maintains the primary work queue for worker threads.
    """
    def __init__(self):
        self.queue = Queue(maxsize=0)
        self.visited = set()
        self.in_progress = set()
        self.discovered = set()
        self.lock = threading.Lock()

    def enqueue(self, url, discovered_from=None, depth=0, preference_url=None) -> str:
        """
        Returns:
        - "enqueued" if successful
        - "policy_skipped" if ExecutionPolicy rejected it
        - "duplicate" if already visited/in-progress
        - "recursion" if recursion detected
        - "failed" for other errors
        """
        # ✅ Synchronize normalization first
        normalized = LinkUtility.normalize_url(url, preference_url=preference_url)

        # ✅ Enforcement of skip rules (Tags, Authors, Assets, etc.)
        if ExecutionPolicy.classify_skip(normalized):
            return "policy_skipped"

        with self.lock:
            p = urlparse(normalized)
            if p.scheme in ("mailto", "tel", "javascript"): return "policy_skipped"
            if normalized in self.visited or normalized in self.in_progress: return "duplicate"
            
            # Recursion protection
            if ExecutionPolicy.is_recursion(normalized):
                return "recursion"

            self.in_progress.add(normalized)
            self.discovered.add(normalized)
        try:
            self.queue.put((normalized, discovered_from, depth))
            return "enqueued"
        except Exception:
            with self.lock: self.in_progress.discard(normalized)
            return "failed"

    def dequeue(self):
        try:
            return self.queue.get(timeout=0.5), True
        except Empty:
            return None, False

    def mark_visited(self, url, *, got_task: bool, preference_url=None):
        normalized = LinkUtility.normalize_url(url, preference_url=preference_url)
        with self.lock:
            self.in_progress.discard(normalized)
            self.visited.add(normalized)
        if got_task:
            try:
                self.queue.task_done()
            except ValueError:
                pass

    def get_stats(self):
        with self.lock:
            return {
                "queued": self.queue.qsize(),
                "visited_count": len(self.visited),
                "in_progress_count": len(self.in_progress),
                "discovered": len(self.discovered)
            }




# === CRAWLER WORKER ===

JS_RENDERER = JSRenderWorker()

class CrawlerWorker(threading.Thread):
    """
    FLOW: Main worker loop -> Dequeues URL -> Throttles per domain rules -> 
    Fetches (with optional JS escalation) -> Persists to DB -> Extracts new links -> Re-enqueues per policy.
    """
    SKIP_REPORT = defaultdict(lambda: {"count": 0, "urls": []})
    SKIP_LOCK = threading.Lock()

    def __init__(self, frontier, name, custid, siteid_map, job_id, crawl_mode, seed_url, original_site_url=None, skip_report=None, skip_lock=None, target_urls=None, compare_results=None, compare_lock=None, waf_ip=None):
        super().__init__(name=name)
        self.frontier = frontier
        self.running = True
        self.custid = custid
        self.siteid = next(iter(siteid_map.values()))
        self.job_id = job_id
        self.crawl_mode = crawl_mode
        self.seed_url = seed_url
        self.original_site_url = original_site_url
        self.waf_ip = waf_ip  # Per-site WAF IP — used in failure logs to show routing context
        self.skip_report = skip_report if skip_report is not None else defaultdict(lambda: {"count": 0, "urls": []})
        self.skip_lock = skip_lock if skip_lock is not None else threading.Lock()
        self.target_urls = target_urls
        self.compare_results = compare_results
        self.compare_lock = compare_lock
        self.saved_count = 0
        self.failed_count = 0
        self.policy_skipped_count = 0
        self.frontier_duplicate_count = 0
        self.redirect_count = 0
        self.existed_urls = set()
        self.new_urls = []
        self.js_render_stats = {"total": 0, "success": 0, "failed": 0}
        self.failure_reasons = defaultdict(int)
        self.failed_throttle_count = 0 # Tracks 429 and 503 errors
        self.missing_baselines = [] # ✅ Track URLs with missing baselines

        # 🕵️ Detect if the site is registered with 'www.' to enforce it in canonical ID
        self.enforce_www = False
        if self.original_site_url:
            from urllib.parse import urlparse
            temp_url = self.original_site_url
            if "://" not in temp_url:
                temp_url = "https://" + temp_url
            self.enforce_www = urlparse(temp_url).netloc.lower().startswith("www.")

    def stop(self):
        self.running = False

    def is_soft_redirect(self, html: str) -> bool:
        if not html: return False
        h = html.lower()
        # Standard Meta/JS redirects
        if 'http-equiv="refresh"' in h or 'window.location' in h:
            return True
        # Sucuri Cloudproxy anti-bot challenge
        if 'sucuri_cloudproxy_js' in h or 'sucuri.net/using-firewall' in h:
            return True
        return False

    def _db_url(self, url):
        # ✅ Standardize on Canonical ID for log consistency
        from crawler.processor import LinkUtility
        return LinkUtility.get_canonical_id(url, self.original_site_url or "", enforce_www=self.enforce_www)

    def log(self, level, msg):
        getattr(logger, level)(msg, extra={'context': self.name})

    def run(self):
        self.log("info", f"started ({self.crawl_mode})")

        # ✅ Initialize CompareEngine once per worker for efficiency (as requested by USER)
        compare_engine = None
        if self.crawl_mode == "COMPARE":
            compare_engine = CompareEngine(custid=self.custid)

        while self.running:
            # ✅ Add Throttling Alignment (from origin/merger)
            remaining = TrafficControl.get_remaining_pause(self.siteid)
            if remaining > 0:
                 self.log("info", f"Domain-wide pause active. Sleeping {remaining:.1f}s...")
                 time.sleep(remaining)

            item, found = self.frontier.dequeue()
            if not found:
                if self.frontier.get_stats()["in_progress_count"] == 0:
                    break
                time.sleep(0.5)
                continue

            url, discovered_from, depth = item
            self.log("info", f"Processing: {url} (depth={depth})")

            try:
                # -------------------------
                # FETCH (Rendering enabled with temp file matching Baseline logic)
                # -------------------------
                # ✅ Smart Preference: Follow the seed URL's subdomain style (naked vs www)
                fetch_url = LinkUtility.normalize_url(url, preference_url=self.seed_url)
                
                force_js = self.crawl_mode == "COMPARE"
                result = PageFetcher.fetch_rendered(fetch_url, siteid=self.siteid, save_to_tmp=True, force_js=force_js)
                final_url = result.get("final_url", fetch_url)
                html = result.get("html", "")

                # 📝 Log Client-Side Redirects (JS or Meta)
                if LinkUtility.normalize_url(final_url) != LinkUtility.normalize_url(fetch_url):
                    self.log("info", f"[REDIRECT] Client-side: {fetch_url} -> {final_url}")

                if result.get("error") and any(err in str(result["error"]) for err in ("429", "503")):
                    self.failed_throttle_count += 1

                initial_status = result.get("status_code", 0)

                # 🛡️ Detect Soft 404
                if result["success"] and result.get("html"):
                    if JSIntelligence.is_404_content(result["html"]):
                        self.log("info", f"Soft 404 detected for {url}. Tracking as 404.")
                        result["success"] = False
                        initial_status = 404

                # 🛡️ Reject chromewebdata / empty renders (ONLY for CRAWL mode)
                if self.crawl_mode == "CRAWL" and (not html or result.get("status_code") == 0):
                    self.log("warning", f"CRAWL: Skipping insertion/processing for empty/failed render: {url}")
                    continue

                if not result["success"]:
                    reason = result.get("error", "Unknown Fetch Error")
                    if "ignored content type" in str(reason):
                        continue

                    self.failed_count += 1
                    if initial_status == 404: reason = "http error: 404"
                    self.failure_reasons[reason] += 1
                    if self.waf_ip:
                        waf_is_hostname = any(c.isalpha() for c in self.waf_ip)
                        if waf_is_hostname:
                            import socket
                            try:
                                resolved = socket.gethostbyname(self.waf_ip)
                                _routing = f"via {self.waf_ip} → {resolved}"
                            except Exception:
                                _routing = f"via {self.waf_ip} (DNS unresolved)"
                        else:
                            _routing = f"via {self.waf_ip}"
                    else:
                        _routing = "direct (no WAF IP)"
                    self.log("error", f"Fetch failed for {url}: {reason} [{_routing}]")
                    
                    # ✅ Sync fail status to DB in COMPARE mode as requested
                    if self.crawl_mode == "COMPARE":
                        # ✅ USE RAW STATUS CODE ONLY (No assumptions as requested)
                        sync_status = result.get("status_code", initial_status)
                        
                        self.log("info", f"[DEBUG-COMPARE] Syncing RAW failure to DB: {url} | Status: {sync_status} (Reason: {reason})")
                        
                        insert_crawl_page({
                            "job_id": self.job_id,
                            "custid": self.custid,
                            "siteid": self.siteid,
                            "url": url,
                            "parent_url": LinkUtility.get_canonical_id(discovered_from, self.original_site_url, enforce_www=self.enforce_www) if discovered_from else None,
                            "depth": depth,
                            "status_code": sync_status,
                            "content_type": "",
                            "content_length": 0,
                            "response_time_ms": result.get("fetch_time_ms", 0),
                            "fetched_at": datetime.now(),
                            "base_url": self.original_site_url
                        })

                    # 🚫 Skip remaining processing (DB insert, link extraction) for failed URLs
                    continue
                    

                # JS Rendering escalation loop (redundant now but kept for logic consistency)
                # ...

                # ✅ Synchronize <base> tag injection (Match BaselineWorker format)
                # if "\u003cbase" not in html_content.lower():
                #     html_content = re.sub(
                #         r"(\u003chead[^>]*>)",
                #         rf'\1\u003cbase href="{fetch_url}"\u003e',
                #         html_content,
                #         count=1,
                #         flags=re.IGNORECASE
                #     )
                # ======================================================
                # MODE: CRAWL (Database Updates)
                # ======================================================
                if self.crawl_mode == "CRAWL":
                    # Insert into crawl_pages (Guard: ONLY in CRAWL mode as requested by USER)
                    # ✅ Capture return value to only increment count on SUCCESSFUL insert
                    res = insert_crawl_page({
                        "job_id": self.job_id,
                        "custid": self.custid,
                        "siteid": self.siteid,
                        "url": final_url, # Pass raw URL, mysql.py handles canonicalization
                        "parent_url": LinkUtility.get_canonical_id(discovered_from, self.original_site_url, enforce_www=self.enforce_www) if discovered_from else None,
                        "depth": depth,
                        "status_code": result.get("status_code", 0),
                        "content_type": result.get("content_type", ""),
                        "content_length": len(html.encode("utf-8")) if html else 0,
                        "response_time_ms": result.get("fetch_time_ms", 0),
                        "fetched_at": datetime.now(),
                        "base_url": self.original_site_url
                    })
                    if res and res.get("action") == "Inserted":
                        self.saved_count += 1
                        self.log("info", f"DB: Inserted {self._db_url(final_url)} (ID: {res.get('id')})")
                    elif res and res.get("action") == "Existed":
                        self.log("info", f"DB: Existed (Not-Touched) {self._db_url(final_url)} (ID: {res.get('id')})")
                        self.existed_urls.add(LinkUtility.normalize_url(final_url, preference_url=self.original_site_url))

                # ======================================================
                # MODE: BASELINE
                # ======================================================
              

                # ======================================================
                # MODE: COMPARE
                # ======================================================
                elif self.crawl_mode == "COMPARE":
                    results = compare_engine.handle_page(
                        siteid=self.siteid,
                        url=final_url, # ✅ Pass full URL to prevent corruption in CompareEngine
                        html=html,
                        base_url=self.original_site_url,
                        enforce_www=self.enforce_www
                    )

                    if results:
                        for r in results:
                            # ✅ Determine description update
                            desc_val = None
                            if r["status"] in ("EMPTY_BASELINE", "NOT_MONITORED"):
                                desc_val = "empty_baseline"
                                self.missing_baselines.append(r["url"])
                            
                            # ✅ Always update/sync description in COMPARE mode
                            res = insert_crawl_page({
                                "job_id": self.job_id,
                                "custid": self.custid,
                                "siteid": self.siteid,
                                "url": r["url"],
                                "parent_url": LinkUtility.get_canonical_id(discovered_from, self.original_site_url, enforce_www=self.enforce_www) if discovered_from else None,
                                "depth": depth,
                                "status_code": result.get("status_code", 0),
                                "content_type": result.get("content_type", ""),
                                "content_length": len(html.encode("utf-8")) if html else 0,
                                "response_time_ms": result.get("fetch_time_ms", 0),
                                "fetched_at": datetime.now(),
                                "base_url": self.original_site_url,
                                "description": desc_val # ✅ Set or Clear
                            })

                            if res and res.get("action") == "Inserted":
                                self.saved_count += 1
                                self.log("info", f"DB: Inserted {self._db_url(r['url'])} (ID: {res.get('id')})")
                            elif res and res.get("action") == "Existed":
                                self.existed_urls.add(LinkUtility.normalize_url(r["url"], preference_url=self.original_site_url))
                            
                            if r["status"] != "NOT_MONITORED":
                                self.log(
                                    "warning" if r["status"] == "CHANGED" else "info",
                                    f"[COMPARE] {r['status']} | {r['url']} | Score={r['score']} | Severity={r['severity']}"
                                )

                            if r["status"] == "CHANGED" and self.compare_results is not None:
                                with self.compare_lock:
                                    self.compare_results.append({
                                        **r,
                                        "siteid": self.siteid,
                                        "custid": self.custid,
                                    })

                # ======================================================
                # SHARED DISCOVERY (CRAWL and COMPARE)
                # ======================================================
                if self.crawl_mode in ("CRAWL", "COMPARE") and not self.target_urls:
                    # Extract + enqueue
                    urls, _ = LinkExtractor.extract_urls(html, final_url)
                    if not urls and JSIntelligence.needs_js_rendering(html):
                        self.js_render_stats["total"] += 1
                        try:
                            html, final_url, js_status = JS_RENDERER.render(final_url)
                            self.js_render_stats["success"] += 1
                            urls, _ = LinkExtractor.extract_urls(html, final_url)
                        except Exception as e:
                            self.js_render_stats["failed"] += 1
                            self.log("error", f"JS Render failed: {e}")

                    if urls:
                        pg_enqueued = 0
                        pg_rules_skipped = 0
                        pg_domain_skipped = 0
                        for u in urls:
                            if not self.running: break # ✅ Fast shutdown
                            if not ExecutionPolicy.is_allowed_domain(self.original_site_url, u, current_url=final_url):
                                pg_domain_skipped += 1
                                continue
                            res = self.frontier.enqueue(u, final_url, depth + 1, preference_url=self.original_site_url)
                            if res == "enqueued":
                                pg_enqueued += 1
                            elif res == "policy_skipped":
                                pg_rules_skipped += 1
                                self.policy_skipped_count += 1
                            elif res == "duplicate":
                                self.frontier_duplicate_count += 1
                                # self.existed_urls.add(...) removed - only add true DB hits
                                # Repetitive logs removed here
                        
                        if self.crawl_mode == "CRAWL":
                             self.log("info", f"Enqueued {pg_enqueued} URLs")
                             self.log("info", f"Skipped: rules={pg_rules_skipped}, domain={pg_domain_skipped}")

            except Exception as e:
                self.log("error", f"Process error for {url}: {e}")

            finally:
                self.frontier.mark_visited(
                    url,
                    got_task=found,
                    preference_url=self.original_site_url
                )
