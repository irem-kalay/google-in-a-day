from __future__ import annotations

import logging
import re
import threading
import time
from html.parser import HTMLParser
from typing import List, Tuple, Iterable, Optional
from urllib.parse import urljoin
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

#data_stuctures.py'den gerekli classları imprt ettim.
from data_structures import (
    ThreadSafeVisitedSet,
    ThreadSafeInvertedIndex,
    CrawlQueue,
    CrawlTask,
    ThreadSafeTitleMap,
    ThreadSafeMetadataMap,
)


# ============================================================
# 1) NativeHTMLParser
# ============================================================

class NativeHTMLParser(HTMLParser):
    """
    Yalnızca standart kütüphane kullanan basit bir HTML parser.

    Sorumluluklar:
      - <a href="..."> linklerini toplamak.
      - <script> ve <style> hariç HTML gövdesindeki metni toplamak.
      - Metni normalize edip kelime listesine (token) dönüştürmek.

    Tasarım notları:
      - HTMLParser re-entrant / thread-safe DEĞİLDİR, bu yüzden her iş parçacığı
        kendi parser örneğini kullanmalıdır. (Aşağıdaki worker'da böyle yapacağız.)
      - Parser state'i (linkler, text buffer) örnek üzerinde tutulur; her HTML
        dokümanı için yeni bir örnek yaratmak en temizi.
    """

    # Kelime normalizasyonu için kullanılacak basit regex:
    # Harf ve rakam dışındaki her şeyi boşluğa çeviriyoruz.
    #_token_splitter = re.compile(r"[^a-zA-Z0-9]+")
    _token_splitter = re.compile(r"[^a-zA-Z0-9çğıöşüÇĞIİÖŞÜ]+") #türkçe karakterleri ekledim düzgün parse için


    def __init__(self, base_url: str) -> None:
        # base_url, relative URL'leri normalize ederken işe yarayacak.
        super().__init__(convert_charrefs=True)
        self.base_url = base_url

        self.links: List[str] = []
        self._text_chunks: List[str] = []

        # <title> içeriğini yakalamak için ek state
        self._in_title = False
        self._title_chunks: List[str] = []

        # <script> veya <style> içindeyken metni yoksaymak için flag'ler
        self._in_script = False
        self._in_style = False

    # ---------- HTMLParser override yöntemleri ----------

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        tag = tag.lower()

        if tag == "script":
            self._in_script = True
        elif tag == "style":
            self._in_style = True
        elif tag == "title":
            # Yeni bir <title> başladığında buffer'ı sıfırla
            self._in_title = True
            self._title_chunks.clear()
        elif tag == "a":
            # href attribute'unu yakala
            href = None
            for (name, value) in attrs:
                if name.lower() == "href":
                    href = value
                    break
            if href:
                # Relative URL'leri base_url ile birleştir
                normalized = urljoin(self.base_url, href)
                self.links.append(normalized)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "script":
            self._in_script = False
        elif tag == "style":
            self._in_style = False
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        # <title> içindeyken başlığı ayrıca topla
        if self._in_title and data:
            self._title_chunks.append(data)

        # <script> veya <style> içindeyken gövde metnini yoksay
        if self._in_script or self._in_style:
            return
        if data.strip():
            self._text_chunks.append(data)

    # ---------- Dış API ----------

    def parse(self, html: str) -> Tuple[List[str], List[str], Optional[str]]:
        """
        Verilen HTML dokümanını işler.

        Dönüş:
            (tokens, links, title)
            tokens: normalize edilmiş kelime listesi
            links:  normalize edilmiş URL listesi
            title:  sayfanın <title> içeriği (veya None)
        """
        self.links.clear()
        self._text_chunks.clear()
        self._title_chunks.clear()

        self.feed(html)
        self.close()

        text = " ".join(self._text_chunks)
        tokens = self._normalize_text(text)
        title = " ".join(self._title_chunks).strip() or None
        return tokens, self.links, title

    @classmethod
    def _normalize_text(cls, text: str) -> List[str]:
        """
        Basit tokenization / normalizasyon.

        Adımlar:
          - lower()
          - harf/rakam dışı karakterleri boşluk yap
          - boş olanları at
        """
        text = text.lower()
        parts = cls._token_splitter.split(text)
        return [p for p in parts if p]


# ============================================================
# 2) CrawlerWorker
# ============================================================

class CrawlerWorker(threading.Thread):
    """
    Bir iş parçacığı olarak çalışan tekil crawler worker.

    Sorumluluklar:
      - CrawlQueue'dan CrawlTask çekmek.
      - Derinlik sınırı (k) kontrolü yapmak.
      - urllib.request.urlopen ile HTML'i indirmek (timeout ve temel hatalarla başa çıkmak).
      - NativeHTMLParser ile HTML'i parse edip:
          * kelimeleri ThreadSafeInvertedIndex'e eklemek,
          * yeni URL'leri normalize edip, ziyaret edilmemiş olanları kuyruğa koymak.
      - Her görev için queue.task_done() çağırmak.

    Thread-safety:
      - CrawlQueue zaten thread-safe bir yapı (queue.Queue üzerine kurulu).
      - URL tekilliği ve index güncellemeleri, dışarıdan verilen
        ThreadSafeVisitedSet ve ThreadSafeInvertedIndex üzerinden güvenle yapılır.
      - Her worker kendi NativeHTMLParser örneğini kullanır; parser paylaşımlı değildir.
    """

    def __init__(
        self,
        name: str,
        queue: CrawlQueue,
        visited: ThreadSafeVisitedSet,
        index: ThreadSafeInvertedIndex,
        stop_event: threading.Event,
        request_timeout: float = 5.0,
        user_agent: str = "SimpleCrawler/0.1",
        idle_exit_seconds: float = 10.0,
        title_map: Optional[ThreadSafeTitleMap] = None,
        metadata_map: Optional[ThreadSafeMetadataMap] = None,
    ) -> None:
        super().__init__(name=name)
        self.daemon = True  # İsteğe bağlı: ana program sonlandığında thread'ler de sonlansın.

        self._queue = queue
        self._visited = visited
        self._index = index
        self._title_map = title_map
        self._metadata_map = metadata_map
        self._stop_event = stop_event
        self._request_timeout = request_timeout
        self._user_agent = user_agent
        self._idle_exit_seconds = idle_exit_seconds

        self._logger = logging.getLogger(f"CrawlerWorker-{name}")

    def run(self) -> None:
        """
        Worker ana döngüsü.

        Çıkış koşulları:
          - stop_event set edilmişse, veya
          - kuyruğun uzun süre boş kalması (idle_exit_seconds).
        """
        self._logger.info("Worker başlıyor.")

        while not self._stop_event.is_set():
            try:
                # Kuyruk boşsa belirli bir süre bekle, sonra yeniden dene veya çık.
                task = self._queue.get_task(block=True, timeout=self._idle_exit_seconds)
            except Exception as exc:
                # queue.Empty dahil tüm istisnalar.
                # Kuyruk uzun süre boşsa, worker'ı zarifçe sonlandırabiliriz.
                if isinstance(exc, Exception):
                    if isinstance(exc, type(getattr(__import__("queue"), "Empty"))):
                        # gerçekten queue.Empty mi diye kaba bir kontrol;
                        # çoğu durumda exc.__class__ is queue.Empty olacaktır.
                        self._logger.info("Kuyruk uzun süre boş, worker sonlanıyor.")
                        break
                # Beklenmedik bir şey olursa loglayıp devam edelim.
                self._logger.exception("Kuyruktan görev alınırken hata: %s", exc)
                continue

            try:
                self._process_task(task)
            finally:
                # Ne olursa olsun task_done çağırmak kritik.
                self._queue.task_done()

        self._logger.info("Worker durdu.")

    # ---------- İç yardımcılar ----------

    def _process_task(self, task: "CrawlTask") -> None:
        url = task.url
        depth = task.depth

        # Eğer bu URL için henüz metadata kaydı yoksa (örneğin seed URL),
        # en azından kendi derinliğiyle bir kayıt oluştur.
        if self._metadata_map is not None:
            if self._metadata_map.get_metadata(url) is None:
                self._metadata_map.record_discovery(url, origin_url=None, depth=depth)

        # Derinlik sınırı kontrolü
        if depth > task.max_depth:
            self._logger.debug("Derinlik sınırı aşıldı (%s): %s", depth, url)
            return

        # URL daha önce ziyaret edilmiş mi? (Ek güvenlik; normalde
        # enqueue etmeden önce de kontrol ediyoruz.)
        if url in self._visited:
            self._logger.debug("Zaten ziyaret edilmiş URL atlanıyor: %s", url)
            return

        # Bu noktada URL'i ziyaret edilmiş olarak işaretleyelim.
        added = self._visited.add(url)
        if not added:
            # Başka bir thread bizden hemen önce işaretlemiş olabilir.
            self._logger.debug("Yarış sonucu: URL zaten eklendi: %s", url)
            return

        self._logger.info("Crawling (depth=%s): %s", depth, url)

        html = self._fetch_html(url)
        if html is None:
            # Ağ hatası vb. durumlarda URL'i sessizce atlarız.
            return

        # HTML'i parse et: kelimeler + linkler
        parser = NativeHTMLParser(base_url=url)
        try:
            tokens, links, title = parser.parse(html)
        except Exception:
            # HTMLParser bazen bozuk HTML'de istisna fırlatabilir; loglayıp devam et.
            self._logger.exception("HTML parse edilirken hata: %s", url)
            return

        # Kelimeleri index'e ekle
        if tokens:
            self._index.add_terms(url, tokens)

        # Başlık varsa title_map'e kaydet
        if self._title_map is not None and title:
            self._title_map.set_title(url, title)

        # Yeni linkleri kuyruğa ekle
        self._enqueue_new_links(links, depth, task.max_depth, origin_url=url)

    def _fetch_html(self, url: str) -> Optional[str]:
        """
        urllib.request.urlopen ile HTML içeriğini indir.

        Hata durumlarında None döner (ve crawler devam eder).
        """
        req = Request(
            url,
            headers={"User-Agent": self._user_agent},
            method="GET",
        )

        try:
            with urlopen(req, timeout=self._request_timeout) as resp:
                # Basit bir içerik tipi kontrolü (opsiyonel)
                content_type = resp.headers.get("Content-Type", "")
                if "text/html" not in content_type:
                    self._logger.debug("HTML olmayan içerik: %s (%s)", url, content_type)
                    return None

                # charset tespiti
                charset = resp.headers.get_content_charset() or "utf-8"
                try:
                    data = resp.read()
                    return data.decode(charset, errors="ignore")
                except Exception:
                    self._logger.exception("Yanıt decode edilirken hata: %s", url)
                    return None

        except HTTPError as e:
            self._logger.warning("HTTP hatası %s: %s", e.code, url)
        except URLError as e:
            self._logger.warning("Ağ hatası %s: %s", e.reason, url)
        except Exception:
            self._logger.exception("Beklenmedik hata (fetch_html): %s", url)

        return None

    def _enqueue_new_links(
        self,
        links: Iterable[str],
        current_depth: int,
        max_depth: int,
        origin_url: str,
    ) -> None:
        """
        Yeni bulunan linkleri normalize edip gerekli filtreleri uygulayarak kuyruğa ekler.
        """
        next_depth = current_depth + 1

        for link in links:
            # Bazı basit filtreler:
            if not link.startswith("http://") and not link.startswith("https://"):
                continue

            # Henüz ziyaret edilmemiş mi?
            if link in self._visited:
                continue

            try:
                # Metadata haritasına bu keşfi kaydet (sadece ilk keşfi tutar).
                if self._metadata_map is not None:
                    self._metadata_map.record_discovery(
                        url=link,
                        origin_url=origin_url,
                        depth=next_depth,
                    )
                # DÜZELTME 1: block=False yaptık. Kuyruk doluysa ANINDA hata verir, beklemez.
                self._queue.put_task(link, next_depth, max_depth=max_depth, block=False)
            except Exception:
                # DÜZELTME 2: continue yerine break koyduk. 
                # Kuyruk doluysa bu sayfadaki geri kalan yüzlerce linki denemekle 
                # vakit kaybetme, döngüyü kır ve hemen yeni sayfa taramaya (get_task) git!
                self._logger.warning("Kuyruk dolu, bu sayfadaki kalan linkler atlanıyor.")
                break