# Where this came from

This directory is a verbatim copy of the public QGIS plugin
[`eodcgmbh/qgis-geozarr`](https://github.com/eodcgmbh/qgis-geozarr), taken at:

```
commit  68eb5b1c7ff0d3095b6a696eb407710bb73052e7
subject Restrict HTTP reads to http(s), and clear the upload security scan
date    2026-08-26
version 0.2.1  (geozarr_viewer/metadata.txt)
```

It is MIT-licensed; `LICENSE` is the upstream file, kept as the license requires.
Original author: Timothy Simons / EODC GmbH.

That commit, `f01754e`, is the pristine import and is tagged `plugin-upstream-0.2.1`.
Everything we have changed since is visible as:

```bash
git diff plugin-upstream-0.2.1 -- plugin/
```

## Deviations from upstream

| Change | Why |
| --- | --- |
| Renamed to **GEO-geo-zarr** (`PLUGIN_NAME`, `metadata.txt`, log tag, dialog titles) | Three GeoZarr plugins can share one QGIS profile; two of them answered to nearly the same name. |
| Its own named toolbar instead of `addRasterToolBarIcon()` | An icon on the shared Raster toolbar is indistinguishable from the other plugins'. |
| **Version comes from the git tag**, not `metadata.txt` | See below. `metadata.txt` now holds the placeholder `0.0.0`. |
| `__version__` removed from `geozarr_viewer/__init__.py` | It said `0.1.0` while `metadata.txt` said `0.2.1`. One source of truth, and it is not a Python literal. |
| `.gitlab-ci.yml` at the repository root | Development happens on GitLab; upstream has no lint/test CI at all. |
| `.github/workflows/release.yml` left in place but **inert** | It is EODC's path to plugins.qgis.org. GitLab never reads `.github/`, and it encodes the opposite versioning policy, so do not take it as current. |

## Versioning

The git tag is the version. Tag `v0.0.1` ships as `0.0.1`;
`scripts/build_plugin.py` injects that into `metadata.txt` as the file goes into the
zip. The working tree keeps `version=0.0.0` and the build **refuses to run** if
someone hand-edits a real number in, because it would be silently overwritten.

```bash
python scripts/build_plugin.py                     # git describe -> 0.0.1.dev3+gabc1234
python scripts/build_plugin.py --version 1.2.3     # explicit; this is what CI passes
```

Off-tag builds are marked (`.dev<n>+g<sha>`, plus `.dirty`) so an installable zip
never looks like a release. Since the version is no longer visible in the tree, the
build reads it back out of the finished archive and fails if it disagrees.

A caveat that comes with this scheme: **a published version cannot be reused.** The
package registry keys on name plus version, and a QGIS client that already installed
`0.0.1` will not fetch a different `0.0.1`. Bump rather than retag.

The copy is deliberate. Upstream is not expected to receive further commits, so
there is no submodule, subtree or remote wired up here — these are just files we
own now, and they are edited in place like any other code in this repository.

Nothing was renamed or rewritten *on the way in* — the import was byte-for-byte, and
the changes listed above came afterwards as ordinary commits. Every path in the
plugin's own tooling is relative to *this* directory (`REPO =
Path(__file__).parent.parent` in `scripts/build_plugin.py`), so the tree works
unmodified inside a subfolder. The
plugin keeps its own `pyproject.toml`, `pixi.toml`, `.gitignore` and `tests/`, which
is what stops it colliding with the outer project's tooling:

| File | Why it stays separate |
| --- | --- |
| `pyproject.toml` | Has no `[project]` table on purpose, and needs ruff `target-version = "py39"` plus `N802`/`BLE001`/`RUF001` ignored — all of which contradict the root config. Ruff resolves config per-file to the nearest ancestor, so both work at once. |
| `.gitignore` | Git honours nested ignore files, so `build/` and `.pixi/` are covered without touching the root one. |
| `tests/` | Needs QGIS and `pytest-qgis`. The root `pyproject.toml` pins `testpaths = ["tests"]`, so the fast outer suite stays fast. |

If upstream ever does publish something worth having, the cheap route is to diff
against a fresh download rather than to reconstruct a git relationship:

```bash
gh repo clone eodcgmbh/qgis-geozarr /tmp/upstream -- --depth 1
diff -ru --strip-trailing-cr /tmp/upstream/geozarr_viewer plugin/geozarr_viewer
```

`--strip-trailing-cr` matters: the root `.gitattributes` normalises to LF, and a
fresh clone on Windows will have CRLF.
