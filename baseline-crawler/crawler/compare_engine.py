from pathlib import Path
from datetime import datetime
from crawler.processor import LinkUtility, ContentNormalizer
from crawler.storage.baseline_reader import get_baseline_hash
from crawler.storage.mysql import insert_observed_page, get_selected_defacement_rows, fetch_observed_page
from crawler.core import logger

DIFF_ROOT = Path("diffs")


class CompareEngine:

    DEFAULT_THRESHOLD = 1.0

    def __init__(self, *, custid: int):
        self.custid = custid
        self._rows = None

        from compare_utils import (
            calculate_defacement_percentage,
            defacement_severity,
            generate_html_diff
        )

        self._percentage_fn = calculate_defacement_percentage
        self._severity_fn = defacement_severity
        self._diff_fn = generate_html_diff

    def _load_rows(self):
        if self._rows is None:
            self._rows = get_selected_defacement_rows() or []
            logger.info(f"[COMPARE] Loaded {len(self._rows)} defacement row(s)")
        return self._rows

    def handle_page(
        self,
        *,
        siteid: int,
        url: str,
        html: str,
        base_url: str | None = None,
        enforce_www: bool = False
    ):
        if not html:
            return []

        # Guard: truncated live render (no <body>) — skip comparison
        if "<body" not in html.lower():
            logger.warning(f"[COMPARE] Truncated live HTML (no <body>) for {url}. Skipping comparison.")
            return []

        rows = self._load_rows()
        if not rows:
            return []

        # Canonical URL match
        live_canon = LinkUtility.get_canonical_id(url, base_url, enforce_www=enforce_www)
        logger.info(f"[COMPARE] LIVE CANON: {live_canon}")

        # Use raw live HTML for fidelity; semantic_hash applies its own stable normalization.
        live_html = html
        observed_hash = ContentNormalizer.semantic_hash(live_html)

        matched = False
        results = []

        for row in rows:
            if int(row["siteid"]) != int(siteid):
                continue

            row_canon = row["url"]

            # ROBUST FUZZY MATCHING
            # 1. Strip 'www.'
            live_loose = live_canon[4:] if live_canon.lower().startswith("www.") else live_canon
            row_loose = row_canon[4:] if row_canon.lower().startswith("www.") else row_canon

            # 2. Lowercase and Strip Trailing Slashes
            live_loose = live_loose.lower().rstrip("/")
            row_loose = row_loose.lower().rstrip("/")

            # SUPER DEBUG for Site 93200 (or any site that says not monitored)
            # We log every comparison attempt for this site specifically
            if int(siteid) == 93200 or "pagentra" in live_canon:
                 logger.info(f"[DEBUG-93200] Comparing LIVE_LOOSE '{live_loose}' vs ROW_LOOSE '{row_loose}' (Original ROW URL: '{row_canon}')")

            # Debug log for mismatch within the same site
            if live_loose != row_loose:
                continue

            matched = True
            baseline_id = row["baseline_id"]
            
           
            threshold_val = row.get("threshold")
            threshold = float(threshold_val) if threshold_val is not None else self.DEFAULT_THRESHOLD

            
            baseline = get_baseline_hash(
                site_id=siteid,
                normalized_url=row_canon
            )

            if not baseline:
                logger.error("[COMPARE] Baseline missing in DB")
                results.append({
                    "baseline_id": baseline_id,
                    "url": url,
                    "status": "EMPTY_BASELINE",
                    "score": 0,
                    "severity": "N/A"
                })
                break

            baseline_path = Path(baseline["baseline_path"])

            if not baseline_path.exists():
                logger.error("[COMPARE] Baseline file missing on disk")
                results.append({
                    "baseline_id": baseline_id,
                    "url": url,
                    "status": "EMPTY_BASELINE",
                    "score": 0,
                    "severity": "N/A"
                })
                break

            # Read baseline HTML exactly as stored on disk.
            old_html = baseline_path.read_text(
                encoding="utf-8",
                errors="ignore"
            )

            # 🛡️ Detect truncated baselines (head-only, no body)
            if "<body" not in old_html.lower():
                logger.warning(
                    f"[COMPARE] STALE_BASELINE (no <body>) for baseline_id={baseline_id} url={url}. "
                    "Re-run BASELINE mode to fix."
                )
                results.append({
                    "baseline_id": baseline_id,
                    "url": url,
                    "status": "STALE_BASELINE",
                    "score": 0,
                    "severity": "N/A"
                })
                break

            baseline_hash = ContentNormalizer.semantic_hash(old_html)
            previous_observed = fetch_observed_page(siteid, row_canon)
            previous_observed_hash = (
                previous_observed.get("observed_hash") if previous_observed else None
            )
            logger.info(
                "[COMPARE] HASH_PAIR siteid=%s canon=%s\n"
                "[COMPARE] BASELINE_HASH: %s\n"
                "[COMPARE] OBSERVED_HASH: %s\n"
                "[COMPARE] PREV_OBSERVED_HASH: %s",
                siteid,
                row_canon,
                baseline_hash,
                observed_hash,
                previous_observed_hash or "None",
            )

            # =====================================
            # HASH COMPARISON (CLEAN + STABLE)
            # =====================================

            if observed_hash == baseline_hash:
                logger.info(f"[COMPARE] UNCHANGED (Hash Match) - {url}")
                results.append({
                    "baseline_id": baseline_id,
                    "url": url,
                    "status": "UNCHANGED",
                    "score": 0,
                    "severity": "N/A"
                })
                break

            if previous_observed_hash and observed_hash == previous_observed_hash:
                logger.info(
                    "[COMPARE] ALREADY_DETECTED siteid=%s canon=%s (observed hash matches previous defacement)",
                    siteid,
                    row_canon,
                )
                results.append({
                    "baseline_id": baseline_id,
                    "url": url,
                    "status": "ALREADY_DETECTED",
                    "score": previous_observed.get("defacement_score") or 0,
                    "severity": previous_observed.get("defacement_severity") or "N/A",
                })
                break

            print("BASELINE SIZE:", len(old_html))
            print("LIVE SIZE:", len(live_html))

            # =====================================
            # CALCULATE SCORE
            # =====================================

            score = self._percentage_fn(
                old_html,
                live_html,
                threshold=threshold
            )

            if score < threshold:
                logger.info(f"[COMPARE] UNCHANGED (Score {score} < {threshold}) - {url}")
                results.append({
                    "baseline_id": baseline_id,
                    "url": url,
                    "status": "UNCHANGED",
                    "score": score,
                    "severity": "N/A"
                })
                break

            # =====================================
            # CHANGE DETECTED >= THRESHOLD
            # =====================================
            
            logger.warning(f"[COMPARE] DEFACEMENT DETECTED: {score}% >= {threshold}%")

            severity = self._severity_fn(score)

            diff_dir = DIFF_ROOT / str(self.custid) / str(siteid)
            diff_dir.mkdir(parents=True, exist_ok=True)

            timestamp = datetime.now().strftime("%H%M%S%d%m%Y")
            prefix = f"{timestamp}-{baseline_id}"

            diff_path = diff_dir / f"{prefix}.html"

            self._diff_fn(
                url=url,
                html_a=old_html,
                html_b=live_html,
                out_dir=diff_dir,
                file_prefix=prefix,
                severity=severity,
                score=score,
                checked_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            )



            # =====================================
            # UPSERT OBSERVED STATE
            # =====================================

            insert_observed_page(
                site_id=siteid,
                baseline_id=baseline_id,
                normalized_url=row_canon,
                observed_hash=observed_hash,
                changed=True,
                diff_path=str(diff_path),
                defacement_score=score,
                defacement_severity=severity
            )

            results.append({
                "baseline_id": baseline_id,
                "url": url,
                "status": "CHANGED",
                "score": score,
                "severity": severity
            })
            break

        if not matched:
            logger.info(f"[COMPARE] Not monitored: {live_canon}")
            results.append({
                "url": url,
                "status": "NOT_MONITORED",
                "score": 0,
                "severity": "N/A"
            })

        return results
