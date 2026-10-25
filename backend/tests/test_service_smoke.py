"""End-to-end smoke test: boot the private sidecar and exercise its contract.

Spawns backend/service.py exactly the way the Tauri host does (handshake over
stdin with a one-time secret and an isolated home), then verifies the runtime
invariants that VALIDATION.md checks by hand:

  * the service reports a ready port on stdout,
  * /api/ping answers an authenticated request with the app version,
  * missing, wrong or cross-origin requests are rejected with 403,
  * a shutdown line exits the process cleanly with code 0.

The scan_on_start default is disabled via config.json so the test never reads
the machine's real tool logs.
"""
from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.request

BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, BACKEND)


class ServiceSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.home = tempfile.TemporaryDirectory()
        with open(os.path.join(cls.home.name, "config.json"), "w", encoding="utf-8") as f:
            json.dump({"scan_on_start": False}, f)
        cls.token = secrets.token_hex(32)
        cls.proc = subprocess.Popen(
            [sys.executable, os.path.join(BACKEND, "service.py")],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=BACKEND,
        )
        handshake = json.dumps({"token": cls.token, "home": cls.home.name})
        cls.proc.stdin.write((handshake + "\n").encode("utf-8"))
        cls.proc.stdin.flush()
        ready_line = cls.proc.stdout.readline().decode("utf-8")
        try:
            ready = json.loads(ready_line)
        except ValueError:
            ready = {}
        if not ready.get("ready") or "port" not in ready:
            cls.tearDownClass()
            raise AssertionError("service did not report ready: %r" % ready_line)
        cls.base = "http://127.0.0.1:%d" % ready["port"]

    @classmethod
    def tearDownClass(cls):
        proc = getattr(cls, "proc", None)
        if proc is None:
            return
        try:
            if proc.poll() is None:
                proc.stdin.write(b'{"shutdown": true}\n')
                proc.stdin.flush()
                # A clean exit must not need the kill path.
                code = proc.wait(timeout=15)
                assert code == 0, "service exited with %r on shutdown" % code
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                try:
                    stream.close()
                except OSError:
                    pass
            cls.home.cleanup()

    def request(self, path, token=None, extra_headers=None):
        req = urllib.request.Request(self.base + path)
        if token is not None:
            req.add_header("X-Kjuan-Secret", token)
        for key, value in (extra_headers or {}).items():
            req.add_header(key, value)
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode("utf-8"))

    def test_ping_authenticated(self):
        from tokenlens.config import APP_NAME, APP_VERSION
        status, body = self.request("/api/ping", token=self.token)
        self.assertEqual(status, 200)
        self.assertTrue(body.get("ok"))
        self.assertEqual(body.get("app"), APP_NAME)
        self.assertEqual(body.get("version"), APP_VERSION)

    def test_missing_secret_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.request("/api/ping")
        self.assertEqual(ctx.exception.code, 403)

    def test_wrong_secret_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.request("/api/ping", token=secrets.token_hex(32))
        self.assertEqual(ctx.exception.code, 403)

    def test_cross_origin_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.request("/api/ping", token=self.token,
                         extra_headers={"Origin": "https://example.com"})
        self.assertEqual(ctx.exception.code, 403)


if __name__ == "__main__":
    unittest.main()
