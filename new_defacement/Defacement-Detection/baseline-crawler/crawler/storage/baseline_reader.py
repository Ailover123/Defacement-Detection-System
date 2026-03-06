# crawler/storage/baseline_reader.py

from crawler.storage.mysql import get_connection
from crawler.storage.db_guard import DB_SEMAPHORE


def get_baseline_hash(
    *,
    site_id: int,
    normalized_url: str,
    base_url: str | None = None,
):
    """
    Fetch baseline row for a given page.

    Returns:
      {
        "id": int,
        "content_hash": str,
        "baseline_path": str
      }
    or None
    """

    conn = get_connection()
    try:
        with conn.cursor(dictionary=True) as cur:
            # Try exact match first
            cur.execute(
                """
                SELECT
                    id,
                    content_hash,
                    baseline_path
                FROM defacement_sites
                WHERE siteid = %s
                  AND url = %s
                LIMIT 1
                """,
                (site_id, normalized_url),
            )
            row = cur.fetchone()
            if row:
                return row

            # Fallback: try without trailing slash
            if normalized_url.endswith("/"):
                alt = normalized_url.rstrip("/")
            else:
                alt = normalized_url + "/"

            cur.execute(
                """
                SELECT
                    id,
                    content_hash,
                    baseline_path
                FROM defacement_sites
                WHERE siteid = %s
                  AND url = %s
                LIMIT 1
                """,
                (site_id, alt),
            )
            return cur.fetchone()

    finally:
        conn.close()
        DB_SEMAPHORE.release()
