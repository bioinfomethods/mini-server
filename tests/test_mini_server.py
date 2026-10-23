"""Tests for mini_server. Standard library only: python -m unittest -v"""

from __future__ import annotations

import http.client
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mini_server  # noqa: E402
from mini_server import Config, OriginPattern, parse_range, url_path_to_file_path  # noqa: E402

ALLOWED = "https://archie.example.org"


class OriginPatternTest(unittest.TestCase):
    def test_wildcard_matches_subdomains_on_any_port(self) -> None:
        p = OriginPattern("https://*.example.org")
        self.assertTrue(p.matches("https://archie.example.org"))
        self.assertTrue(p.matches("https://a.b.example.org"))
        self.assertTrue(p.matches("https://archie.example.org:8443"))
        self.assertTrue(p.matches("https://ARCHIE.Example.Org"))

    def test_wildcard_rejects_lookalikes(self) -> None:
        p = OriginPattern("https://*.example.org")
        self.assertFalse(p.matches("https://example.org"))
        self.assertFalse(p.matches("https://evilexample.org"))
        self.assertFalse(p.matches("https://example.org.evil.com"))
        self.assertFalse(p.matches("http://archie.example.org"))
        self.assertFalse(p.matches("null"))
        self.assertFalse(p.matches("https://archie.example.org/path"))
        self.assertFalse(p.matches("https://archie.example.org:notaport"))

    def test_exact_with_port(self) -> None:
        p = OriginPattern("http://localhost:5173")
        self.assertTrue(p.matches("http://localhost:5173"))
        self.assertFalse(p.matches("http://localhost:5174"))
        self.assertFalse(p.matches("http://sub.localhost:5173"))


class ParseRangeTest(unittest.TestCase):
    def test_ranges(self) -> None:
        self.assertEqual(parse_range("bytes=0-9", 100), (0, 9))
        self.assertEqual(parse_range("bytes=90-", 100), (90, 99))
        self.assertEqual(parse_range("bytes=90-200", 100), (90, 99))
        self.assertEqual(parse_range("bytes=-10", 100), (90, 99))
        self.assertEqual(parse_range("bytes=-200", 100), (0, 99))

    def test_unsatisfiable(self) -> None:
        self.assertEqual(parse_range("bytes=100-", 100), "unsatisfiable")
        self.assertEqual(parse_range("bytes=-0", 100), "unsatisfiable")
        self.assertEqual(parse_range("bytes=0-0", 0), "unsatisfiable")

    def test_ignored(self) -> None:
        self.assertIsNone(parse_range("", 100))
        self.assertIsNone(parse_range("bytes=0-1,5-6", 100))
        self.assertIsNone(parse_range("items=0-1", 100))
        self.assertIsNone(parse_range("bytes=9-1", 100))
        self.assertIsNone(parse_range("bytes=-", 100))


class UrlPathTest(unittest.TestCase):
    def test_posix(self) -> None:
        self.assertEqual(url_path_to_file_path("/Users/me/a%20b.bam", windows=False),
                         "/Users/me/a b.bam")
        self.assertIsNone(url_path_to_file_path("relative.bam", windows=False))
        self.assertIsNone(url_path_to_file_path("/a%00b", windows=False))

    def test_windows(self) -> None:
        self.assertEqual(url_path_to_file_path("/C:/data/x.bam", windows=True), "C:/data/x.bam")
        self.assertEqual(url_path_to_file_path("/C%3A/data/x.bam", windows=True), "C:/data/x.bam")
        self.assertEqual(url_path_to_file_path("//server/share/x.bam", windows=True),
                         "//server/share/x.bam")
        self.assertIsNone(url_path_to_file_path("/data/x.bam", windows=True))


class ServerTest(unittest.TestCase):
    """Runs a real server on a free port against files in a temp dir."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        # realpath: on macOS the temp dir is under a /var -> /private/var symlink
        root = Path(os.path.realpath(cls.tmp.name))
        cls.root = root
        (root / "data").mkdir()
        cls.content = bytes(range(256)) * 4  # 1024 bytes
        (root / "data" / "sample.bam").write_bytes(cls.content)
        (root / "data" / "notes.txt").write_text("secret")
        (root / ".hidden").mkdir()
        (root / ".hidden" / "x.bam").write_bytes(b"hidden")
        (root / "outside.bam").write_bytes(b"outside")
        config = Config(
            port=0,
            allowed_origins=[ALLOWED],
            roots=[str(root / "data")],
            allowed_extensions=list(mini_server.DEFAULT_CONFIG["allowed_extensions"]),
        )
        cls.server = mini_server.make_server(config)
        cls.server.RequestHandlerClass.log_message = lambda *args: None
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def request(self, method: str, path: str, headers: dict[str, str] | None = None,
                host: str | None = None) -> tuple[int, dict[str, str], bytes]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            all_headers = {"Host": host or f"127.0.0.1:{self.port}"}
            all_headers.update(headers or {})
            conn.putrequest(method, path, skip_host=True)
            for k, v in all_headers.items():
                conn.putheader(k, v)
            conn.endheaders()
            res = conn.getresponse()
            return res.status, {k.lower(): v for k, v in res.getheaders()}, res.read()
        finally:
            conn.close()

    def file_path(self, path: Path | str) -> str:
        posix = Path(path).as_posix() if isinstance(path, Path) else path
        if not posix.startswith("/"):  # Windows: C:/... -> /C:/...
            posix = "/" + posix
        return "/f" + quote(posix)

    def test_health(self) -> None:
        status, headers, body = self.request("GET", "/health", {"Origin": ALLOWED})
        self.assertEqual(status, 200)
        self.assertEqual(headers["access-control-allow-origin"], ALLOWED)
        self.assertIn(b"mini-server", body)

    def test_test_page(self) -> None:
        status, _, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"igv", body)

    def test_whole_file(self) -> None:
        status, headers, body = self.request(
            "GET", self.file_path(self.root / "data" / "sample.bam"), {"Origin": ALLOWED})
        self.assertEqual(status, 200)
        self.assertEqual(body, self.content)
        self.assertEqual(headers["accept-ranges"], "bytes")
        self.assertEqual(headers["access-control-allow-origin"], ALLOWED)

    def test_range(self) -> None:
        status, headers, body = self.request(
            "GET", self.file_path(self.root / "data" / "sample.bam"),
            {"Origin": ALLOWED, "Range": "bytes=10-19"})
        self.assertEqual(status, 206)
        self.assertEqual(body, self.content[10:20])
        self.assertEqual(headers["content-range"], "bytes 10-19/1024")
        self.assertIn("Content-Range", headers["access-control-expose-headers"])

    def test_range_unsatisfiable(self) -> None:
        status, headers, _ = self.request(
            "GET", self.file_path(self.root / "data" / "sample.bam"), {"Range": "bytes=5000-"})
        self.assertEqual(status, 416)
        self.assertEqual(headers["content-range"], "bytes */1024")

    def test_head(self) -> None:
        status, headers, body = self.request(
            "HEAD", self.file_path(self.root / "data" / "sample.bam"))
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-length"], "1024")
        self.assertEqual(body, b"")

    def test_disallowed_origin(self) -> None:
        status, headers, _ = self.request(
            "GET", self.file_path(self.root / "data" / "sample.bam"),
            {"Origin": "https://evil.example.com"})
        self.assertEqual(status, 403)
        self.assertNotIn("access-control-allow-origin", headers)

    def test_null_origin(self) -> None:
        status, _, _ = self.request(
            "GET", self.file_path(self.root / "data" / "sample.bam"), {"Origin": "null"})
        self.assertEqual(status, 403)

    def test_dns_rebinding_host(self) -> None:
        status, _, _ = self.request("GET", "/health", host=f"evil.example.com:{self.port}")
        self.assertEqual(status, 403)

    def test_outside_roots(self) -> None:
        status, _, _ = self.request("GET", self.file_path(self.root / "outside.bam"))
        self.assertEqual(status, 403)

    def test_traversal(self) -> None:
        status, _, _ = self.request(
            "GET", self.file_path((self.root / "data").as_posix() + "/../outside.bam"))
        self.assertEqual(status, 403)

    def test_hidden(self) -> None:
        status, _, _ = self.request("GET", self.file_path(self.root / ".hidden" / "x.bam"))
        self.assertEqual(status, 403)

    def test_extension_not_allowed(self) -> None:
        status, _, _ = self.request("GET", self.file_path(self.root / "data" / "notes.txt"))
        self.assertEqual(status, 403)

    def test_missing(self) -> None:
        status, headers, _ = self.request(
            "GET", self.file_path(self.root / "data" / "missing.bam"), {"Origin": ALLOWED})
        self.assertEqual(status, 404)
        self.assertEqual(headers["access-control-allow-origin"], ALLOWED)

    def test_preflight(self) -> None:
        status, headers, _ = self.request("OPTIONS", self.file_path(self.root / "data" / "sample.bam"), {
            "Origin": ALLOWED,
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "range",
            "Access-Control-Request-Private-Network": "true",
        })
        self.assertEqual(status, 204)
        self.assertEqual(headers["access-control-allow-origin"], ALLOWED)
        self.assertEqual(headers["access-control-allow-headers"], "range")
        self.assertEqual(headers["access-control-allow-private-network"], "true")

    def test_preflight_disallowed(self) -> None:
        status, _, _ = self.request("OPTIONS", "/health", {
            "Origin": "https://evil.example.com", "Access-Control-Request-Method": "GET"})
        self.assertEqual(status, 403)


if __name__ == "__main__":
    unittest.main()
