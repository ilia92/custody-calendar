"""End-to-end tests for the Custody Calendar API.

Run:  python3 -m unittest discover tests -v
"""

import http.cookiejar
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request

sys.argv = sys.argv[:1]          # server.py reads a port from argv[1] at import time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server  # noqa: E402

TOKEN = "script-token-xyz"


class ApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        server.CONFIG_PATH = os.path.join(self.tmp, "config.json")
        server.DATA_PATH = os.path.join(self.tmp, "data.json")
        server.HOLIDAYS_PATH = os.path.join(self.tmp, "holidays.json")
        with open(server.CONFIG_PATH, "w") as f:
            json.dump({"parents": {
                "mum": {"username": "anna", "display_name": "Anna", "password_hash": server.hash_password("pw-mum")},
                "dad": {"username": "martin", "display_name": "Martin", "password_hash": server.hash_password("pw-dad")},
            }, "api_tokens": {"bot": {"hash": server.hash_password(TOKEN), "role": "dad"}}}, f)
        server.CONFIG.clear()
        server.CONFIG.update(server.load_config())
        server._config_mtime = server._config_mtime_now()
        server._change_log.clear()
        server._holiday_mem.update(mtime=None, cache={})
        server.load_data()
        self.httpd = server.ThreadedHTTPServer(("127.0.0.1", 0), server.Handler)
        server.Handler.log_message = lambda *a: None
        self.base = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.mum = self.login("anna", "pw-mum")
        self.dad = self.login("martin", "pw-dad")

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        shutil.rmtree(self.tmp)

    def login(self, user, pw):
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        opener.open(self.base + "/do-login", urllib.parse.urlencode({"username": user, "password": pw}).encode()).read()
        return opener

    def call(self, who, path, body=None, token=None):
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode() if body is not None else None)
        req.add_header("Content-Type", "application/json")
        if token:
            req.add_header("Authorization", "Bearer " + token)
        opener = who or urllib.request.build_opener()
        try:
            with opener.open(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def day(self, who, iso, changes, **extra):
        return self.call(who, "/api/day", dict({"date": iso, "changes": changes}, **extra))

    def rev(self):
        return self.call(self.mum, "/api/rev")[1]["rev"]

    def stored(self):
        with open(server.DATA_PATH) as f:
            return json.load(f)

    # ── auth & permissions ────────────────────────────────────────────────────
    def test_requires_auth(self):
        self.assertEqual(self.call(None, "/api/changes?since=0")[0], 401)
        self.assertEqual(self.call(None, "/api/day", {"date": "2026-10-01"})[0], 401)
        self.assertEqual(self.call(None, "/api/state", token="wrong")[0], 401)

    def test_own_flags_only(self):
        self.assertEqual(self.day(self.mum, "2026-10-01", {"off": {"dad": True}})[0], 403)
        self.assertEqual(self.day(self.mum, "2026-10-01", {"lock": {"dad": True}})[0], 403)
        self.assertEqual(self.day(self.mum, "2026-10-01", {"off": {"mum": True}})[0], 200)

    def test_lock_freezes_day_for_everyone(self):
        self.call(self.mum, "/api/lock", {"date": "2026-10-02", "locked": True})
        status, res = self.day(self.dad, "2026-10-02", {"day": {"dad": True}})
        self.assertEqual(status, 409)
        self.assertIn("Anna", res["error"])
        self.assertEqual(self.day(self.mum, "2026-10-02", {"day": {"mum": True}})[0], 409)
        self.assertEqual(self.call(self.dad, "/api/lock", {"date": "2026-10-02", "locked": True})[0], 200)
        self.call(self.mum, "/api/lock", {"date": "2026-10-02", "locked": False})
        self.assertTrue(self.stored()["days"]["2026-10-02"]["lock"]["dad"])

    # ── merging ───────────────────────────────────────────────────────────────
    def test_different_slots_merge(self):
        # both parents edit the same day from the same starting point - no conflict any more
        self.assertEqual(self.day(self.mum, "2026-10-03", {"day": {"mum": True}})[0], 200)
        self.assertEqual(self.day(self.dad, "2026-10-03", {"night": {"dad": True}})[0], 200)
        d = self.stored()["days"]["2026-10-03"]
        self.assertTrue(d["day"]["mum"] and d["night"]["dad"])

    def test_base_rev_still_guards_old_clients(self):
        self.day(self.mum, "2026-10-04", {"day": {"mum": True}})
        self.assertEqual(self.day(self.dad, "2026-10-04", {"night": {"dad": True}}, base_rev=0)[0], 409)

    def test_note_conflict(self):
        self.assertEqual(self.day(self.dad, "2026-10-05", {"note": "pick up 17:00"}, note_base="")[0], 200)
        status, res = self.day(self.mum, "2026-10-05", {"note": "pick up 18:00"}, note_base="")
        self.assertEqual(status, 409)
        self.assertEqual(res["conflict"], "note")
        self.assertEqual(res["day"]["note"], "pick up 17:00")
        # writing the same text, or from the right base, is fine
        self.assertEqual(self.day(self.mum, "2026-10-05", {"note": "pick up 17:00"}, note_base="")[0], 200)
        self.assertEqual(self.day(self.mum, "2026-10-05", {"note": "18:00"}, note_base="pick up 17:00")[0], 200)
        self.assertEqual(self.stored()["days"]["2026-10-05"]["note"], "18:00")

    # ── live sync ─────────────────────────────────────────────────────────────
    def test_changes_incremental(self):
        r0 = self.rev()
        self.day(self.mum, "2026-10-06", {"day": {"mum": True}})
        self.day(self.dad, "2026-10-07", {"night": {"dad": True}})
        self.day(self.dad, "2026-10-07", {"night": {"dad": False}})     # back to blank → removed
        status, res = self.call(self.mum, "/api/changes?since=%d" % r0)
        self.assertEqual(status, 200)
        self.assertEqual((res["since"], res["rev"]), (r0, r0 + 3))
        self.assertTrue(res["days"]["2026-10-06"]["day"]["mum"])
        self.assertIsNone(res["days"]["2026-10-07"])
        self.assertEqual(self.call(self.mum, "/api/changes?since=%d" % (r0 + 3))[1]["days"], {})

    def test_changes_reset_after_restart(self):
        self.day(self.mum, "2026-10-08", {"day": {"mum": True}})
        server._change_log.clear()                                       # what a restart looks like
        res = self.call(self.mum, "/api/changes?since=0")[1]
        self.assertTrue(res["reset"])
        self.assertIn("2026-10-08", res["days"])
        self.assertTrue(self.call(self.mum, "/api/changes?since=999")[1]["reset"])

    def test_long_poll(self):
        r0 = self.rev()
        threading.Timer(0.3, lambda: self.day(self.dad, "2026-10-09", {"day": {"dad": True}})).start()
        t0 = time.monotonic()
        res = self.call(self.mum, "/api/changes?since=%d&wait=10" % r0)[1]
        self.assertLess(time.monotonic() - t0, 5)
        self.assertTrue(res["days"]["2026-10-09"]["day"]["dad"])
        t0 = time.monotonic()
        res = self.call(self.mum, "/api/changes?since=%d&wait=1" % res["rev"])[1]
        self.assertGreaterEqual(time.monotonic() - t0, 0.9)
        self.assertEqual(res["days"], {})

    def test_range_feeds_changes(self):
        r0 = self.rev()
        self.call(self.mum, "/api/lock", {"date": "2026-11-03", "locked": True})
        status, res = self.call(self.dad, "/api/range", {"from": "2026-11-02", "to": "2026-11-04",
                                                          "changes": {"day": {"dad": True}}})
        self.assertEqual(status, 200)
        self.assertEqual(res["skipped_locked"], ["2026-11-03"])
        feed = self.call(self.mum, "/api/changes?since=%d" % r0)[1]
        self.assertEqual(set(feed["days"]), {"2026-11-02", "2026-11-03", "2026-11-04"})
        self.assertEqual(self.call(self.dad, "/api/range", {"from": "2026-02-30", "to": "2026-03-02",
                                                            "changes": {}})[0], 400)

    # ── batch ─────────────────────────────────────────────────────────────────
    def test_batch_atomic(self):
        r0 = self.rev()
        status, res = self.call(self.mum, "/api/batch", {"ops": [
            {"op": "day", "date": "2026-12-01", "changes": {"day": {"mum": True}}},
            {"op": "day", "date": "2026-12-02", "changes": {"off": {"dad": True}}},   # not allowed
        ]})
        self.assertEqual(status, 403)
        self.assertIn("ops[1]", res["error"])
        self.assertEqual(self.rev(), r0)
        self.assertNotIn("2026-12-01", self.call(self.mum, "/api/state")[1]["days"])

        status, res = self.call(self.mum, "/api/batch", {"ops": [
            {"op": "day", "date": "2026-12-01", "changes": {"day": {"mum": True}, "night": {"mum": True}}},
            {"op": "day", "date": "2026-12-02", "changes": {"day": {"dad": True}}},
            {"op": "lock", "date": "2026-12-02", "locked": True},
        ]})
        self.assertEqual(status, 200, res)
        self.assertEqual((res["since"], res["rev"]), (r0, r0 + 3))
        self.assertTrue(res["days"]["2026-12-02"]["lock"]["mum"])
        self.assertEqual(set(self.call(self.dad, "/api/changes?since=%d" % r0)[1]["days"]),
                         {"2026-12-01", "2026-12-02"})

    def test_batch_rolls_back_on_lock(self):
        self.call(self.dad, "/api/lock", {"date": "2026-12-05", "locked": True})
        r0 = self.rev()
        status, _ = self.call(self.mum, "/api/batch", {"ops": [
            {"op": "day", "date": "2026-12-04", "changes": {"day": {"mum": True}}},
            {"op": "day", "date": "2026-12-05", "changes": {"day": {"mum": True}}},
        ]})
        self.assertEqual(status, 409)
        self.assertEqual(self.rev(), r0)
        self.assertNotIn("2026-12-04", self.stored()["days"])

    # ── tokens & reads ────────────────────────────────────────────────────────
    def test_token_acts_as_its_parent(self):
        status, res = self.call(None, "/api/day", {"date": "2026-10-10", "changes": {"off": {"dad": True}}}, token=TOKEN)
        self.assertEqual(status, 200)
        self.assertEqual(res["day"]["log"][-1]["by"], "bot (token)")
        self.assertEqual(res["day"]["log"][-1]["role"], "dad")
        self.assertEqual(self.call(None, "/api/day", {"date": "2026-10-10", "changes": {"off": {"mum": True}}},
                                   token=TOKEN)[0], 403)
        self.assertEqual(self.call(None, "/api/account", {"current_password": "pw-dad", "display_name": "X"},
                                   token=TOKEN)[0], 403)

    def test_days_range_read(self):
        self.day(self.mum, "2026-10-11", {"day": {"mum": True}})
        self.day(self.mum, "2026-10-20", {"day": {"mum": True}})
        res = self.call(None, "/api/days?from=2026-10-01&to=2026-10-15", token=TOKEN)[1]
        self.assertEqual(list(res["days"]), ["2026-10-11"])

    def test_persisted_atomically(self):
        self.day(self.mum, "2026-10-12", {"day": {"mum": True}, "note": "hi"}, note_base="")
        self.assertEqual(self.stored()["days"]["2026-10-12"]["note"], "hi")
        self.assertFalse(os.path.exists(server.DATA_PATH + ".tmp"))
        server.load_data()
        self.assertEqual(server._data["days"]["2026-10-12"]["note"], "hi")


if __name__ == "__main__":
    unittest.main()
