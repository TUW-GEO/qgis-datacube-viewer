"""Command line interface: ``geozarr <command>``."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from geozarr import __version__
from geozarr.store import GeoZarrInfo, describe


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="geozarr", description=__doc__)
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    subparsers = parser.add_subparsers(dest="command", required=True)
    info = subparsers.add_parser("info", help="print the geospatial metadata of a store")
    info.add_argument("store", help="path or URL of a GeoZarr store")
    info.set_defaults(func=_cmd_info)

    return parser


def _cmd_info(args: argparse.Namespace) -> int:
    print(_format(describe(args.store)))
    return 0


def _format(info: GeoZarrInfo) -> str:
    lines = [
        info.path,
        f"  zarr format: {info.zarr_format}",
        f"  conventions: {', '.join(info.conventions) or '-'}",
        f"  crs:         {info.crs or '-'}",
        f"  dimensions:  {', '.join(info.dimensions or ()) or '-'}",
        f"  bbox:        {info.bbox or '-'}",
        f"  levels:      {len(info.levels)}",
    ]
    for level in info.levels:
        lines.append(f"    {level.path}: shape={level.shape} arrays={', '.join(level.arrays)}")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
