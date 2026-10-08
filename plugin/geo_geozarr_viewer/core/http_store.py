"""A read-only Zarr store backed by plain HTTP(S).

``zarr.open_group("https://…")`` builds an :class:`~zarr.storage.FsspecStore`, which
imports ``fsspec`` and ``aiohttp``. QGIS ships neither, so on a stock install that call
fails with ``ModuleNotFoundError: No module named 'fsspec'`` before a single byte is
requested. This store replaces that path with :mod:`urllib`, so a public URL works with
nothing installed beyond ``zarr`` itself.

**An HTTP server cannot be listed.** There is no reliable directory index behind a
plain URL — an HTML autoindex is a rendering, not an API — so :attr:`supports_listing`
is False and the store depends on the hierarchy being discoverable from
``zarr.json``/``.zmetadata`` alone, i.e. on **consolidated metadata**. Every key Zarr
then asks for is one it learned from that document, so it is only ever a GET.
:func:`geo_geozarr_viewer.core.store.open_store` checks for consolidated metadata up front
and says so plainly rather than letting the store look mysteriously empty.

This module imports ``zarr`` at import time and must therefore only be imported from
inside a function, after ``zarr``'s availability has been established.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterable

from zarr.abc.store import (
    ByteRequest,
    OffsetByteRequest,
    RangeByteRequest,
    Store,
    SuffixByteRequest,
)
from zarr.core.buffer import Buffer, BufferPrototype

from . import httpio


class HttpZarrStore(Store):
    """Read-only Zarr store over ``http(s)://``, using only the standard library.

    Parameters
    ----------
    base_url
        URL of the store root — the directory containing ``zarr.json``.
    timeout
        Per-request timeout in seconds.
    retries
        Retries per request, for transient failures (timeouts, 5xx, 429).
    headers
        Static headers added to every request, e.g. ``{"authorization": "Bearer …"}``.
    """

    supports_writes: bool = False
    supports_deletes: bool = False
    supports_listing: bool = False
    supports_partial_writes: bool = False

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = httpio.DEFAULT_TIMEOUT,
        retries: int = httpio.DEFAULT_RETRIES,
        headers: dict[str, str] | None = None,
        read_only: bool = True,
    ) -> None:
        if not read_only:
            raise ValueError("HttpZarrStore is read-only; a writable HTTP store is not supported.")
        super().__init__(read_only=True)
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.retries = retries
        self.headers = dict(headers or {})

    # -- identity ---------------------------------------------------------
    def __str__(self) -> str:
        return self.base_url

    def __repr__(self) -> str:
        return f"HttpZarrStore({self.base_url!r})"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, type(self)) and self.base_url == other.base_url

    def __hash__(self) -> int:
        return hash((type(self).__name__, self.base_url))

    def with_read_only(self, read_only: bool = False) -> HttpZarrStore:
        # docstring inherited
        if read_only:
            return self
        raise NotImplementedError("An HTTP store cannot be made writable.")

    def _url(self, key: str) -> str:
        return httpio.join_url(self.base_url, key)

    # -- reading ----------------------------------------------------------
    def _fetch(self, key: str, byte_range: ByteRequest | None) -> bytes | None:
        """Blocking body of :meth:`get`; runs on a worker thread."""
        start: int | None = None
        end: int | None = None
        suffix: int | None = None
        if isinstance(byte_range, RangeByteRequest):
            start, end = byte_range.start, byte_range.end
        elif isinstance(byte_range, OffsetByteRequest):
            start = byte_range.offset
        elif isinstance(byte_range, SuffixByteRequest):
            suffix = byte_range.suffix
        elif byte_range is not None:
            raise ValueError(f"Unexpected byte_range, got {byte_range!r}.")

        header = httpio.range_header(start, end, suffix)
        if byte_range is not None and header is None:
            # A degenerate request (zero-length, or end <= start). No bytes are being
            # asked for, so there is nothing to ask the server for either.
            return b""

        result = httpio.fetch(
            self._url(key),
            range_value=header,
            timeout=self.timeout,
            retries=self.retries,
            headers=self.headers,
        )
        if result is None:
            return None
        body, ranged = result
        if header is not None and not ranged:
            # The server ignored Range and sent the whole object (legal, and common
            # for static file servers). Slice it here so the caller still gets
            # exactly the bytes it asked for.
            body = body[-suffix:] if suffix is not None else body[start:end]
        return body

    async def get(
        self,
        key: str,
        prototype: BufferPrototype,
        byte_range: ByteRequest | None = None,
    ) -> Buffer | None:
        # docstring inherited
        data = await asyncio.to_thread(self._fetch, key, byte_range)
        if data is None:
            return None
        return prototype.buffer.from_bytes(data)

    async def get_partial_values(
        self,
        prototype: BufferPrototype,
        key_ranges: Iterable[tuple[str, ByteRequest | None]],
    ) -> list[Buffer | None]:
        # docstring inherited
        requests = list(key_ranges)
        if not requests:
            return []
        return list(
            await asyncio.gather(
                *(self.get(key, prototype, byte_range) for key, byte_range in requests)
            )
        )

    async def exists(self, key: str) -> bool:
        # docstring inherited
        return await asyncio.to_thread(
            httpio.exists,
            self._url(key),
            timeout=self.timeout,
            retries=self.retries,
            headers=self.headers,
        )

    # -- writing ----------------------------------------------------------
    async def set(self, key: str, value: Buffer) -> None:
        # docstring inherited
        raise NotImplementedError("HttpZarrStore is read-only.")

    async def delete(self, key: str) -> None:
        # docstring inherited
        raise NotImplementedError("HttpZarrStore is read-only.")

    # -- listing ----------------------------------------------------------
    #
    # Unsupported, as advertised by `supports_listing = False`. Zarr will not call
    # these, but they are abstract, and returning an empty iterator is the honest
    # implementation: "no keys can be enumerated", not "the store is empty".
    async def list(self) -> AsyncIterator[str]:
        # docstring inherited
        return
        yield ""  # pragma: no cover - unreachable; makes this an async generator

    async def list_prefix(self, prefix: str) -> AsyncIterator[str]:
        # docstring inherited
        return
        yield ""  # pragma: no cover - unreachable; makes this an async generator

    async def list_dir(self, prefix: str) -> AsyncIterator[str]:
        # docstring inherited
        return
        yield ""  # pragma: no cover - unreachable; makes this an async generator
