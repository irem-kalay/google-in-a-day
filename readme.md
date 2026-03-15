# Google in a Day — Web Crawler & Real-Time Search Engine

A concurrent web crawler and real-time search engine built using only Python's standard library (`urllib`, `html.parser`, `threading`, `queue`, `http.server`).

## Requirements

- Python 3.8+
- No external dependencies

## How to Run

Run `web_server.py` inside the `core` folder.

- **With `state.json`:** The crawler resumes from a previously saved state (pre-crawled by me).
- **Without `state.json`:** The crawler starts fresh. When you press `Ctrl+C`, it saves a new `state.json` automatically.

The original `state.json` is in the `old_versions_and_extras` folder.


```bash
python web_server.py
```

Then open your browser and go to localhost:

```
http://127.0.0.1:8000
```

## Configuration

Edit the constants at the top of `web_server.py`:

| Parameter | Default | Description |
|---|---|---|
| `SEED_URL` | Wikipedia TR + 5 ITU URLs | Starting URLs for the crawl (Wikipedia TR, obs.itu.edu.tr, itu.edu.tr, sis.itu.edu.tr, ninova.itu.edu.tr, kutuphane.itu.edu.tr) |
| `MAX_DEPTH` | `2` | Maximum crawl depth |
| `NUM_WORKERS` | `4` | Number of concurrent crawler threads |
| `QUEUE_MAXSIZE` | `1000` | Back-pressure queue limit |

## Dashboard

The web UI updates every second and shows:
- Number of URLs visited
- Current queue depth and back-pressure status (LOW / MEDIUM / HIGH)

**To search:** type a query into the search box and press Enter. Results are returned as `(url, origin_url, depth)` triples, ranked by term frequency, co-occurrence bonus, and title match.

**To stop:** press `Ctrl+C` in the terminal.

## State Persistence

The crawler automatically saves its state to `state.json` on exit. On the next run, it resumes from where it left off instead of restarting from scratch.

## File Structure

```
core/
    web_server.py        # Entry point: HTTP server, dashboard UI, search logic
    parser.py            # HTML parser and crawler worker threads
    data_structures.py   # Thread-safe data structures

old_versions_and_extras/
    main.py              # Original terminal-based (curses) old version
    server.py            # Earlier version of the web server
    state.json           # Pre-crawled state file
    crawler.log          # Sample log output

product_prd.md
readme.md
recommendation.md
```