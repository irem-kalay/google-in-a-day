from __future__ import annotations

import json
import logging
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Dict, List, Tuple
from urllib.parse import urlparse, parse_qs, quote

# ============================================================
# Path setup so we can import from parent directory
# ============================================================

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PARENT_DIR = os.path.dirname(CURRENT_DIR)

if PARENT_DIR not in sys.path:
    sys.path.insert(0, PARENT_DIR)

# Core modules are assumed to live in the parent directory
from data_structures import (  # type: ignore
    ThreadSafeVisitedSet,
    ThreadSafeInvertedIndex,
    CrawlQueue,
)
from parser import CrawlerWorker  # type: ignore


# ============================================================
# Configuration
# ============================================================

SEED_URL = "https://obs.itu.edu.tr/public/DersProgram"
MAX_DEPTH = 2
NUM_WORKERS = 4
QUEUE_MAXSIZE = 1000
SEARCH_TOP_N = 10
METRICS_BACKPRESSURE_HIGH = 0.9
METRICS_BACKPRESSURE_MED = 0.6

QUERY_TOKEN_SPLIT_RE = re.compile(r"[^a-zA-Z0-9]+")


# ============================================================
# Shared crawler state
# ============================================================

visited_set = ThreadSafeVisitedSet()
inverted_index = ThreadSafeInvertedIndex()
crawl_queue = CrawlQueue(maxsize=QUEUE_MAXSIZE)
stop_event = threading.Event()
workers: List[CrawlerWorker] = []


# ============================================================
# Search helpers
# ============================================================

def normalize_query(query: str) -> List[str]:
    query = query.lower()
    parts = QUERY_TOKEN_SPLIT_RE.split(query)
    return [p for p in parts if p]


def search_index(index: ThreadSafeInvertedIndex, query: str, top_n: int) -> List[Tuple[str, int]]:
    tokens = normalize_query(query)
    if not tokens:
        return []

    scores: Dict[str, int] = {}
    for term in tokens:
        postings = index.get_postings(term)
        for url, freq in postings.items():
            scores[url] = scores.get(url, 0) + freq

    ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    return ranked[:top_n]


# ============================================================
# HTML Template
# ============================================================

HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <title>Google-in-a-Day Search</title>
  <style>
    * { box-sizing: border-box; }
    body {{
      margin: 0;
      padding: 0;
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: #0f172a;
      color: #e5e7eb;
    }}
    header {{
      padding: 1.5rem 2rem;
      background: #020617;
      border-bottom: 1px solid #1f2937;
      display: flex;
      justify-content: space-between;
      align-items: center;
    }}
    header h1 {{
      margin: 0;
      font-size: 1.3rem;
      color: #f97316;
    }}
    header span {{
      font-size: 0.8rem;
      color: #9ca3af;
    }}
    main {{
      padding: 1.5rem 2rem 2rem 2rem;
      display: grid;
      grid-template-columns: 2fr 1fr;
      gap: 1.5rem;
    }}
    @media (max-width: 900px) {{
      main {{ grid-template-columns: 1fr; }}
    }}
    .card {{
      background: #020617;
      border-radius: 0.75rem;
      border: 1px solid #1f2937;
      padding: 1rem 1.25rem;
      box-shadow: 0 15px 30px rgba(0,0,0,0.35);
    }}
    .card h2 {{
      margin-top: 0;
      font-size: 1.1rem;
      margin-bottom: 0.75rem;
      color: #f9fafb;
    }}
    .search-bar {{
      display: flex;
      gap: 0.75rem;
      margin-bottom: 0.75rem;
    }}
    .search-bar input[type="text"] {{
      flex: 1;
      padding: 0.65rem 0.85rem;
      border-radius: 999px;
      border: 1px solid #374151;
      background: #020617;
      color: #e5e7eb;
      font-size: 0.95rem;
      outline: none;
    }}
    .search-bar input[type="text"]:focus {{
      border-color: #f97316;
      box-shadow: 0 0 0 1px #f97316aa;
    }}
    .search-bar button {{
      padding: 0.65rem 1.1rem;
      border-radius: 999px;
      border: none;
      background: linear-gradient(135deg, #f97316, #ea580c);
      color: #111827;
      font-weight: 600;
      font-size: 0.9rem;
      cursor: pointer;
    }}
    .search-bar button:hover {{
      filter: brightness(1.08);
    }}
    .metrics-grid {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 0.75rem;
      margin-top: 0.25rem;
    }}
    .metric {{
      background: #020617;
      border-radius: 0.6rem;
      border: 1px solid #1f2937;
      padding: 0.6rem 0.75rem;
    }}
    .metric-label {{
      font-size: 0.7rem;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: #9ca3af;
      margin-bottom: 0.2rem;
    }}
    .metric-value {{
      font-size: 0.95rem;
      font-weight: 600;
      color: #f9fafb;
    }}
    .metric-pill {{
      display: inline-flex;
      align-items: center;
      padding: 0.20rem 0.5rem;
      border-radius: 999px;
      font-size: 0.72rem;
      font-weight: 500;
    }}
    .metric-pill.ok {{
      background: rgba(22, 163, 74, 0.15);
      color: #4ade80;
    }}
    .metric-pill.med {{
      background: rgba(234, 179, 8, 0.15);
      color: #facc15;
    }}
    .metric-pill.high {{
      background: rgba(239, 68, 68, 0.15);
      color: #fb7185;
    }}
    .results-list {{
      list-style: none;
      padding: 0;
      margin: 0.5rem 0 0 0;
    }}
    .result-item {{
      padding: 0.55rem 0;
      border-bottom: 1px solid #1f2937;
    }}
    .result-item:last-child {{
      border-bottom: none;
    }}
    .result-url {{
      color: #93c5fd;
      font-size: 0.9rem;
      word-break: break-all;
      text-decoration: none;
    }}
    .result-url:hover {{
      text-decoration: underline;
    }}
    .result-score {{
      font-size: 0.75rem;
      color: #9ca3af;
    }}
    .muted {{
      font-size: 0.8rem;
      color: #6b7280;
    }}
    footer {{
      padding: 0.75rem 2rem 1rem 2rem;
      font-size: 0.75rem;
      color: #4b5563;
      border-top: 1px solid #1f2937;
      background: #020617;
    }}
  </style>
</head>
<body>
  <header>
    <h1>Google-in-a-Day <span>· Minimal Web UI</span></h1>
    <span id="statusText">Crawler çalışıyor...</span>
  </header>
  <main>
    <section class="card">
      <h2>Arama</h2>
      <form id="searchForm" class="search-bar">
        <input id="searchInput" type="text" placeholder="Bir sorgu yazın ve Enter'a basın..." autocomplete="off" />
        <button type="submit">Ara</button>
      </form>
      <p class="muted" id="searchInfo">Sonuç görmek için bir sorgu gönderin.</p>
      <ul id="results" class="results-list"></ul>
    </section>
    <aside class="card">
      <h2>Dashboard</h2>
      <div class="metrics-grid">
        <div class="metric">
          <div class="metric-label">Ziyaret Edilen URL</div>
          <div class="metric-value" id="visitedCount">-</div>
        </div>
        <div class="metric">
          <div class="metric-label">Kuyruk Boyutu</div>
          <div class="metric-value"><span id="queueSize">-</span> / {queue_max}</div>
        </div>
        <div class="metric">
          <div class="metric-label">Back-Pressure</div>
          <div class="metric-value">
            <span id="backpressurePill" class="metric-pill muted">-</span>
          </div>
        </div>
      </div>
      <p class="muted" style="margin-top:0.75rem;">
        Dashboard, her saniye metrikleri yeniler. Arama sonuçları, terim frekanslarının
        basit bir toplamına göre sıralanır.
      </p>
    </aside>
  </main>
  <footer>
    Bu demo yalnızca Python standart k&uuml;t&uuml;phanesi ile &ccedil;alışır
    (<code>http.server</code>, <code>threading</code>, <code>urllib</code>, vb.).
  </footer>

  <script>
    const visitedEl = document.getElementById("visitedCount");
    const queueEl = document.getElementById("queueSize");
    const pillEl = document.getElementById("backpressurePill");
    const statusText = document.getElementById("statusText");
    const resultsEl = document.getElementById("results");
    const searchInfoEl = document.getElementById("searchInfo");
    const searchForm = document.getElementById("searchForm");
    const searchInput = document.getElementById("searchInput");

    function updatePill(status) {{
      pillEl.textContent = status;
      pillEl.classList.remove("ok", "med", "high", "muted");
      if (status === "LOW") {{
        pillEl.classList.add("ok");
      }} else if (status === "MEDIUM") {{
        pillEl.classList.add("med");
      }} else if (status === "HIGH") {{
        pillEl.classList.add("high");
      }} else {{
        pillEl.classList.add("muted");
      }}
    }}

    async function fetchMetrics() {{
      try {{
        const res = await fetch("/api/metrics");
        if (!res.ok) throw new Error("HTTP " + res.status);
        const data = await res.json();
        visitedEl.textContent = data.visited_count;
        queueEl.textContent = data.queue_size;
        updatePill(data.backpressure_status || "-");
        statusText.textContent = "Crawler çalışıyor...";
      }} catch (err) {{
        statusText.textContent = "Metrikler okunamıyor: " + err;
      }}
    }}

    async function performSearch(query) {{
      if (!query.trim()) {{
        searchInfoEl.textContent = "Lütfen boş olmayan bir sorgu girin.";
        resultsEl.innerHTML = "";
        return;
      }}
      searchInfoEl.textContent = "Aranıyor...";
      resultsEl.innerHTML = "";
      const encoded = encodeURIComponent(query);
      try {{
        const res = await fetch("/api/search?q=" + encoded);
        if (!res.ok) throw new Error("HTTP " + res.status);
        const data = await res.json();
        const results = data.results || [];
        if (results.length === 0) {{
          searchInfoEl.textContent = "Sonuç bulunamadı.";
          resultsEl.innerHTML = "";
          return;
        }}
        searchInfoEl.textContent = "Toplam " + results.length + " sonuç gösteriliyor.";
        const frag = document.createDocumentFragment();
        results.forEach(item => {{
          const li = document.createElement("li");
          li.className = "result-item";
          const a = document.createElement("a");
          a.href = item.url;
          a.textContent = item.url;
          a.className = "result-url";
          a.target = "_blank";
          const score = document.createElement("div");
          score.className = "result-score";
          score.textContent = "Score: " + item.score;
          li.appendChild(a);
          li.appendChild(score);
          frag.appendChild(li);
        }});
        resultsEl.appendChild(frag);
      }} catch (err) {{
        searchInfoEl.textContent = "Arama isteği başarısız: " + err;
      }}
    }}

    searchForm.addEventListener("submit", function (e) {{
      e.preventDefault();
      performSearch(searchInput.value || "");
    }});

    // Poll metrics every second
    fetchMetrics();
    setInterval(fetchMetrics, 1000);
  </script>
</body>
</html>
""".format(queue_max=QUEUE_MAXSIZE)


# ============================================================
# HTTP Handler
# ============================================================

class SearchHTTPRequestHandler(BaseHTTPRequestHandler):
    server_version = "GoogleInADayHTTP/0.1"

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        # Forward logs to the standard logging module instead of stderr
        logging.getLogger("http").info(
            "%s - %s",
            self.address_string(),
            format % args,
        )

    # ------------- Routing -------------

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/":
            self._handle_root()
        elif path == "/api/metrics":
            self._handle_metrics()
        elif path == "/api/search":
            self._handle_search(parsed)
        else:
            self.send_error(404, "Not Found")

    # ------------- Handlers -------------

    def _handle_root(self) -> None:
        body = HTML_PAGE.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_metrics(self) -> None:
        visited_count = visited_set.size()
        queue_size = crawl_queue.qsize()

        if QUEUE_MAXSIZE > 0:
            ratio = queue_size / float(QUEUE_MAXSIZE)
        else:
            ratio = 0.0

        if ratio >= METRICS_BACKPRESSURE_HIGH:
            status = "HIGH"
        elif ratio >= METRICS_BACKPRESSURE_MED:
            status = "MEDIUM"
        else:
            status = "LOW"

        payload = {
            "visited_count": visited_count,
            "queue_size": queue_size,
            "queue_maxsize": QUEUE_MAXSIZE,
            "backpressure_status": status,
            "backpressure_ratio": ratio,
        }

        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _handle_search(self, parsed_url) -> None:
        qs = parse_qs(parsed_url.query)
        query_list = qs.get("q", [])
        if not query_list:
            self._send_json(
                400,
                {"error": "Missing 'q' query parameter.", "results": []},
            )
            return

        query = query_list[0]
        results = search_index(inverted_index, query, SEARCH_TOP_N)

        payload = {
            "query": query,
            "results": [
                {"url": url, "score": score}
                for url, score in results
            ],
        }
        self._send_json(200, payload)

    # ------------- Utility -------------

    def _send_json(self, status_code: int, payload: Dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


# ============================================================
# Server lifecycle
# ============================================================

def start_crawler_workers() -> None:
    # Seed the initial URL
    crawl_queue.put_task(SEED_URL, depth=0)

    for i in range(NUM_WORKERS):
        worker = CrawlerWorker(
            name=f"worker-{i}",
            queue=crawl_queue,
            visited=visited_set,
            index=inverted_index,
            max_depth=MAX_DEPTH,
            stop_event=stop_event,
        )
        worker.start()
        workers.append(worker)


def run_server(host: str = "127.0.0.1", port: int = 8000) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    start_crawler_workers()

    httpd = HTTPServer((host, port), SearchHTTPRequestHandler)
    logging.info("Web UI running on http://%s:%d/", host, port)

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        logging.info("KeyboardInterrupt received, shutting down...")
    finally:
        stop_event.set()
        httpd.shutdown()
        httpd.server_close()

        # Allow workers to finish outstanding tasks (best effort)
        for w in workers:
            try:
                w.join(timeout=5.0)
            except Exception:
                pass

        logging.info("Server stopped.")


if __name__ == "__main__":
    run_server()