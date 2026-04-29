# Web Crawler Engineering Specification: BASELINE Mode

This document is the Absolute Definitive Authoritative Guide to the Web Crawler's **BASELINE** mode. It provides a strictly additive, exhaustive technical breakdown of the entire baseline generation lifecycle.

---

## 1. System Module Directory (BASELINE-Exclusive)

### 1.1 Orchestration Layer

**`main.py`**: The central orchestrator.
- **Mode Entry**: Detects `CRAWL_MODE == "BASELINE"` and delegates execution to `BaselineWorker`.
- **WAF Pre-Configuration**: Calls `_configure_waf_bypass(start_url)` to set up both `BrowserManager` and `PageFetcher` with the site's `waf_ip`.
- **Heartbeat Integration**: Passes `update_heartbeat()` callback to `BaselineWorker` to keep the global `watchdog_thread()` alive during long-running baseline operations. The watchdog triggers `os._exit(1)` after 900 seconds of inactivity.
- **Failed URL Aggregation**: Collects `failed_urls` from returned stats and appends to global `BASELINE_FAILED_URLS` (protected by `BASELINE_LOCK`), printed in session summary.

### 1.2 Core Worker

**`crawler/baseline_worker.py`**: The dedicated baseline generation engine.
- **Class**: `BaselineWorker`
- **Input Source**: Consumes URLs exclusively from the `crawl_pages` database table via `iter_crawl_urls()` (streaming iterator), OR from a user-supplied `target_urls` list.
- **Health Check**: Before spawning any worker threads, performs a `PageFetcher.fetch_rendered()` call against the seed URL. If the parent site is inaccessible, the entire site is aborted and all URLs are marked as failed.
- **Thread Pool**: Uses `ThreadPoolExecutor` with `MAX_WORKERS` threads (configurable via `.env`, default: 5).
- **Rendering**: Always uses Playwright (via `BrowserManager.render_sync()`) for full JavaScript rendering — baselines must capture the fully rendered DOM, not just raw HTML source.
- **Truncation Guard**: Rejects any render that lacks a `<body>` tag. Retries once after a 2-second delay before marking as failed.

### 1.3 Storage Layer

**`crawler/storage/baseline_store.py`**: Manages baseline persistence.
- **Function**: `save_baseline()`
- **Canonicalization**: Generates a canonical URL ID using `LinkUtility.get_canonical_id()` with `enforce_www` awareness.
- **Normalization**: Applies `ContentNormalizer.normalize_html()` to strip dynamic noise, then generates a `semantic_hash()`.
- **ID Generation**: Thread-safe `_next_baseline_id()` using `_ID_LOCK`, generating sequential IDs in format `{siteid}-{sequence}`.
- **Upsert Logic**: Updates existing baselines (preserving ID) or creates new ones.
- **File System**: Writes normalized HTML to `baselines/{custid}/{siteid}/{baseline_id}.html`.
- **Database Sync**: Calls `upsert_baseline_hash()` and `insert_defacement_site()` to register the URL for monitoring.

**`crawler/storage/crawl_reader.py`**: Provides the streaming URL iterator `iter_crawl_urls(siteid)`.

**`crawler/storage/db_guard.py`**: `DB_SEMAPHORE` limits concurrent DB connections to `MYSQL_POOL_SIZE`.

### 1.4 Content Processing Pipeline

**`crawler/processor.py`**: URL normalization and content hashing.
- **`LinkUtility.get_canonical_id()`**: Generates the storage-safe canonical ID by stripping scheme, normalizing `www.`, removing trailing slashes. This is the **DB identity** of a URL.
- **`LinkUtility.force_www_url()`**: Fetch-only helper ensuring `https://` scheme and adding `www.` for naked domains (via `tldextract`).
- **`ContentNormalizer.normalize_html()`**: Strips 10+ dynamic noise patterns (nonces, value attributes, tracking IDs, aria attributes), then BeautifulSoup `prettify()` for deterministic output.
- **`ContentNormalizer.semantic_hash()`**: Delegates to `compare_utils.semantic_hash()` — converts HTML to semantic lines, generates SHA256 hash.

### 1.5 JS Intelligence & Rendering

**`crawler/js_engine.py`**: Playwright browser management.
- **`BrowserManager.render_sync()`**: Synchronous Playwright render with `--host-rules` for WAF IP routing. Navigates with `JS_GOTO_TIMEOUT` (25s), waits for network idle and DOM stability. Returns `(html, final_url, status_code)`.

---

## 2. Configuration & CLI Infrastructure

### 2.1 CLI Arguments (BASELINE-Specific)

| Argument | Description |
|---|---|
| `--mode baseline` | Activates BASELINE mode |
| `--siteid <id>` | Target a specific site |
| `--custid <id>` | Target a specific customer |
| `--urls <url1> <url2>` | Target specific URLs instead of all crawled pages |
| `--parallel` | Enable parallel site processing |
| `--max-parallel-sites <n>` | Maximum concurrent site baselines (default: 3) |

### 2.2 Environmental Variables (`.env`)

| Variable | Impact on BASELINE |
|---|---|
| `MAX_WORKERS` | Number of parallel fetch threads per site (default: 5) |
| `MYSQL_POOL_SIZE` | Maximum concurrent DB connections via `DB_SEMAPHORE` |
| `SITE_PROCESS_TIMEOUT` | Per-site timeout in seconds (default: 1800) |

### 2.3 Technical Definition: enforce_www

- **`True`**: Site registered as `www.example.com` — canonical ID includes `www.`.
- **`False`**: Canonical ID strips `www.`.

Auto-detected from the site's registered URL in the `sites` table.

---

## 3. Logic & Policy Hub (Exhaustive)

### 3.1 Baseline Generation Decision Matrix

```
INPUT: URL from crawl_pages or target_urls
  │
  ├─ Health Check Seed URL
  │   ├─ FAIL → Abort entire site, mark all URLs as failed
  │   └─ PASS → Continue
  │
  ├─ For each URL:
  │   ├─ Normalize URL → Force www → Apply CRAWL_DELAY
  │   ├─ Playwright Full Render
  │   │   ├─ Empty HTML → FAILED
  │   │   ├─ No <body> → Retry once → FAILED if still missing
  │   │   └─ Valid HTML → Continue
  │   ├─ save_baseline()
  │   │   ├─ Canonicalize URL (enforce_www)
  │   │   ├─ Normalize HTML → Generate semantic_hash
  │   │   ├─ EXISTS in DB → Update | NEW → Create
  │   │   ├─ Write normalized HTML to filesystem
  │   │   └─ Register for monitoring (insert_defacement_site)
  │   └─ Return (action, details, thread_name)
  │
  └─ Summary: created, updated, failed, skipped
```

### 3.2 Normalization Pipeline

1. **Noise Stripping**: Removes nonce, value, id, name, cb, aria-controls, aria-labelledby, data-smartmenus-id attributes. Decomposes `<noscript>` tags.
2. **Standardization**: BeautifulSoup `prettify()` normalizes tag casing, attribute ordering, whitespace.
3. **Final Cleanup**: Strip blank lines and leading/trailing whitespace per line.

---

## 4. Multi-Tier Reliability Stack

| Layer | Mechanism | Description |
|---|---|---|
| 1 | **Parent Site Health Check** | Smart fetch against seed URL before spawning workers |
| 2 | **Truncation Detection** | Every render validated for `<body>` tag; retry once |
| 3 | **Thread Pool Isolation** | Each site gets its own `ThreadPoolExecutor` |
| 4 | **Heartbeat Watchdog** | Callback invoked every 100 URLs preventing watchdog kill |
| 5 | **WAF IP Routing** | Netloc substitution with original domain in `Host` header |

---

## 5. Annotated Cycle Flow: Block-to-Code Mapping

**Block A: Mode Detection & WAF Setup** (`main.py`)
```python
if CRAWL_MODE == "BASELINE":
    _configure_waf_bypass(start_url)
    waf_ip = site.get("waf_ip")
```

**Block B: Health Check** (`baseline_worker.py`)
```python
health_check = PageFetcher.fetch_rendered(self.seed_url, siteid=self.siteid)
if not health_check["success"]:
    return {"created": 0, "updated": 0, "failed": failed_count}
```

**Block C: Parallel URL Processing** (`baseline_worker.py`)
```python
with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
    for url in url_iter:
        f = executor.submit(self._process_url, url)
```

**Block D: Full JS Rendering** (`baseline_worker.py`)
```python
html_content, final_url, status_code = BrowserManager.render_sync(
    fetch_url, waf_ip=self.waf_ip, primary_host=primary_host
)
```

**Block E: Normalization & Persistence** (`baseline_store.py`)
```python
canonical = LinkUtility.get_canonical_id(url, enforce_www=enforce_www)
normalized = ContentNormalizer.normalize_html(html)
content_hash = ContentNormalizer.semantic_hash(normalized)
upsert_baseline_hash(site_id, canonical, content_hash, str(path), baseline_id)
path.write_text(normalized, encoding="utf-8")
```

**Block F: Monitoring Registration** (`baseline_store.py`)
```python
insert_defacement_site(siteid=siteid, baseline_id=baseline_id, url=canonical)
```

---

## 6. Appendix: Baseline Summary Tables

### 6.1 Per-Site Summary
```
--------------------------------------------------------------
BASELINE GENERATION COMPLETED
Site URL          : example.com
--------------------------------------------------------------
Baselines Created : 45
Baselines Updated : 12
Baselines Failed  : 3
Duration          : 127.84 seconds
--------------------------------------------------------------
```

### 6.2 Filesystem Structure
```
baselines/
└── {custid}/
    └── {siteid}/
        ├── {siteid}-1.html
        ├── {siteid}-2.html
        └── ...
```
