from __future__ import annotations

import threading
import queue
from dataclasses import dataclass
from typing import Dict, Iterable, List, Tuple, Optional, Set


# ============================================================
# 1) ThreadSafeVisitedSet
# ============================================================

class ThreadSafeVisitedSet:
    """
    Thread-safe set for tracking visited URLs.

    Thread-safety strategy:
    - Internal representation: a plain Python set (not thread-safe by itself).
    - We guard *all* mutations (and any read that must be consistent) with a
      single `threading.Lock`.
    - The critical sections are intentionally small: check+add operations happen
      under the same lock to avoid race conditions like:
          T1: if url not in visited:   # context switch
          T2: if url not in visited: visited.add(url)
          T1: visited.add(url)        # URL added twice
    """

    def __init__(self) -> None:
        self._visited: Set[str] = set()
        self._lock = threading.Lock()

    def add(self, url: str) -> bool:
        """
        Atomically mark `url` as visited.

        Returns:
            True  if the URL was not visited before and is added now.
            False if the URL was already present.
        """
        with self._lock:
            if url in self._visited:
                return False
            self._visited.add(url)
            return True

    def __contains__(self, url: str) -> bool:
        """
        Membership check.

        We still take the lock to guarantee that we never read from the set
        while another thread is mutating it. The critical section is tiny, so
        this is cheap in practice.
        """
        with self._lock:
            return url in self._visited

    def size(self) -> int:
        """Return the number of visited URLs (thread-safe)."""
        with self._lock:
            return len(self._visited)

    def snapshot(self) -> Set[str]:
        """
        Return a shallow copy of the visited set.

        Useful for diagnostics / metrics without exposing the internal set
        to concurrent modification.
        """
        with self._lock:
            return set(self._visited)

    def load_snapshot(self, data: Iterable[str]) -> None:
        """
        Replace internal visited set with data from an iterable of URLs.

        Intended to be used when restoring crawler state from disk.
        """
        with self._lock:
            self._visited = set(data)


# ============================================================
# 2) ThreadSafeInvertedIndex
# ============================================================

class ThreadSafeInvertedIndex:
    """
    Thread-safe inverted index.

    Internal data structure:
        index[keyword][url] = term_frequency_in_that_url

    Thread-safety strategy:
    - Use `threading.RLock` (re-entrant lock) instead of a simple Lock.
      This allows higher-level methods to safely call lower-level methods
      that also acquire the same lock, without deadlocking.
    - All reads and writes are guarded by the same lock. Python's standard
      library does not provide a read/write lock, so we choose correctness
      and simplicity over maximum parallelism.
    """

    def __init__(self) -> None:
        # keyword -> (url -> frequency)
        self._index: Dict[str, Dict[str, int]] = {}
        self._lock = threading.RLock()

    # ---------- Write operations ----------

    def add_terms(self, url: str, terms: Iterable[str]) -> None:
        """
        Add all terms from a document (URL) into the index.

        This method expects *normalized* tokens (e.g., lowercased, filtered)
        to be passed in by the caller.

        The whole update is done under a single lock to keep the index in a
        consistent state. This keeps things simple and is acceptable because
        updating one document is typically a relatively small piece of work.
        """
        with self._lock:
            for term in terms:
                # Get or create the postings list for this term
                postings = self._index.setdefault(term, {})
                # Increment frequency for this (term, url) pair
                postings[url] = postings.get(url, 0) + 1

    def remove_url(self, url: str) -> None:
        """
        Optional maintenance operation: remove all references to `url`
        from the index (e.g., if a document is deleted).

        Not strictly necessary for a prototype, but demonstrates how to do
        more complex write operations safely.
        """
        with self._lock:
            to_delete: List[str] = []
            for term, postings in self._index.items():
                if url in postings:
                    del postings[url]
                    if not postings:
                        # If no URLs left for this term, mark the term for deletion
                        to_delete.append(term)

            for term in to_delete:
                del self._index[term]

    # ---------- Read operations ----------

    def get_postings(self, term: str) -> Dict[str, int]:
        """
        Return a copy of the postings dict for a given term.

        We return a *copy* so that callers cannot mutate internal state
        without holding the lock.
        """
        with self._lock:
            postings = self._index.get(term, {})
            return dict(postings)

    def get_vocabulary(self) -> List[str]:
        """
        Return a snapshot list of all terms in the index.
        """
        with self._lock:
            return list(self._index.keys())

    def get_document_frequency(self, term: str) -> int:
        """
        Number of distinct documents that contain `term`.
        """
        with self._lock:
            postings = self._index.get(term)
            return 0 if not postings else len(postings)

    def get_top_n(self, term: str, n: int) -> List[Tuple[str, int]]:
        """
        Return top-N URLs for a term, ranked by term frequency.

        This can be used as a very simple relevance heuristic.
        """
        with self._lock:
            postings = self._index.get(term, {})
            # Sort by frequency descending, then by URL for determinism
            sorted_items = sorted(
                postings.items(),
                key=lambda kv: (-kv[1], kv[0])
            )
            return sorted_items[:n]

    def snapshot(self) -> Dict[str, Dict[str, int]]:
        """
        Deep-ish snapshot of the whole index.

        Returns:
            A dict where both the outer and inner dicts are copies, so
            the caller cannot affect internal state.

        This is mainly for metrics / debugging (careful: can be large).
        """
        with self._lock:
            return {term: dict(postings) for term, postings in self._index.items()}

    def load_snapshot(self, data: Dict[str, Dict[str, int]]) -> None:
        """
        Replace the entire inverted index with the given mapping.

        The input should have the same structure as `snapshot()` produces.
        """
        with self._lock:
            # Ensure we take shallow copies so callers cannot mutate internals.
            self._index = {
                term: dict(postings) for term, postings in (data or {}).items()
            }


# ============================================================
# 3) ThreadSafeTitleMap
# ============================================================

class ThreadSafeTitleMap:
    """
    Thread-safe map for storing page titles by URL.

    This is intentionally simple: a dictionary protected by a lock.
    We only ever need to read or overwrite titles, so a basic Lock
    is sufficient.
    """

    def __init__(self) -> None:
        self._titles: Dict[str, str] = {}
        self._lock = threading.Lock()

    def set_title(self, url: str, title: str) -> None:
        """Set or update the title for a given URL."""
        clean_title = title.strip()
        if not clean_title:
            return
        with self._lock:
            self._titles[url] = clean_title

    def get_title(self, url: str, default: Optional[str] = None) -> Optional[str]:
        """Get the stored title for a URL, or `default` if missing."""
        with self._lock:
            return self._titles.get(url, default)

    def snapshot(self) -> Dict[str, str]:
        """Return a shallow copy of the internal map."""
        with self._lock:
            return dict(self._titles)


# ============================================================
# 4) ThreadSafeMetadataMap
# ============================================================

class ThreadSafeMetadataMap:
    """
    Thread-safe map for storing per-URL crawl metadata.

    Each URL is associated with:
        - origin_url: The page on which the link was first discovered
        - depth:      The depth at which the URL will be crawled

    This structure is queried by the Searcher so that search results can be
    returned as triples: (relevant_url, origin_url, depth).
    """

    def __init__(self) -> None:
        # url -> (origin_url, depth)
        self._data: Dict[str, Tuple[Optional[str], int]] = {}
        self._lock = threading.Lock()

    def record_discovery(
        self,
        url: str,
        origin_url: Optional[str],
        depth: int,
    ) -> None:
        """
        Record the first time a URL is discovered.

        If the URL was already seen before, we keep the existing metadata so
        that we always report the earliest-known origin/depth.
        """
        with self._lock:
            if url not in self._data:
                self._data[url] = (origin_url, depth)

    def get_metadata(self, url: str) -> Optional[Tuple[Optional[str], int]]:
        """Return (origin_url, depth) for a URL, or None if unknown."""
        with self._lock:
            return self._data.get(url)

    def snapshot(self) -> Dict[str, Tuple[Optional[str], int]]:
        """Return a shallow copy of the internal metadata map."""
        with self._lock:
            return dict(self._data)

    def load_snapshot(
        self,
        data: Dict[str, Tuple[Optional[str], int]],
    ) -> None:
        """
        Replace internal metadata with given mapping.

        The input is expected to be compatible with JSON-loaded data:
        values may be 2-element lists or tuples of (origin_url, depth).
        """
        with self._lock:
            new_data: Dict[str, Tuple[Optional[str], int]] = {}
            for url, value in (data or {}).items():
                origin_url: Optional[str]
                depth: int
                if isinstance(value, dict):
                    origin_url = value.get("origin_url")
                    depth = int(value.get("depth", 0))
                else:
                    try:
                        origin_url = value[0]  # type: ignore[index]
                        depth = int(value[1])  # type: ignore[index]
                    except Exception:
                        # Skip malformed entries
                        continue
                new_data[url] = (origin_url, depth)
            self._data = new_data


# ============================================================
# 5) CrawlQueue with back-pressure
# ============================================================

@dataclass(frozen=True)
class CrawlTask:
    """
    A simple task unit for the crawler.

    Fields:
        url:           URL to crawl.
        depth:         Depth in the crawl tree (0 = origin).
        max_depth:     Maximum crawl depth (k) for this crawl run.
        hit_rate_secs: Optional per-task delay between HTTP requests; when set,
                       workers can sleep before processing to throttle crawl rate.
    """
    url: str
    depth: int
    max_depth: int
    hit_rate_secs: Optional[float] = None


class CrawlQueue:
    """
    Thin wrapper around `queue.Queue` with back-pressure.

    Design choices:
    - Internally uses `queue.Queue`, which is already thread-safe.
    - We expose a focused, intention-revealing API tailored to crawling.
    - `maxsize` is used to implement back-pressure: when the queue is full,
      producers (indexer/crawler threads) will block on `put()` until space
      becomes available, preventing unbounded memory growth.
    """

    def __init__(self, maxsize: int = 1000) -> None:
        # `queue.Queue` is a multi-producer, multi-consumer FIFO queue.
        # It uses its own internal locks/condition variables, so we don't
        # need extra locking around it.
        self._queue: "queue.Queue[CrawlTask]" = queue.Queue(maxsize=maxsize)

    # ---------- Producer-side API ----------

    def put_task(
        self,
        url: str,
        depth: int,
        max_depth: int,
        hit_rate_secs: Optional[float] = None,
        block: bool = True,
        timeout: Optional[float] = None,
    ) -> None:
        """
        Enqueue a new crawl task.

        Parameters:
            max_depth: Maximum crawl depth (k) for this task's crawl run.
            block:     If True (default), block until a free slot is available
                       when the queue is full. This is the main back-pressure
                       mechanism.
            timeout:   Optional max time in seconds to wait for a free slot.

        Raises:
            queue.Full if `block` is False and the queue is already full,
                       or if `timeout` expires.
        """
        # hit_rate_secs is stored in the task so that workers can optionally
        # throttle processing without changing the underlying queue behavior.
        task = CrawlTask(url=url, depth=depth, max_depth=max_depth, hit_rate_secs=hit_rate_secs)
        self._queue.put(task, block=block, timeout=timeout)

    # ---------- Consumer-side API ----------

    def get_task(
        self,
        block: bool = True,
        timeout: Optional[float] = None,
    ) -> CrawlTask:
        """
        Dequeue the next crawl task.

        Parameters mirror `queue.Queue.get`.

        Raises:
            queue.Empty if `block` is False and the queue is empty,
                        or if `timeout` expires.
        """
        task: CrawlTask = self._queue.get(block=block, timeout=timeout)
        return task

    def task_done(self) -> None:
        """
        Indicate that a previously fetched task is complete.

        This must be called once for every `get_task()` call. It allows
        higher-level code to use `join()` to wait until all tasks are done.
        """
        self._queue.task_done()

    # ---------- Monitoring / metrics ----------

    def qsize(self) -> int:
        """Approximate current queue size (thread-safe but not exact)."""
        return self._queue.qsize()

    def is_empty(self) -> bool:
        """Return True if the queue is empty at this instant."""
        return self._queue.empty()

    def is_full(self) -> bool:
        """Return True if the queue is full at this instant."""
        return self._queue.full()

    def join(self) -> None:
        """
        Block until all enqueued tasks have been processed.

        A task is considered "processed" when:
        - It has been retrieved with `get_task()`, and
        - `task_done()` has been called for it.
        """
        self._queue.join()

    # ---------- State snapshot / restore ----------

    def snapshot(self) -> List[Dict[str, int]]:
        """
        Take a snapshot of the pending tasks in the queue.

        We lock the underlying queue mutex and copy its internal deque to
        avoid races with producers/consumers.
        """
        with self._queue.mutex:  # type: ignore[attr-defined]
            return [
                {"url": task.url, "depth": task.depth, "max_depth": task.max_depth}
                for task in list(self._queue.queue)  # type: ignore[attr-defined]
            ]

    def load_snapshot(self, data: List[Dict[str, int]]) -> None:
        """
        Replace the queue contents with tasks from the given snapshot.

        This is intended to be used only during initialization, before
        worker threads start consuming from the queue.
        """
        if data is None:
            data = []

        with self._queue.mutex:  # type: ignore[attr-defined]
            # Clear existing state
            self._queue.queue.clear()  # type: ignore[attr-defined]

            for item in data:
                try:
                    url = item["url"]
                    depth = int(item["depth"])
                    max_depth = int(item.get("max_depth", 2))
                except Exception:
                    continue
                self._queue.queue.append(CrawlTask(url=url, depth=depth, max_depth=max_depth))  # type: ignore[attr-defined]

            # Reset unfinished_tasks counter to reflect current queue size
            self._queue.unfinished_tasks = len(self._queue.queue)  # type: ignore[attr-defined]

    # ---------- Capacity Management ----------

    def get_maxsize(self) -> int:
        """Kuyruğun güncel maksimum kapasitesini döndürür."""
        return self._queue.maxsize

    def set_maxsize(self, new_maxsize: int) -> None:
        """
        Kuyruğun kapasitesini dinamik olarak günceller.
        Eğer yeni kapasite mevcut boyuttan küçükse, kuyruk boşalana kadar yeni öğe eklenemez.
        """
        with self._queue.mutex:
            self._queue.maxsize = new_maxsize