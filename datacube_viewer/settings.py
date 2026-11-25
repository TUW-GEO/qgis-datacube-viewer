"""Plugin settings that the provider reads, not only the GUI.

Stored in ``QgsSettings`` under ``plugins/datacube_viewer/``. The settings page in
Settings → Options (:mod:`datacube_viewer.gui.options`) writes them; everything
here only reads and writes values, so the provider can use it without importing
any widgets.
"""

from __future__ import annotations

from qgis.core import QgsSettings

from .core.reader import BILINEAR_VALIDITY_RULES, BilinearValidity, ReaderConfig

_PREFIX = "plugins/datacube_viewer"

# ------------------------------------------------------------------ resampling
BILINEAR_VALIDITY_KEY = f"{_PREFIX}/bilinear_validity"
DEFAULT_BILINEAR_VALIDITY: BilinearValidity = "any"


def bilinear_validity() -> BilinearValidity:
    """When a bilinear pixel next to nodata counts as data. GDAL's rule by default."""
    value = str(QgsSettings().value(BILINEAR_VALIDITY_KEY, DEFAULT_BILINEAR_VALIDITY))
    return value if value in BILINEAR_VALIDITY_RULES else DEFAULT_BILINEAR_VALIDITY  # type: ignore[return-value]


def set_bilinear_validity(rule: BilinearValidity) -> None:
    QgsSettings().setValue(BILINEAR_VALIDITY_KEY, rule)


def reader_config() -> ReaderConfig:
    """The read-path tunables for a new layer, from the settings."""
    return ReaderConfig(bilinear_validity=bilinear_validity())


# ---------------------------------------------------------------- colour range
#: How the colour range is derived: "minmax", "cumulative_cut" or "stddev".
STRETCH_LIMITS_KEY = f"{_PREFIX}/stretch/limits"
#: Cumulative cut bounds, as fractions (QGIS stores them the same way).
STRETCH_CUT_LOWER_KEY = f"{_PREFIX}/stretch/cut_lower"
STRETCH_CUT_UPPER_KEY = f"{_PREFIX}/stretch/cut_upper"
STRETCH_STD_DEV_FACTOR_KEY = f"{_PREFIX}/stretch/std_dev_factor"
#: "exact" or "estimated".
STRETCH_ACCURACY_KEY = f"{_PREFIX}/stretch/accuracy"
#: Most source pixels an exact range reads by default, in millions. Past it the
#: range is sampled, unless a full scan was asked for.
EXACT_BUDGET_KEY = f"{_PREFIX}/stretch/exact_budget_mpix"

STRETCH_LIMITS = ("minmax", "cumulative_cut", "stddev")
STRETCH_ACCURACIES = ("exact", "estimated")
DEFAULT_STRETCH_LIMITS = "minmax"
DEFAULT_CUT_LOWER = 0.02
DEFAULT_CUT_UPPER = 0.98
DEFAULT_STD_DEV_FACTOR = 2.0
DEFAULT_STRETCH_ACCURACY = "exact"
#: 100 million pixels: about ten seconds from a local NetCDF file, and roughly a
#: hundred slices of a global quarter-degree grid.
DEFAULT_EXACT_BUDGET_MPIX = 100


def _choice(key: str, allowed: tuple[str, ...], default: str) -> str:
    value = str(QgsSettings().value(key, default))
    return value if value in allowed else default


def _number(key: str, default: float) -> float:
    try:
        return float(QgsSettings().value(key, default))
    except (TypeError, ValueError):
        return default


def stretch_limits() -> str:
    return _choice(STRETCH_LIMITS_KEY, STRETCH_LIMITS, DEFAULT_STRETCH_LIMITS)


def stretch_accuracy() -> str:
    return _choice(STRETCH_ACCURACY_KEY, STRETCH_ACCURACIES, DEFAULT_STRETCH_ACCURACY)


def cumulative_cut() -> tuple[float, float]:
    lower = _number(STRETCH_CUT_LOWER_KEY, DEFAULT_CUT_LOWER)
    upper = _number(STRETCH_CUT_UPPER_KEY, DEFAULT_CUT_UPPER)
    if not 0.0 <= lower < upper <= 1.0:
        return DEFAULT_CUT_LOWER, DEFAULT_CUT_UPPER
    return lower, upper


def std_dev_factor() -> float:
    factor = _number(STRETCH_STD_DEV_FACTOR_KEY, DEFAULT_STD_DEV_FACTOR)
    return factor if factor > 0 else DEFAULT_STD_DEV_FACTOR


def exact_budget_pixels() -> int:
    mpix = _number(EXACT_BUDGET_KEY, DEFAULT_EXACT_BUDGET_MPIX)
    return int(max(mpix, 1.0) * 1_000_000)


def set_stretch_defaults(
    limits: str,
    accuracy: str,
    cut: tuple[float, float],
    factor: float,
    budget_mpix: float,
) -> None:
    settings = QgsSettings()
    settings.setValue(STRETCH_LIMITS_KEY, limits)
    settings.setValue(STRETCH_ACCURACY_KEY, accuracy)
    settings.setValue(STRETCH_CUT_LOWER_KEY, float(cut[0]))
    settings.setValue(STRETCH_CUT_UPPER_KEY, float(cut[1]))
    settings.setValue(STRETCH_STD_DEV_FACTOR_KEY, float(factor))
    settings.setValue(EXACT_BUDGET_KEY, float(budget_mpix))
