# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Serve a local Zarr store over HTTP for browser-based viewers.

Browser viewers such as https://source-cooperative.github.io/zarr-viewer/ fetch
the store straight from the page, so the server has to send CORS headers. It
also has to answer byte-range requests, because Zarr v3 stores with the
sharding codec read inner chunks with ``Range: bytes=...``. Python's built-in
``http.server`` does neither, so this script adds both. It uses only the
standard library.

The store's parent directory is served, so the store is available at
``http://<host>:<port>/<store name>``. That URL is printed on startup; paste it
into the viewer.

Chrome and Firefox allow an HTTPS page to read from ``http://127.0.0.1``, but
Chrome/Edge gate it behind the "Local network access" site permission: until
that is allowed for the viewer's site, every request fails with
``net::ERR_FAILED`` (the viewer reports it as a slow connection). Allow it via
the site-info icon in the address bar -> Site settings. Safari blocks such
requests outright, so use an HTTPS tunnel there.

Usage::

    uv run --script scripts/serve_zarr.py dummy.zarr
    uv run --script scripts/serve_zarr.py dem.zarr --port 9000
    uv run --script scripts/serve_zarr.py dem.zarr --host 0.0.0.0
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from functools import partial
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote

_RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")
_COPY_BUFSIZE = 1024 * 1024
_DEFAULT_PORT = 8080
_PORT_ATTEMPTS = 20


class ZarrRequestHandler(SimpleHTTPRequestHandler):
    """Static file handler with CORS, single byte-range support and no caching."""

    def end_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header(
            "Access-Control-Expose-Headers",
            "Content-Length, Content-Range, Accept-Ranges, ETag",
        )
        # Chrome's Private/Local Network Access preflight for public -> local requests.
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        range_header = self.headers.get("Range")
        if range_header is None:
            super().do_GET()
            return
        self._send_range(range_header, include_body=True)

    def do_HEAD(self) -> None:
        range_header = self.headers.get("Range")
        if range_header is None:
            super().do_HEAD()
            return
        self._send_range(range_header, include_body=False)

    def _send_range(self, range_header: str, *, include_body: bool) -> None:
        path = Path(self.translate_path(self.path))
        if not path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND, "File not found")
            return

        size = path.stat().st_size
        span = _parse_range(range_header, size)
        if span is None:
            self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
            self.send_header("Content-Range", f"bytes */{size}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        start, end = span
        length = end - start + 1
        self.send_response(HTTPStatus.PARTIAL_CONTENT)
        self.send_header("Content-Type", self.guess_type(str(path)))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(length))
        self.end_headers()
        if not include_body:
            return

        with path.open("rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(_COPY_BUFSIZE, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def send_response(self, code: int, message: str | None = None) -> None:
        super().send_response(code, message)
        # Advertise range support on full responses too.
        if code == HTTPStatus.OK:
            self.send_header("Accept-Ranges", "bytes")


class _Server(ThreadingHTTPServer):
    # On Windows, SO_REUSEADDR lets a second process bind a port that is already
    # in use, so a busy port would be "shared" instead of reported.
    allow_reuse_address = sys.platform != "win32"


def _parse_range(header: str, size: int) -> tuple[int, int] | None:
    """Parse a single ``bytes=`` range into an inclusive ``(start, end)``.

    Returns ``None`` when the range is malformed or not satisfiable. Multi-range
    requests are not supported and are treated as unsatisfiable.
    """
    match = _RANGE_RE.match(header.strip())
    if match is None or size == 0:
        return None
    first, last = match.groups()
    if first == "" and last == "":
        return None
    if first == "":
        # Suffix range: the last N bytes.
        suffix = int(last)
        if suffix == 0:
            return None
        return max(size - suffix, 0), size - 1
    start = int(first)
    end = size - 1 if last == "" else min(int(last), size - 1)
    if start > end:
        return None
    return start, end


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("store", type=Path, help="Path to the .zarr store directory.")
    parser.add_argument(
        "--host", default="127.0.0.1", help="Interface to bind (default: 127.0.0.1)."
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help=f"Port to listen on (default: first free port from {_DEFAULT_PORT}).",
    )
    args = parser.parse_args(argv)

    store: Path = args.store.resolve()
    if not store.is_dir():
        parser.error(f"{store} is not a directory")
    if not any((store / name).is_file() for name in ("zarr.json", ".zgroup", ".zarray")):
        print(f"warning: {store} has no zarr.json, .zgroup or .zarray at its root", file=sys.stderr)

    handler = partial(ZarrRequestHandler, directory=os.fspath(store.parent))
    ports = (
        [args.port]
        if args.port is not None
        else range(_DEFAULT_PORT, _DEFAULT_PORT + _PORT_ATTEMPTS)
    )
    server = None
    for port in ports:
        try:
            server = _Server((args.host, port), handler)
            break
        except OSError as exc:
            print(f"Port {port} is not available ({exc.strerror}).", file=sys.stderr)
    if server is None:
        hint = "Pick another --port." if args.port is not None else "Pass a free one with --port."
        print(f"error: could not bind to {args.host}. {hint}", file=sys.stderr)
        return 1

    # Not "localhost": browsers try ::1 first, and since the server listens on IPv4
    # only, every connection would wait for that attempt to fail.
    display_host = "127.0.0.1" if args.host in ("127.0.0.1", "0.0.0.0") else args.host
    url = f"http://{display_host}:{server.server_address[1]}/{quote(store.name)}"
    print(f"Serving {store}")
    print(f"Store URL: {url}")
    print(
        "Chrome/Edge: allow 'Local network access' for the viewer site "
        "(site-info icon in the address bar -> Site settings)."
    )
    print("Press Ctrl+C to stop.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
