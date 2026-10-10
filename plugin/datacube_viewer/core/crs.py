"""CRS resolution.

The sample store shows why this needs to be a documented chain rather than one lookup:
it carries a full CF grid mapping in a ``spatial_ref`` array, but **no data variable has
a ``grid_mapping`` attribute pointing at it**, so a strict CF reader finds nothing. The
authoritative value is the ``proj:code`` attribute from the ``proj:`` Zarr convention.
Other stores do the opposite. So we try everything, in order, and record which rule won
so the UI can explain itself.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .model import CrsInfo

try:  # pyproj is present in QGIS, but keep the layer importable without it.
    from pyproj import CRS as _PyprojCRS
except Exception:  # pragma: no cover - only on broken installs
    _PyprojCRS = None  # type: ignore[assignment]

#: CRS resolution is the most common source of "why did my layer not load" reports,
#: and every fallback below is silent by design. Logging at debug level keeps the
#: UI quiet while leaving a trail for anyone who turns logging up.
_log = logging.getLogger(__name__)

# --------------------------------------------------------------- PROJ threading
#
# **PROJ is only ever entered from the main thread.** This is a hard rule, not a
# preference, and it is the single most important constraint in this module.
#
# Building a ``pyproj.CRS`` creates a *thread-local* PROJ context, which opens
# proj.db. Inside QGIS there is a second PROJ user — QGIS and GDAL themselves — on
# the main thread, and creating a context on a worker thread while that is happening
# kills the process:
#
#     Windows fatal exception: access violation
#       pyproj/crs/crs.py:350 in __init__     self._local.crs = _CRS(self.srs)
#       projCppContext::getDatabaseContext
#       osgeo::proj::io::DatabaseContext::toWGS84AutocorrectWrongValues
#       proj_create
#
# An earlier attempt serialised pyproj behind a lock. That was the wrong fix: a lock
# of ours orders our own calls against each other, but cannot order them against
# QGIS's PROJ use on the main thread. Confining PROJ to the main thread does, because
# a single thread is serialised by definition.
#
# The callers uphold this by doing I/O on workers and metadata description on the GUI
# thread (see datacube_viewer.gui.load_dialog). The guard below is the safety net: if
# something does reach pyproj off-thread, it degrades to a shape check and marks the
# result provisional instead of taking QGIS down.
_MAIN_THREAD = threading.main_thread()
#: Re-entrancy guard for the main thread itself; PROJ is cheap and uncontended here.
_proj_lock = threading.RLock()


def proj_usable() -> bool:
    """True when it is safe to call into pyproj from the calling thread."""
    return _PyprojCRS is not None and threading.current_thread() is _MAIN_THREAD


def _cached_crs(definition: str):
    """Construct a ``pyproj.CRS`` on the main thread. May raise."""
    with _proj_lock:
        return _PyprojCRS.from_user_input(definition)


def _looks_like_a_crs(text: str) -> bool:
    """Shape-only plausibility check, for when PROJ cannot be consulted."""
    stripped = text.strip()
    return bool(
        is_authority_code(stripped)
        or _looks_like_wkt(stripped)
        or stripped.startswith("+proj=")
        or stripped.startswith("{")
    )


#: Attribute names that hold a ready-to-use WKT string.
_WKT_KEYS = ("crs_wkt", "spatial_ref", "esri_pe_string", "proj:wkt2", "wkt")

#: Group attributes that, in the wild, hold a CRS as either an authority code or WKT.
_GROUP_CRS_KEYS = ("crs", "crs_wkt", "spatial_ref", "esri_pe_string", "wkt", "epsg", "EPSG")

#: ``standard_name`` values that identify a geographic (lon/lat) coordinate pair.
_GEOGRAPHIC_NAMES = {"longitude", "latitude", "grid_longitude", "grid_latitude"}

_AUTHORITY_RE = re.compile(r"^\s*([A-Za-z]{2,16})\s*:\s*(\d{1,10})\s*$")


@dataclass
class CrsBundle:
    """Every attribute dictionary that could plausibly carry CRS information.

    Collected by the inspector so :func:`resolve_crs` stays free of Zarr specifics and
    can be unit-tested with plain dicts.
    """

    root_attrs: Mapping[str, Any] = field(default_factory=dict)
    dataset_attrs: Mapping[str, Any] = field(default_factory=dict)
    level_attrs: Mapping[str, Any] = field(default_factory=dict)
    #: Attributes of the data variables, keyed by variable name.
    variable_attrs: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    #: Attributes of non-data arrays in the group (``spatial_ref``, ``crs``, …).
    aux_attrs: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    #: Attributes of the 1-D coordinate arrays, keyed by dimension name.
    coord_attrs: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)

    @property
    def _group_scopes(self) -> tuple[tuple[str, Mapping[str, Any]], ...]:
        """Group attribute dicts, most specific first."""
        return (
            ("level", self.level_attrs),
            ("dataset", self.dataset_attrs),
            ("root", self.root_attrs),
        )


def is_authority_code(text: str) -> bool:
    """True for strings like ``EPSG:25832`` / ``ESRI:102008``."""
    return bool(_AUTHORITY_RE.match(text))


def normalise_authority_code(text: str) -> str:
    match = _AUTHORITY_RE.match(text)
    if not match:
        return text.strip()
    return f"{match.group(1).upper()}:{match.group(2)}"


def _coerce_str(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, (int,)) and not isinstance(value, bool):
        # `proj:epsg: 25832` shows up in the wild as a bare integer.
        return f"EPSG:{value}"
    return None


def _find_wkt(attrs: Mapping[str, Any]) -> tuple[str, str] | None:
    """Return ``(wkt, key)`` for the first attribute holding a WKT string."""
    for key in _WKT_KEYS:
        text = _coerce_str(attrs.get(key))
        if text and _looks_like_wkt(text):
            return text, key
    return None


def _looks_like_wkt(text: str) -> bool:
    head = text.lstrip()[:80].upper()
    return any(
        head.startswith(kw)
        for kw in ("PROJCS", "GEOGCS", "PROJCRS", "GEOGCRS", "COMPD_CS", "COMPOUNDCRS", "BOUNDCRS")
    )


def _grid_mapping_target(attrs: Mapping[str, Any]) -> str | None:
    """Resolve a CF ``grid_mapping`` attribute to the variable name it names.

    Handles both the simple form (``grid_mapping = "crs"``) and the extended form
    (``grid_mapping = "crs: x y"``) introduced in CF-1.7.
    """
    raw = _coerce_str(attrs.get("grid_mapping"))
    if not raw:
        return None
    if ":" in raw:
        return raw.split(":", 1)[0].strip() or None
    return raw.split()[0] if raw.split() else None


def _crs_from_cf_attrs(attrs: Mapping[str, Any]) -> str | None:
    """Build WKT from CF grid-mapping parameters via pyproj.

    Returns None off the main thread — there is no shape-only substitute for
    synthesising WKT from CF parameters, so the caller marks the result provisional
    and the GUI re-resolves it where PROJ is safe to use.
    """
    if not proj_usable() or "grid_mapping_name" not in attrs:
        return None
    payload = {k: v for k, v in attrs.items() if not k.startswith("_")}
    try:
        with _proj_lock:
            return _PyprojCRS.from_cf(payload).to_wkt()
    except Exception:
        return None


def _validate(definition: str) -> bool:
    """Cheap sanity check so we don't hand QGIS something unparseable.

    Off the main thread this falls back to a shape check. Accepting a
    shape-plausible definition is the right failure mode: QGIS validates it again
    when the layer is built, whereas rejecting it here would silently discard a
    perfectly good CRS and leave the layer unplaceable.
    """
    if not proj_usable():
        return _looks_like_a_crs(definition)
    try:
        _cached_crs(definition)
        return True
    except Exception:
        return False


def resolve_crs(bundle: CrsBundle, override: str | None = None) -> CrsInfo:
    """Determine the CRS of a dataset, most trustworthy rule first.

    Order:

    1. explicit user override
    2. ``proj:code`` / ``proj:epsg`` group attribute (the ``proj:`` Zarr convention)
    3. ``proj:wkt2`` group attribute
    4. ``proj:projjson`` group attribute
    5. CF ``grid_mapping`` on a data variable -> that variable's WKT
    6. WKT on *any* auxiliary array (covers the "grid mapping present but unreferenced"
       case that the sample store exhibits)
    7. a bare ``crs`` / ``crs_wkt`` / ``spatial_ref`` / ``epsg`` group attribute
    8. WKT on a data variable (``esri_pe_string``)
    9. CF grid-mapping parameters rebuilt through ``pyproj.CRS.from_cf``
    10. lon/lat coordinate ``standard_name`` -> assume EPSG:4326
    11. give up
    """
    # When PROJ cannot be consulted, definitions are accepted on shape alone and the
    # result is flagged so a caller on the main thread can confirm it.
    unconfirmed = not proj_usable()

    if override:
        text = override.strip()
        if text:
            return CrsInfo(
                normalise_authority_code(text) if is_authority_code(text) else text,
                "user override",
                "override",
            )

    # 2-4: the proj: convention, checked from the most specific group outwards.
    for scope, attrs in bundle._group_scopes:
        for key in ("proj:code", "proj:epsg"):
            text = _coerce_str(attrs.get(key))
            if text:
                code = normalise_authority_code(text) if is_authority_code(text) else text
                if _validate(code):
                    return CrsInfo(
                        code, f"{key} attribute on {scope} group", "authority", unconfirmed
                    )

        text = _coerce_str(attrs.get("proj:wkt2"))
        if text and _validate(text):
            return CrsInfo(text, f"proj:wkt2 attribute on {scope} group", "wkt", unconfirmed)

        projjson = attrs.get("proj:projjson")
        if projjson is not None:
            text = projjson if isinstance(projjson, str) else json.dumps(projjson)
            if _validate(text):
                return CrsInfo(
                    text, f"proj:projjson attribute on {scope} group", "projjson", unconfirmed
                )

    # 5: a data variable explicitly names its grid mapping.
    for var_name, var_attrs in bundle.variable_attrs.items():
        target = _grid_mapping_target(var_attrs)
        if not target:
            continue
        target_attrs = bundle.aux_attrs.get(target)
        if target_attrs is None:
            continue
        found = _find_wkt(target_attrs)
        if found and _validate(found[0]):
            return CrsInfo(
                found[0],
                f"CF grid_mapping: {var_name}.grid_mapping -> {target}.{found[1]}",
                "cf",
                unconfirmed,
            )
        wkt = _crs_from_cf_attrs(target_attrs)
        if wkt:
            return CrsInfo(
                wkt,
                f"CF grid_mapping parameters on {target} (via {var_name}.grid_mapping)",
                "cf",
            )

    # 6: an auxiliary array carries a WKT even though nothing references it.
    for aux_name, aux in bundle.aux_attrs.items():
        found = _find_wkt(aux)
        if found and _validate(found[0]):
            return CrsInfo(
                found[0], f"{found[1]} attribute on {aux_name} array", "wkt", unconfirmed
            )

    # 7: a bare CRS attribute on a group. No convention blesses this, but stores
    #    converted from netCDF carry it routinely — EODC's SPARTACUS names its
    #    grid mapping in `grid_mapping` without shipping the variable it names, and
    #    the only usable CRS in the store is the root attribute `crs = "EPSG:3416"`.
    for scope, attrs in bundle._group_scopes:
        for key in _GROUP_CRS_KEYS:
            text = _coerce_str(attrs.get(key))
            if not text:
                continue
            if key.lower() == "epsg" and text.isdigit():
                text = f"EPSG:{text}"
            if is_authority_code(text):
                code = normalise_authority_code(text)
                if _validate(code):
                    return CrsInfo(
                        code, f"{key} attribute on {scope} group", "authority", unconfirmed
                    )
            elif _looks_like_wkt(text) and _validate(text):
                return CrsInfo(text, f"{key} attribute on {scope} group", "wkt", unconfirmed)

    # 8: a data variable carries its own WKT. ArcGIS-derived stores put a full PROJCS
    #    in `esri_pe_string` on each variable and nowhere else.
    for var_name, var_attrs in bundle.variable_attrs.items():
        found = _find_wkt(var_attrs)
        if found and _validate(found[0]):
            return CrsInfo(
                found[0], f"{found[1]} attribute on {var_name} variable", "wkt", unconfirmed
            )

    # 9: CF parameters without a WKT string.
    for aux_name, aux in bundle.aux_attrs.items():
        wkt = _crs_from_cf_attrs(aux)
        if wkt:
            return CrsInfo(wkt, f"CF grid-mapping parameters on {aux_name} array", "cf")

    # 8: geographic coordinates are a safe-ish assumption; flag it as assumed.
    names = {_coerce_str(attrs.get("standard_name")) or "" for attrs in bundle.coord_attrs.values()}
    units = {_coerce_str(attrs.get("units")) or "" for attrs in bundle.coord_attrs.values()}
    if names & _GEOGRAPHIC_NAMES or {"degrees_east", "degrees_north"} & units:
        return CrsInfo(
            "EPSG:4326",
            "assumed from lon/lat coordinate names — no CRS declared in store",
            "assumed",
        )

    if unconfirmed:
        # Rules 5 and 7 need pyproj to synthesise WKT from CF parameters and were
        # skipped, so "nothing found" is not yet a final answer.
        return CrsInfo(
            None,
            "no CRS resolved yet — CF grid-mapping parameters could not be checked "
            "off the main thread",
            "none",
            True,
        )
    return CrsInfo.unknown(
        "no proj:code, CF grid_mapping or WKT attribute found — set a CRS override"
    )


def refine(bundle: CrsBundle, info: CrsInfo, override: str | None = None) -> CrsInfo:
    """Re-resolve a provisional :class:`CrsInfo` now that PROJ is usable.

    A no-op for anything already confirmed, and a no-op off the main thread, so it is
    safe to call unconditionally from GUI code.
    """
    if not info.provisional or not proj_usable():
        return info
    return resolve_crs(bundle, override)


def describe_crs(definition: str | None) -> str:
    """Short human label for a CRS definition, for display in the UI.

    Falls back to the bare definition off the main thread. This is display text, so
    losing the friendly name is a cosmetic downgrade — and it is only ever called
    from GUI code, which is on the main thread anyway.
    """
    if not definition:
        return "unknown"
    if is_authority_code(definition):
        code = normalise_authority_code(definition)
        if proj_usable():
            try:
                return f"{code} — {_cached_crs(code).name}"
            except Exception:
                return code
        return code
    if proj_usable():
        try:
            with _proj_lock:
                crs = _PyprojCRS.from_user_input(definition)
                authority = crs.to_authority()
                name = crs.name
            if authority:
                return f"{authority[0]}:{authority[1]} — {name}"
            return name
        except Exception:
            # Deliberately non-fatal: this is a display label, so falling back to the
            # raw definition is a cosmetic downgrade rather than a failure. Logged
            # because a definition PROJ cannot parse here will usually also be the
            # reason the layer's CRS looks wrong.
            _log.debug("PROJ could not describe CRS definition %r", definition, exc_info=True)
    return definition[:60] + ("…" if len(definition) > 60 else "")
