#!/usr/bin/env python3
"""Upload a file to a GitLab generic package registry. Standard library only.

``python:3.12-slim`` -- the image the release job runs in -- ships neither ``curl``
nor ``wget`` nor ``git``. Reaching for any of them costs either an ``apt-get`` on
every release (needing a Debian mirror the runner may not reach) or a second job in
a second image just to make one HTTP request. Since the image is already a Python
image, the request is made in Python, which keeps the release to one job and matches
``build_plugin.py``: no dependency beyond the interpreter.

The endpoint is a plain PUT:

    PUT /projects/:id/packages/generic/:package/:version/:filename

Credentials come from the CI environment -- ``CI_JOB_TOKEN`` is scoped to the single
job and expires with it, so nothing long-lived is involved.

Usage::

    python scripts/upload_package.py --file build/x.zip --package geo_geozarr_viewer \
        --version 0.0.1 --as geo_geozarr_viewer.zip
"""

from __future__ import annotations

import argparse
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

#: GitLab answers 201 on first upload. A re-upload of an existing name+version
#: answers 200 when the project allows duplicates and 403/409 when it does not --
#: which is the "a published version cannot be reused" rule showing up as an HTTP
#: status rather than as a silently overwritten package.
OK_STATUSES = (200, 201)

TIMEOUT_SECONDS = 120


def upload(
    path: Path,
    *,
    api_url: str,
    project_id: str,
    package: str,
    version: str,
    token: str,
    filename: str | None = None,
) -> str:
    """PUT ``path`` into the registry and return the URL it now lives at."""
    name = filename or path.name
    url = f"{api_url}/projects/{project_id}/packages/generic/{package}/{version}/{name}"
    payload = path.read_bytes()

    request = urllib.request.Request(
        url,
        data=payload,
        method="PUT",
        headers={
            "JOB-TOKEN": token,
            # The registry stores bytes verbatim; naming a more specific type would
            # only risk a mismatch with what the download serves.
            "Content-Type": "application/octet-stream",
            "Content-Length": str(len(payload)),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            status, body = response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        # The body carries GitLab's reason ("insufficient_permissions", a duplicate
        # rejection, ...). Printing it is the difference between a fixable error and
        # a bare 403.
        detail = error.read().decode("utf-8", "replace").strip()
        raise SystemExit(
            f"Upload failed: HTTP {error.code} {error.reason}\n  {url}\n  {detail or '(no body)'}"
        ) from error
    except urllib.error.URLError as error:
        raise SystemExit(f"Upload failed: could not reach {api_url}\n  {error.reason}") from error

    if status not in OK_STATUSES:
        raise SystemExit(f"Upload failed: unexpected HTTP {status}\n  {body}")
    print(f"uploaded {path.name} ({len(payload) / 1024:.0f} KiB) -> {url}")
    return url


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--file", type=Path, required=True, help="file to upload")
    parser.add_argument("--package", required=True, help="generic package name")
    parser.add_argument("--version", required=True, help="package version")
    parser.add_argument("--as", dest="filename", default=None, help="name in the registry")
    # Defaults come from the CI environment so the command line stays short in
    # .gitlab-ci.yml, but every one can be overridden to test against a stub.
    parser.add_argument("--api-url", default=os.environ.get("CI_API_V4_URL", ""))
    parser.add_argument("--project-id", default=os.environ.get("CI_PROJECT_ID", ""))
    parser.add_argument("--token", default=os.environ.get("CI_JOB_TOKEN", ""))
    args = parser.parse_args(argv)

    if not args.file.is_file():
        raise SystemExit(f"No such file: {args.file}")
    missing = [
        flag
        for flag, value in (
            ("--api-url (or $CI_API_V4_URL)", args.api_url),
            ("--project-id (or $CI_PROJECT_ID)", args.project_id),
            ("--token (or $CI_JOB_TOKEN)", args.token),
        )
        if not value
    ]
    if missing:
        raise SystemExit("Missing required values: " + ", ".join(missing))

    upload(
        args.file,
        api_url=args.api_url.rstrip("/"),
        project_id=args.project_id,
        package=args.package,
        version=args.version,
        token=args.token,
        filename=args.filename,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
