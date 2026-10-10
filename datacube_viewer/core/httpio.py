"""Blocking HTTP GET/HEAD primitives, standard library only.

These exist so that reading a Zarr store over ``http(s)://`` needs nothing that a
QGIS install does not already have. ``zarr`` routes every non-local URL through
``FsspecStore``, which needs ``fsspec`` **and** ``aiohttp``; neither ships with QGIS,
and telling someone to pip-install an async HTTP stack into the OSGeo4W shell before
they can open a public URL is a worse answer than the ~100 lines below.

Reading a Zarr store over HTTP only ever needs two operations: fetch a whole object,
and fetch a byte range of one. That is what this module provides.

Deliberate limitation: ``urllib`` opens a fresh connection per request, so there is no
keep-alive. For Zarr that cost is amortised over chunks measured in megabytes, and the
reader caches decoded blocks (see :mod:`datacube_viewer.core.reader`), so it is not
worth taking on a dependency — or hand-rolling a connection pool — to avoid it.

Only ``http://`` and ``https://`` are ever opened; see :data:`ALLOWED_SCHEMES`.
"""

from __future__ import annotations

import time
import urllib.error
import urllib.parse
import urllib.request

#: Sent on every request. Some CDNs serve a different (or no) body to a client that
#: does not identify itself.
USER_AGENT = "datacube-viewer"

#: Statuses that mean "this key is not in the store", as opposed to "the store is
#: broken". 403 is included because object stores fronted by HTTP commonly return it
#: for a missing key when anonymous listing is denied.
_MISSING_STATUS = frozenset({403, 404, 410})

#: Statuses worth retrying: transient server-side and rate-limit failures.
_RETRY_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})

DEFAULT_TIMEOUT = 30.0
DEFAULT_RETRIES = 2


class HttpError(OSError):
    """A request failed for a reason that is not "the key does not exist"."""


#: The only schemes these helpers will open. ``urllib`` also handles ``file:``,
#: ``ftp:`` and ``data:``, and a store URL reaches here straight from the Add Datacube
#: Layer dialog — so without this, entering ``file:///etc/shadow`` would turn "read a
#: remote store" into a local file read whose bytes surface in error text.
ALLOWED_SCHEMES = frozenset({"http", "https"})


def _scheme_of(url: str) -> str:
    return urllib.parse.urlsplit(url).scheme.lower()


class _SchemeRestrictedRedirects(urllib.request.HTTPRedirectHandler):
    """Re-check the scheme on every redirect hop.

    Checking only the URL the caller passed is not enough: ``urllib``'s default
    redirect handler permits http, https **and** ftp, so a 302 pointing at
    ``ftp://…`` would sail straight past a check done up front.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if _scheme_of(newurl) not in ALLOWED_SCHEMES:
            # Drain and close before raising, as urllib's own http_error_302 does on
            # the paths where it gives up: the redirect body is of no interest, and
            # leaving it to the garbage collector logs a ResourceWarning. fp=None on
            # the error for the same reason — nothing is going to read it.
            fp.read()
            fp.close()
            raise urllib.error.HTTPError(
                newurl, code, f"refusing to follow redirect to {newurl!r}", headers, None
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


#: Built once, at import. Going through an opener rather than the module-level
#: ``urllib.request.urlopen`` is what makes the redirect handler above take effect.
_OPENER = urllib.request.build_opener(_SchemeRestrictedRedirects)


def _request(url: str, method: str, headers: dict[str, str] | None) -> urllib.request.Request:
    """Build a request, refusing any scheme not in :data:`ALLOWED_SCHEMES`."""
    scheme = _scheme_of(url)
    if scheme not in ALLOWED_SCHEMES:
        supported = "/".join(sorted(ALLOWED_SCHEMES))
        raise HttpError(f"refusing to open {url!r}: only {supported} URLs are supported")
    request = urllib.request.Request(url, method=method)
    request.add_header("User-Agent", USER_AGENT)
    for name, value in (headers or {}).items():
        request.add_header(name, value)
    return request


def join_url(base: str, key: str) -> str:
    """Append a Zarr key to a base URL, percent-escaping only what must be escaped."""
    if not key:
        return base
    return f"{base.rstrip('/')}/{urllib.parse.quote(key.lstrip('/'), safe='/-_.~')}"


def range_header(start: int | None, end: int | None, suffix: int | None) -> str | None:
    """Build a ``Range`` header value. ``end`` is exclusive, as in Zarr's requests."""
    if suffix is not None:
        return f"bytes=-{int(suffix)}"
    if start is None:
        return None
    if end is None:
        return f"bytes={int(start)}-"
    if end <= start:
        return None
    return f"bytes={int(start)}-{int(end) - 1}"


def fetch(
    url: str,
    *,
    range_value: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    retries: int = DEFAULT_RETRIES,
    headers: dict[str, str] | None = None,
) -> tuple[bytes, bool] | None:
    """GET ``url``, optionally a byte range of it.

    Returns ``(body, ranged)`` — where ``ranged`` says whether the server honoured the
    ``Range`` header — or ``None`` when the object does not exist. A server that
    ignores ``Range`` answers 200 with the whole object, which is legal HTTP; the
    caller has to slice locally, so it needs to be told.

    Raises :class:`HttpError` for anything else, after ``retries`` attempts.
    """
    request = _request(url, "GET", headers)
    if range_value:
        request.add_header("Range", range_value)

    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            with _OPENER.open(request, timeout=timeout) as response:
                body = response.read()
                return body, response.status == 206
        except urllib.error.HTTPError as exc:
            if exc.code in _MISSING_STATUS:
                return None
            if exc.code == 416:
                # Range beyond the end of the object. Zarr treats this as an empty
                # read rather than an error, and so does fsspec.
                return b"", True
            last = exc
            if exc.code not in _RETRY_STATUS:
                break
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            last = exc
        if attempt < retries:
            time.sleep(0.25 * (attempt + 1))

    raise HttpError(f"GET {url} failed: {last}") from last


def exists(
    url: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    retries: int = DEFAULT_RETRIES,
    headers: dict[str, str] | None = None,
) -> bool:
    """True when ``url`` resolves to an object.

    HEAD first, falling back to a one-byte ranged GET: HEAD is cheapest, but plenty of
    servers answer it with 405 or 501 while serving GET perfectly well.
    """
    request = _request(url, "HEAD", headers)
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            return 200 <= response.status < 300
    except urllib.error.HTTPError as exc:
        if exc.code in _MISSING_STATUS:
            return False
        if exc.code not in {405, 501}:
            raise HttpError(f"HEAD {url} failed: {exc}") from exc
    except (urllib.error.URLError, TimeoutError, ConnectionError):
        pass  # fall through to GET, which retries

    result = fetch(url, range_value="bytes=0-0", timeout=timeout, retries=retries, headers=headers)
    return result is not None
