"""Ownership + permissions, linuxserver.io style: PUID / PGID / UMASK (+ optional CHOWN_RECURSIVE).

The container starts as root (default), prepares the directories, hands them to PUID:PGID and then drops
privileges, so every file we download, unpack or write (state, API key, debug pages) belongs to that user.
If the container is already started as a non-root user (docker `user:`), PUID/PGID are ignored.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger("privileges")


def _int(name):
    v = os.environ.get(name, "").strip()
    return int(v) if v.isdigit() else None


def _mkdirs_owned(path: Path, uid, gid):
    """mkdir -p, giving every directory we create (not pre-existing ones) to uid:gid."""
    missing = []
    p = path
    while not p.exists() and p != p.parent:
        missing.append(p)
        p = p.parent
    for d in reversed(missing):
        d.mkdir(exist_ok=True)
        if uid is not None:
            os.chown(d, uid, gid)


def _chown_tree(root: Path, uid, gid):
    os.chown(root, uid, gid)
    for dirpath, dirnames, filenames in os.walk(root):
        for n in dirnames + filenames:
            try:
                os.lchown(os.path.join(dirpath, n), uid, gid)
            except OSError:
                pass


def setup(cfg) -> None:
    try:
        mask = int(os.environ.get("UMASK", "022"), 8)
    except ValueError:
        log.warning("Invalid UMASK, using 022")
        mask = 0o022
    os.umask(mask)

    puid, pgid = _int("PUID"), _int("PGID")
    if pgid is None:
        pgid = puid
    is_root = hasattr(os, "getuid") and os.getuid() == 0

    if not is_root:
        if puid is not None and puid != os.getuid():
            log.warning("PUID/PGID ignored: not started as root (running as uid %d)", os.getuid())
        return

    uid, gid = (puid, pgid) if puid is not None else (None, pgid)
    if uid is None and gid is None:
        log.warning("Running as root; set PUID/PGID to match your *arr containers")
        return

    recursive = os.environ.get("CHOWN_RECURSIVE", "").lower() in ("1", "true", "yes", "on")
    own_uid, own_gid = (uid if uid is not None else -1), (gid if gid is not None else -1)
    for d in (cfg.config_dir, cfg.incomplete_dir, cfg.complete_dir):
        _mkdirs_owned(Path(d), own_uid, own_gid)
    _chown_tree(Path(cfg.config_dir), own_uid, own_gid)  # small; always fixed up (api_key, state.json)
    for d in (cfg.incomplete_dir, cfg.complete_dir):
        if recursive:
            _chown_tree(Path(d), own_uid, own_gid)
        else:
            os.chown(d, own_uid, own_gid)

    os.setgroups([])
    if gid is not None:
        os.setgid(gid)
    if uid is not None:
        os.setuid(uid)
    log.info("Running as uid=%s gid=%s umask=%s", os.getuid(), os.getgid(), oct(mask))
