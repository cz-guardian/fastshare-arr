"""Post-processing: unpack zip/rar/7z archives like NZBGet does."""
from __future__ import annotations

import logging
import re
import shutil
import subprocess
import zipfile
from pathlib import Path

log = logging.getLogger("extract")

_RAR_PART = re.compile(r"^(?P<base>.+)\.part(?P<n>\d+)\.rar$", re.I)


class ExtractError(Exception):
    pass


def _first_volumes(folder: Path):
    out = []
    for f in sorted(p for p in folder.iterdir() if p.is_file()):
        n = f.name.lower()
        m = _RAR_PART.match(f.name)
        if m:
            if int(m.group("n")) == 1:
                out.append(f)
        elif n.endswith(".rar") or n.endswith(".zip") or n.endswith(".7z") or re.search(r"\.(7z|zip)\.001$", n):
            out.append(f)
    return out


def _volume_family(first: Path):
    folder, name = first.parent, first.name
    m = _RAR_PART.match(name)
    if m:
        rx = re.compile(rf"^{re.escape(m.group('base'))}\.part\d+\.rar$", re.I)
    elif name.lower().endswith(".rar"):
        base = re.escape(name[:-4])
        rx = re.compile(rf"^{base}\.(rar|[r-z]\d\d)$", re.I)
    elif re.search(r"\.(7z|zip)\.001$", name, re.I):
        rx = re.compile(rf"^{re.escape(name[:-4])}\.\d{{3}}$", re.I)
    else:
        rx = re.compile(rf"^{re.escape(name)}$", re.I)
    return [p for p in folder.iterdir() if p.is_file() and rx.match(p.name)]


def _run(cmd):
    r = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if r.returncode != 0:
        raise ExtractError(f"{Path(cmd[0]).name} exit {r.returncode}: {r.stdout.strip()[-300:]}")


def _extract_one(first: Path, password: str):
    low = first.name.lower()
    out = first.parent
    if low.endswith(".zip"):
        try:
            with zipfile.ZipFile(first) as z:
                z.extractall(out, pwd=password.encode() if password else None)
            return
        except (zipfile.BadZipFile, RuntimeError, NotImplementedError) as e:
            seven = shutil.which("7z") or shutil.which("7zz")
            if not seven:
                raise ExtractError(f"{first.name}: {e}") from e
    if low.endswith(".rar"):
        tool = shutil.which("unrar")
        if tool:
            _run([tool, "x", "-o+", "-idq", f"-p{password}" if password else "-p-", str(first), str(out) + "/"])
            return
    seven = shutil.which("7z") or shutil.which("7zz") or shutil.which("7za")
    if not seven:
        raise ExtractError(f"No extractor available for {first.name} (install unrar / 7zip)")
    _run([seven, "x", "-y", f"-p{password}" if password else "-p", f"-o{out}", str(first)])


def extract_all(folder: Path, password: str = "", delete: bool = True) -> int:
    """Extract every archive set in folder (one level of nesting). Returns sets extracted."""
    count = 0
    for _ in range(2):  # allow archive-inside-archive once
        firsts = _first_volumes(folder)
        if not firsts:
            break
        for first in firsts:
            family = _volume_family(first)
            log.info("Extracting %s (%d volume(s))", first.name, len(family))
            _extract_one(first, password)
            count += 1
            if delete:
                for p in family:
                    p.unlink(missing_ok=True)
    return count
