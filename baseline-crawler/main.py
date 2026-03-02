#!/usr/bin/env python3
"""
Entry point for the web crawler and baseline generator.
"""

from pathlib import Path
from dotenv import load_dotenv
env_path = Path(__file__).resolve().parents[1] / '.env'
load_dotenv(dotenv_path=env_path)

import argparse
import logging
import threading
import time
import uuid
import os
import requests
import urllib3
import sys
import os
from datetime import datetime
from urllib.parse import urlparse, urlunparse
from concurrent.futures import ThreadPoolExecutor, as_completed
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

class FlushingFileHandler(logging.FileHandler):
    def emit(self, record):
        super().emit(record)
        self.flush()

from report_generator import generate_report

from crawler.engine import Frontier, CrawlerWorker, ExecutionPolicy
from crawler.processor import LinkUtility, TrafficControl
from crawler.storage.db import (
    check_db_health,
    fetch_enabled_sites,
    insert_crawl_job,
    complete_crawl_job,
    fail_crawl_job,
    has_site_crawl_data
)
from crawler.storage.mysql import fetch_site_info_by_baseline_id, site_has_baselines, get_defacement_rows_for_site
from crawler.baseline_worker import BaselineWorker
from crawler.core import (
    logger,
    MIN_WORKERS,
    MAX_WORKERS,
    MAX_PARALLEL_SITES,
    CompanyFormatter,
)

CRAWL_MODE = os.getenv("CRAWL_MODE", "CRAWL").upper()
assert CRAWL_MODE in ("BASELINE", "CRAWL", "COMPARE")

# Global crawl counters (Session Summary)
GLOBAL_SUCCESS = 0
GLOBAL_THROTTLE_ERRORS = 0 # 429 and 503 errors
GLOBAL_OTHER_ERRORS = 0
GLOBAL_TOTAL_URLS = 0
GLOBAL_START_TIME = time.time()
LAST_ACTIVITY_TIME = time.time()
GLOBAL_LOCK = threading.Lock()
SUMMARY_LOCK = threading.Lock() # 🔒 Atomic logging for summary tables
# Global skip report aggregation
from collections import defaultdict
SKIP_REPORT = defaultdict(lambda: {"count": 0, "urls": []})
SKIP_LOCK = threading.Lock()
site_skip_lock = threading.Lock()
GLOBAL_SESSIONS = [] # Tracks (siteid, url, visited, duration, alerts_count)
WATCHDOG_TIMEOUT = 900  # 15 minutes hard timeout for inactivity

# Global Compare Results (Session Summary)
GLOBAL_COMPARE_RESULTS = []
COMPARE_LOCK = threading.Lock()

# Global Baseline Failures (Session Summary)
BASELINE_FAILED_URLS = []
BASELINE_LOCK = threading.Lock()

def watchdog_thread():
    """Kills the process if no activity is detected for WATCHDOG_TIMEOUT seconds."""
    while True:
        time.sleep(60)
        elapsed = time.time() - LAST_ACTIVITY_TIME
        if elapsed > WATCHDOG_TIMEOUT:
            logger.critical(f"FATAL: Watchdog timer expired! No activity for {elapsed:.0f}s. Force killing process.")
            os._exit(1)  # Force kill, no cleanup

# ============================================================
# SEED URL RESOLUTION
# ============================================================

def resolve_seed_url(raw_url: str) -> str:
    """
    Resolve a working URL for crawling.
    This does NOT change DB identity.
    """
    raw = raw_url.strip()

    # Try without / and with /
    candidates = (
        [raw.rstrip("/"), raw]
        if raw.endswith("/")
        else [raw, raw + "/"]
    )

    for u in candidates:
        try:
            r = requests.get(
                u if u.startswith("https://") else "https://" + u,
                timeout=12,
                allow_redirects=True,
                headers={"User-Agent": USER_AGENT},
                verify=False,
            )
            if r.status_code < 400:
                return r.url
        except Exception:
            continue

    # Last-resort fallback
    if not raw.startswith("https://"):
        raw = "https://" + raw
    return raw


# ============================================================
# PER-SITE CRAWL LOGIC
# ============================================================

def crawl_site(site, args, target_urls=None):
    """
    Crawls a single site. This function can be called sequentially or in parallel.
    """
    global GLOBAL_TOTAL_URLS, GLOBAL_SUCCESS, GLOBAL_THROTTLE_ERRORS, GLOBAL_OTHER_ERRORS, LAST_ACTIVITY_TIME
    siteid = site["siteid"]
    custid = site["custid"]

    # 🔒 EXACT value from sites table (DB identity)
    original_site_url = site["url"].strip()

    # 🔑 Resolve seed FIRST, normalize AFTER
    resolved_seed = resolve_seed_url(original_site_url)
    
    # Respect the resolved seed exactly (don't force www)
    p = urlparse(resolved_seed)
    path = p.path.rstrip("/")
    start_url = urlunparse((p.scheme, p.netloc, path, "", "", ""))

    job_id = str(uuid.uuid4())
    
    # Site-local metrics for thread-safety in parallel mode
    site_skip_report = defaultdict(lambda: {"count": 0, "urls": []})

    job_logger = logger

    job_logger.info("=" * 60)
    job_logger.info(f"Starting job {job_id} ({CRAWL_MODE})")
    job_logger.info(f"Customer ID    : {custid}")
    job_logger.info(f"Site ID        : {siteid}")
    job_logger.info(f"Registered URL : {original_site_url}")
    job_logger.info(f"Starting URL   : {start_url}")
    job_logger.info("=" * 60)

    try:
        insert_crawl_job(
            job_id=job_id,
            custid=custid,
            siteid=siteid,
            start_url=start_url,
        )

        start_time = time.time()

        # ====================================================
        # BASELINE MODE (REFETCH FROM DB)
        # ====================================================
        if CRAWL_MODE == "BASELINE":
            job_logger.info(f"[MODE] BASELINE (offline refetch from DB for siteid={siteid})")
            
            # Since BaselineWorker currently hardcodes max_workers=5
            worker_count = MAX_WORKERS
            logger.info(f"Worker-X : started (BASELINE) x{worker_count}")
            logger.info(f"Started {worker_count} workers.")

            # Heartbeat helper
            def update_heartbeat():
                global LAST_ACTIVITY_TIME
                LAST_ACTIVITY_TIME = time.time()

            stats = BaselineWorker(
                custid=custid,
                siteid=siteid,
                seed_url=start_url,
                target_urls=target_urls,
                heartbeat_callback=update_heartbeat
            ).run()

            # 🛡️ Safety: Ensure stats is always a dictionary
            if stats is None:
                stats = {"created": 0, "updated": 0, "failed": 0}

            # Calculate actual baseline counts
            baseline_count = stats.get('created', 0) + stats.get('updated', 0)
            
            duration = time.time() - start_time
            complete_crawl_job(job_id=job_id, pages_crawled=baseline_count)

            # Update global counters
            with threading.Lock():
                GLOBAL_TOTAL_URLS += baseline_count
            
            # Capture Failed URLs
            failed_list = stats.get('failed_urls', [])
            if failed_list:
                with BASELINE_LOCK:
                    for f in failed_list:
                        # f is {"url": ..., "error": ...}
                        f['siteid'] = siteid
                        BASELINE_FAILED_URLS.append(f)

            job_logger.info("-" * 60)
            job_logger.info("BASELINE GENERATION COMPLETED")
            job_logger.info(f"Site URL          : {original_site_url}")
            job_logger.info("-" * 60)
            job_logger.info(f"Baselines Created : {stats.get('created', 0)}")
            job_logger.info(f"Baselines Updated : {stats.get('updated', 0)}")
            job_logger.info(f"Baselines Failed  : {stats.get('failed', 0)}")
            job_logger.info(f"Duration          : {duration:.2f} seconds")
            job_logger.info("-" * 60)
            return

        # ====================================================
        # CRAWL MODE (LIVE DISCOVERY)
        # ====================================================
        if CRAWL_MODE == "CRAWL":
            job_logger.info(f"[MODE] CRAWL (live discovery for siteid={siteid})")

            # 🛡️ Capture initial state for "NEW LINK FOUND" logic
            initial_has_data = has_site_crawl_data(siteid, start_url)

            # 🛡️ Seed Skip Check
            skip_reason = ExecutionPolicy.classify_skip(start_url)
            if skip_reason:
                 job_logger.warning(f"Seed URL {start_url} skipped by rule: {skip_reason}")
                 return

            frontier = Frontier()
            
            if target_urls:
                 for t_url in target_urls:
                     frontier.enqueue(t_url, None, 0, preference_url=original_site_url)
                 job_logger.info(f"Targeting {len(target_urls)} specific URL(s). Site walking disabled.")
            else:
                 frontier.enqueue(start_url, None, 0, preference_url=original_site_url)

            siteid_map = {siteid: siteid}
            workers = []
            retired_workers = []
            try:
                for i in range(MIN_WORKERS):
                    w = CrawlerWorker(
                        frontier=frontier, name=f"Worker-{siteid}-{i}",
                        custid=custid, siteid_map=siteid_map, job_id=job_id,
                        crawl_mode="CRAWL", seed_url=start_url, 
                        original_site_url=original_site_url,
                        skip_report=site_skip_report, skip_lock=site_skip_lock,
                        target_urls=target_urls
                    )
                    w.start()
                    workers.append(w)

                job_logger.info(f"Started {len(workers)} workers for site {siteid}.")
                
                while True:
                    time.sleep(5)
                    qsize = frontier.queue.qsize()
                    unfinished = frontier.queue.unfinished_tasks
                    if qsize == 0 and unfinished == 0: break
                    
                    if TrafficControl.should_scale_down(siteid) and len(workers) > MIN_WORKERS:
                        num_to_stop = len(workers) - MIN_WORKERS
                        job_logger.warning(f"[Site {siteid}] 429 hit! Scaling down {num_to_stop} workers.")
                        for _ in range(num_to_stop):
                            if len(workers) > MIN_WORKERS:
                                w = workers.pop()
                                w.stop()
                                retired_workers.append(w)
                        TrafficControl.reset_scale_down(siteid)
                    
                    if qsize > 100 and len(workers) < MAX_WORKERS:
                        scale_count = min(5, MAX_WORKERS - len(workers))
                        for i in range(scale_count):
                            w = CrawlerWorker(
                                frontier=frontier, name=f"Worker-{siteid}-{len(workers)}",
                                custid=custid, siteid_map=siteid_map, job_id=job_id,
                                crawl_mode="CRAWL", seed_url=start_url, 
                                original_site_url=original_site_url,
                                skip_report=site_skip_report, skip_lock=site_skip_lock,
                                target_urls=target_urls
                            )
                            w.start()
                            workers.append(w)
                        job_logger.info(f"[Site {siteid}] Scaled up to {len(workers)} workers (Queue: {qsize})")

                    if unfinished == 0: break
                    LAST_ACTIVITY_TIME = time.time()
                        
            finally:
                all_active_workers = workers + retired_workers
                job_logger.info(f"Stopping {len(all_active_workers)} workers for site {siteid}...")
                for w in all_active_workers: w.stop()
                
                # ✅ V3 Join Logic: Ensure ALL workers are joined before summary prints
                # This prevents messy logs where workers print "DB: Inserted" inside the table
                for w in all_active_workers:
                    if w.is_alive():
                        w.join(timeout=10) # Give them 10s each to finish current fetch
                
                TrafficControl.set_pause(siteid, 0)
                TrafficControl.reset_scale_down(siteid)
                job_logger.info(f"All workers for site {siteid} stopped.")

            duration = time.time() - start_time
            stats = frontier.get_stats()
            complete_crawl_job(job_id=job_id, pages_crawled=stats["visited_count"])

            total_saved = sum(getattr(w, 'saved_count', 0) for w in all_active_workers)
            total_failed = sum(getattr(w, 'failed_count', 0) for w in all_active_workers)
            total_policy_skipped = sum(getattr(w, 'policy_skipped_count', 0) for w in all_active_workers)
            total_frontier_skips = sum(getattr(w, 'frontier_duplicate_count', 0) for w in all_active_workers)
            total_skipped_rules = sum(data.get("count", 0) if isinstance(data, dict) else 0 for data in site_skip_report.values())
            
            # 🛡️ Deduplicate "Existed" URLs per site session to match DB row count accurately
            all_existed_urls = set()
            for w in all_active_workers:
                all_existed_urls.update(getattr(w, 'existed_urls', set()))
            total_db_existed = len(all_existed_urls)

            total_duplicates = total_db_existed + total_frontier_skips
            total_visited = total_saved + total_duplicates + total_skipped_rules + total_failed + total_policy_skipped
            
            # Aggregating metrics from workers
            total_redirects = sum(getattr(w, 'redirect_count', 0) for w in all_active_workers)
            js_renders = {
                "total": sum(getattr(w, 'js_render_stats', {}).get("total", 0) for w in all_active_workers),
                "success": sum(getattr(w, 'js_render_stats', {}).get("success", 0) for w in all_active_workers),
                "failed": sum(getattr(w, 'js_render_stats', {}).get("failed", 0) for w in all_active_workers)
            }
            combined_fails = defaultdict(int)
            total_throttles = sum(getattr(w, 'failed_throttle_count', 0) for w in all_active_workers)
            for w in all_active_workers:
                for reason, count in getattr(w, 'failure_reasons', {}).items():
                    combined_fails[reason] += count

            # Collect session data early to be safe
            all_new_urls = []
            for w in all_active_workers:
                all_new_urls.extend(getattr(w, 'new_urls', []))

            session_entry = {
                "custid": custid,
                "siteid": siteid,
                "url": original_site_url,
                "total_attempted": total_visited,
                "new_saved": total_saved,
                "db_existed": total_db_existed,
                "total_throttles": total_throttles,
                "duration": duration,
                "new_urls": all_new_urls,
                "has_existing_data": initial_has_data, 
                "failure_reasons": dict(combined_fails),
                "alerts": 0 
            }

            # ====================================================
            # PERFORMANCE SUMMARY LOGGING
            # ====================================================
            fail_details = ", ".join([f"{k}: {v}" for k, v in combined_fails.items()]) if combined_fails else "None"
            
            summary_lines = [
                f"\n" + "-" * 70,
                f"SITE {siteid} PERFORMANCE SUMMARY (CRAWL)",
                "-" * 70,
                f"{'METRIC':<25} | {'COUNT':<7} | {'DETAILS'}",
                "-" * 70,
                f"{'Total URLs Attempted':<25} | {total_visited:<7} | (Sum of all attempts below)",
                f"{'  - Newly Saved':<25} | {total_saved:<7} | (New pages added to DB)",
                f"{'  - Already in DB':<25} | {total_db_existed:<7} | (Previously crawled/existed)",
                f"{'  - Redirects':<25} | {total_redirects:<7} | (Found during discovery)",
                f"{'  - SkipRules/Policy':<25} | {total_skipped_rules + total_policy_skipped:<7} | (Filtered out by config)",
                f"{'  - Failures':<25} | {total_failed:<7} | (Network/Server errors)",
                f"{'  - 429/503 Throttles':<25} | {total_throttles:<7} | (May be recovered)",
                "-" * 70,
                f"{'JS Rendering':<25} | {js_renders['total']:<7} | (Success: {js_renders['success']} | Failed: {js_renders['failed']})",
                f"{'Failure Details':<25} | {total_failed:<7} | ({fail_details})",
                "-" * 70 + "\n"
            ]
            
            with SUMMARY_LOCK:
                job_logger.info("\n".join(summary_lines))

            with GLOBAL_LOCK:
                GLOBAL_SESSIONS.append(session_entry)
                GLOBAL_TOTAL_URLS += total_saved
                GLOBAL_SUCCESS += total_saved
                GLOBAL_THROTTLE_ERRORS += total_throttles
                GLOBAL_OTHER_ERRORS += total_failed

            with site_skip_lock:
                for skip_type, data in site_skip_report.items():
                    with SKIP_LOCK:
                        SKIP_REPORT[skip_type]["count"] += data["count"]
                        SKIP_REPORT[skip_type]["urls"].extend(data["urls"][:5])
            return

        # ====================================================
        # COMPARE MODE (LIVE DISCOVERY + DIFFER)
        # ====================================================
        elif CRAWL_MODE == "COMPARE":
             # 🛡️ High-level check: Verify baselines exist in DB and FS
             has_db_data = site_has_baselines(siteid)
             baseline_dir = Path("baselines") / str(custid) / str(siteid)
             has_fs_files = baseline_dir.exists() and any(baseline_dir.glob("*.html"))
             
             if not (has_db_data and has_fs_files):
                  job_logger.warning(f"Site {siteid} has no existing baselines. Proceeding with crawl to identify and mark missing baselines.")
             
             job_logger.info(f"[MODE] COMPARE (live discovery + diff for siteid={siteid})")

             # 🛡️ Capture initial state for "NEW LINK FOUND" logic
             initial_has_data = has_site_crawl_data(siteid, start_url)

             # 🛡️ Seed Skip Check
             skip_reason = ExecutionPolicy.classify_skip(start_url)
             if skip_reason:
                  job_logger.warning(f"Seed URL {start_url} skipped by rule: {skip_reason}")
                  return

             frontier = Frontier()

             # Auto-target only the monitored URLs from defacement_sites table
             if not target_urls:
                  site_rows = get_defacement_rows_for_site(siteid)
                  if site_rows:
                       target_urls = ["https://" + row["url"] for row in site_rows]
                       job_logger.info(f"[COMPARE] Auto-targeting {len(target_urls)} monitored URL(s) from defacement_sites (skipping full site crawl).")

             if target_urls:
             # Targeting specific pages only - do not seed the whole site
                  for t_url in target_urls:
                      frontier.enqueue(t_url, None, 0, preference_url=original_site_url)
                  job_logger.info(f"Targeting {len(target_urls)} specific URL(s). Site walking disabled.")
             else:
             # Default: Seed with start_url to crawl entire site
                  frontier.enqueue(start_url, None, 0, preference_url=original_site_url)

             siteid_map = {siteid: siteid}
             workers = []
             retired_workers = []
             try:
                 for i in range(MIN_WORKERS):
                     w = CrawlerWorker(
                    frontier=frontier,
                    name=f"Worker-{siteid}-{i}",
                    custid=custid,
                    siteid_map=siteid_map,
                    job_id=job_id,
                    crawl_mode=CRAWL_MODE,
                    seed_url=start_url, 
                         original_site_url=original_site_url,
                         skip_report=site_skip_report, skip_lock=site_skip_lock,
                         target_urls=target_urls,
                         compare_results=GLOBAL_COMPARE_RESULTS, compare_lock=COMPARE_LOCK,
                     )
                     w.start()
                     workers.append(w)

                 job_logger.info(f"Started {len(workers)} workers for site {siteid}.")
                 
            # --- DYNAMIC SCALING LOOP ---
                 while True:
                     time.sleep(5)
                     qsize = frontier.queue.qsize()
                     unfinished = frontier.queue.unfinished_tasks
                     if qsize == 0 and unfinished == 0: break
                     
                # --- ADAPTIVE SCALE DOWN ON 429 ---
                     if TrafficControl.should_scale_down(siteid) and len(workers) > MIN_WORKERS:
                         num_to_stop = len(workers) - MIN_WORKERS
                         job_logger.warning(f"[Site {siteid}] 429 hit! Scaling down {num_to_stop} workers.")
                         for _ in range(num_to_stop):
                             if len(workers) > MIN_WORKERS:
                                 w = workers.pop()
                                 w.stop()
                                 retired_workers.append(w)
                         TrafficControl.reset_scale_down(siteid)
                     
                     if qsize > 100 and len(workers) < MAX_WORKERS:
                         scale_count = min(5, MAX_WORKERS - len(workers))
                         for i in range(scale_count):
                             w = CrawlerWorker(
                            frontier=frontier,
                            name=f"Worker-{siteid}-{len(workers)}",
                            custid=custid,
                            siteid_map=siteid_map,
                            job_id=job_id,
                            crawl_mode=CRAWL_MODE,
                            seed_url=start_url, 
                                 original_site_url=original_site_url,
                                 skip_report=site_skip_report, skip_lock=site_skip_lock,
                                 target_urls=target_urls,
                                 compare_results=GLOBAL_COMPARE_RESULTS, compare_lock=COMPARE_LOCK,
                             )
                             w.start()
                             workers.append(w)
                         job_logger.info(f"[Site {siteid}] Scaled up to {len(workers)} workers (Queue: {qsize})")

                # HEARTBEAT: Keep watchdog happy while we are working
                     LAST_ACTIVITY_TIME = time.time()
                         
             finally:
            # 🛑 CRITICAL: Always signal workers to stop and wait for them
                 all_active_workers = workers + retired_workers
                 job_logger.info(f"Stopping {len(all_active_workers)} workers for site {siteid}...")
                 for w in all_active_workers: w.stop()
                 
            # Wait with a "Hang Alert" loop
                 start_join = time.time()
                 for w in all_active_workers:
                     if w.is_alive():
                    # Wait in small chunks to allow logging if it takes too long
                         while w.is_alive():
                             w.join(timeout=2)
                        
                        # HEARTBEAT during cleanup
                             LAST_ACTIVITY_TIME = time.time()

                             elapsed = time.time() - start_join
                             if elapsed > 15:
                                 job_logger.warning(f"Thread {w.name} is taking exceptionally long to terminate ({elapsed:.1f}s). System may be hanging.")
                                 break # Don't block forever if it's truly stuck
            
            # Reset the Global Site Pause so fresh runs aren't blocked
                 TrafficControl.set_pause(siteid, 0)
                 TrafficControl.reset_scale_down(siteid)
                 job_logger.info(f"All workers for site {siteid} stopped.")

             duration = time.time() - start_time
             stats = frontier.get_stats()
             complete_crawl_job(job_id=job_id, pages_crawled=stats["visited_count"])

             total_saved = sum(getattr(w, 'saved_count', 0) for w in all_active_workers)
             total_failed = sum(getattr(w, 'failed_count', 0) for w in all_active_workers)
             total_policy_skipped = sum(getattr(w, 'policy_skipped_count', 0) for w in all_active_workers)
             total_frontier_skips = sum(getattr(w, 'frontier_duplicate_count', 0) for w in all_active_workers)
             total_skipped_rules = sum(data.get("count", 0) if isinstance(data, dict) else 0 for data in site_skip_report.values())
             
        # 🛡️ Deduplicate "Existed" URLs per site session to match DB row count accurately
             all_existed_urls = set()
             for w in all_active_workers:
                 all_existed_urls.update(getattr(w, 'existed_urls', set()))
             total_db_existed = len(all_existed_urls)

             total_visited = total_saved + total_db_existed + total_skipped_rules + total_failed + total_policy_skipped
             
        # Aggregating metrics from workers
             total_redirects = sum(getattr(w, 'redirect_count', 0) for w in all_active_workers)
             js_renders = {
                 "total": sum(getattr(w, 'js_render_stats', {}).get("total", 0) for w in all_active_workers),
                 "success": sum(getattr(w, 'js_render_stats', {}).get("success", 0) for w in all_active_workers),
                 "failed": sum(getattr(w, 'js_render_stats', {}).get("failed", 0) for w in all_active_workers)
             }
             combined_fails = defaultdict(int)
             total_throttles = sum(getattr(w, 'failed_throttle_count', 0) for w in all_active_workers)
             for w in all_active_workers:
                 for reason, count in getattr(w, 'failure_reasons', {}).items(): combined_fails[reason] += count

        # Collect session data early to be safe
             all_new_urls = []
             all_missing_baselines = []
             for w in all_active_workers:
                 all_new_urls.extend(getattr(w, 'new_urls', []))
                 all_missing_baselines.extend(getattr(w, 'missing_baselines', []))

             session_entry = {
                 "custid": custid, "siteid": siteid, "url": original_site_url,
                 "total_attempted": total_visited, "new_saved": total_saved,
                 "db_existed": total_db_existed, "total_throttles": total_throttles,
                 "duration": duration, "new_urls": all_new_urls,
                 "has_existing_data": initial_has_data, 
                 "failure_reasons": dict(combined_fails), "alerts": 0 
             }

             # Reporting
             fail_details = ", ".join([f"{k}: {v}" for k, v in combined_fails.items()]) if combined_fails else "None"
             summary_lines = [
            f"\n" + "-" * 70,
            f"SITE {siteid} PERFORMANCE SUMMARY",
            "-" * 70,
            f"{'METRIC':<25} | {'COUNT':<7} | {'DETAILS'}",
            "-" * 70,
            f"{'Total URLs Attempted':<25} | {total_visited:<7} | (Sum of all attempts below)",
            f"{'  - Newly Saved':<25} | {total_saved:<7} | (New pages added to DB)",
            f"{'  - Already in DB':<25} | {total_db_existed:<7} | (Previously crawled/existed)",
            f"{'  - Redirects':<25} | {total_redirects:<7} | (Found during discovery)",
            f"{'  - SkipRules/Policy':<25} | {total_skipped_rules + total_policy_skipped:<7} | (Filtered out by config)",
            f"{'  - Failures':<25} | {total_failed:<7} | (Network/Server errors)",
            f"{'  - 429/503 Throttles':<25} | {total_throttles:<7} | (May be recovered)",
            "-" * 70,
            f"{'JS Rendering':<25} | {js_renders['total']:<7} | (Success: {js_renders['success']} | Failed: {js_renders['failed']})",
            f"{'Failure Details':<25} | {total_failed:<7} | ({fail_details})",
            "-" * 70,
        ]

             if all_missing_baselines:
                 summary_lines.append(f"MISSING BASELINES URLs ({len(all_missing_baselines)}):")
                 for mu in all_missing_baselines[:50]: # Cap at 50 for logs
                     summary_lines.append(f"  - {mu}")
                 if len(all_missing_baselines) > 50:
                     summary_lines.append(f"  ... and {len(all_missing_baselines)-50} more.")
                 summary_lines.append("-" * 70)
             summary_lines.append("\n") # This was part of the original last line, now needs to be appended separately
             with SUMMARY_LOCK: job_logger.info("\n".join(summary_lines))

             # ── Per-site defacement summary (COMPARE mode) ────────────────
             with COMPARE_LOCK:
                 site_detections = [
                     r for r in GLOBAL_COMPARE_RESULTS
                     if r.get("siteid") == siteid and r.get("status") == "CHANGED"
                 ]

             det_lines = [
                 "",
                 "=" * 70,
                 f"DEFACEMENT SUMMARY — Site {siteid} ({original_site_url})",
                 "=" * 70,
             ]
             if site_detections:
                 det_lines.append(f"{'BASELINE ID':<25} | {'SCORE':<8} | {'SEVERITY':<10} | URL")
                 det_lines.append("-" * 70)
                 for r in sorted(site_detections, key=lambda x: x.get("score", 0), reverse=True):
                     det_lines.append(
                         f"{str(r.get('baseline_id','N/A')):<25} | "
                         f"{r.get('score', 0):.1f}%    | "
                         f"{str(r.get('severity','N/A')):<10} | "
                         f"{r.get('url','')}"
                     )
                 det_lines.append(f"\nTotal defacements detected: {len(site_detections)}")
             else:
                 det_lines.append("No defacements detected for this site.")
             det_lines.append("=" * 70 + "\n")
             with SUMMARY_LOCK:
                 job_logger.warning("\n".join(det_lines)) if site_detections else job_logger.info("\n".join(det_lines))

             with GLOBAL_LOCK:
                 GLOBAL_SESSIONS.append(session_entry)
                 GLOBAL_TOTAL_URLS += (total_saved + total_db_existed)
                 GLOBAL_SUCCESS += total_saved
                 GLOBAL_THROTTLE_ERRORS += total_throttles
                 GLOBAL_OTHER_ERRORS += total_failed

             with site_skip_lock:
                 for skip_type, data in site_skip_report.items():
                     with SKIP_LOCK:
                         SKIP_REPORT[skip_type]["count"] += data["count"]
                         SKIP_REPORT[skip_type]["urls"].extend(data["urls"][:5])
             return

    except Exception as e:
        fail_crawl_job(job_id, str(e))
        job_logger.error(f"Crawl job {job_id} failed: {e}")
        import traceback
        traceback.print_exc()


def main():
    # ... (existing parser args code) ...
    parser = argparse.ArgumentParser(description="Web Crawler / Baseline Tool")
    parser.add_argument("--siteid", type=int, nargs='+', help="Crawl one or more specific Site IDs")
    parser.add_argument("--baseline_id", "--baselineid", "--baseline-id", type=str, dest="baseline_id", help="Run only for this specific BASELINE ID")
    parser.add_argument("--custid", type=int, nargs='+', help="Crawl all sites for one or more specific Customer IDs")
    parser.add_argument("--mode", type=str, choices=["CRAWL", "BASELINE", "COMPARE"], help="Override CRAWL_MODE (CRAWL, BASELINE, COMPARE)")
    parser.add_argument("--parallel", action="store_true", help="Crawl multiple sites in parallel")
    parser.add_argument("--max_parallel_sites", type=int, help="Override MAX_PARALLEL_SITES limit")
    parser.add_argument("--log", action="store_true", help="Enable file logging for each job")

    args = parser.parse_args()

    # Override CRAWL_MODE if provided via CLI
    global CRAWL_MODE
    if args.mode:
        CRAWL_MODE = args.mode.upper()
        os.environ["CRAWL_MODE"] = CRAWL_MODE
        logger.info(f"CRAWL_MODE overridden by CLI: {CRAWL_MODE}")

    # Start Watchdog
    t = threading.Thread(target=watchdog_thread, daemon=True)
    t.start()
    logger.info(f"Watchdog active: Force kill if inactivity > {WATCHDOG_TIMEOUT}s")

    file_handler = None # Initialize to avoid NameError in finally block

    # ... (existing db check and site fetching) ...

    # ---------------- DB CHECK ----------------
    if not check_db_health():
        logger.error("MySQL health check failed.")
        return

    logger.info("MySQL health check passed.")

    target_urls = None
    
    if args.baseline_id:
        # Priority: Fetch specific baseline info directly (bypassing enabled check if needed)
        info = fetch_site_info_by_baseline_id(args.baseline_id)
        if not info:
             logger.error(f"Baseline ID '{args.baseline_id}' not found in defacement_sites.")
             return
        
        target_siteid = info["siteid"]
        target_urls = [info["url"]]
        
        # Construct site object manually from the DB info
        sites = [{
            "siteid": info["siteid"], 
            "custid": info["custid"], 
            "url": info["url"]
        }]
        logger.info(f"--> Targeting specific Baseline ID: {args.baseline_id} (Site {target_siteid}, Cust {info['custid']})")
    
    else:
        # Standard Mode: Fetch all enabled sites
        sites = fetch_enabled_sites()
        if not sites:
            logger.info("No enabled sites found.")
            return

        if args.siteid:
            sites = [s for s in sites if s["siteid"] in args.siteid]
        if args.custid:
            sites = [s for s in sites if s["custid"] in args.custid]

    if not sites:
        logger.info("No sites matched the specified filters.")
        return

    logger.info(f"Found {len(sites)} enabled site(s) to process.")

    # LOGGING SETUP
    if args.log:
        today_dir = datetime.now().strftime('%Y-%m-%d')
        log_dir = os.path.join("logs", today_dir)
        os.makedirs(log_dir, exist_ok=True)
        
        log_filename = f"{CRAWL_MODE}_{datetime.now().strftime('%H%M%S')}.log"
        log_path = os.path.join(log_dir, log_filename)
        
        # Manually attach FileHandler since setup_logger returns early if handlers exist
        
        file_handler = FlushingFileHandler(log_path, encoding='utf-8')
        file_handler.setFormatter(CompanyFormatter())
        logger.addHandler(file_handler)
        
        # --- SYMLINK LATEST LOG ---
        try:
            latest_path = os.path.join("logs", "latest.log")
            if os.path.lexists(latest_path):
                os.remove(latest_path)
            # Use relative symlink for portability
            os.symlink(os.path.relpath(log_path, "logs"), latest_path)
        except Exception as e:
            logger.warning(f"Could not create symlink to latest log: {e}")

        logger.info(f"--- Session started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ---")
        logger.info(f"Log file: {log_path} (linked as logs/latest.log)")

    # ---------------- EXECUTION ----------------
    try:
        if args.parallel:
            max_parallel_sites = args.max_parallel_sites or MAX_PARALLEL_SITES
            logger.info(f"Parallel mode enabled (max {max_parallel_sites} sites at once).")

            # Batch processing to prevent resource exhaustion (leaks, open files, etc.)
            BATCH_SIZE = 20
            
            def chunked_sites(iterable, n):
                for i in range(0, len(iterable), n):
                    yield iterable[i:i + n]

            site_batches = list(chunked_sites(sites, BATCH_SIZE))
            total_batches = len(site_batches)

            for i, batch in enumerate(site_batches, 1):
                logger.info(f"Processing Batch {i}/{total_batches} ({len(batch)} sites)...")
                
                # Create a FRESH executor for each batch to ensure threads are cleaned up
                with ThreadPoolExecutor(max_workers=max_parallel_sites) as executor:
                    future_to_site = {executor.submit(crawl_site, s, args, target_urls): s for s in batch}
                    
                    site_timeout = int(os.getenv("SITE_PROCESS_TIMEOUT", 1800))
                    
                    for future in as_completed(future_to_site):
                        site = future_to_site[future]
                        try:
                            future.result(timeout=site_timeout)
                            if file_handler:
                                file_handler.flush()
                            
                            # Reset watchdog
                            global LAST_ACTIVITY_TIME
                            LAST_ACTIVITY_TIME = time.time()
                            
                        except Exception as e:
                            logger.error(f"Site {site.get('siteid')} task failed or timed out ({site_timeout}s): {e}")

                logger.info(f"Batch {i}/{total_batches} completed.")
                # Small cool-down between batches
                time.sleep(2)

        else:
            for site in sites:
                crawl_site(site, args, target_urls)
                if file_handler:
                    file_handler.flush()
    finally:
        if file_handler:
            total_duration = time.time() - GLOBAL_START_TIME
            
            # --- Global Session Performance Table ---
            if GLOBAL_SESSIONS:
                table_lines = [
                    "\n" + "=" * 95,
                    "           GLOBAL SESSION PERFORMANCE TABLE",
                    "=" * 95,
                    f"{'Site ID':<8} | {'URL':<22} | {'Total':<7} | {'New':<5} | {'DB':<5} | {'Failures':<20} | {'Duration':<8}",
                    "-" * 95
                ]
                
                for s in GLOBAL_SESSIONS:
                    url_short = (s['url'][:19] + '..') if len(s['url']) > 21 else s['url']
                    fail_reasons = s.get('failure_reasons', {})
                    total_f = sum(fail_reasons.values())
                    f_details = ", ".join([f"{k}:{v}" for k, v in fail_reasons.items()])
                    
                    # Include Throttles (429/503) in the failures string if they exist
                    f_throttle = s.get('total_throttles', 0)
                    f_str = f"{total_f} ({f_details})" if total_f > 0 else "0"
                    if f_throttle > 0:
                        f_str = f"{f_str} [T:{f_throttle}]"
                    
                    table_lines.append(f"{s['siteid']:<8} | {url_short:<22} | {s['total_attempted']:<7} | {s['new_saved']:<5} | {s['db_existed']:<5} | {f_str:<20} | {s['duration']:>6.1f}s")
                
                total_total_attempted = sum(s['total_attempted'] for s in GLOBAL_SESSIONS)
                total_new_saved = sum(s['new_saved'] for s in GLOBAL_SESSIONS)
                total_db_existed = sum(s['db_existed'] for s in GLOBAL_SESSIONS)
                total_failures = sum(sum(s.get('failure_reasons', {}).values()) for s in GLOBAL_SESSIONS)

                table_lines.append("-" * 95)
                table_lines.append(f"{'TOTAL':<8} | {len(GLOBAL_SESSIONS):<22} | {total_total_attempted:<7} | {total_new_saved:<5} | {total_db_existed:<5} | {total_failures:<20} | {total_duration:>6.1f}s")
                table_lines.append("=" * 95 + "\n")
                
                with SUMMARY_LOCK:
                    logger.info("\n".join(table_lines))

            # --- New Link Found Summary (CRAWL mode only) ---
            if CRAWL_MODE == "CRAWL":
                new_links_found = [s for s in GLOBAL_SESSIONS if s.get("new_urls") and s.get("has_existing_data")]
                if new_links_found:
                    nl_lines = [
                        "\n" + "=" * 60,
                        "           NEW LINK FOUND",
                        "=" * 60
                    ]
                    for s in new_links_found:
                        nl_lines.append(f"Cust ID: {s['custid']}, Site ID: {s['siteid']}, Domain: {s['url']}")
                        for url in s['new_urls']:
                            nl_lines.append(f"  - {url}")
                        nl_lines.append("-" * 40)
                    nl_lines.append("=" * 60 + "\n")
                    
                    with SUMMARY_LOCK:
                        logger.info("\n".join(nl_lines))

            logger.info("=" * 60 + "\n")
            
            if CRAWL_MODE == "COMPARE" and GLOBAL_COMPARE_RESULTS:
                logger.info("\n" + "=" * 100)
                logger.info(f"{'COMPARE MODE DETAILED SUMMARY':^100}")
                logger.info("=" * 100)
                
                # Header
                logger.info(f"{'BASELINE ID':<25} | {'STATUS':<20} | {'SCORE':<8} | {'SEVERITY':<10} | {'URL'}")
                logger.info("-" * 100)
                
                # Sort by Score DESC (critical first), then Severity
                # Assign sort priority to severity strings if needed, but score is usually enough.
                # If score is same (e.g. 0), sort by URL.
                sorted_results = sorted(
                    GLOBAL_COMPARE_RESULTS, 
                    key=lambda x: (x.get('score', 0), x.get('url', '')), 
                    reverse=True
                )
                
                for r in sorted_results:
                    b_id = str(r.get('baseline_id', 'N/A'))
                    status = str(r.get('status', 'UNKNOWN'))
                    score = f"{r.get('score', 0.0):.1f}%"
                    sev = str(r.get('severity', 'NONE'))
                    url = str(r.get('url', ''))
                    
                    logger.info(f"{b_id:<25} | {status:<20} | {score:<8} | {sev:<10} | {url}")
                
                logger.info("=" * 100 + "\n")

            # --- Baseline Failure Summary (BASELINE mode) ---
            if CRAWL_MODE == "BASELINE" and BASELINE_FAILED_URLS:
                logger.info("\n" + "=" * 100)
                logger.info(f"{'BASELINE FAILED URLS SUMMARY':^100}")
                logger.info("=" * 100)
                
                logger.info(f"{'SITE ID':<10} | {'URL':<50} | {'ERROR'}")
                logger.info("-" * 100)
                
                for f in BASELINE_FAILED_URLS:
                    sid = str(f.get('siteid', ''))
                    # Truncate URL if too long
                    u = f.get('url', '')
                    if len(u) > 48:
                        u = u[:45] + "..."
                    err = f.get('error', '')
                    
                    logger.info(f"{sid:<10} | {u:<50} | {err}")
                
                logger.info("=" * 100 + "\n")

                # 1. Group the failures by siteid
                from collections import defaultdict
                grouped_failures = defaultdict(list)
                for f in BASELINE_FAILED_URLS:
                    sid = f.get('siteid', 'UNKNOWN')
                    grouped_failures[sid].append(f.get('url', ''))
                # 2. Print each site's failures as a separate array/list
                for sid, urls in grouped_failures.items():
                    logger.info(f"SITE ID: {sid} | Total Failed: {len(urls)}")
                    logger.info("-" * 40)
                    for u in urls:
                        logger.info(f"  - {u}")
                    logger.info("-" * 100)
            
            logger.info(f"--- Session ended: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ---")
            
            # 🛡️ Ensure everything is written to file before closing
            file_handler.flush()
            logger.removeHandler(file_handler)
            file_handler.close()

    logger.info("All site crawls processed successfully.")

    # AUTOMATED REPORT GENERATION
    try:
        generate_report()
    except Exception as e:
        logger.error(f"Failed to generate automated report: {e}")


# ============================================================
# ENTRY
# ============================================================

if __name__ == "__main__":
    main()

    if SKIP_REPORT:
        print("\n" + "=" * 60)
        print("GLOBAL SKIPPED URL REPORT")
        print("=" * 60)
        for skip_type, data in SKIP_REPORT.items():
             print(f"{skip_type:<20} | {data['count']}")
    
    sys.exit(0)
