# Web Crawler Engineering Specification: COMPARE Mode

This document is the Absolute Definitive Authoritative Guide to the Web Crawler's **COMPARE** mode. It provides a strictly additive, exhaustive technical breakdown of the entire defacement detection lifecycle.

---

## 1. System Module Directory (COMPARE-Exclusive)

### 1.1 Orchestration Layer

**`main.py`**: The central orchestrator.
- **Mode Entry**: Detects `CRAWL_MODE == "COMPARE"` and initializes the full discovery + diffing pipeline.
- **Baseline Pre-Check**: Verifies both DB records (`site_has_baselines(siteid)`) and filesystem files (`baselines/{custid}/{siteid}/*.html`) exist before proceeding. Logs a warning but continues if missing.
- **Auto-Targeting**: If no `target_urls` are provided, auto-fetches monitored URLs from `defacement_sites` table via `get_defacement_rows_for_site(siteid)` and pre-populates the Frontier with them, skipping full site crawl.
- **Worker Initialization**: Spawns `CrawlerWorker` threads with `crawl_mode="COMPARE"` and passes shared `GLOBAL_COMPARE_RESULTS` list (protected by `COMPARE_LOCK`) for thread-safe result collection.
- **Dynamic Scaling**: Implements the same scale-up/scale-down logic as CRAWL mode — scales up when queue > 100, scales down on 429/503 throttle events.

### 1.2 Discovery Engine

**`crawler/engine.py`**: The `CrawlerWorker` thread implements COMPARE-specific logic.
- **CompareEngine Init**: Each worker instantiates one `CompareEngine` instance on startup for efficiency.
- **Force JS Rendering**: Sets `force_js=True` in `PageFetcher.fetch_rendered()` — every page is rendered via Playwright, ensuring the live DOM matches baseline rendering conditions.
- **Failure Sync**: Even when a fetch fails, the status is synced to `crawl_pages` DB to maintain an audit trail.
- **Shared Discovery**: After comparison, the worker extracts links from the HTML and enqueues them (same as CRAWL mode), enabling full-site comparison discovery.
- **Missing Baseline Tracking**: URLs with `EMPTY_BASELINE` or `NOT_MONITORED` status are collected in `worker.missing_baselines[]` for the session summary.

### 1.3 Comparison Engine

**`crawler/compare_engine.py`**: The core defacement detection engine.
- **Class**: `CompareEngine`
- **Lazy Loading**: Loads all monitored `defacement_sites` rows once via `get_selected_defacement_rows()`, cached in `self._rows`.
- **Canonical Matching**: Uses `LinkUtility.get_canonical_id()` to generate live URL's canonical form, then performs **fuzzy matching** against stored baselines — stripping `www.`, lowercasing, and removing trailing slashes.
- **Hash Comparison**: First compares semantic hashes (fast path). If hashes match → `UNCHANGED`. If different → calculates defacement percentage.
- **Score Calculation**: Delegates to `compare_utils.calculate_defacement_percentage()`.
- **Threshold Gating**: Only raises an alert if score >= configurable threshold (default: 1.0%).
- **Diff Generation**: On change detection, generates a side-by-side HTML diff report via `compare_utils.generate_html_diff()`.
- **Observed State Upsert**: Persists comparison results to `observed_pages` table.

### 1.4 Comparison Utilities

**`compare_utils.py`**: Low-level comparison and reporting engine.
- **`_html_to_semantic_lines()`**: Converts HTML to normalized semantic lines for stable comparison. Strips noise tags, normalizes whitespace, removes dynamic attributes.
- **`calculate_defacement_percentage()`**: Uses `difflib.SequenceMatcher` on semantic lines. Calculates change percentage with a **Critical Content Boost** — if `<title>` or `<h1>` content changes and score is below threshold, adds a boost to ensure detection.
- **`defacement_severity()`**: Maps score to severity levels: `LOW` (<5%), `MEDIUM` (5-20%), `HIGH` (20-50%), `CRITICAL` (>50%).
- **`semantic_hash()`**: SHA256 hash of semantic lines for fast equality checking.
- **`generate_html_diff()`**: Produces a professional dark-themed HTML evidence report with side-by-side diff, severity badges, and print-optimized CSS.

### 1.5 Storage Layer

**`crawler/storage/baseline_reader.py`**: Retrieves baseline data for comparison.
- **Function**: `get_baseline_hash(site_id, normalized_url)` — Fetches `(id, content_hash, baseline_path)` from `defacement_sites` table. Includes trailing-slash fallback.

**`crawler/storage/mysql.py`**: Core database operations.
- **`insert_observed_page()`**: Upserts comparison results into `observed_pages` table with defacement score, severity, diff path, and observed hash.
- **`get_selected_defacement_rows()`**: Returns all active baseline entries for comparison matching.
- **`get_defacement_rows_for_site()`**: Returns monitored URLs for a specific site.

---

## 2. Configuration & CLI Infrastructure

### 2.1 CLI Arguments (COMPARE-Specific)

| Argument | Description |
|---|---|
| `--mode compare` | Activates COMPARE mode |
| `--siteid <id>` | Target a specific site |
| `--custid <id>` | Target a specific customer |
| `--urls <url1> <url2>` | Compare specific URLs only |
| `--parallel` | Enable parallel site processing |
| `--max-parallel-sites <n>` | Maximum concurrent comparisons (default: 3) |

### 2.2 Environmental Variables (`.env`)

| Variable | Impact on COMPARE |
|---|---|
| `MAX_WORKERS` | Parallel worker threads per site (default: 5) |
| `MIN_WORKERS` | Minimum workers (floor for scale-down, default: 5) |
| `MYSQL_POOL_SIZE` | DB connection pool limit |

### 2.3 Technical Definition: Defacement Threshold

The `threshold` value (default: 1.0%) is per-baseline configurable:
- Stored in `defacement_sites.threshold` column
- Any change percentage below this value is classified as `UNCHANGED`
- Allows fine-tuning per page to reduce false positives on dynamic pages

---

## 3. Logic & Policy Hub (Exhaustive)

### 3.1 Compare Decision Matrix

```
INPUT: Live HTML from fetched page
  │
  ├─ Guard: Empty HTML → Skip (return [])
  ├─ Guard: No <body> in HTML → Skip (truncated render)
  │
  ├─ Load monitored rows (lazy, cached)
  ├─ Generate live canonical ID
  ├─ Normalize live HTML → Generate observed_hash
  │
  ├─ For each monitored row (same siteid):
  │   ├─ Fuzzy Match canonical IDs
  │   │   ├─ NO MATCH → Continue to next row
  │   │   └─ MATCH → Continue comparison
  │   │
  │   ├─ Fetch baseline from DB
  │   │   ├─ NO BASELINE → Status: EMPTY_BASELINE
  │   │   ├─ FILE MISSING → Status: EMPTY_BASELINE
  │   │   └─ FILE EXISTS → Read baseline HTML
  │   │
  │   ├─ Guard: No <body> in baseline → Status: STALE_BASELINE
  │   │
  │   ├─ Hash Comparison (FAST PATH)
  │   │   ├─ MATCH → Status: UNCHANGED (score: 0)
  │   │   └─ MISMATCH → Calculate defacement percentage
  │   │
  │   ├─ Score vs Threshold
  │   │   ├─ score < threshold → Status: UNCHANGED
  │   │   └─ score >= threshold → Status: CHANGED
  │   │       ├─ Calculate severity
  │   │       ├─ Generate HTML diff report
  │   │       └─ Upsert to observed_pages
  │   │
  │   └─ Break (first match wins)
  │
  ├─ No row matched → Status: NOT_MONITORED
  │
  └─ Return results[]
```

### 3.2 Fuzzy URL Matching Algorithm

The system uses a multi-step fuzzy matching process:

1. **Canonical Generation**: `LinkUtility.get_canonical_id()` strips scheme, handles `www.`, removes trailing slashes.
2. **Loose Normalization**: Strip `www.` prefix, lowercase, remove trailing `/`.
3. **Comparison**: `live_loose == row_loose`

This handles edge cases like:
- `www.example.com/about` vs `example.com/about`
- `example.com/about/` vs `example.com/about`
- `EXAMPLE.COM/About` vs `example.com/about`

### 3.3 Defacement Scoring Algorithm

```python
def calculate_defacement_percentage(baseline_html, observed_html, threshold=1.0):
    base_lines = _html_to_semantic_lines(baseline_html, strip_noise=True)
    obs_lines  = _html_to_semantic_lines(observed_html, strip_noise=True)
    
    # 1. Standard SequenceMatcher diff
    sm = difflib.SequenceMatcher(None, base_lines, obs_lines)
    changed = sum(i2-i1 for tag,i1,i2,j1,j2 in sm.get_opcodes() 
                  if tag in ("replace","delete","insert"))
    pct = (changed / len(base_lines)) * 100
    
    # 2. Critical Content Boost (title, h1 changes)
    if pct < threshold:
        boost = check_critical_tags(baseline_html, observed_html)
        pct = min(100.0, pct + boost)
    
    return round(pct, 2)
```

### 3.4 Severity Classification

| Score Range | Severity | Description |
|---|---|---|
| < 5% | `LOW` | Minor meta-tag or layout shifts. Likely noise or tracking nonces |
| 5% – 20% | `MEDIUM` | Partial content changes. Text updates or layout alterations |
| 20% – 50% | `HIGH` | Major content modification. Sections missing or swapped |
| > 50% | `CRITICAL` | Complete data loss or structure change. Highly suspicious |

---

## 4. Multi-Tier Reliability Stack

| Layer | Mechanism | Description |
|---|---|---|
| 1 | **Baseline Pre-Check** | Validates DB + filesystem baseline existence before starting |
| 2 | **Truncation Guards** | Both live HTML and baseline HTML validated for `<body>` tag |
| 3 | **Stale Baseline Detection** | Baselines without `<body>` marked as `STALE_BASELINE` |
| 4 | **Hash Fast-Path** | Semantic hash comparison before expensive diff calculation |
| 5 | **Threshold Gating** | Per-baseline configurable threshold prevents false positives |
| 6 | **Fuzzy URL Matching** | Multi-step normalization handles www/case/slash variations |
| 7 | **Dynamic Worker Scaling** | Scale down on 429/503, scale up when queue > 100 |

---

## 5. Annotated Cycle Flow: Block-to-Code Mapping

**Block A: Auto-Target Monitored URLs** (`main.py`)
```python
site_rows = get_defacement_rows_for_site(siteid)
if site_rows:
    target_urls = ["https://" + row["url"] for row in site_rows]
```

**Block B: Worker Initialization** (`main.py`)
```python
w = CrawlerWorker(
    crawl_mode=CRAWL_MODE,
    compare_results=GLOBAL_COMPARE_RESULTS,
    compare_lock=COMPARE_LOCK,
)
```

**Block C: Force JS Fetch** (`engine.py`)
```python
force_js = self.crawl_mode == "COMPARE"
result = PageFetcher.fetch_rendered(fetch_url, siteid=self.siteid, force_js=force_js)
```

**Block D: Comparison Dispatch** (`engine.py`)
```python
results = compare_engine.handle_page(
    siteid=self.siteid, url=final_url,
    html=html, base_url=self.original_site_url,
    enforce_www=self.enforce_www
)
```

**Block E: Hash Comparison** (`compare_engine.py`)
```python
if observed_hash == baseline_hash:
    return [{"status": "UNCHANGED", "score": 0}]
```

**Block F: Defacement Detection** (`compare_engine.py`)
```python
score = self._percentage_fn(old_normalized, live_normalized, threshold=threshold)
if score >= threshold:
    severity = self._severity_fn(score)
    self._diff_fn(url=url, html_a=old, html_b=live, ...)
    insert_observed_page(site_id, baseline_id, ...)
```

---

## 6. Appendix: Compare Summary Tables

### 6.1 Per-Site Defacement Summary
```
======================================================================
DEFACEMENT SUMMARY — Site 12345 (example.com)
======================================================================
BASELINE ID               | SCORE    | SEVERITY   | URL
----------------------------------------------------------------------
12345-3                   | 45.2%    | HIGH       | example.com/about
12345-7                   | 12.8%    | MEDIUM     | example.com/contact

Total defacements detected: 2
======================================================================
```

### 6.2 Global Compare Results Table
```
====================================================================================================
                                   COMPARE MODE DETAILED SUMMARY
====================================================================================================
BASELINE ID               | STATUS               | SCORE    | SEVERITY   | URL
----------------------------------------------------------------------------------------------------
12345-3                   | CHANGED              | 45.2%    | HIGH       | example.com/about
12345-1                   | UNCHANGED            | 0.0%     | N/A        | example.com
====================================================================================================
```

### 6.3 Evidence Report Structure
```
diffs/
└── {custid}/
    └── {siteid}/
        ├── {timestamp}-{baseline_id}.html   (Side-by-side diff report)
        └── ...
```

### 6.4 Database Schema: `observed_pages`

| Column | Type | Description |
|---|---|---|
| `id` | INT AUTO_INCREMENT | Primary key |
| `site_id` | INT | Foreign key to `sites` table |
| `baseline_id` | VARCHAR | Reference to baseline used for comparison |
| `normalized_url` | VARCHAR | Canonical URL that was compared |
| `observed_hash` | VARCHAR | SHA256 hash of live normalized HTML |
| `changed` | BOOLEAN | Whether defacement was detected |
| `diff_path` | VARCHAR | Path to HTML evidence report |
| `defacement_score` | FLOAT | Change percentage (0.0 – 100.0) |
| `defacement_severity` | VARCHAR | LOW / MEDIUM / HIGH / CRITICAL |
| `checked_at` | DATETIME | Timestamp of comparison |
