"""Turn whatever an *arr app hands us (an 'NZB') into a list of files to download.

Our own indexer produces:
    <nzb><head><meta type="name">Release</meta><meta type="password">pw</meta></head>
      <file subject="x.mkv"><fastshare url="https://..." size="123" name="x.mkv"/></file></nzb>
Plain text / JSON / HTML containing fastshare.cloud|cz URLs is accepted too.
Real Usenet NZBs (with <segment> entries) are rejected with a clear error.
"""
from __future__ import annotations

import gzip
import re
from dataclasses import dataclass, field
from html import unescape

LINK_RE = re.compile(r"""https?://(?:[a-z0-9-]+\.)*fastshare\.(?:cloud|cz)/[^\s"'<>\]\)\\]+""", re.I)
TAG_RE = re.compile(r"<fastshare\b([^>]*?)/?>", re.I)
ATTR_RE = re.compile(r"""(\w+)\s*=\s*"([^"]*)\"""")
META_RE = re.compile(r"""<meta\s+type=["'](\w+)["']\s*>(.*?)</meta>""", re.I | re.S)


class IngestError(Exception):
    pass


@dataclass
class Payload:
    items: list = field(default_factory=list)  # [{"url", "size", "name"}]
    name: str = ""
    password: str = ""

    @property
    def links(self):
        return [i["url"] for i in self.items]


def is_fastshare_url(s: str) -> bool:
    return bool(LINK_RE.fullmatch(s.strip()))


def parse_payload(data: bytes) -> Payload:
    if data[:2] == b"\x1f\x8b":
        try:
            data = gzip.decompress(data)
        except OSError as e:
            raise IngestError(f"bad gzip payload: {e}") from e
    raw = data.decode("utf-8", "replace")
    items, seen = [], set()
    for m in TAG_RE.finditer(raw):
        a = {k.lower(): unescape(v) for k, v in ATTR_RE.findall(m.group(1))}
        if a.get("url") and a["url"] not in seen:
            seen.add(a["url"])
            items.append({"url": a["url"], "size": int(a.get("size") or 0), "name": a.get("name", "")})
    if not items:
        for m in LINK_RE.finditer(unescape(raw)):
            u = m.group(0).rstrip(".,;")
            if u not in seen:
                seen.add(u)
                items.append({"url": u, "size": 0, "name": ""})
    if not items:
        if "<segment" in raw.lower():
            raise IngestError("This is a real Usenet NZB, not a Fastshare release - route it to a Usenet client")
        raise IngestError("No Fastshare links found in the release payload")
    meta = {k.lower(): unescape(v.strip()) for k, v in META_RE.findall(raw)}
    return Payload(items=items, name=meta.get("name", ""), password=meta.get("password", ""))


def split_password(name: str):
    """SABnzbd convention: 'Release.Name{{password}}'."""
    m = re.match(r"^(.*?)\{\{(.*)\}\}\s*$", name)
    return (m.group(1).strip(), m.group(2)) if m else (name, "")
