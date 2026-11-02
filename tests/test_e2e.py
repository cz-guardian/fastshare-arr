"""End-to-end test against a fake Fastshare API. Run: python -m unittest discover tests"""
import io, json, os, sys, tempfile, threading, time, unittest, urllib.parse, urllib.request, zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

MKV = os.urandom(700_000)
BIG = 9_000 * 1048576  # pretend 9 GB (never actually served)
buf = io.BytesIO()
with zipfile.ZipFile(buf, "w") as z:
    z.writestr("inside.txt", "hello from the archive")
ZIP = buf.getvalue()
STATE = {"terms": [], "logins": 0}
PORT = {}


def rows():
    b = f"http://127.0.0.1:{PORT['fake']}"
    return [
        {"filename": "Show.Name.S01E02.1080p.WEB-DL.x264-GRP.mkv", "data": {"value": str(len(MKV))},
         "download_url": f"{b}/dl/111/Show.Name.S01E02.1080p.WEB-DL.x264-GRP.mkv"},
        {"filename": "Huge.Movie.2020.2160p.mkv", "data": {"value": str(BIG)}, "download_url": f"{b}/dl/999/huge.mkv"},
    ]


class Fake(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *a): pass

    def _send(self, body, ctype="application/json", code=200, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urllib.parse.urlsplit(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        if u.path == "/api/api_kodi.php":
            if q.get("process") == "login":
                if q.get("login") == "me" and q.get("password") == "p&ss#word":
                    STATE["logins"] += 1
                    return self._send(json.dumps({"user": {"hash": "abc123", "data": {"value": "5000"}, "unlimited": "False"}}).encode())
                return self._send(json.dumps({"user": {"hash": ""}}).encode())
            if q.get("process") == "search":
                STATE["terms"].append(q["term"])
                return self._send(json.dumps({"search": {"file": rows()}}).encode())
            if q.get("process") == "top":
                return self._send(json.dumps({"search": {"file": rows()[:1]}}).encode())
        if u.path.startswith("/dl/"):
            if "FASTSHARE=abc123" not in self.headers.get("Cookie", ""):
                return self._send(b"<html>please log in</html>", "text/html")
            name = u.path.rsplit("/", 1)[1]
            data = ZIP if name.endswith(".zip") else MKV
            hdr = {"Content-Disposition": f'attachment; filename="{name}"'}
            rng = self.headers.get("Range")
            if rng:
                start = int(rng.split("=")[1].split("-")[0])
                if start >= len(data):
                    return self._send(b"", code=416)
                hdr["Content-Range"] = f"bytes {start}-{len(data)-1}/{len(data)}"
                return self._send(data[start:], "application/octet-stream", 206, hdr)
            return self._send(data, "application/octet-stream", 200, hdr)
        self._send(b"nope", code=404)


def serve(handler):
    s = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    return s


def multipart(fname, content):
    b = "----t" + os.urandom(4).hex()
    out = (f'--{b}\r\nContent-Disposition: form-data; name="name"; filename="{fname}"\r\n'
           f'Content-Type: application/x-nzb\r\n\r\n').encode() + content + f"\r\n--{b}--\r\n".encode()
    return out, f"multipart/form-data; boundary={b}"


class E2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fake = serve(Fake)
        PORT["fake"] = cls.fake.server_port
        cls.tmp = tempfile.mkdtemp()
        os.environ.update({"CONFIG_DIR": cls.tmp + "/cfg", "INCOMPLETE_DIR": cls.tmp + "/inc", "COMPLETE_DIR": cls.tmp + "/done",
                           "API_KEY": "k3y", "FASTSHARE_USER": "me", "FASTSHARE_PASS": "p&ss#word",
                           "FASTSHARE_API_BASE": f"http://127.0.0.1:{cls.fake.server_port}", "HOST": "127.0.0.1", "PORT": "0"})
        from fastshare_arr import config
        from fastshare_arr.fastshare import FastshareClient
        from fastshare_arr.manager import Manager
        from fastshare_arr.sabapi import make_server
        cls.cfg = config.load()
        cls.fs = FastshareClient(cls.cfg)
        cls.mgr = Manager(cls.cfg, cls.fs)
        cls.mgr.start()
        cls.api = make_server(cls.cfg, cls.mgr, cls.fs)
        threading.Thread(target=cls.api.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.api.server_port}"

    def get(self, path, **q):
        with urllib.request.urlopen(self.base + path + "?" + urllib.parse.urlencode(q)) as r:
            return r.read()

    def sab(self, **q):
        q.setdefault("apikey", "k3y"); q.setdefault("output", "json")
        return json.loads(self.get("/api", **q))

    def wait_history(self, jid):
        for _ in range(100):
            for s in self.sab(mode="history", start=0, limit=60)["history"]["slots"]:
                if s["nzo_id"] == jid:
                    return s
            time.sleep(0.2)
        self.fail("job never reached history")

    # ---------------------------------------------------------------- SAB --
    def test_1_sab_basics(self):
        self.assertEqual(self.sab(mode="version", apikey="")["version"], "4.3.3")
        self.assertFalse(self.sab(mode="queue", apikey="wrong")["status"])
        cfg = self.sab(mode="get_config")["config"]
        self.assertEqual(cfg["misc"]["complete_dir"], str(self.cfg.complete_dir))
        self.assertIn("tv", self.sab(mode="get_cats")["categories"])

    def test_2_login_with_special_chars_and_credit(self):
        self.fs.login()
        self.assertEqual(self.fs.credit_mb, 5000)
        from fastshare_arr.fastshare import FastshareError
        self.cfg.fs_pass, good = "wrong", self.cfg.fs_pass
        try:
            with self.assertRaises(FastshareError):
                self.fs.login()
        finally:
            self.cfg.fs_pass = good

    # ----------------------------------------------------------- indexer --
    def test_3_indexer_to_download_full_flow(self):
        caps = ET.fromstring(self.get("/newznab/api", t="caps"))
        self.assertEqual(caps.find("searching/tv-search").get("available"), "yes")
        rss = ET.fromstring(self.get("/newznab/api", t="tvsearch", apikey="k3y", q="Show Name", season="1", ep="2"))
        self.assertEqual(STATE["terms"][-1], "Show Name S01E02")
        item = rss.find("channel/item")
        self.assertEqual(item.find("title").text, "Show.Name.S01E02.1080p.WEB-DL.x264-GRP")
        cats = [a.get("value") for a in item.findall("{http://www.newznab.com/DTD/2010/feeds/attributes/}attr") if a.get("name") == "category"]
        self.assertEqual(cats, ["5000", "5040"])
        link = item.find("link").text
        nzb = urllib.request.urlopen(link).read()  # what Sonarr does
        body, ctype = multipart("x.nzb", nzb)
        qs = urllib.parse.urlencode({"mode": "addfile", "apikey": "k3y", "cat": "tv", "output": "json",
                                     "nzbname": "Show.Name.S01E02.1080p.WEB-DL.x264-GRP", "priority": "-100"})
        res = json.load(urllib.request.urlopen(urllib.request.Request(self.base + "/api?" + qs, data=body, headers={"Content-Type": ctype})))
        self.assertTrue(res["status"], res)
        jid = res["nzo_ids"][0]
        slot = self.sab(mode="queue", start=0, limit=0)["queue"]["slots"]
        if slot:  # may already be done on a fast machine
            self.assertEqual(slot[0]["cat"], "tv")
            self.assertGreater(float(slot[0]["mb"]), 0)  # size known from the search result
        h = self.wait_history(jid)
        self.assertEqual(h["status"], "Completed", h)
        path = os.path.join(h["storage"], "Show.Name.S01E02.1080p.WEB-DL.x264-GRP.mkv")
        self.assertEqual(open(path, "rb").read(), MKV)
        self.sab(mode="history", name="delete", value=jid, del_files="1")
        self.assertFalse(os.path.exists(h["storage"]))

    def test_4_indexer_rejects_bad_key_and_empty_query_uses_top(self):
        self.assertIn(b"error", self.get("/newznab/api", t="search", apikey="nope"))
        rss = ET.fromstring(self.get("/newznab/api", t="search", apikey="k3y"))
        self.assertEqual(len(rss.findall("channel/item")), 1)

    def test_5_insufficient_credit_fails_cleanly(self):
        rss = ET.fromstring(self.get("/newznab/api", t="movie", apikey="k3y", q="Huge Movie"))
        link = [i for i in rss.findall("channel/item") if "Huge" in i.find("title").text][0].find("link").text
        body, ctype = multipart("x.nzb", urllib.request.urlopen(link).read())
        res = json.load(urllib.request.urlopen(urllib.request.Request(
            self.base + "/api?mode=addfile&apikey=k3y&cat=movies&output=json&nzbname=Huge.Movie.2020.2160p", data=body, headers={"Content-Type": ctype})))
        h = self.wait_history(res["nzo_ids"][0])
        self.assertEqual(h["status"], "Failed")
        self.assertIn("credit", h["fail_message"])

    # --------------------------------------------------------- pipeline --
    def test_6_resume_and_unpack(self):
        from fastshare_arr.manager import FileItem, Job
        b = f"http://127.0.0.1:{self.fake.server_port}"
        job = Job(nzo_id="SABnzbd_nzo_resume", name="Resume.Test", category="movies", created=time.time(),
                  files=[FileItem(url=f"{b}/dl/1/movie.mkv"), FileItem(url=f"{b}/dl/2/extras.zip")])
        tmp = self.cfg.incomplete_dir / job.nzo_id
        tmp.mkdir(parents=True)
        (tmp / "0.part").write_bytes(MKV[:300_000])  # leftover partial -> must resume via Range
        with self.mgr.cv:
            self.mgr.queue.append(job); self.mgr.cv.notify_all()
        for _ in range(100):
            if job.status in ("Completed", "Failed"): break
            time.sleep(0.2)
        self.assertEqual(job.status, "Completed", job.fail_message)
        self.assertEqual(sorted(os.listdir(job.storage)), ["inside.txt", "movie.mkv"])  # zip unpacked + removed
        self.assertEqual(open(os.path.join(job.storage, "movie.mkv"), "rb").read(), MKV)

    def test_7_rejects_usenet_nzb(self):
        body, ctype = multipart("x.nzb", b'<nzb><file><segments><segment bytes="1">abc@x</segment></segments></file></nzb>')
        res = json.load(urllib.request.urlopen(urllib.request.Request(
            self.base + "/api?mode=addfile&apikey=k3y&output=json", data=body, headers={"Content-Type": ctype})))
        self.assertFalse(res["status"])
        self.assertIn("Usenet", res["error"])


if __name__ == "__main__":
    unittest.main()
