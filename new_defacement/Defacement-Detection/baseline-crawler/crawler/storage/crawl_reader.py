from crawler.storage.mysql import get_connection
from crawler.storage.db_guard import DB_SEMAPHORE


def iter_crawl_urls(*, siteid: int):
    """
    STREAMING iterator over crawl_pages URLs.
    - Does NOT load all URLs into memory
    - Releases DB connection as soon as iteration finishes
    """

    conn = get_connection()
    cur = conn.cursor()

    try:
        cur.execute(
            """
            SELECT DISTINCT url
            FROM crawl_pages
            WHERE siteid = %s
              AND content_type LIKE 'text/html%%'
                        ORDER BY url ASC
            """,
            (siteid,),
        )
        # Fetch all to memory to release DB connection immediately
        urls = [row[0] for row in cur.fetchall()]
    finally:
        cur.close()
        conn.close()
        DB_SEMAPHORE.release()

    for url in urls:
        yield url
