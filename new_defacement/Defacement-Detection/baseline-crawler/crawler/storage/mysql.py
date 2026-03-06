from mysql.connector.pooling import MySQLConnectionPool
from mysql.connector import Error
import os
import mysql.connector
import threading
from dotenv import load_dotenv
from crawler.storage.db_guard import DB_SEMAPHORE
from crawler.processor import LinkUtility

load_dotenv(override=True)


def with_retry(func):
    """Placeholder retry decorator (currently no-op)."""
    return func

pool_size = int(os.getenv("MYSQL_POOL_SIZE", 5))
db_host = os.getenv("MYSQL_HOST")
db_user = os.getenv("MYSQL_USER")
db_password = os.getenv("MYSQL_PASSWORD")
db_name = os.getenv("MYSQL_DATABASE")
db_port = os.getenv("MYSQL_PORT", 3306)

pool = MySQLConnectionPool(
    pool_name="crawler_pool",
    pool_size=pool_size,
    host=db_host,
    port=db_port,
    user=db_user,
    password=db_password,
    database=db_name,
)


import time
from crawler.core import logger

def get_connection(timeout: int = 10):
    """
    Acquire a DB connection with a timeout.
    Prevents silent deadlocks when DB_SEMAPHORE is exhausted.
    """
    acquired = DB_SEMAPHORE.acquire(timeout=timeout)

    if not acquired:
        logger.error(
            "[DB] Semaphore acquire timeout after "
            f"{timeout}s — possible connection starvation or deadlock"
        )
        raise RuntimeError(
            "DB semaphore timeout: too many concurrent DB operations"
        )

    try:
        return pool.get_connection()
    except Exception:
        DB_SEMAPHORE.release()
        raise

def ensure_baseline_columns(conn):
    """
    Ensures defacement_sites has the necessary columns for baseline storage.
    Migration helper.
    """
    try:
        cur = conn.cursor()
        
        # Add columns if missing to defacement_sites
        cols = [
            ("content_hash", "CHAR(64) NULL"),
            ("baseline_path", "VARCHAR(1024) NULL"),
            ("baseline_id", "VARCHAR(255) NULL"),
            ("updated_at", "TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP")
        ]
        
        for col_name, col_def in cols:
            # Check if column exists
            cur.execute(
                """
                SELECT COUNT(*)
                FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE()
                  AND TABLE_NAME = 'defacement_sites'
                  AND COLUMN_NAME = %s
                """,
                (col_name,)
            )
            if cur.fetchone()[0] == 0:
                logger.info(f"[MIGRATION] Adding column {col_name} to defacement_sites...")
                cur.execute(f"ALTER TABLE defacement_sites ADD COLUMN {col_name} {col_def}")
        conn.commit()
    except Exception as e:
        logger.error(f"[MIGRATION] Failed to ensure baseline schema: {e}")

def ensure_crawl_pages_columns(conn):
    """ Migration helper for crawl_pages table. """
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT COUNT(*)
            FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME = 'crawl_pages'
              AND COLUMN_NAME = 'description'
            """
        )
        if cur.fetchone()[0] == 0:
            logger.info("[MIGRATION] Adding column 'description' to crawl_pages...")
            cur.execute("ALTER TABLE crawl_pages ADD COLUMN description VARCHAR(255) NULL")
        conn.commit()
    except Exception as e:
        logger.error(f"[MIGRATION] Failed to ensure crawl_pages schema: {e}")

def repair_schema(conn):
    """ Fix crawl_jobs Foreign Key and other schema inconsistencies. """
    try:
        cur = conn.cursor()
        # -------------------------------------------------------------
        # SCHEMA REPAIR: Fix crawl_jobs Foreign Key (sites_old -> sites)
        # -------------------------------------------------------------
        # 1. Check if sites.siteid is indexed
        cur.execute(
            """
            SELECT COUNT(*) FROM information_schema.STATISTICS 
            WHERE TABLE_SCHEMA = DATABASE() 
              AND TABLE_NAME = 'sites' 
              AND COLUMN_NAME = 'siteid'
            """
        )
        if cur.fetchone()[0] == 0:
            logger.info("[SCHEMA REPAIR] Creating index idx_siteid on sites table...")
            cur.execute("CREATE INDEX idx_siteid ON sites(siteid)")

        # 2. Check if crawl_jobs references sites_old
        cur.execute(
            """
            SELECT CONSTRAINT_NAME 
            FROM information_schema.KEY_COLUMN_USAGE 
            WHERE TABLE_SCHEMA = DATABASE() 
              AND TABLE_NAME = 'crawl_jobs' 
              AND REFERENCED_TABLE_NAME = 'sites_old'
            """
        )
        old_fk = cur.fetchone()
        
        if old_fk:
            fk_name = old_fk[0]
            logger.info(f"[SCHEMA REPAIR] Found obsolete FK {fk_name} (crawl_jobs -> sites_old). Dropping...")
            cur.execute(f"ALTER TABLE crawl_jobs DROP FOREIGN KEY {fk_name}")
            
            # 3. Add new Foreign Key (crawl_jobs -> sites)
            logger.info("[SCHEMA REPAIR] Adding new FK (crawl_jobs -> sites)...")
            cur.execute("DELETE FROM crawl_jobs WHERE siteid NOT IN (SELECT siteid FROM sites)")
            cur.execute(
                """
                ALTER TABLE crawl_jobs 
                ADD CONSTRAINT crawl_jobs_fk_sites 
                FOREIGN KEY (siteid) REFERENCES sites(siteid) 
                ON DELETE CASCADE
                """
            )
        conn.commit()
    except Exception as e:
        logger.error(f"[SCHEMA REPAIR] Failed: {e}")
        
        conn.commit()
    except Exception as e:
        logger.error(f"[MIGRATION] Failed to ensure schema: {e}")
        # Don't raise, allow startup to proceed/fail naturally later


def check_db_health():
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT 1")
        res = cur.fetchone()
        is_healthy = res[0] == 1
        
        # Run schema check on health check
        ensure_baseline_columns(conn)
        ensure_crawl_pages_columns(conn)
        repair_schema(conn)
        
        return is_healthy
    finally:
        cur.close()
        conn.close()
        DB_SEMAPHORE.release()


def fetch_enabled_sites():
    conn = get_connection()
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT siteid, custid, url FROM sites ")
        return cur.fetchall()
    finally:
        cur.close()
        conn.close()
        DB_SEMAPHORE.release()


@with_retry
def insert_crawl_job(job_id, custid, siteid, start_url=None):
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT 1
            FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME = 'crawl_jobs'
              AND COLUMN_NAME = 'start_url'
            LIMIT 1
            """
        )
        has_start_url = cursor.fetchone() is not None

        # BYPASS FK CHECK: sites_prod (parent) is missing data from sites (child source).
        # We must disable FK checks to allow crawl_jobs to link to sites IDs.
        cursor.execute("SET FOREIGN_KEY_CHECKS=0")
        if has_start_url:
            cursor.execute(
                """
                INSERT INTO crawl_jobs (
                    job_id,
                    custid,
                    siteid,
                    start_url,
                    status
                ) VALUES (%s, %s, %s, %s, 'running')
                """,
                (job_id, custid, siteid, start_url),
            )
        else:
            cursor.execute(
                """
                INSERT INTO crawl_jobs (
                    job_id,
                    custid,
                    siteid,
                    status
                ) VALUES (%s, %s, %s, 'running')
                """,
                (job_id, custid, siteid),
            )
        cursor.execute("SET FOREIGN_KEY_CHECKS=1")
        conn.commit()
    except Error:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()
        DB_SEMAPHORE.release()


def complete_crawl_job(job_id, pages_crawled=None):
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE crawl_jobs SET status='completed' WHERE job_id=%s",
            (job_id,),
        )
        conn.commit()
    finally:
        cur.close()
        conn.close()
        DB_SEMAPHORE.release()


def fail_crawl_job(job_id, err):
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE crawl_jobs SET status='failed' WHERE job_id=%s",
            (job_id,),
        )
        conn.commit()
    finally:
        cur.close()
        conn.close()
        DB_SEMAPHORE.release()


def insert_crawl_page(data):
    # EXCEPT if we are manually marking (or clearing) a baseline description as requested.
    crawl_mode = os.getenv("CRAWL_MODE", "CRAWL").upper()
    has_description = "description" in data
    
    # DEBUG: Track what's happening in the safety check
    # from crawler.core import logger
    # logger.info(f"[DEBUG-DB] insert_crawl_page called: mode={crawl_mode}, desc={has_description}")

    if (crawl_mode == "BASELINE") and not has_description:
        from crawler.core import logger
        logger.warning(
            f"[SAFETY] Attempted to insert into crawl_pages during {crawl_mode} mode without a description. "
            f"This operation is prohibited. URL: {data.get('url')}"
        )
        return None

    # Pass seed_url if available to ensure domain matches sites table
    base_url = data.get("base_url")
    
    # 🕵️ Detect if the site is registered with 'www.' to enforce it in canonical ID
    enforce_www = False
    if base_url:
        from urllib.parse import urlparse
        temp_url = base_url
        if "://" not in temp_url:
            temp_url = "https://" + temp_url
        enforce_www = urlparse(temp_url).netloc.lower().startswith("www.")

    canonical_url = LinkUtility.get_canonical_id(data["url"], base_url, enforce_www=enforce_www)
    if not canonical_url:
        return None

    conn = get_connection()
    try:
        cur = conn.cursor()
        
        # 1. 🛡️ Check if the page already exists for this site
        # We use (siteid, url) which matches the UNIQUE KEY unique_site_page
        cur.execute(
            "SELECT id FROM crawl_pages WHERE siteid = %s AND url = %s",
            (data["siteid"], canonical_url)
        )
        row = cur.fetchone()
        action = ""

        if row:
            # 2. 🛡️ UPDATE metadata (and description if provided) in COMPARE mode
            affected_id = row[0]
            if crawl_mode == "COMPARE":
                # In COMPARE mode, we always want to sync the latest reachability (status_code, etc.)
                if has_description:
                    # Sync EVERYTHING including description (allows clearing it)
                    cur.execute(
                        """
                        UPDATE crawl_pages 
                        SET status_code = %s, content_type = %s, content_length = %s, 
                            response_time_ms = %s, fetched_at = %s, description = %s
                        WHERE id = %s
                        """,
                        (
                            data["status_code"], data["content_type"], data["content_length"],
                            data["response_time_ms"], data["fetched_at"], data.get("description"),
                            affected_id
                        )
                    )
                    from crawler.core import logger
                    logger.info(f"[DB-UPDATE] {canonical_url} id={affected_id} status={data['status_code']} desc='{data.get('description')}'")
                else:
                    # Sync metadata only (preserves existing description e.g. during fetch failures)
                    cur.execute(
                        """
                        UPDATE crawl_pages 
                        SET status_code = %s, content_type = %s, content_length = %s, 
                            response_time_ms = %s, fetched_at = %s
                        WHERE id = %s
                        """,
                        (
                            data["status_code"], data["content_type"], data["content_length"],
                            data["response_time_ms"], data["fetched_at"],
                            affected_id
                        )
                    )
                    from crawler.core import logger
                    logger.info(f"[DB-SYNC] {canonical_url} id={affected_id} status={data['status_code']}")
            elif has_description:
                # Fallback for other modes if a description is explicitly passed (rare)
                # cur.execute(
                #     "UPDATE crawl_pages SET description = %s, fetched_at = %s WHERE id = %s",
                #     (data.get("description"), data.get("fetched_at"), affected_id)
                # )
                action = "Existed"
        else:
            # 3. 🆕 INSERT: Doesn't exist, so insert fresh
            try:
                # cur.execute(
                #     """
                #     INSERT INTO crawl_pages
                #     (job_id, custid, siteid, url, parent_url, depth, status_code,
                #      content_type, content_length, response_time_ms, fetched_at, description)
                #     VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                #     """,
                #     (
                #         data["job_id"], data["custid"], data["siteid"],
                #         canonical_url, data["parent_url"], data["depth"],
                #         data["status_code"], data["content_type"],
                #         data["content_length"], data["response_time_ms"],
                #         data["fetched_at"], data.get("description")
                #     )
                # )
                action = "Inserted"
                # affected_id = cur.lastrowid
            except mysql.connector.errors.DataError as e:
                if e.errno == 1406: # Data too long
                    from crawler.core import logger
                    logger.error(f"[DB] Skipping insertion: URL too long for database column ({len(canonical_url)} chars)")
                    return None
                raise e
            except mysql.connector.errors.IntegrityError as e:
                if e.errno == 1062: # Duplicate entry
                    # 🛡️ RACE CONDITION: Another worker inserted this URL since our SELECT
                    action = "Existed"
                    cur.execute(
                        "SELECT id FROM crawl_pages WHERE siteid = %s AND url = %s",
                        (data["siteid"], canonical_url)
                    )
                    row = cur.fetchone()
                    affected_id = row[0] if row else 0
                else:
                    raise e

        conn.commit()

        return {
            "action": action
            # "id": affected_id
        }
    finally:
        cur.close()
        conn.close()
        DB_SEMAPHORE.release()


def check_crawl_page(data):
    """
    This function now ONLY checks whether URL exists.
    Actual insertion is handled by batch API endpoint.
    """

    crawl_mode = os.getenv("CRAWL_MODE", "CRAWL").upper()
    has_description = "description" in data

    # 🚨 Safety check for BASELINE mode
    if crawl_mode == "BASELINE" and not has_description:
        from crawler.core import logger
        logger.warning(
            f"[SAFETY] Insert blocked in BASELINE mode. URL: {data.get('url')}"
        )
        return None

    base_url = data.get("base_url")

    # Detect www enforcement
    enforce_www = False
    if base_url:
        from urllib.parse import urlparse
        temp_url = base_url if "://" in base_url else "https://" + base_url
        enforce_www = urlparse(temp_url).netloc.lower().startswith("www.")

    canonical_url = LinkUtility.get_canonical_id(
        data["url"], base_url, enforce_www=enforce_www
    )

    if not canonical_url:
        return None

    conn = get_connection()
    cur = conn.cursor()

    try:
        # ✅ Only check existence
        cur.execute(
            "SELECT id FROM crawl_pages WHERE siteid = %s AND url = %s",
            (data["siteid"], canonical_url)
        )

        row = cur.fetchone()

        if row:
            return {
                "action": "Existed",
                "id": row[0]
            }
        else:
            return {
                "action": "Inserted"
            }

    finally:
        cur.close()
        conn.close()
        DB_SEMAPHORE.release()

def insert_defacement_site(siteid, baseline_id, url):
    canonical_url = url  # Already canonical

    logger.info(f"Mysql data :- {siteid}, {baseline_id}, {url}")

    conn = get_connection()
    cur = None

    try:
        cur = conn.cursor(buffered=True)

        cur.execute(
            """
            SELECT id 
            FROM defacement_sites 
            WHERE siteid=%s AND url=%s AND baseline_id=%s
            """,
            (siteid, canonical_url, baseline_id)
        )

        row = cur.fetchone()

        if row:
            return "update"
        else:
            return "insert"

    except Exception as e:
        logger.error(f"MySQL error for siteid={siteid}, url={url}: {e}")
        return "failed"

    finally:
        if cur:
            cur.close()

        if conn:
            conn.close()

        DB_SEMAPHORE.release()


def upsert_baseline_hash(site_id, normalized_url, content_hash, baseline_path, baseline_id=None):
    """
    Insert or UPDATE baseline for a URL into defacement_sites.
    No branding. No base_url. Deterministic.
    """
    canonical_url = normalized_url  # Already canonical

    conn = get_connection()
    try:
        cur = conn.cursor(buffered=True)

        cur.execute(
            "SELECT id FROM defacement_sites WHERE siteid=%s AND url=%s",
            (site_id, canonical_url)
        )
        row = cur.fetchone()

        if row:
            cur.execute(
                """
                UPDATE defacement_sites
                SET content_hash = %s,
                    baseline_path = %s,
                    baseline_id = %s,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = %s
                """,
                (content_hash, baseline_path, baseline_id, row[0]),
            )
        else:
            cur.execute(
                """
                INSERT INTO defacement_sites
                    (siteid, url, content_hash, baseline_path, baseline_id, action, updated_at)
                VALUES (%s, %s, %s, %s, %s, 'selected', CURRENT_TIMESTAMP)
                """,
                (site_id, canonical_url, content_hash, baseline_path, baseline_id),
            )

        conn.commit()
        return True

    finally:
        cur.close()
        conn.close()
        DB_SEMAPHORE.release()


def fetch_baseline_hash(site_id, normalized_url):
    conn = get_connection()
    try:
        cur = conn.cursor(dictionary=True, buffered=True)

        cur.execute(
            """
            SELECT baseline_id, content_hash, baseline_path
            FROM defacement_sites
            WHERE siteid=%s AND url=%s 
            """,
            (site_id, normalized_url),
        )

        return cur.fetchone()

    finally:
        cur.close()
        conn.close()
        DB_SEMAPHORE.release()



def site_has_baselines(site_id):
    conn = get_connection()
    try:
        cur = conn.cursor(buffered=True)
        cur.execute(
            "SELECT 1 FROM defacement_sites WHERE siteid=%s AND content_hash IS NOT NULL LIMIT 1",
            (site_id,),
        )
        row = cur.fetchone()
        try:
             cur.fetchall()
        except:
             pass
        return row is not None
    finally:
        cur.close()
        conn.close()
        DB_SEMAPHORE.release()


def has_site_crawl_data(site_id, url=None):
    """Checks if crawl_pages already has entries for this site."""
    conn = get_connection()
    try:
        cur = conn.cursor()
        if url:
            # Use canonical ID to match DB storage format
            canonical_url = LinkUtility.get_canonical_id(url)
            cur.execute(
                "SELECT 1 FROM crawl_pages WHERE siteid=%s AND url=%s LIMIT 1",
                (site_id, canonical_url),
            )
        else:
            cur.execute(
                "SELECT 1 FROM crawl_pages WHERE siteid=%s LIMIT 1",
                (site_id,),
            )
        return cur.fetchone() is not None
    finally:
        cur.close()
        conn.close()
        DB_SEMAPHORE.release()


def fetch_site_info_by_baseline_id(baseline_id):
    conn = get_connection()
    try:
        baseline_id = str(baseline_id).strip()

        logger.info(f"[DEBUG] baseline_id raw: {repr(baseline_id)}")
        logger.info(f"[DEBUG] baseline_id type: {type(baseline_id)}")

        cur = conn.cursor(dictionary=True)

        query = """
            SELECT d.siteid, d.url, s.custid 
            FROM defacement_sites d
            JOIN sites s ON d.siteid = s.siteid
            WHERE d.baseline_id=%s
        """

        logger.info(f"[DEBUG] running query with baseline_id={baseline_id}")

        cur.execute(query, (baseline_id,))
        row = cur.fetchone()

        logger.info(f"[DEBUG] Row fetched: {row}")

        return row

    finally:
        cur.close()
        conn.close()
        DB_SEMAPHORE.release()

# ============================================================
# OBSERVED PAGE UPSERT (STATE, NOT HISTORY)
# ============================================================

def insert_observed_page(
    site_id,
    baseline_id,
    normalized_url,
    observed_hash,
    changed,
    diff_path=None,
    defacement_score=None,
    defacement_severity=None,
):
    canonical_url = normalized_url  # Already canonical

    conn = get_connection()
    try:
        cur = conn.cursor()

        cur.execute(
            """
            INSERT INTO observed_pages
                (site_id, baseline_id, normalized_url,
                 observed_hash, changed, diff_path,
                 defacement_score, defacement_severity)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                baseline_id = VALUES(baseline_id),
                observed_hash = VALUES(observed_hash),
                changed = VALUES(changed),
                diff_path = VALUES(diff_path),
                defacement_score = VALUES(defacement_score),
                defacement_severity = VALUES(defacement_severity)
            """,
            (
                site_id,
                baseline_id,
                canonical_url,
                observed_hash,
                changed,
                diff_path,
                defacement_score,
                defacement_severity,
            ),
        )

        conn.commit()

    finally:
        cur.close()
        conn.close()
        DB_SEMAPHORE.release()



def fetch_observed_page(site_id, normalized_url):
    """
    Fetch the last known observed state of a page.
    Used to skip re-processing if the hash hasn't changed.
    """
    conn = get_connection()
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute(
            """
            SELECT observed_hash, baseline_id, defacement_score, defacement_severity
            FROM observed_pages 
            WHERE site_id=%s AND normalized_url=%s
            """,
            (site_id, normalized_url),
        )
        return cur.fetchone()
    finally:
        cur.close()
        conn.close()
        DB_SEMAPHORE.release()


def get_selected_defacement_rows():
    """Return defacement_sites rows marked as 'selected'.

    Uses the shared MySQL connection pool and DB_SEMAPHORE, so we must
    release the semaphore after closing the connection.
    """
    conn = get_connection()
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute(
            """
            SELECT d.siteid, d.url, d.baseline_id, d.threshold, s.url as base_url
            FROM defacement_sites d
            JOIN sites s ON d.siteid = s.siteid
            WHERE d.action = 'selected'
            """
        )
        return cur.fetchall()
    finally:
        cur.close()
        conn.close()
        DB_SEMAPHORE.release()
        
def get_support_defacement_email():
    """
    Returns the latest active email for Defacement_New_Links.
    Falls back to 'Others' type if not found.
    """

    conn = get_connection()
    try:
        cur = conn.cursor(dictionary=True)

        cur.execute("""
            SELECT COALESCE(
                (SELECT waf_ref.fld_name 
                    FROM mail_receiver 
                    INNER JOIN waf_filepath AS waf_ref 
                        ON waf_ref.file_path = mail_receiver.waf_filepath 
                    WHERE mail_receiver.type = 'Defacement_New_Links' 
                    AND mail_receiver.status = 1 
                    ORDER BY time DESC 
                    LIMIT 1),

                (SELECT waf_ref.fld_name 
                    FROM mail_receiver 
                    INNER JOIN waf_filepath AS waf_ref 
                        ON waf_ref.file_path = mail_receiver.waf_filepath 
                    WHERE mail_receiver.type = 'Others' 
                    AND mail_receiver.status = 1 
                    ORDER BY time DESC 
                    LIMIT 1)
            ) AS email
        """)

        row = cur.fetchone()
        return row['email'] if row else None

    finally:
        cur.close()
        conn.close()
        DB_SEMAPHORE.release()
        
def get_data_by_fld_name(fld_name):
    conn = get_connection()
    try:
        cur = conn.cursor(dictionary=True)

        cur.execute(
            """
            SELECT *
            FROM waf_filepath
            WHERE fld_name = %s
            """,
            (fld_name,)
        )

        return cur.fetchall()

    finally:
        cur.close()
        conn.close()
        DB_SEMAPHORE.release()
        
def get_custname_data(cust_id):
    conn = get_connection()
    try:
        cur = conn.cursor(dictionary=True)

        cur.execute(
            """
            SELECT cust_name
            FROM waf_customer
            WHERE cust_id = %s
            """,
            (cust_id,)
        )

        row = cur.fetchone()
        return row['cust_name'] if row else None
    
    finally:
        cur.close()
        conn.close()
        DB_SEMAPHORE.release()