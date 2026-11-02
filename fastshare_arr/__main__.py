import logging
import signal
import sys
import threading

from . import __version__, config, extract
from .fastshare import FastshareClient, FastshareError
from .manager import Manager
from .sabapi import make_server


def check(cfg, term, probe=False):
    fs = FastshareClient(cfg)
    fs.login()
    print(f"Login OK: {'unlimited' if fs.unlimited else str(fs.credit_mb) + ' MB credit'}")
    if term:
        rows = fs.search(term, 5)
        print(f"Search {term!r}: {len(rows)} result(s)")
        for r in rows[:5]:
            print("  ", r.get("filename"), (r.get("data") or {}).get("value"), "bytes")
        if rows and probe:  # opening a file may cost credit, so only on request
            resp, name = fs.open_download(rows[0]["download_url"], 0)
            print(f"Download link OK: {name!r} {resp.headers.get('Content-Type')} {resp.headers.get('Content-Length')} bytes")
            resp.close()


def main():
    cfg = config.load()
    logging.basicConfig(
        level=logging.DEBUG if cfg.debug else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    if len(sys.argv) > 1 and sys.argv[1] == "check":
        try:
            check(cfg, " ".join(a for a in sys.argv[2:] if a != "--probe"), "--probe" in sys.argv)
        except FastshareError as e:
            print(f"FAILED: {e}")
            sys.exit(1)
        return

    if cfg.extract and not extract.rar_support():
        logging.warning("No RAR-capable extractor found (install 7zip); .rar releases will fail to unpack")
    fs = FastshareClient(cfg)
    manager = Manager(cfg, fs)
    manager.start()
    server = make_server(cfg, manager, fs)
    logging.info("fastshare-arr %s listening on %s:%d", __version__, cfg.host, cfg.port)
    logging.info("API key: %s", cfg.api_key)

    def shutdown(*_):
        manager.stop()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    server.serve_forever()


main()
