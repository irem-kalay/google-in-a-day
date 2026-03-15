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

from data_structures import (
    ThreadSafeVisitedSet,
    ThreadSafeInvertedIndex,
    CrawlQueue,
    ThreadSafeTitleMap,
    ThreadSafeMetadataMap,
)
from parser import CrawlerWorker



# ============================================================
# Configuration
# ============================================================

SEED_URL = "https://tr.wikipedia.org/wiki/Anasayfa" # ITU link: "https://obs.itu.edu.tr/public/DersProgram"
MAX_DEPTH = 2
NUM_WORKERS = 4
QUEUE_MAXSIZE = 1000
SEARCH_TOP_N = 10
METRICS_BACKPRESSURE_HIGH = 0.9
METRICS_BACKPRESSURE_MED = 0.6

QUERY_TOKEN_SPLIT_RE = re.compile(r"[^a-zA-Z0-9çğıöşüÇĞIİÖŞÜ]+")


# ============================================================
# Shared crawler state
# ============================================================

visited_set = ThreadSafeVisitedSet()
inverted_index = ThreadSafeInvertedIndex()
crawl_queue = CrawlQueue(maxsize=QUEUE_MAXSIZE)
title_map = ThreadSafeTitleMap()
metadata_map = ThreadSafeMetadataMap()
stop_event = threading.Event()
workers: List[CrawlerWorker] = []


# ============================================================
# State persistence helpers
# ============================================================

STATE_FILE = "state.json"


def save_state_to_disk() -> None:
    """
    Serialize crawler state to STATE_FILE.
    """
    try:
        visited_snapshot = list(visited_set.snapshot())
        index_snapshot = inverted_index.snapshot()
        title_snapshot = title_map.snapshot()

        metadata_raw = metadata_map.snapshot()
        metadata_snapshot: Dict[str, Dict[str, object]] = {
            url: {"origin_url": origin, "depth": depth}
            for url, (origin, depth) in metadata_raw.items()
        }

        queue_snapshot = crawl_queue.snapshot()

        state = {
            "visited": visited_snapshot,
            "inverted_index": index_snapshot,
            "metadata": metadata_snapshot,
            "crawl_queue": queue_snapshot,
            "title_map": title_snapshot,
        }

        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f)
        logging.info("Web crawler state saved to %s", STATE_FILE)
    except Exception:
        logging.exception("Failed to save web crawler state to disk.")


def load_state_from_disk() -> bool:
    """
    Attempt to load crawler state from STATE_FILE.

    Returns True on success, False otherwise.
    """
    if not os.path.exists(STATE_FILE):
        return False

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
    except Exception:
        logging.exception("Failed to read or parse %s; starting fresh.", STATE_FILE)
        return False

    try:
        visited_set.load_snapshot(state.get("visited", []))
        inverted_index.load_snapshot(state.get("inverted_index", {}))
        metadata_map.load_snapshot(state.get("metadata", {}))
        crawl_queue.load_snapshot(state.get("crawl_queue", []))
        saved_titles = state.get("title_map", {})
        for url, title in saved_titles.items():
            try:
                title_map.set_title(url, title)
            except Exception:
                logging.exception("Failed to restore title for URL: %s", url)
        logging.info("Web crawler state loaded from %s", STATE_FILE)
        return True
    except Exception:
        logging.exception("Failed to apply loaded web crawler state; starting fresh.")
        return False


# ============================================================
# Search helpers
# ============================================================

def normalize_query(query: str) -> List[str]:
    query = query.lower()
    parts = QUERY_TOKEN_SPLIT_RE.split(query)
    return [p for p in parts if p]


def search_index(
    index: ThreadSafeInvertedIndex,
    metadata: ThreadSafeMetadataMap,
    title_map: ThreadSafeTitleMap,
    query: str,
    top_n: int,
) -> List[Tuple[str, str, int]]:
    """
    Advanced search engine:

    1) Strict Intersection (AND logic):
       - A URL is included in results only if it contains ALL query terms.
    2) Co-occurrence bonus:
       - Since we have no positional data, we approximate co-occurrence using
         (url, freq) pairs for each term:
           base_score        = sum of all term frequencies
           bonus             = minimum of all term frequencies
           final_score       = base_score + bonus
       - This way pages containing all terms are selected, and pages where
         terms appear at similar density (frequently co-occurring) are ranked higher.
    """
    tokens = normalize_query(query)
    if not tokens:
        return []

    # Postings per term: term -> {url: freq}
    term_postings: Dict[str, Dict[str, int]] = {}
    for term in tokens:
        postings = index.get_postings(term)
        # If a term appears in no pages, the result set is empty.
        if not postings:
            return []
        term_postings[term] = postings

    # AND logic: intersection of all terms
    # Start with the URL set of the first term, then intersect with the rest.
    iter_tokens = iter(tokens)
    first_term = next(iter_tokens)
    candidate_urls = set(term_postings[first_term].keys())

    for term in iter_tokens:
        candidate_urls &= set(term_postings[term].keys())
        if not candidate_urls:
            return []

    scores: Dict[str, int] = {}

    for url in candidate_urls:
        freqs: List[int] = []
        for term in tokens:
            freq = term_postings[term].get(url, 0)
            freqs.append(freq)

        base_score = sum(freqs)
        cooccurrence_bonus = min(freqs)  # extra weight for pages where all terms appear with similar strength
        total_score = base_score + cooccurrence_bonus

        scores[url] = total_score

    # Title matching bonus: +50 points for each query term found in the page title
    if scores:
        title_tokens_cache: Dict[str, List[str]] = {}
        for url in scores.keys():
            title = title_map.get_title(url)
            if not title:
                continue
            title_tokens_cache[url] = normalize_query(title)

        for url in list(scores.keys()):
            tokens_in_title = title_tokens_cache.get(url)
            if not tokens_in_title:
                continue
            title_terms = set(tokens_in_title)
            bonus_terms = 0
            for term in tokens:
                if term in title_terms:
                    bonus_terms += 1
            if bonus_terms:
                scores[url] += bonus_terms * 50

    ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))

    results: List[Tuple[str, str, int]] = []
    for url, _score in ranked[:top_n]:
        meta = metadata.get_metadata(url)
        if meta is None:
            origin_url, depth = None, 0
        else:
            origin_url, depth = meta
        results.append((url, origin_url or "", depth))

    return results


# ============================================================
# HTML Template
# ============================================================

HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <title>Google-in-a-Day Search</title>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <style>
    * {
      box-sizing: border-box;
      -webkit-font-smoothing: antialiased;
    }
    body {
      margin: 0;
      padding: 0;
      font-family: Arial, Helvetica, sans-serif;
      background: #ffffff;
      color: #202124;
    }
    a {
      text-decoration: none;
      color: inherit;
    }

    /* Header */
    header {
      position: sticky;
      top: 0;
      z-index: 10;
      background: #ffffff;
      border-bottom: 1px solid #dadce0;
      padding: 14px 32px;
      display: flex;
      align-items: center;
      justify-content: space-between;
    }
    .logo {
      font-size: 20px;
      font-weight: 600;
      letter-spacing: 0.02em;
    }
    .logo span:nth-child(1) { color: #4285f4; }
    .logo span:nth-child(2) { color: #ea4335; }
    .logo span:nth-child(3) { color: #fbbc05; }
    .logo span:nth-child(4) { color: #4285f4; }
    .logo span:nth-child(5) { color: #34a853; }
    .logo span:nth-child(6) { color: #ea4335; }
    header #statusText {
      font-size: 12px;
      color: #5f6368;
    }

    /* Layout */
    .page-shell {
      max-width: 1200px;
      margin: 24px auto 40px auto;
      padding: 0 32px;
    }
    @media (max-width: 960px) {
      .page-shell {
        padding: 0 16px;
      }
    }
    .content-grid {
      display: grid;
      grid-template-columns: minmax(0, 3fr) minmax(280px, 2fr);
      column-gap: 32px;
      align-items: flex-start;
    }
    @media (max-width: 960px) {
      .content-grid {
        grid-template-columns: minmax(0, 1fr);
        row-gap: 24px;
      }
    }

    /* Search area */
    .search-panel {
      padding-right: 8px;
    }
    .search-form-shell {
      margin-bottom: 24px;
    }
    .search-form-wrapper {
      display: flex;
      align-items: center;
      justify-content: flex-start;
      max-width: 700px;
    }
    .search-bar {
      display: flex;
      align-items: center;
      flex: 1;
      border-radius: 999px;
      border: 1px solid #dfe1e5;
      background: #ffffff;
      padding: 8px 14px;
      box-shadow: none;
      transition: box-shadow 0.2s ease, border-color 0.2s ease, background-color 0.2s ease;
    }
    .search-bar:hover {
      background: #ffffff;
      box-shadow: 0 1px 6px rgba(32,33,36,0.28);
      border-color: rgba(223,225,229,0);
    }
    .search-bar:focus-within {
      box-shadow: 0 1px 6px rgba(32,33,36,0.28);
      border-color: rgba(223,225,229,0);
    }
    .search-bar input[type="text"] {
      flex: 1;
      border: none;
      outline: none;
      font-size: 16px;
      padding: 4px 6px;
      color: #202124;
      background: transparent;
    }
    .search-bar input[type="text"]::placeholder {
      color: #9aa0a6;
    }
    .search-bar button {
      border: none;
      background: #f8f9fa;
      color: #3c4043;
      font-size: 14px;
      padding: 6px 14px;
      margin-left: 8px;
      border-radius: 4px;
      cursor: pointer;
      border: 1px solid #dadce0;
      transition: background-color 0.2s ease, box-shadow 0.2s ease, border-color 0.2s ease, transform 0.1s ease;
    }
    .search-bar button:hover {
      background: #f8f9fa;
      border-color: #c6c6c6;
      box-shadow: 0 1px 1px rgba(0,0,0,0.1);
    }
    .search-bar button:active {
      transform: translateY(1px);
      box-shadow: none;
    }

    .search-hint {
      margin-top: 12px;
      font-size: 13px;
      color: #5f6368;
    }

    /* Results */
    .results-header {
      font-size: 13px;
      color: #5f6368;
      margin-bottom: 6px;
    }
    .results-list {
      list-style: none;
      padding: 0;
      margin: 0;
    }
    .result-item {
      margin-bottom: 24px;
      max-width: 680px;
    }
    .result-item:last-child {
      margin-bottom: 0;
    }
    .result-link {
      font-size: 18px;
      line-height: 1.3;
      color: #1a0dab;
      text-decoration: none;
    }
    .result-link:hover {
      text-decoration: underline;
    }
    .result-display-url {
      font-size: 14px;
      color: #4d5156;
      margin-top: 2px;
      word-break: break-all;
    }
    .result-score {
      font-size: 13px;
      color: #5f6368;
      margin-top: 4px;
    }
    .muted {
      font-size: 13px;
      color: #5f6368;
    }

    /* Dashboard sidebar */
    .dashboard-panel {
      border: 1px solid #dadce0;
      border-radius: 8px;
      padding: 16px 18px 18px 18px;
      background: #f8f9fa;
    }
    .dashboard-title {
      margin: 0 0 10px 0;
      font-size: 16px;
      font-weight: 500;
      color: #202124;
    }
    .dashboard-subtitle {
      margin: 0 0 14px 0;
      font-size: 13px;
      color: #5f6368;
    }
    .metrics-grid {
      display: grid;
      grid-template-columns: 1fr;
      row-gap: 12px;
    }
    .metric-row {
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 8px 10px;
      border-radius: 6px;
      background: #ffffff;
      border: 1px solid #e0e0e0;
    }
    .metric-label {
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: #5f6368;
    }
    .metric-value {
      font-size: 14px;
      font-weight: 500;
      color: #202124;
      display: flex;
      align-items: center;
      gap: 4px;
    }

    .indexing-section {
      margin-top: 18px;
      padding-top: 12px;
      border-top: 1px solid #e0e0e0;
    }
    .indexing-title {
      margin: 0 0 6px 0;
      font-size: 13px;
      font-weight: 500;
      color: #202124;
    }
    .indexing-description {
      margin: 0 0 10px 0;
      font-size: 12px;
      color: #5f6368;
    }
    .indexing-form {
      display: flex;
      gap: 6px;
    }
    .indexing-form input[type="url"] {
      flex: 1;
      border-radius: 999px;
      border: 1px solid #dfe1e5;
      padding: 6px 10px;
      font-size: 13px;
      outline: none;
    }
    .indexing-form button {
      border: none;
      background: #1a73e8;
      color: #ffffff;
      font-size: 13px;
      padding: 6px 12px;
      border-radius: 4px;
      cursor: pointer;
      transition: background-color 0.2s ease, box-shadow 0.2s ease, transform 0.1s ease;
    }
    .indexing-form button:hover {
      background: #185abc;
      box-shadow: 0 1px 1px rgba(0,0,0,0.1);
    }
    .indexing-form button:active {
      transform: translateY(1px);
      box-shadow: none;
    }
    .indexing-status {
      margin-top: 6px;
      font-size: 12px;
      color: #5f6368;
    }

    .metric-pill {
      display: inline-flex;
      align-items: center;
      padding: 2px 8px 2px 6px;
      border-radius: 999px;
      font-size: 12px;
      font-weight: 500;
      border: 1px solid #dadce0;
      background: #ffffff;
      color: #5f6368;
    }
    .metric-pill::before {
      content: "";
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background: #9aa0a6;
      margin-right: 5px;
      box-shadow: 0 0 0 1px rgba(154,160,166,0.4);
    }
    .metric-pill.ok {
      color: #137333;
      border-color: #cce8d8;
      background: #e6f4ea;
    }
    .metric-pill.ok::before {
      background: #34a853;
      box-shadow: 0 0 0 1px rgba(52,168,83,0.4);
    }
    .metric-pill.med {
      color: #b06000;
      border-color: #fce8b2;
      background: #fef7e0;
    }
    .metric-pill.med::before {
      background: #fbbc04;
      box-shadow: 0 0 0 1px rgba(251,188,4,0.4);
    }
    .metric-pill.high {
      color: #c5221f;
      border-color: #fad2cf;
      background: #fce8e6;
    }
    .metric-pill.high::before {
      background: #ea4335;
      box-shadow: 0 0 0 1px rgba(234,67,53,0.4);
    }
    .metric-pill.muted {
      color: #5f6368;
    }

    .metrics-footnote {
      margin-top: 14px;
      font-size: 12px;
      color: #5f6368;
      line-height: 1.5;
    }

    footer {
      padding: 12px 32px 18px 32px;
      border-top: 1px solid #dadce0;
      font-size: 12px;
      color: #5f6368;
      background: #f8f9fa;
    }
    @media (max-width: 960px) {
      footer {
        padding-inline: 16px;
      }
    }
    code {
      font-family: "Roboto Mono", Menlo, Monaco, Consolas, monospace;
      font-size: 12px;
      color: #202124;
    }
  </style>
</head>
<body>
  <header>
    <div class="logo">
      <span>G</span><span>o</span><span>o</span><span>g</span><span>l</span><span>e</span>-in-a-Day
    </div>
    <span id="statusText">Crawler running...</span>
  </header>

  <main class="page-shell">
    <div class="content-grid">
      <!-- Left: Search -->
      <section class="search-panel">
        <div class="search-form-shell">
          <form id="searchForm" class="search-form-wrapper">
            <div class="search-bar">
              <input
                id="searchInput"
                type="text"
                placeholder="Type a search term and press Enter..."
                autocomplete="off"
              />
              <button type="submit">Search</button>
            </div>
          </form>
          <p id="searchInfo" class="search-hint">Submit a query to see results. (Enter a starting URL first.)</p>
        </div>

        <div>
          <p class="results-header">Search results</p>
          <ul id="results" class="results-list"></ul>
        </div>
      </section>

      <!-- Right: Dashboard -->
      <aside class="dashboard-panel">
        <h2 class="dashboard-title">Crawler Dashboard</h2>
        <p class="dashboard-subtitle">
          Monitor the crawler state in real time.
        </p>
        <div class="metrics-grid">
          <div class="metric-row">
            <div class="metric-label">Visited URLs</div>
            <div class="metric-value" id="visitedCount">-</div>
          </div>
          <div class="metric-row">
            <div class="metric-label">Queue Size</div>
            <div class="metric-value">
              <span id="queueSize">-</span>
              <span class="muted">/ {queue_max}</span>
            </div>
          </div>
          <div class="metric-row">
            <div class="metric-label">Back-Pressure</div>
            <div class="metric-value">
              <span id="backpressurePill" class="metric-pill muted">-</span>
            </div>
          </div>
        </div>
        <p class="metrics-footnote">
          Metrics are refreshed every second from the <code>/api/metrics</code> endpoint.
          Search results are fetched from <code>/api/search</code>.
        </p>
        <div class="indexing-section">
          <h3 class="indexing-title">Start Manual Indexing</h3>
          <p class="indexing-description">
            Enter a seed URL to manually add a page to the crawler queue.
          </p>
          <form id="indexForm" class="indexing-form">
            <input
              id="indexUrlInput"
              type="url"
              placeholder="e.g. https://www.example.com/"
              autocomplete="off"
            />
            <button type="submit">Start Indexing</button>
          </form>
          <p id="indexStatus" class="indexing-status"></p>
        </div>
      </aside>
    </div>
  </main>

  <footer>
    This demo runs entirely on Python's standard library
    (<code>http.server</code>, <code>threading</code>, <code>urllib</code>, etc.).
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
    const indexForm = document.getElementById("indexForm");
    const indexUrlInput = document.getElementById("indexUrlInput");
    const indexStatusEl = document.getElementById("indexStatus");

    function updatePill(status) {
      pillEl.textContent = status;
      pillEl.classList.remove("ok", "med", "high", "muted");
      if (status === "LOW") {
        pillEl.classList.add("ok");
      } else if (status === "MEDIUM") {
        pillEl.classList.add("med");
      } else if (status === "HIGH") {
        pillEl.classList.add("high");
      } else {
        pillEl.classList.add("muted");
      }
    }

    async function fetchMetrics() {
      try {
        const res = await fetch("/api/metrics");
        if (!res.ok) throw new Error("HTTP " + res.status);
        const data = await res.json();
        visitedEl.textContent = data.visited_count;
        queueEl.textContent = data.queue_size;
        updatePill(data.backpressure_status || "-");
        statusText.textContent = "Crawler running...";
      } catch (err) {
        statusText.textContent = "Could not read metrics: " + err;
      }
    }

    async function performSearch(query) {
      if (!query.trim()) {
        searchInfoEl.textContent = "Please enter a non-empty query.";
        resultsEl.innerHTML = "";
        return;
      }
      searchInfoEl.textContent = "Searching...";
      resultsEl.innerHTML = "";
      const encoded = encodeURIComponent(query);
      try {
        const res = await fetch("/api/search?q=" + encoded);
        if (!res.ok) throw new Error("HTTP " + res.status);
        const data = await res.json();
        const results = (data.results || []);
        if (results.length === 0) {
          searchInfoEl.textContent = "No results found.";
          resultsEl.innerHTML = "";
          return;
        }
        searchInfoEl.textContent = "Showing " + results.length + " results.";
        const frag = document.createDocumentFragment();
        results.forEach(item => {
          const li = document.createElement("li");
          li.className = "result-item";

          const link = document.createElement("a");
          link.href = item.url;
          link.target = "_blank";
          link.className = "result-link";
          // Use the page title as link text if available, otherwise show the URL
          link.textContent = item.title || item.url;

          const displayUrl = document.createElement("div");
          displayUrl.className = "result-display-url";
          displayUrl.textContent = item.url;

          const meta = document.createElement("div");
          meta.className = "result-score";
          const origin = item.origin_url || "seed";
          meta.textContent = "Origin: " + origin + " · Depth: " + item.depth;

          li.appendChild(link);
          li.appendChild(displayUrl);
          li.appendChild(meta);
          frag.appendChild(li);
        });
        resultsEl.appendChild(frag);
      } catch (err) {
        searchInfoEl.textContent = "Search request failed: " + err;
      }
    }

    searchForm.addEventListener("submit", function (e) {
      e.preventDefault();
      performSearch(searchInput.value || "");
    });

    async function startIndexing(url) {
      const trimmed = (url || "").trim();
      if (!trimmed) {
        indexStatusEl.textContent = "Please enter a valid URL.";
        indexStatusEl.style.color = "#c5221f";
        return;
      }
      indexStatusEl.textContent = "Adding URL to queue...";
      indexStatusEl.style.color = "#5f6368";
      const encoded = encodeURIComponent(trimmed);
      try {
        const res = await fetch("/api/index?url=" + encoded, { method: "GET" });
        let data = {};
        try {
          data = await res.json();
        } catch (e) {
          // No JSON response; fall through to generic error handling
        }
        if (!res.ok || !data.ok) {
          const message = (data && data.error) ? data.error : ("Request failed: HTTP " + res.status);
          indexStatusEl.textContent = message;
          indexStatusEl.style.color = "#c5221f";
          return;
        }
        indexStatusEl.textContent = "URL added to queue: " + (data.url || trimmed);
        indexStatusEl.style.color = "#137333";
        indexUrlInput.value = "";
        setTimeout(() => {
          indexStatusEl.textContent = "";
          indexStatusEl.style.color = "#5f6368";
        }, 4000);
      } catch (err) {
        indexStatusEl.textContent = "Indexing request failed: " + err;
        indexStatusEl.style.color = "#c5221f";
      }
    }

    if (indexForm) {
      indexForm.addEventListener("submit", function (e) {
        e.preventDefault();
        startIndexing(indexUrlInput.value || "");
      });
    }

    // Poll metrics every second
    fetchMetrics();
    setInterval(fetchMetrics, 1000);
  </script>
</body>
</html>
""".replace("{queue_max}", str(QUEUE_MAXSIZE))

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
        elif path == "/api/index":
            self._handle_index(parsed)
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
        results = search_index(inverted_index, metadata_map, title_map, query, SEARCH_TOP_N)

        payload = {
            "query": query,
            "results": [
                {
                    "url": url,
                    "origin_url": origin_url,
                    "depth": depth,
                    "title": title_map.get_title(url, url),
                }
                for url, origin_url, depth in results
            ],
        }
        self._send_json(200, payload)

    def _handle_index(self, parsed_url) -> None:
        qs = parse_qs(parsed_url.query)
        url_list = qs.get("url", [])
        if not url_list:
            self._send_json(
                400,
                {"ok": False, "error": "Missing 'url' query parameter."},
            )
            return

        raw_url = (url_list[0] or "").strip()
        if not raw_url:
            self._send_json(
                400,
                {"ok": False, "error": "URL cannot be empty."},
            )
            return

        parsed_target = urlparse(raw_url)
        if parsed_target.scheme not in ("http", "https") or not parsed_target.netloc:
            self._send_json(
                400,
                {
                    "ok": False,
                    "error": "Invalid URL. Only http/https URLs with a hostname are allowed.",
                },
            )
            return

        normalized_url = raw_url

        try:
            crawl_queue.put_task(normalized_url, depth=0)
            metadata_map.record_discovery(normalized_url, origin_url=None, depth=0)
        except Exception:
            logging.exception("Failed to enqueue manual index URL: %s", normalized_url)
            self._send_json(
                500,
                {
                    "ok": False,
                    "error": "Failed to enqueue URL for indexing.",
                },
            )
            return

        self._send_json(
            200,
            {
                "ok": True,
                "url": normalized_url,
            },
        )

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
    # Best ITU starting points
    itu_seeds = [
        "https://obs.itu.edu.tr/public/DersProgram",        # Course schedules and codes
        "https://www.itu.edu.tr/",                          # Main page (general news)
        "https://www.sis.itu.edu.tr/",                      # Student Affairs (regulations, announcements)
        "https://ninova.itu.edu.tr/tr/dersler/",            # Ninova (course content and faculties)
        "https://kutuphane.itu.edu.tr/"                     # Library (academic resources)
    ]

    # Try to load existing state from disk first; if successful, skip re-adding seeds.
    resumed = load_state_from_disk()
    if not resumed:
        pass  # Automatic seed loading is disabled.
        # Use the UI to manually add URLs, or load from state.json.

    # Start worker threads
    for i in range(NUM_WORKERS):
        worker = CrawlerWorker(
            name=f"worker-{i}",
            queue=crawl_queue,
            visited=visited_set,
            index=inverted_index,
            max_depth=MAX_DEPTH,
            stop_event=stop_event,
            title_map=title_map,
            metadata_map=metadata_map,
        )
        worker.start()
        workers.append(worker)
        # Automatic seed loading is disabled.
        # Use the UI to manually add URLs, or load from state.json.


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
        # Save current state to disk before exiting
        save_state_to_disk()

        logging.info("Server stopped.")

        os._exit(0)

if __name__ == "__main__":
    run_server()