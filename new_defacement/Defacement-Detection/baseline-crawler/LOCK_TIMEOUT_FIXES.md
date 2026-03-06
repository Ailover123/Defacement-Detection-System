# Lock Timeout Fixes - March 5, 2026

## Problem
The baseline crawler was experiencing "Lock wait timeout exceeded; try restarting transaction" errors (MySQL error 1205) during the `upsert_baseline_hash()` operation.

### Root Causes
1. **Non-atomic database operations**: Original code used `SELECT` followed by `INSERT` or `UPDATE`, which is not atomic
2. **Race conditions**: When multiple workers (or retries) try to process the same URL concurrently, the SELECT might return "no row" for both, causing duplicate INSERT attempts
3. **Lock contention**: Long-running transactions holding locks without proper timeout handling
4. **Poor connection cleanup**: Connections and cursors weren't being properly released on errors

## Solutions Implemented

### 1. Atomic Database Operations (`mysql.py`)
**Before:**
```python
# Non-atomic pattern - vulnerable to race conditions
cur.execute("SELECT id FROM defacement_sites WHERE siteid=%s AND url=%s", (site_id, canonical_url))
row = cur.fetchone()
if row:
    cur.execute("UPDATE ...")
else:
    cur.execute("INSERT ...")
```

**After:**
```python
# Atomic operation - MySQL handles both insert and update in single transaction
cur.execute("""
    INSERT INTO defacement_sites
        (siteid, url, content_hash, baseline_path, baseline_id, action, updated_at)
    VALUES (%s, %s, %s, %s, %s, 'selected', CURRENT_TIMESTAMP)
    ON DUPLICATE KEY UPDATE
        content_hash = VALUES(content_hash),
        baseline_path = VALUES(baseline_path),
        baseline_id = VALUES(baseline_id),
        updated_at = CURRENT_TIMESTAMP
""", (site_id, canonical_url, content_hash, baseline_path, baseline_id))
```

**Benefits:**
- Single atomic operation eliminates race conditions
- MySQL handles INSERT/UPDATE internally
- No gap between SELECT and INSERT/UPDATE for concurrent requests

### 2. Exponential Backoff Retry Logic (`mysql.py`)
Added retry wrapper that:
- Detects MySQL lock timeouts (error 1205)
- Retries up to 3 times with exponential backoff (0.5s, 1.0s, 2.0s)
- Logs retry attempts for visibility
- Re-raises exception after max retries exceeded

```python
def upsert_baseline_hash(...):
    """Uses atomic ON DUPLICATE KEY UPDATE with retry logic"""
    
    def _do_upsert():
        # Atomic operation with proper cleanup
        ...
    
    # Retry with exponential backoff on lock timeouts
    max_retries = 3
    for attempt in range(max_retries):
        try:
            return _do_upsert()
        except Exception as e:
            if is_lock_timeout and attempt < max_retries - 1:
                # Exponential backoff: 0.5s, 1.0s, 2.0s
                wait_time = 0.5 * (2 ** attempt)
                logger.warning(f"Retry {attempt + 1}/{max_retries} in {wait_time}s...")
                time.sleep(wait_time)
            else:
                raise
```

### 3. Improved Error Handling and Cleanup (`mysql.py`)
```python
finally:
    try:
        cur.close()
    except:
        pass
    try:
        conn.close()
    except:
        pass
    DB_SEMAPHORE.release()
```

**Benefits:**
- Catches exceptions during cleanup
- Always releases DB semaphore, preventing connection starvation
- Prevents cascading failures

### 4. Better Baseline Save Strategy (`baseline_store.py`)
Changed order of operations:
1. **Write file first** (atomic, local filesystem doesn't have locks)
2. **Then update database** with retry logic
3. If DB fails, file is already written (no partial state)
4. Return action status to caller for proper accounting

```python
# Write file first
with open(path, "w") as f:
    f.write(html.strip())

# Then DB with retry logic
try:
    upsert_baseline_hash(...)
    return baseline_id, str(path), action  # action: 'created'/'updated'
except Exception as e:
    # File written but DB failed - not a total loss
    return baseline_id, str(path), "failed"
```

## Expected Performance Impact

| Issue | Before | After |
|-------|--------|-------|
| Lock timeouts | Causes complete failure | Retries 3x with backoff |
| Race conditions | Duplicate key errors | Atomically handled |
| Connection leaks | Possible on errors | Always released |
| Retry wait time | 0 (instant fail) | 0.5-2.0s (adaptive) |

## Testing Recommendations

1. **Run baseline refetch** on a multi-URL site with 10+ workers
2. **Monitor logs** for:
   - `[DB-UPSERT-RETRY]` messages (confirms retries are working)
   - `[BASELINE] CREATED/UPDATED` (confirms success after retry)
3. **Check baseline files** are written even if DB update fails
4. **Verify final counts** match actual files created

## Files Modified
- `crawler/storage/mysql.py` - Atomic operations, retry logic
- `crawler/storage/baseline_store.py` - File-first strategy, error handling

## Monitoring Commands
```bash
# Check for retry messages
tail -f logs/Baseline_*.log | grep -i "retry\|lock\|timeout"

# Count baseline creations vs database updates
grep -c "CREATED" logs/Baseline_*.log
grep -c "UPDATED" logs/Baseline_*.log
grep -c "Failed" logs/Baseline_*.log
```
