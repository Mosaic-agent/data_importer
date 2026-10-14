# StockScans fetcher

- A batch de-duplicates symbols while preserving order and reuses one HTTP
  session for each multi-symbol fundamentals, insider, or shareholding request.
- Formatted or missing numeric API fields normalize to zero; a malformed field
  must not discard the remaining data for that symbol.
- By default (`max_workers=1`) requests stay sequential with the existing
  per-call throttle/retry behavior. Passing `max_workers > 1` fetches symbols
  concurrently but still returns rows in input-symbol order.
- StockScans enforces a rolling-window request budget, not a simple
  concurrent-connection cap: live testing found 4-way concurrency on a
  30-symbol batch clean (0 429s in 0.25s), while 8-way tripped it partway
  through, and once tripped even slow sequential requests kept failing for
  30s+. So every worker thread (regardless of `max_workers`) shares one
  pacing gate and one circuit breaker (`_throttle` / `_trip_breaker`): a 429
  seen by any thread pauses every thread for a cooldown instead of retrying
  hot. Production callers (`fetchers/adapters.py`) use `max_workers=4` — the
  exact level proven safe, not a guess beyond it.
