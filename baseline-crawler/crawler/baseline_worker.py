# crawler/baseline_worker.py

from concurrent.futures import ThreadPoolExecutor, as_completed
import threading

from crawler.processor import PageFetcher, LinkUtility
from crawler.storage.crawl_reader import iter_crawl_urls
from crawler.storage.baseline_store import save_baseline
from crawler.core import logger, MAX_WORKERS, CRAWL_DELAY
from crawler.js_engine import JSIntelligence, BrowserManager
import time


class BaselineWorker:
    """
    BaselineWorker:
    - Uses ONLY URLs from crawl_pages
    - Streams URLs (no big list in memory)
    - Safely parallelizes fetch + baseline update
    """

    def __init__(self, *, custid, siteid, seed_url, target_urls=None, heartbeat_callback=None):
        self.custid = custid
        self.siteid = siteid
        self.seed_url = seed_url
        self.target_urls = target_urls
        self.heartbeat_callback = heartbeat_callback
        
        # 🕵️ Detect if the site is registered with 'www.' to enforce it in canonical ID
        self.enforce_www = False
        if self.seed_url:
            from urllib.parse import urlparse
            temp_url = self.seed_url
            if "://" not in temp_url:
                temp_url = "https://" + temp_url
            self.enforce_www = urlparse(temp_url).netloc.lower().startswith("www.")

        # Use MAX_WORKERS from config. User requested 5 workers.
        self.max_workers = MAX_WORKERS

    # ------------------------------------------------------------
    # Worker function (runs in thread pool)
    # ------------------------------------------------------------
    def _process_url(self, url: str):
        thread_name = threading.current_thread().name

        try:
            fetch_url = LinkUtility.normalize_url(url, preference_url=self.seed_url)
            fetch_url = LinkUtility.force_www_url(fetch_url)

            if CRAWL_DELAY > 0:
                time.sleep(CRAWL_DELAY)

            # 🔥 ALWAYS use Playwright for baseline
            html_content, final_url, status_code = BrowserManager.render_sync(fetch_url)

            if not html_content:
                return "failed", f"Empty render for site={self.siteid} url={url}", thread_name

            # Base tag injection (keep your existing logic)
            # if "<base" not in html_content.lower():
            #     import re
            #     html_content = re.sub(
            #         r"(<head[^>]*>)",
            #         rf'\1<base href="{fetch_url}">',
            #         html_content,
            #         count=1,
            #         flags=re.IGNORECASE
            #     )

            baseline_id, path, action = save_baseline(
                custid=self.custid,
                siteid=self.siteid,
                url=url,
                html=html_content,
                enforce_www=self.enforce_www
            )   

            return action, f"id={baseline_id} url={url}", thread_name

        except Exception as e:
            return "failed", f"Exception for site={self.siteid} url={url}: {e}", thread_name

    # ------------------------------------------------------------
    # Main entry
    # ------------------------------------------------------------
    def run(self):
        logger.info(
            f"[BASELINE] Controlled baseline refetch started "
            f"for site_id={self.siteid}"
        )

        # --------------------------------------------------------
        # Determine input URL source
        # --------------------------------------------------------
        # --------------------------------------------------------
        # Determine input URL source
        # --------------------------------------------------------
        if self.target_urls is not None:
            logger.info(
                f"[BASELINE] Targeting {len(self.target_urls)} specific URL(s)."
            )
            url_iter = iter(self.target_urls)
        else:
            # STREAMING iterator (CRITICAL FIX)
            url_iter = iter_crawl_urls(siteid=self.siteid)

        # --------------------------------------------------------
        # 🛡️ PARENT SITE HEALTH CHECK
        # --------------------------------------------------------
        # Before spawning workers, check if the seed URL is accessible.
        # If the main site is down, we skip everything to avoid 100s of retries.
        logger.info(f"[BASELINE] Health Checking Seed URL: {self.seed_url}")
        
        # We perform a single fetch with standard retries. 
        # If this fails, we assume the site is down/blocking us completely.
        start_url = LinkUtility.force_www_url(self.seed_url)
        health_check = PageFetcher.fetch(start_url, siteid=self.siteid)
        
        if not health_check["success"]:
            error_msg = f"Parent site inaccessible: {health_check.get('error')}"
            logger.error(f"[BASELINE] 🛑 ABORTING SITE {self.siteid}: {error_msg}")
            
            # Consume iterator and mark all as failed
            failed_count = 0
            failed_list = []
            
            for url in url_iter:
                failed_count += 1
                failed_list.append({"url": url, "error": error_msg})
                
            logger.info(f"[BASELINE] Marked {failed_count} URLs as failed due to parent site outage.")
            
            return {
                "created": 0,
                "updated": 0,
                "failed": failed_count,
                "skipped": 0,
                "failed_urls": failed_list
            }

        logger.info(f"[BASELINE] Health Check PASSED. Proceeding with crawl.")

        # --------------------------------------------------------
        # Counters
        # --------------------------------------------------------
        created = 0
        updated = 0
        failed = 0
        skipped = 0
        processed = 0
        failed_list = []

        logger.info(
            f"[BASELINE] Parallel fetch with {self.max_workers} workers"
        )

        # --------------------------------------------------------
        # Thread pool
        # --------------------------------------------------------
        with ThreadPoolExecutor(
            max_workers=self.max_workers,
            thread_name_prefix="Worker"
        ) as executor:

            future_to_url = {}

            for url in url_iter:
                f = executor.submit(self._process_url, url)
                future_to_url[f] = url
                # Invoke heartbeat while queuing (in case iterator is slow)
                if self.heartbeat_callback:
                    self.heartbeat_callback()

            if not future_to_url:
                logger.warning(
                    f"[BASELINE] No crawl_pages data found for site_id={self.siteid}. "
                    "Run CRAWL mode first."
                )
                return {
                    "created": 0,
                    "updated": 0,
                    "failed": 0,
                    "skipped": 0,
                    "failed_urls": []
                }

            for future in as_completed(future_to_url):
                url = future_to_url[future]
                try:
                    action, details, thread_name = future.result()
                    processed += 1

                    # Normalize thread name to Worker-X
                    worker_display = thread_name
                    if "_" in thread_name:
                        try:
                            prefix, idx = thread_name.rsplit("_", 1)
                            if prefix.startswith("Worker"):
                                worker_display = f"Worker-{idx}"
                        except Exception:
                            pass

                    if action == "created":
                        created += 1
                        logger.info(
                            f"{worker_display} : [BASELINE] Created {details}"
                        )
                    elif action == "updated":
                        updated += 1
                        logger.info(
                            f"{worker_display} : [BASELINE] Updated {details}"
                        )
                    elif action == "skipped":
                        skipped += 1
                    else:
                        failed += 1
                        # details contains the error message
                        failed_list.append({"url": url, "error": details})
                        logger.error(
                            f"{worker_display} : [BASELINE] Failed {details}"
                        )

                    # Heartbeat every 100 URLs (prevents “silent hang”)
                    if processed % 100 == 0:
                        logger.info(
                            f"[BASELINE] Progress: processed={processed} "
                            f"created={created} updated={updated} failed={failed}"
                        )

                    # Invoke heartbeat to keep watchdog happy
                    if self.heartbeat_callback:
                        self.heartbeat_callback()

                except Exception as e:
                    failed += 1
                    failed_list.append({"url": url, "error": str(e)})
                    logger.error(f"[BASELINE] Worker exception: {e}")

        # --------------------------------------------------------
        # Final summary
        # --------------------------------------------------------
        logger.info(
            f"[BASELINE] Done | "
            f"created={created} "
            f"updated={updated} "
            f"failed={failed} "
            f"skipped={skipped}"
        )

        return {
            "created": created,
            "updated": updated,
            "failed": failed,
            "skipped": skipped,
            "failed_urls": failed_list
        }
