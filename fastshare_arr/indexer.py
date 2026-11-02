"""Newznab-compatible indexer backed by Fastshare's search API.

Add in Prowlarr as 'Generic Newznab': URL http://fastshare-arr:8085/newznab , API path /api, API key = ours.
Each result's download link returns a tiny pseudo-NZB that our SABnzbd-style client understands, so
search -> grab -> download -> import works end to end with the stock arr apps.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import threading
import time
import unicodedata
from email.utils import formatdate
from xml.sax.saxutils import escape, quoteattr

log = logging.getLogger("indexer")
SE_RE = re.compile(r"\bS(\d{1,2})[ ._-]?E(\d{1,3})((?:[ ._-]?E\d{1,3})*)|\b(\d{1,2})x(\d{2,3})\b", re.I)
VIDEO_EXT = re.compile(r"\.(mkv|mp4|avi|m4v|mov|wmv|ts|flac|mp3|m4a|ogg|opus|wav)$", re.I)
REAL_VIDEO = re.compile(r"\.(mkv|mp4|avi|m4v|mov|wmv|ts)$", re.I)
AUDIO_EXT = re.compile(r"\.(flac|mp3|m4a|ogg|opus|wav)$", re.I)
DATE_RE = re.compile(r"\b(19|20)\d\d[.\-_ ](0[1-9]|1[0-2])[.\-_ ](0[1-9]|[12]\d|3[01])\b")
TV_RE = re.compile(r"\bS\d{1,2}(E\d{1,3})?\b|\b\d{1,2}x\d{2}\b", re.I)
HD_RE = re.compile(r"2160p|1080[pi]|720p|4k|uhd", re.I)

CAPS = """<?xml version="1.0" encoding="UTF-8"?>
<caps>
  <server version="1.0" title="Fastshare" strapline="Fastshare via fastshare-arr"/>
  <limits max="200" default="100"/>
  <searching>
    <search available="yes" supportedParams="q"/>
    <tv-search available="yes" supportedParams="q,season,ep"/>
    <movie-search available="yes" supportedParams="q,year"/>
    <music-search available="yes" supportedParams="q,artist,album"/>
  </searching>
  <categories>
    <category id="2000" name="Movies"><subcat id="2030" name="Movies/SD"/><subcat id="2040" name="Movies/HD"/></category>
    <category id="3000" name="Audio"/>
    <category id="5000" name="TV"><subcat id="5030" name="TV/SD"/><subcat id="5040" name="TV/HD"/></category>
  </categories>
</caps>"""


def parse_episode(name: str):
    """(season, {episodes}, start_index_of_marker) or None. Handles S01E02, S01E02E03 and 1x02."""
    m = SE_RE.search(name)
    if not m:
        return None
    if m.group(1):
        eps = {int(m.group(2))} | {int(x) for x in re.findall(r"E(\d{1,3})", m.group(3) or "", re.I)}
        return int(m.group(1)), eps, m.start()
    return int(m.group(4)), {int(m.group(5))}, m.start()


def _tokens(s: str) -> set:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    return set(re.findall(r"[a-z0-9]+", s))


def rewrite_title(title: str, q: str) -> str:
    """If the file's series name does not contain what Sonarr searched for (Czech title, alias, odd naming),
    present the release under the searched name so Sonarr can match the series. Episode + tags are kept."""
    pe = parse_episode(title)
    if not pe or not q:
        return title
    if _tokens(q) <= _tokens(title[:pe[2]]):
        return title  # already matches: leave untouched
    base = re.sub(r"[^\w.()\-]+", ".", q.strip()).strip(".")
    return f"{base}.{re.sub(r'\s+', '.', title[pe[2]:].strip())}"


class Indexer:
    def __init__(self, cfg, fs):
        self.cfg, self.fs = cfg, fs
        self._cache, self._lock = {}, threading.Lock()

    # ------------------------------------------------------------ helpers --
    def _results(self, term: str) -> list:
        key = term.lower()
        with self._lock:
            hit = self._cache.get(key)
            if hit and time.time() - hit[0] < 60:
                return hit[1]
        rows = self.fs.search(term) if term else self.fs.top()
        with self._lock:
            self._cache[key] = (time.time(), rows)
            if len(self._cache) > 200:
                self._cache.pop(next(iter(self._cache)))
        return rows

    @staticmethod
    def build_term(p: dict) -> str:
        t, q = p.get("t", "search"), (p.get("q") or "").strip()
        if t == "tvsearch":
            season, ep = p.get("season", ""), p.get("ep", "")
            if season.isdigit():
                q += f" S{int(season):02d}" + (f"E{int(ep):02d}" if ep.isdigit() else "")
            elif ep:
                q += f" {ep}"
        elif t == "movie" and p.get("year"):
            q += f" {p['year']}"
        elif t == "music":
            q = " ".join(x for x in (p.get("artist", ""), p.get("album", ""), q) if x)
        return q.strip()

    @staticmethod
    def kind(name: str) -> str:
        """Classify a Fastshare file by its name: 'tv', 'audio', 'movie' (video/other)."""
        if AUDIO_EXT.search(name):
            return "audio"
        if TV_RE.search(name) or DATE_RE.search(name):
            return "tv"
        return "movie"

    @classmethod
    def wanted(cls, t: str, name: str) -> bool:
        """Only return what the asking app can use: Radarr must not see episodes, Sonarr must not see movies."""
        k = cls.kind(name)
        if t == "tvsearch":
            return k == "tv"
        if t == "movie":
            return k == "movie"
        if t == "music":
            return k in ("audio", "movie") and not REAL_VIDEO.search(name)  # tracks or archives, not videos
        return True

    @classmethod
    def categories(cls, name: str, t: str) -> list:
        k = cls.kind(name)
        if t == "music" or k == "audio":
            return [3000]
        hd = bool(HD_RE.search(name))
        if k == "tv":
            return [5000, 5040 if hd else 5030]
        return [2000, 2040 if hd else 2030]

    @staticmethod
    def _token(row: dict, name: str = "") -> str:
        data = {"u": row["download_url"], "n": name or row.get("filename", ""), "s": int((row.get("data") or {}).get("value") or 0)}
        return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")

    # ------------------------------------------------------------ handlers --
    def handle(self, params: dict, base_url: str):
        """Return (body_bytes, content_type, http_status)."""
        t = params.get("t", "")
        if t == "caps":
            return self._caps(), "text/xml", 200
        if params.get("apikey") != self.cfg.api_key:
            return self._error(100, "Incorrect user credentials"), "text/xml", 200
        if t in ("search", "tvsearch", "movie", "music"):
            try:
                return self._rss(params, base_url), "application/rss+xml", 200
            except Exception as e:  # noqa: BLE001
                return self._error(900, f"Fastshare search failed: {e}"), "text/xml", 200
        return self._error(202, f"No such function: {t}"), "text/xml", 200

    def _caps(self) -> bytes:
        return CAPS.encode()

    @staticmethod
    def _error(code, msg) -> bytes:
        return f'<?xml version="1.0"?><error code="{code}" description={quoteattr(msg)}/>'.encode()

    def _rss(self, p: dict, base_url: str) -> bytes:
        t = p.get("t", "search")
        q = (p.get("q") or "").strip()
        term = self.build_term(p)
        raw = self._results(term)
        season = int(p["season"]) if str(p.get("season", "")).isdigit() and int(p["season"]) < 1900 else None
        ep = int(p["ep"]) if str(p.get("ep", "")).isdigit() else None
        items, rewrites = [], 0
        for r in raw:
            name = r.get("filename", "")
            if not r.get("download_url") or not self.wanted(t, name):
                continue
            title = VIDEO_EXT.sub("", name)
            ext = name[len(title):]
            if t == "tvsearch":
                pe = parse_episode(title)
                if pe and season is not None and pe[0] != season:
                    continue  # wrong season
                if pe and ep is not None and ep not in pe[1]:
                    continue  # wrong episode
                if self.cfg.tv_title_mode == "auto":
                    new = rewrite_title(title, q)
                    if new != title:
                        rewrites += 1
                        title = new
            items.append((r, title, title + ext))
        log.info("%s q=%r season=%s ep=%s -> %d raw, %d returned, %d titles rewritten",
                 t, q, season, ep, len(raw), len(items), rewrites)
        limit = min(int(p.get("limit") or 100), 200)
        offset = int(p.get("offset") or 0)
        page = items[offset:offset + limit]
        now = formatdate(usegmt=True)
        out = ['<?xml version="1.0" encoding="UTF-8"?>',
               '<rss version="2.0" xmlns:newznab="http://www.newznab.com/DTD/2010/feeds/attributes/">',
               "<channel><title>Fastshare</title>",
               f'<newznab:response offset="{offset}" total="{len(items)}"/>']
        for r, title, save_name in page:
            size = int((r.get("data") or {}).get("value") or 0)
            link = f"{base_url}/newznab/nzb/{self._token(r, save_name)}.nzb?apikey={self.cfg.api_key}"
            guid = hashlib.sha1(r["download_url"].encode()).hexdigest()
            out.append(
                f'<item><title>{escape(title)}</title><guid isPermaLink="false">{guid}</guid>'
                f"<link>{escape(link)}</link><pubDate>{now}</pubDate><size>{size}</size>"
                f'<enclosure url={quoteattr(link)} length="{size}" type="application/x-nzb"/>'
                + "".join(f'<newznab:attr name="category" value="{c}"/>' for c in self.categories(save_name, t))
                + f'<newznab:attr name="size" value="{size}"/></item>'
            )
        out.append("</channel></rss>")
        return "".join(out).encode()

    def nzb(self, token: str, params: dict):
        if params.get("apikey") != self.cfg.api_key:
            return b"unauthorized", "text/plain", 401
        try:
            d = json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))
            name = d.get("n") or "download"
            body = (
                '<?xml version="1.0" encoding="UTF-8"?>\n<nzb xmlns="http://www.newzbin.com/DTD/2003/nzb">'
                f"<head><meta type=\"name\">{escape(VIDEO_EXT.sub('', name))}</meta></head>"
                f"<file subject={quoteattr(name)}><fastshare url={quoteattr(d['u'])} size=\"{int(d.get('s') or 0)}\" "
                f"name={quoteattr(name)}/></file></nzb>"
            )
        except (ValueError, KeyError):
            return b"bad token", "text/plain", 400
        return body.encode(), "application/x-nzb", 200
