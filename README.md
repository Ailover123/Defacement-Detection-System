<p align="center">
  <img src="https://img.shields.io/badge/Python-3.10+-blue?style=for-the-badge&logo=python&logoColor=white" alt="Python"/>
  <img src="https://img.shields.io/badge/MySQL-8.0-orange?style=for-the-badge&logo=mysql&logoColor=white" alt="MySQL"/>
  <img src="https://img.shields.io/badge/Playwright-Chromium-green?style=for-the-badge&logo=playwright&logoColor=white" alt="Playwright"/>
  <img src="https://img.shields.io/badge/License-Private-red?style=for-the-badge" alt="License"/>
</p>

# 🛡️ Defacement Detection System

A production-grade **Web Defacement Detection Engine** built for continuous website integrity monitoring. The system operates through a three-phase pipeline — **Crawl**, **Baseline**, and **Compare** — to automatically discover web pages, capture their stable snapshots, and detect unauthorized modifications with semantic HTML diffing and evidence-grade reporting.

---

## 📋 Table of Contents

- [Architecture Overview](#-architecture-overview)
- [Operating Modes](#-operating-modes)
- [Tech Stack](#-tech-stack)
- [Project Structure](#-project-structure)
- [Installation](#-installation)
- [Configuration](#-configuration)
- [Usage](#-usage)
- [Reporting](#-reporting)
- [Documentation](#-documentation)

---

## 🏗️ Architecture Overview

The system is designed around a **three-phase detection lifecycle**:

```
┌─────────────┐     ┌──────────────┐     ┌──────────────┐
│   CRAWL     │────▶│   BASELINE   │────▶│   COMPARE    │
│  Discovery  │     │  Snapshot    │     │  Detection   │
└─────────────┘     └──────────────┘     └──────────────┘
     │                    │                    │
     ▼                    ▼                    ▼
  crawl_pages        baselines/           observed_pages
   (MySQL)          (Filesystem)           (MySQL)
                   defacement_sites         diffs/
                     (MySQL)            (HTML Reports)
```

### Core Design Principles

- **WAF Bypass Routing**: Netloc substitution with preserved `Host` headers for direct origin access
- **Semantic Hashing**: SHA256 fingerprinting of noise-stripped HTML for stable comparison
- **Dynamic Worker Scaling**: Auto scale-up on heavy queue, scale-down on 429/503 throttling
- **5-Layer Reliability Stack**: Health checks, truncation guards, watchdog, thread isolation, and retry logic
- **Evidence-Grade Reporting**: Professional HTML diff reports with severity scoring and print-optimized CSS

---

## 🔄 Operating Modes

### 🔍 CRAWL Mode — Discovery Phase

The entry point of the pipeline. Recursively discovers all accessible pages on a target domain.

**Key Capabilities:**
- Multi-threaded discovery with `Frontier` queue management
- `ExecutionPolicy` skip rules for assets, pagination, tags, authors, and 20+ static extensions
- Recursion detection via path segment analysis
- Domain boundary locking (same registered domain only)
- Smart JS escalation — Playwright only when `JSIntelligence` detects SPA patterns
- Automatic redirect handling with WAF IP preservation

**Output:** Populates `crawl_pages` MySQL table with discovered URLs, status codes, and metadata.

### 📸 BASELINE Mode — Snapshot Phase

Generates stable HTML snapshots for all crawled pages.

**Key Capabilities:**
- Consumes URLs from `crawl_pages` (streaming iterator — no memory bloat)
- **Always** uses Playwright for full JS rendering (ensures DOM parity with compare)
- Parent site health check before spawning workers (prevents mass retries on downed sites)
- Noise-stripping normalization pipeline (nonces, IDs, tracking attributes)
- Thread-safe sequential ID generation (`{siteid}-{sequence}`)
- Upsert logic — updates existing baselines, creates new ones

**Output:** Normalized HTML files in `baselines/{custid}/{siteid}/`, registered in `defacement_sites` table.

### ⚔️ COMPARE Mode — Detection Phase

Live defacement detection engine with semantic HTML comparison.

**Key Capabilities:**
- Auto-targets monitored URLs from `defacement_sites` table
- Force JS rendering for live pages (matches baseline rendering conditions)
- **Hash fast-path**: Semantic hash comparison before expensive diff
- `SequenceMatcher`-based defacement percentage calculation
- **Critical Content Boost**: Extra scoring weight for `<title>` and `<h1>` changes
- Per-baseline configurable threshold (default: 1.0%)
- Side-by-side HTML diff evidence generation with severity badges
- Fuzzy URL matching (handles www/case/slash variations)

**Severity Levels:**

| Score | Severity | Description |
|---|---|---|
| < 5% | `LOW` | Minor meta/layout shifts |
| 5-20% | `MEDIUM` | Partial content changes |
| 20-50% | `HIGH` | Major content modification |
| > 50% | `CRITICAL` | Complete structure change |

**Output:** `observed_pages` records, HTML diff reports in `diffs/{custid}/{siteid}/`.

---

## 🛠️ Tech Stack

| Component | Technology |
|---|---|
| **Language** | Python 3.10+ |
| **Database** | MySQL 8.0 with connection pooling |
| **JS Rendering** | Playwright (Chromium) |
| **HTML Parsing** | BeautifulSoup4 + lxml |
| **HTTP Client** | Requests (with manual redirect handling) |
| **URL Intelligence** | tldextract |
| **Concurrency** | ThreadPoolExecutor + threading primitives |
| **Diffing** | difflib.SequenceMatcher |
| **Reporting** | Custom HTML generator with dark/light theme |

---

## 📁 Project Structure

```
Defacement-Detection-System/
├── baseline-crawler/
│   ├── main.py                          # Central orchestrator (CLI, mode routing, batching)
│   ├── compare_utils.py                 # HTML normalization, diffing, scoring engine
│   ├── report_generator.py              # Automated dashboard report generation
│   │
│   ├── crawler/
│   │   ├── core.py                      # Global config, logging, constants
│   │   ├── engine.py                    # CrawlerWorker, Frontier, ExecutionPolicy
│   │   ├── processor.py                 # LinkUtility, PageFetcher, LinkExtractor
│   │   ├── js_engine.py                 # BrowserManager, JSIntelligence
│   │   ├── baseline_worker.py           # Baseline generation engine
│   │   ├── compare_engine.py            # Defacement detection engine
│   │   │
│   │   └── storage/
│   │       ├── mysql.py                 # Connection pool, table operations
│   │       ├── db.py                    # High-level semantic DB operations
│   │       ├── db_guard.py              # DB_SEMAPHORE connection limiter
│   │       ├── baseline_store.py        # Baseline persistence logic
│   │       ├── baseline_reader.py       # Baseline retrieval for comparison
│   │       └── crawl_reader.py          # Streaming URL iterator
│   │
│   ├── baselines/                       # Normalized HTML snapshots (gitignored)
│   ├── diffs/                           # HTML evidence reports (gitignored)
│   ├── logs/                            # Session logs (gitignored)
│   └── reports/                         # Dashboard reports (gitignored)
│
├── docs/
│   ├── baseline_mode.md                 # Baseline mode engineering spec
│   └── compare_mode.md                  # Compare mode engineering spec
│
├── .env                                 # Environment config (gitignored)
├── .gitignore
└── README.md
```

---

## ⚙️ Installation

### Prerequisites

- Python 3.10+
- MySQL 8.0+
- Playwright browsers

### Setup

```bash
# Clone the repository
git clone https://github.com/Ailover123/Defacement-Detection-System.git
cd Defacement-Detection-System

# Install dependencies
pip install -r baseline-crawler/requirements.txt

# Install Playwright browsers
playwright install chromium

# Configure environment
cp .env.example .env
# Edit .env with your MySQL credentials and configuration
```

---

## 🔧 Configuration

### Environment Variables (`.env`)

```env
# Database
MYSQL_HOST=localhost
MYSQL_USER=root
MYSQL_PASSWORD=your_password
MYSQL_DATABASE=crawler_db
MYSQL_POOL_SIZE=10

# Worker Scaling
MIN_WORKERS=5
MAX_WORKERS=5
MAX_PARALLEL_SITES=3

# Timeouts
SITE_PROCESS_TIMEOUT=1800
```

### Key Constants (`crawler/core.py`)

| Constant | Default | Description |
|---|---|---|
| `REQUEST_TIMEOUT` | 30s | HTTP request timeout |
| `CRAWL_DELAY` | 0.1s | Delay between requests per worker |
| `JS_GOTO_TIMEOUT` | 25s | Playwright navigation timeout |
| `JS_WAIT_TIMEOUT` | 5s | Network idle wait |
| `JS_STABILITY_TIME` | 2s | DOM stability wait |

---

## 🚀 Usage

### Crawl Mode — Discover Pages
```bash
cd baseline-crawler

# Crawl all enabled sites
python main.py --mode crawl

# Crawl specific site
python main.py --mode crawl --siteid 12345

# Parallel processing
python main.py --mode crawl --parallel --max-parallel-sites 5
```

### Baseline Mode — Generate Snapshots
```bash
# Baseline all crawled pages
python main.py --mode baseline

# Baseline specific site
python main.py --mode baseline --siteid 12345

# Baseline specific URLs
python main.py --mode baseline --urls https://example.com/about https://example.com/contact
```

### Compare Mode — Detect Defacement
```bash
# Compare all monitored sites
python main.py --mode compare

# Compare specific site
python main.py --mode compare --siteid 12345

# Compare with parallel processing
python main.py --mode compare --parallel
```

---

## 📊 Reporting

### Automated Dashboard

After each session, the system generates an interactive HTML dashboard at `reports/latest_report.html` featuring:

- **Summary Statistics**: Total sites, baselines, pages crawled, critical alerts
- **Detection Alerts Table**: Sortable by site, score, severity, and date
- **Domain Coverage**: Per-site page count and alert status
- **Severity Glossary**: Visual reference for LOW/MEDIUM/HIGH/CRITICAL classifications
- **Dark/Light Theme Toggle**: Professional presentation with theme persistence

### Evidence Reports

Each defacement detection generates a standalone HTML evidence report containing:

- **Side-by-side diff**: Baseline vs. live content with line-level highlighting
- **Change classification**: Inserted (green), Modified (yellow), Removed (red)
- **Intra-line changes**: Character-level highlighting within modified lines
- **Print-optimized CSS**: High-contrast mode for PDF export
- **Metadata cards**: URL, timestamp, change percentage, risk assessment

---

## 📖 Documentation

Detailed engineering specifications for each operating mode:

| Document | Description |
|---|---|
| [`docs/baseline_mode.md`](docs/baseline_mode.md) | Complete BASELINE mode technical specification |
| [`docs/compare_mode.md`](docs/compare_mode.md) | Complete COMPARE mode technical specification |

---

<p align="center">
  <sub>Built for production web integrity monitoring</sub>
</p>
