import threading
import os
from dotenv import load_dotenv

load_dotenv(override=True)

# Global limit on concurrent DB ops matching the connection pool size
pool_size = int(os.getenv("MYSQL_POOL_SIZE", 5))
DB_SEMAPHORE = threading.BoundedSemaphore(pool_size)
