from __future__ import annotations

import curses
import json
import logging
import os
import re
import sys
import threading
import time
from typing import Dict, List, Tuple


from data_structures import (
    ThreadSafeVisitedSet,
    ThreadSafeInvertedIndex,
    CrawlQueue,
    ThreadSafeMetadataMap,
    ThreadSafeTitleMap,
)
from parser import CrawlerWorker

# ------------------------------------------------------------
# Configuration
# ------------------------------------------------------------

SEED_URL = "https://obs.itu.edu.tr/public/DersProgram"  # Starting URL for the crawl
MAX_DEPTH = 2                     # Maximum crawl depth (k)
NUM_WORKERS = 4                   # Number of crawler threads
QUEUE_MAXSIZE = 1000              # Back-pressure size for CrawlQueue
DASHBOARD_REFRESH_SECONDS = 1.0   # Dashboard update interval
SEARCH_TOP_N = 10                 # Number of results to show per query

#QUERY_TOKEN_SPLIT_RE = re.compile(r"[^a-zA-Z0-9]+")
QUERY_TOKEN_SPLIT_RE = re.compile(r"[^a-zA-Z0-9çğıöşüÇĞIİÖŞÜ]+") #türkçe karakterleri ekledim düzgün parse için

# ------------------------------------------------------------
# Search functionality
# ------------------------------------------------------------

def normalize_query(query: str) -> List[str]:
    """
    Normalize a free-form query string to tokens.

    Steps:
      - lower-case
      - split on non-alphanumeric characters
      - drop empty tokens
    """
    query = query.lower()
    parts = QUERY_TOKEN_SPLIT_RE.split(query)
    return [p for p in parts if p]


# ------------------------------------------------------------
# State persistence helpers
# ------------------------------------------------------------

STATE_FILE = "state.json"


def save_state_to_disk(
    visited: ThreadSafeVisitedSet,
    index: ThreadSafeInvertedIndex,
    metadata_map: ThreadSafeMetadataMap,
    crawl_queue: CrawlQueue,
) -> None:
    """
    Serialize crawler state to STATE_FILE.
    """
    try:
        visited_snapshot = list(visited.snapshot())
        index_snapshot = index.snapshot()

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
        }

        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f)
        logging.info("Crawler state saved to %s", STATE_FILE)
    except Exception:
        logging.exception("Failed to save crawler state to disk.")


def load_state_from_disk(
    visited: ThreadSafeVisitedSet,
    index: ThreadSafeInvertedIndex,
    metadata_map: ThreadSafeMetadataMap,
    crawl_queue: CrawlQueue,
) -> bool:
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
        visited.load_snapshot(state.get("visited", []))
        index.load_snapshot(state.get("inverted_index", {}))
        metadata_map.load_snapshot(state.get("metadata", {}))
        crawl_queue.load_snapshot(state.get("crawl_queue", []))
        logging.info("Crawler state loaded from %s", STATE_FILE)
        return True
    except Exception:
        logging.exception("Failed to apply loaded state; starting fresh.")
        return False


def search_index(
    index: ThreadSafeInvertedIndex,
    metadata_map: ThreadSafeMetadataMap,
    title_map: ThreadSafeTitleMap,
    query: str,
    top_n: int,
) -> List[Tuple[str, str, int]]:
    """
    Simple search engine on top of ThreadSafeInvertedIndex.

    Strategy:
      - Normalize query into tokens.
      - For each term, fetch postings: {url: frequency_in_doc}.
      - Score each URL by summing term frequencies over all query terms.
      - Return top-N (url, score) pairs sorted by score desc, then url.
    """
    tokens = normalize_query(query)
    if not tokens:
        return []

    scores: Dict[str, int] = {}
    for term in tokens:
        postings = index.get_postings(term)
        for url, freq in postings.items():
            scores[url] = scores.get(url, 0) + freq

    # Title matching bonus: +50 puan / başlıktaki her eşleşen terim
    if scores:
        # URL bazında başlık token'larını önceden hazırla
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
        meta = metadata_map.get_metadata(url)
        if meta is None:
            origin_url, depth = None, 0
        else:
            origin_url, depth = meta
        # origin_url None ise kullanıcı arayüzünde okunaklı olması için boş string dönelim
        results.append((url, origin_url or "", depth))

    return results


# ------------------------------------------------------------
# Dashboard (curses-based UI)
# ------------------------------------------------------------

def dashboard_loop(
    stdscr,
    visited: ThreadSafeVisitedSet,
    crawl_queue: CrawlQueue,
    index: ThreadSafeInvertedIndex,
    metadata_map: ThreadSafeMetadataMap,
    title_map: ThreadSafeTitleMap,
    stop_event: threading.Event,
) -> None:
    """
    Main curses UI loop.

    Responsibilities:
      - Periodically display crawl metrics (visited size, queue size, back-pressure).
      - Let user type search queries.
      - Execute searches and display latest results.
      - Exit cleanly when user types "quit".
    """
    curses.curs_set(1)  # Show cursor
    stdscr.nodelay(True)  # Non-blocking input
    stdscr.timeout(100)   # getch() timeout in ms

    input_buffer = ""
    last_results: List[Tuple[str, str, int]] = []
    last_query = ""
    last_info_message = ""

    queue_maxsize = QUEUE_MAXSIZE
    last_dashboard_update = 0.0

    while not stop_event.is_set():
        now = time.time()

        # ----------------------------------------------------
        # Handle keyboard input (non-blocking)
        # ----------------------------------------------------
        try:
            ch = stdscr.getch()
        except KeyboardInterrupt:
            stop_event.set()
            break

        if ch != -1:
            if ch in (curses.KEY_ENTER, 10, 13):
                # Enter pressed: treat buffer as a query
                query = input_buffer.strip()
                input_buffer = ""

                if query:
                    if query.lower() == "quit":
                        stop_event.set()
                        break
                    last_query = query
                    last_results = search_index(index, metadata_map, title_map, query, SEARCH_TOP_N)
                    last_info_message = f"Sorgu: {query!r} -> {len(last_results)} sonuç"
                else:
                    last_info_message = "Boş sorgu görmezden gelindi."
            elif ch in (curses.KEY_BACKSPACE, 127, 8):
                if input_buffer:
                    input_buffer = input_buffer[:-1]
            elif 32 <= ch <= 126:
                # Printable ASCII
                input_buffer += chr(ch)

        # ----------------------------------------------------
        # Periodic dashboard refresh
        # ----------------------------------------------------
        if now - last_dashboard_update >= DASHBOARD_REFRESH_SECONDS:
            stdscr.erase()

            rows, cols = stdscr.getmaxyx()
            safe_cols = max(cols - 1, 1)

            # Metrics
            visited_count = visited.size()
            queue_size = crawl_queue.qsize()
            ratio = (queue_size / float(queue_maxsize)) if queue_maxsize > 0 else 0.0

            if ratio >= 0.9:
                backpressure = "YÜKSEK"
            elif ratio >= 0.6:
                backpressure = "ORTA"
            else:
                backpressure = "DÜŞÜK"

            # Header
            stdscr.addstr(0, 0, "Google-in-a-Day: Canlı Crawler Dashboard".ljust(safe_cols))
            stdscr.addstr(1, 0, "-" * min(safe_cols, 60))

            # Metrics section
            stdscr.addstr(3, 0, f"Ziyaret edilen URL sayısı : {visited_count}".ljust(safe_cols))
            stdscr.addstr(4, 0, f"Kuyruk boyutu           : {queue_size}/{queue_maxsize}".ljust(safe_cols))
            stdscr.addstr(5, 0, f"Back-pressure durumu    : {backpressure} ({ratio:.0%})".ljust(safe_cols))

            # Query input section
            stdscr.addstr(7, 0, "Sorgu girin ve Enter'a basın (çıkmak için 'quit'):".ljust(safe_cols))
            # Show current input buffer
            visible_input = ("> " + input_buffer)[:safe_cols]
            stdscr.addstr(8, 0, visible_input.ljust(safe_cols))

            # Info / last query
            if last_info_message:
                stdscr.addstr(10, 0, last_info_message[:safe_cols].ljust(safe_cols))

            # Search results section
            line = 12
            if last_query:
                header = f"Son sorgu sonuçları (en fazla {SEARCH_TOP_N}) - {last_query!r}:"
                stdscr.addstr(line, 0, header[:safe_cols].ljust(safe_cols))
                line += 1
                stdscr.addstr(line, 0, "-" * min(safe_cols, 60))
                line += 1

                # Each row: "d=depth origin_url -> url"
                for url, origin_url, depth in last_results:
                    if line >= rows - 1:
                        break
                    origin_display = origin_url or "-"
                    display = f"d={depth:2d} origin={origin_display}  {url}"
                    stdscr.addstr(line, 0, display[:safe_cols].ljust(safe_cols))
                    line += 1

            stdscr.refresh()
            last_dashboard_update = now

        # Avoid busy-waiting
        time.sleep(0.05)


# ------------------------------------------------------------
# Orchestration
# ------------------------------------------------------------

def run() -> None:
    """
    Entry point for the crawler + dashboard.

    - Initializes shared thread-safe structures.
    - Starts crawler workers in the background.
    - Launches curses UI for live monitoring + search.
    - Shuts down all worker threads gracefully on exit.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        filename="crawler.log",  # <-- BUNU EKLE: Loglar artık bu dosyaya yazılacak
        filemode="w"             # <-- BUNU EKLE: Her çalışmada dosyayı sıfırlar
    )

    visited = ThreadSafeVisitedSet()
    index = ThreadSafeInvertedIndex()
    crawl_queue = CrawlQueue(maxsize=QUEUE_MAXSIZE)
    metadata_map = ThreadSafeMetadataMap()
    title_map = ThreadSafeTitleMap()
    stop_event = threading.Event()

    # Mevcut state'i diskten yüklemeyi dene; olmazsa seed ile sıfırdan başla.
    resumed = load_state_from_disk(visited, index, metadata_map, crawl_queue)
    if not resumed:
        crawl_queue.put_task(SEED_URL, depth=0)
        metadata_map.record_discovery(SEED_URL, origin_url=None, depth=0)

    # Start worker threads
    workers: List[CrawlerWorker] = []
    for i in range(NUM_WORKERS):
        worker = CrawlerWorker(
            name=f"worker-{i}",
            queue=crawl_queue,
            visited=visited,
            index=index,
            max_depth=MAX_DEPTH,
            stop_event=stop_event,
            title_map=title_map,
            metadata_map=metadata_map,
        )
        worker.start()
        workers.append(worker)

    # Run curses dashboard
    try:
        curses.wrapper(
            dashboard_loop,
            visited,
            crawl_queue,
            index,
            metadata_map,
            title_map,
            stop_event,
        )
    finally:
        # On any exit path, signal workers to stop and wait for them
        stop_event.set()

        # Best-effort wait for queue to drain (non-strict)
        # We avoid blocking forever; workers themselves will eventually exit.
        try:
            # Short grace period for outstanding tasks
            for _ in range(3):
                if crawl_queue.qsize() == 0:
                    break
                time.sleep(1.0)
        except Exception:
            pass

        for w in workers:
            try:
                w.join(timeout=5.0)
            except Exception:
                pass

        # Çıkmadan önce mevcut durumu diske kaydet
        save_state_to_disk(visited, index, metadata_map, crawl_queue)


if __name__ == "__main__":
    run()