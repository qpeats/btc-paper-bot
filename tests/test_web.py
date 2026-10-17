"""Web wrapper: /api/run returns the same simulation the CLI would, from synthetic prices."""
import json
import math
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request

from web import server


def fake_binance(url: str) -> bytes:
    """Binance-shaped klines for the requested range: a gentle oscillating random-ish walk."""
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    start, end, limit = int(q["startTime"][0]), int(q["endTime"][0]), int(q["limit"][0])
    rows = []
    t = start
    while t <= end and len(rows) < limit:
        i = t // 60_000
        mid = 60_000 + 400 * math.sin(i / 37) + 150 * math.sin(i / 7)
        o, c = mid, mid + 20 * math.sin(i / 3)
        rows.append([t, f"{o:.2f}", f"{max(o, c) + 15:.2f}", f"{min(o, c) - 15:.2f}", f"{c:.2f}"])
        t += 60_000
    return json.dumps(rows).encode()


class WebTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._orig = server._http_get
        server._http_get = fake_binance
        cls.srv = server.make_server("127.0.0.1", 0)
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        server._http_get = cls._orig

    def setUp(self):
        server._cache.update(end_ms=None, payload=None)

    def test_page_served(self):
        with urllib.request.urlopen(self.base + "/") as r:
            self.assertIn(b"Run the bot", r.read())

    def test_run_returns_full_result(self):
        req = urllib.request.Request(self.base + "/api/run", method="POST")
        with urllib.request.urlopen(req) as r:
            d = json.loads(r.read())
        self.assertEqual(d["window"]["candles"], 1440)
        self.assertEqual(len(d["closes"]), 1440)
        self.assertEqual(d["totals"]["trades"], len(d["trades"]))
        self.assertIn("ASSIGNMENT CHECKS", d["report"])
        self.assertAlmostEqual(d["totals"]["pnl"], sum(t["pnl"] for t in d["trades"]), places=6)
        self.assertFalse(d["cached"])
        with urllib.request.urlopen(req) as r:  # same minute -> cached, identical
            d2 = json.loads(r.read())
        if d2["window"]["end_ms"] == d["window"]["end_ms"]:
            self.assertTrue(d2["cached"])
            self.assertEqual(d2["totals"], d["totals"])

    def test_data_failure_is_reported(self):
        def broken(url):
            raise urllib.error.URLError("network down")
        server._http_get = broken
        try:
            req = urllib.request.Request(self.base + "/api/run", method="POST")
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(req)
            self.assertEqual(cm.exception.code, 502)
            self.assertIn("unavailable", json.loads(cm.exception.read())["error"])
        finally:
            server._http_get = fake_binance


if __name__ == "__main__":
    unittest.main()
