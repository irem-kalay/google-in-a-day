# Google in a Day — Web Crawler & Real-Time Search Engine
**GitHub Repository:** https://github.com/irem-kalay/google-in-a-day.git

A concurrent web crawler and real-time search engine built using only Python's standard library (`urllib`, `html.parser`, `threading`, `queue`, `http.server`).

## Requirements

- Python 3.8+
- No external dependencies

## How to Run

Run `web_server.py` inside the `core` folder.

- **With `state.json`:** The crawler resumes from a previously saved state (pre-crawled by me).
- **Without `state.json`:** The crawler starts fresh with an empty index. Add URLs to crawl manually via the UI.

The original `state.json` is in the `old_versions_and_extras` folder.

```bash
cd core
python3 web_server.py
```

Then open your browser and go to:

```
http://127.0.0.1:8000
```

## How to Index

To start crawling, fill in the **Start Manual Indexing** form in the dashboard and press **Start Indexing**. The crawler will immediately begin crawling from that URL up to the configured depth.

You can add multiple URLs one by one — each is enqueued and crawled concurrently by the worker threads.

## Configuration

These constants can be edited at the top of `web_server.py`:

| Parameter | Default | Description |
|---|---|---|
| `NUM_WORKERS` | `4` | Number of concurrent crawler threads |
| `QUEUE_DEFAULT_SIZE` | `1000` | Default back-pressure queue limit on startup |
| `QUEUE_ABSOLUTE_MAX` | `10000` | Hard ceiling on queue capacity the user can set via UI |
| `SEARCH_TOP_N` | `10` | Maximum number of search results returned |
| `METRICS_BACKPRESSURE_HIGH` | `0.9` | Queue fill ratio threshold for HIGH back-pressure |
| `METRICS_BACKPRESSURE_MED` | `0.6` | Queue fill ratio threshold for MEDIUM back-pressure |

Crawl depth (`k`) is set **per crawl session** in the UI (0–5), not as a global constant.

## Dashboard & UI Controls

The web UI updates every second and shows:
- **Visited URLs** — number of pages successfully crawled
- **Queue Size / Capacity** — current pending tasks vs. the active queue limit
- **Back-Pressure** — LOW / MEDIUM / HIGH based on queue fill ratio
- **Recent Crawlers** — list of crawl sessions with origin URL, depth limit, and status

### Indexing form fields

| Field | Description |
|---|---|
| URL | Seed URL to start crawling from |
| Depth (k) | Maximum crawl depth (0–5) |
| Hit Rate (s) | Optional per-request delay in seconds (throttles crawl speed) |
| Queue Capacity | Overrides the active queue size limit for this crawl |

**To search:** type a query into the search box and press Enter. Results are returned as `(url, origin_url, depth)` triples, ranked by term frequency, co-occurrence bonus, and title match.

**To stop:** press `Ctrl+C` in the terminal.

## API Endpoints

| Endpoint | Description |
|---|---|
| `GET /api/metrics` | Returns crawler state, queue stats, back-pressure status, and session list |
| `GET /api/search?q={query}` | Returns ranked search results for a given query |
| `GET /api/index?url={url}&k={depth}&hit_rate={s}&capacity={n}` | Enqueues a new URL for crawling |

## Ranking Algorithm

Search results are scored using three signals:

1. **Term frequency** — sum of how often each query term appears in the page
2. **Co-occurrence bonus** — the minimum frequency across all terms (rewards pages where all terms appear with similar density)
3. **Title match bonus** — +50 points per query term found in the page `<title>`

Only pages containing **all** query terms (AND logic) are returned.

## State Persistence

The crawler automatically saves its state to `state.json` on exit (`Ctrl+C`). On the next run, it resumes from where it left off instead of restarting from scratch.

The saved state includes: visited URLs, inverted index, page titles, URL metadata (origin + depth), and the pending crawl queue.

## File Structure

```
core/
    web_server.py        # Entry point: HTTP server, dashboard UI, search logic
    parser.py            # HTML parser and crawler worker threads
    data_structures.py   # Thread-safe data structures
    state.json           # Pre-crawled state file

product_prd.md
readme.md
recommendation.md
```
