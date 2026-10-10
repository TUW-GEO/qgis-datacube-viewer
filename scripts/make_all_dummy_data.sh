#!/usr/bin/env bash
# Generate every dummy dataset in data/ with scripts/make_dummy_data.py.
#
# By default only datasets that are missing are created; --overwrite regenerates all
# of them. The Copernicus datasets need a one-off tile download (cached in scripts/.cache).
#
# Usage: scripts/make_all_dummy_data.sh [--overwrite] [-h|--help]

set -euo pipefail

overwrite=0
case "${1:-}" in
    "") ;;
    --overwrite) overwrite=1 ;;
    -h | --help)
        sed -n '2,7p' "$0" | sed 's/^# \{0,1\}//'
        exit 0
        ;;
    *)
        echo "unknown argument: $1 (try --help)" >&2
        exit 2
        ;;
esac
[ $# -le 1 ] || { echo "too many arguments (try --help)" >&2; exit 2; }

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
script="$root/scripts/make_dummy_data.py"
out="$root/data"
mkdir -p "$out"

created=0
skipped=0

# make NAME [ARGS...]: write data/NAME unless it exists (or --overwrite is given).
make() {
    local name="$1"
    shift
    if [ -e "$out/$name" ] && [ "$overwrite" -eq 0 ]; then
        echo "skip   $name (exists)"
        skipped=$((skipped + 1))
        return
    fi
    echo "create $name"
    local flags=()
    [ "$overwrite" -eq 1 ] && flags+=(--overwrite)
    uv run --script "$script" -o "$out/$name" "$@" ${flags[@]+"${flags[@]}"} > /dev/null
    created=$((created + 1))
}

# --- Zarr ------------------------------------------------------------------
make dummy.zarr
make dem.zarr --source copernicus
make flat_v2.zarr --zarr-format 2 --levels 1
make cube.zarr --sensors
make shuffled.zarr --dim-order shuffled --sensors
make nested.zarr --nest 3 --sensors

# --- NetCDF (same content as the Zarr stores above) --------------------------
make dummy.nc
make dem.nc --source copernicus
make flat.nc --levels 1
make cube.nc --sensors
make shuffled.nc --dim-order shuffled --sensors
make nested.nc --nest 3 --sensors
# Coordinates declared once in the root, inherited by the data group (netcdf only)
make shared.nc --levels 1 --shared-coords

echo "done: $created created, $skipped skipped"
