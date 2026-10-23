#!/usr/bin/env python3
"""A tiny read-only file server for browser-based genome viewers.

Lets web tools such as igv.js, running on an allowed website, read files
that the person running this script can already open on their own
computer (local disks and mounted network shares). It listens only on
127.0.0.1, serves only to allowed origins, and supports HTTP range
requests so viewers fetch just the parts of large indexed files they
need.

Files are addressed by their absolute path after /f, for example
    http://127.0.0.1:60152/f/Users/me/data/sample.bam
    http://127.0.0.1:60152/f/C:/data/sample.bam
    http://127.0.0.1:60152/f//server/share/data/sample.bam   (UNC path)

Standard library only; runs on Python 3.9 and later.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

__version__ = "0.1.0"

MIN_PYTHON = (3, 9)
BIND_ADDRESS = "127.0.0.1"  # never configurable: other machines must not reach this
CHUNK_SIZE = 256 * 1024
CONFIG_NAME = "config.json"
HERE = Path(__file__).resolve().parent

DEFAULT_CONFIG = {
    "port": 60152,
    "allowed_origins": ["https://*.mcri.edu.au"],
    "roots": [],
    "allowed_extensions": [
        ".bam", ".bai", ".cram", ".crai", ".csi", ".tbi", ".gzi",
        ".vcf", ".vcf.gz", ".bcf", ".bed", ".bed.gz", ".bedpe", ".narrowpeak",
        ".bw", ".bigwig", ".bb", ".bigbed", ".wig", ".bedgraph", ".tdf",
        ".seg", ".seg.gz", ".gff", ".gff3", ".gff3.gz", ".gtf", ".gtf.gz",
        ".fa", ".fasta", ".fa.gz", ".fasta.gz", ".fai", ".2bit",
    ],
}


# ---------------------------------------------------------------------------
# Configuration


class Config:
    def __init__(self, port: int, allowed_origins: list[str], roots: list[str],
                 allowed_extensions: list[str]) -> None:
        self.port = port
        self.allowed_origins = allowed_origins
        self.origin_patterns = [OriginPattern(p) for p in allowed_origins]
        self.roots = [os.path.realpath(os.path.expanduser(r)) for r in roots]
        self.allowed_extensions = [e.lower() for e in allowed_extensions]

    @classmethod
    def load(cls, path: Path) -> Config:
        data = dict(DEFAULT_CONFIG)
        if path.exists():
            with open(path, encoding="utf-8") as f:
                data.update(json.load(f))
        return cls(
            port=int(data["port"]),
            allowed_origins=list(data["allowed_origins"]),
            roots=list(data["roots"]),
            allowed_extensions=list(data["allowed_extensions"]),
        )

    def origin_allowed(self, origin: str) -> bool:
        return any(p.matches(origin) for p in self.origin_patterns)


class OriginPattern:
    """An allowed origin such as https://archie.example.org or
    https://*.example.org. A leading "*." matches one or more subdomain
    labels (but not the bare domain). With no port given, any port
    matches."""

    def __init__(self, pattern: str) -> None:
        parts = urlsplit(pattern)
        if not parts.scheme or not parts.hostname:
            raise ValueError(f"bad origin pattern: {pattern!r}")
        self.scheme = parts.scheme.lower()
        self.port = parts.port
        # urlsplit can't parse a "*" host, so take the host from netloc.
        host = parts.netloc.rsplit("@", 1)[-1]
        host = re.sub(r":\d+$", "", host).lower()
        self.wildcard = host.startswith("*.")
        self.host = host[2:] if self.wildcard else host

    def matches(self, origin: str) -> bool:
        try:
            parts = urlsplit(origin)
            port = parts.port
        except ValueError:
            return False
        if parts.scheme.lower() != self.scheme or not parts.hostname:
            return False
        if parts.path or parts.query or parts.fragment or parts.username:
            return False
        if self.port is not None and port != self.port:
            return False
        host = parts.hostname.lower()
        if self.wildcard:
            return host.endswith("." + self.host)
        return host == self.host


# ---------------------------------------------------------------------------
# Request helpers (pure functions, unit tested directly)


def url_path_to_file_path(url_path: str, windows: bool = os.name == "nt") -> str | None:
    """Turns the part of a request path after /f into a filesystem path."""
    path = unquote(url_path)
    if "\x00" in path or not path.startswith("/"):
        return None
    if windows:
        if re.match(r"^/[A-Za-z]:", path):  # /C:/data/x.bam -> C:/data/x.bam
            path = path[1:]
        elif not path.startswith("//"):  # Windows paths need a drive or a server
            return None
    return path


def parse_range(header: str, size: int) -> tuple[int, int] | None | str:
    """Parses a Range header for a file of the given size.

    Returns (start, end) inclusive for a single satisfiable range,
    None when the header should be ignored (serve the whole file), or
    "unsatisfiable" for a 416 response.
    """
    m = re.fullmatch(r"\s*bytes\s*=\s*(\d*)\s*-\s*(\d*)\s*", header)
    if not m:
        return None  # multiple ranges or unknown units: ignoring Range is allowed
    first, last = m.groups()
    if first == "" and last == "":
        return None
    if first == "":  # suffix range: the last N bytes
        length = int(last)
        if length == 0:
            return "unsatisfiable"
        return max(0, size - length), size - 1
    start = int(first)
    end = size - 1 if last == "" else min(int(last), size - 1)
    if last != "" and int(last) < start:
        return None  # syntactically invalid: ignore
    if start >= size:
        return "unsatisfiable"
    return start, end


def check_file(path: str, config: Config) -> tuple[HTTPStatus, str | None]:
    """Decides whether a resolved path may be served. Returns a status
    and, when it's OK, the real path to open."""
    real = os.path.realpath(path)
    parts = Path(real).parts
    if any(p.startswith(".") for p in parts[1:]):
        return HTTPStatus.FORBIDDEN, None  # hidden files and folders: ~/.ssh and friends
    if config.roots and not any(_inside(real, root) for root in config.roots):
        return HTTPStatus.FORBIDDEN, None
    name = os.path.basename(real).lower()
    if config.allowed_extensions and not any(name.endswith(e) for e in config.allowed_extensions):
        return HTTPStatus.FORBIDDEN, None
    if not os.path.isfile(real):
        return HTTPStatus.NOT_FOUND, None
    return HTTPStatus.OK, real


def _inside(path: str, root: str) -> bool:
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:  # different drives on Windows
        return False


# ---------------------------------------------------------------------------
# HTTP handler


class Handler(BaseHTTPRequestHandler):
    server_version = f"mini-server/{__version__}"
    protocol_version = "HTTP/1.1"  # keep-alive: viewers make many small range requests
    config: Config  # set on the subclass made by make_server

    # -- dispatch -----------------------------------------------------------

    def do_OPTIONS(self) -> None:
        if not self._check_host_and_origin(require_origin=True):
            return
        self.send_response(HTTPStatus.NO_CONTENT)
        self._send_cors_headers()
        self.send_header("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS")
        requested = self.headers.get("Access-Control-Request-Headers")
        if requested:
            self.send_header("Access-Control-Allow-Headers", requested)
        if self.headers.get("Access-Control-Request-Private-Network") == "true":
            self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        self._handle(send_body=True)

    def do_HEAD(self) -> None:
        self._handle(send_body=False)

    def _handle(self, send_body: bool) -> None:
        if not self._check_host_and_origin(require_origin=False):
            return
        path = urlsplit(self.path).path
        if path == "/health":
            self._send_json({"name": "mini-server", "version": __version__}, send_body)
        elif path in ("/", "/index.html"):
            self._send_static("index.html", "text/html; charset=utf-8", send_body)
        elif path.startswith("/f/"):
            self._send_file(path[2:], send_body)
        else:
            self._send_error(HTTPStatus.NOT_FOUND)

    # -- checks -------------------------------------------------------------

    def _check_host_and_origin(self, require_origin: bool) -> bool:
        # A Host other than our own address means DNS rebinding (some
        # website's name pointed at 127.0.0.1), so refuse it.
        host = (self.headers.get("Host") or "").lower()
        port = self.server.server_address[1]
        if host not in (f"127.0.0.1:{port}", f"localhost:{port}"):
            self._send_error(HTTPStatus.FORBIDDEN, "bad Host header")
            return False
        # Browsers send Origin on cross-origin requests. Without one this
        # is a same-origin request (our own test page), a typed-in URL or
        # a non-browser client, none of which another website can read.
        origin = self.headers.get("Origin")
        if origin is None:
            if require_origin:
                self._send_error(HTTPStatus.FORBIDDEN, "missing Origin")
                return False
            return True
        if not self.config.origin_allowed(origin):
            self._send_error(HTTPStatus.FORBIDDEN, "origin not allowed")
            return False
        return True

    # -- responses ----------------------------------------------------------

    def _send_cors_headers(self) -> None:
        origin = self.headers.get("Origin")
        if origin is not None:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Expose-Headers",
                             "Content-Length, Content-Range, Accept-Ranges")
        self.send_header("Vary", "Origin")

    def _send_file(self, url_path: str, send_body: bool) -> None:
        path = url_path_to_file_path(url_path)
        if path is None:
            self._send_error(HTTPStatus.BAD_REQUEST, "not an absolute path")
            return
        status, real = check_file(path, self.config)
        if real is None:
            self._send_error(status)
            return
        try:
            f = open(real, "rb")
        except OSError:
            self._send_error(HTTPStatus.FORBIDDEN, "cannot open file")
            return
        with f:
            size = os.fstat(f.fileno()).st_size
            rng = parse_range(self.headers.get("Range", ""), size)
            if rng == "unsatisfiable":
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self._send_cors_headers()
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if rng is None:
                start, end = 0, size - 1
                self.send_response(HTTPStatus.OK)
            else:
                start, end = rng
                self.send_response(HTTPStatus.PARTIAL_CONTENT)
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            length = max(0, end - start + 1)
            self._send_cors_headers()
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            if not send_body:
                return
            f.seek(start)
            remaining = length
            try:
                while remaining > 0:
                    chunk = f.read(min(CHUNK_SIZE, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass  # the viewer cancelled the request; normal for igv.js

    def _send_static(self, name: str, content_type: str, send_body: bool) -> None:
        body = (HERE / name).read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if send_body:
            self.wfile.write(body)

    def _send_json(self, data: object, send_body: bool) -> None:
        body = json.dumps(data).encode()
        self.send_response(HTTPStatus.OK)
        self._send_cors_headers()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if send_body:
            self.wfile.write(body)

    def _send_error(self, status: HTTPStatus, detail: str | None = None) -> None:
        body = f"{status.value} {status.phrase}{': ' + detail if detail else ''}\n".encode()
        self.send_response(status)
        self._send_cors_headers_if_allowed()
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_cors_headers_if_allowed(self) -> None:
        # Let an allowed site see why a request failed (404 vs 403), but
        # tell other sites nothing.
        origin = self.headers.get("Origin")
        if origin is not None and self.config.origin_allowed(origin):
            self._send_cors_headers()

    def log_message(self, format: str, *args: object) -> None:
        sys.stdout.write(f"{self.log_date_time_string()} {format % args}\n")
        sys.stdout.flush()


def make_server(config: Config, port: int | None = None) -> ThreadingHTTPServer:
    handler = type("ConfiguredHandler", (Handler,), {"config": config})
    server = ThreadingHTTPServer((BIND_ADDRESS, config.port if port is None else port), handler)
    server.daemon_threads = True
    return server


# ---------------------------------------------------------------------------
# Entry point


def main(argv: list[str] | None = None) -> int:
    if sys.version_info < MIN_PYTHON:
        print(f"mini-server needs Python {'.'.join(map(str, MIN_PYTHON))} or later; "
              f"this is {sys.version.split()[0]}.")
        return 1
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", type=Path, default=HERE / CONFIG_NAME,
                        help=f"configuration file (default: {CONFIG_NAME} next to this script)")
    parser.add_argument("--port", type=int, help="override the configured port")
    args = parser.parse_args(argv)

    config = Config.load(args.config)
    try:
        server = make_server(config, args.port)
    except OSError as e:
        print(f"Could not listen on port {args.port or config.port}: {e}")
        print("Is mini-server already running in another window?")
        return 1
    port = server.server_address[1]
    print(f"mini-server {__version__} is running at http://{BIND_ADDRESS}:{port}/")
    print("Allowed websites: " + ", ".join(config.allowed_origins))
    if config.roots:
        print("Serving only files under: " + ", ".join(config.roots))
    print("Leave this window open while you use it. Close it (or press Ctrl+C) to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
