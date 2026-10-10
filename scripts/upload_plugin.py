"""Upload a built plugin zip to plugins.qgis.org with a per-plugin API token.

Standard-library only, like build_plugin.py. Create the token on the plugin's page on
plugins.qgis.org (Manage > Tokens) and pass it in the environment, never on the command
line, so it stays out of process listings and CI logs:

    QGIS_TOKEN=... python scripts/upload_plugin.py build/datacube_viewer-0.0.2.zip

This is the same endpoint qgis-plugin-ci's --qgis-token uses. The registry rejects a
version it has already seen, so a retried upload fails loudly instead of overwriting.
"""

from __future__ import annotations

import argparse
import os
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path

REGISTRY = "https://plugins.qgis.org"


def build_request(zip_path: Path, token: str) -> urllib.request.Request:
    # The URL slug is the archive's top-level directory (datacube_viewer), which is
    # what build_plugin.py guarantees and what qgis-plugin-ci uses as well.
    package = zip_path.name.rsplit("-", 1)[0]
    boundary = uuid.uuid4().hex
    body = (
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="package"; filename="{zip_path.name}"\r\n'
            "Content-Type: application/zip\r\n\r\n"
        ).encode()
        + zip_path.read_bytes()
        + f"\r\n--{boundary}--\r\n".encode()
    )
    return urllib.request.Request(
        f"{REGISTRY}/plugins/api/{package}/version/add/",
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            # The registry sits behind Cloudflare, which answers urllib's default
            # "Python-urllib" agent with HTTP 403 "error code: 1010" before the request
            # reaches the registry.
            "User-Agent": "datacube-viewer-upload/1.0 (+https://github.com/TUW-GEO/qgis-datacube-viewer)",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        method="POST",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("zip", type=Path, help="plugin archive from build_plugin.py")
    args = parser.parse_args()

    token = os.environ.get("QGIS_TOKEN")
    if not token:
        print("error: QGIS_TOKEN is not set", file=sys.stderr)
        return 2

    request = build_request(args.zip, token)
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            print(f"uploaded {args.zip.name} (HTTP {response.status}) -> {request.full_url}")
    except urllib.error.HTTPError as error:
        print(
            f"error: plugins.qgis.org answered HTTP {error.code}: {error.read().decode(errors='replace')[:500]}",
            file=sys.stderr,
        )
        return 1
    except urllib.error.URLError as error:
        print(f"error: could not reach plugins.qgis.org: {error.reason}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
