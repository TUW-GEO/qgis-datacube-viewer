"""Exception hierarchy for the data-access layer.

Everything raised deliberately by :mod:`datacube_viewer.core` derives from
:class:`GeoZarrError` so the GUI can catch one type and show the message verbatim.
Messages are written to be shown to a user, not just logged.
"""

from __future__ import annotations


class GeoZarrError(Exception):
    """Base class for all errors raised by the data-access layer."""

    #: Optional longer explanation / remediation hint shown under the message.
    hint: str | None = None

    def __init__(self, message: str, hint: str | None = None):
        super().__init__(message)
        self.message = message
        if hint is not None:
            self.hint = hint

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.message

    @property
    def detailed(self) -> str:
        """Message plus hint, for anywhere the user actually reads the text.

        ``str()`` stays terse so log lines and ``repr``s do not sprawl, but the hint
        is usually the part that tells someone how to fix the problem — so the
        provider's ``lastError()`` and the dialog's message area use this instead.
        """
        return f"{self.message}\n{self.hint}" if self.hint else self.message


class MissingDependencyError(GeoZarrError):
    """``zarr`` or ``icechunk`` is not importable from the QGIS interpreter."""


class StoreOpenError(GeoZarrError):
    """The store URI could not be opened as an Icechunk repo or a Zarr group."""


class RefNotFoundError(StoreOpenError):
    """The requested branch, tag or snapshot does not exist in the repository."""


class MetadataError(GeoZarrError):
    """The store opened, but its geospatial metadata is missing or inconsistent."""


class NoDatasetsError(MetadataError):
    """No group in the store looked like a georeferenced raster dataset."""


class NoSuchVariableError(MetadataError):
    """Every variable the layer asked for is missing from the dataset.

    Its own class because the remedy is specific and nothing else implies it: the
    store opened and has datasets, the request just names variables that are not in
    this one — typically a project saved against a store whose variables have since
    been renamed, or a hand-edited layer URI.
    """


class CrsResolutionError(MetadataError):
    """No CRS could be determined and no override was supplied."""


class ReadError(GeoZarrError):
    """A chunk read failed or returned something unusable."""
