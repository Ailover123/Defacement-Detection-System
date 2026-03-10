# crawler/storage/baseline_store.py

from pathlib import Path
from crawler.storage.db import insert_defacement_site
from crawler.storage.mysql import (
    upsert_baseline_hash,
    fetch_baseline_hash,
    site_has_baselines,
)
from crawler.processor import LinkUtility, ContentNormalizer
from crawler.core import logger

import threading

BASELINE_ROOT = Path("baselines")

# Global lock and cache for sequence numbers
_ID_LOCK = threading.Lock()
_SITE_MAX_IDS = {}
_SITE_HAS_BASELINES = {}


# --------------------------------------------------
# ID GENERATOR (UNCHANGED LOGIC, STABLE)
# --------------------------------------------------
def _next_baseline_id(site_dir: Path, siteid: int) -> str:
    with _ID_LOCK:

        if siteid not in _SITE_MAX_IDS:
            max_seq = 0
            prefix = f"{siteid}-"

            if site_dir.exists():
                for f in site_dir.glob(f"{siteid}-*.html"):
                    try:
                        stem = f.stem
                        if stem.startswith(prefix):
                            num = int(stem[len(prefix):])
                            if num > max_seq:
                                max_seq = num
                    except ValueError:
                        pass

            _SITE_MAX_IDS[siteid] = max_seq

        _SITE_MAX_IDS[siteid] += 1
        return f"{siteid}-{_SITE_MAX_IDS[siteid]}"


# --------------------------------------------------
# FINAL STABLE BASELINE SAVE
# --------------------------------------------------
def save_baseline(*, custid, siteid, url, html, enforce_www=False):
    """
    Stable baseline logic:

    - Canonicalize ONCE (respecting enforce_www)
    - Handle www based on site preference
    - Never re-canonicalize DB rows later
    - Update existing baseline if present
    """

    # 🔒 Canonical for DB (Respecting site preference)
    canonical = LinkUtility.get_canonical_id(url, enforce_www=enforce_www)

    raw_html = html or ""
    content_hash = ContentNormalizer.semantic_hash(raw_html)

    site_dir = BASELINE_ROOT / str(custid) / str(siteid)
    site_dir.mkdir(parents=True, exist_ok=True)

    # --------------------------------------------------
    # 1️⃣ Check existing baseline (NO base_url)
    # --------------------------------------------------
    existing = fetch_baseline_hash(
        site_id=siteid,
        normalized_url=canonical
    )

    if siteid not in _SITE_HAS_BASELINES:
        _SITE_HAS_BASELINES[siteid] = site_has_baselines(siteid)

    if existing and existing.get("baseline_path"):
        baseline_id = Path(existing["baseline_path"]).stem
        action = "updated"
    else:
        baseline_id = _next_baseline_id(site_dir, siteid)
        action = "created"
        _SITE_HAS_BASELINES[siteid] = True

    path = site_dir / f"{baseline_id}.html"

    logger.info(
        f"[BASELINE] {action.upper()} id={baseline_id} url={canonical}"
    )

    # --------------------------------------------------
    # 2️⃣ UPSERT DB (NO base_url, NO branding logic)
    # --------------------------------------------------
    upsert_baseline_hash(
        site_id=siteid,
        normalized_url=canonical,
        content_hash=content_hash,
        baseline_path=str(path),
        baseline_id=baseline_id,
    )

    # --------------------------------------------------
    # 3️⃣ Always overwrite file with raw HTML so baseline keeps full source attributes.
    # --------------------------------------------------
    path.write_text(raw_html, encoding="utf-8")

    # --------------------------------------------------
    # 4️⃣ Ensure defacement_sites stores canonical only
    # --------------------------------------------------
    insert_defacement_site(
        siteid=siteid,
        baseline_id=baseline_id,
        url=canonical,   #  STORE WITHOUT WWW
    )

    return baseline_id, str(path), action
