# Where this came from

The plugin began as a verbatim copy of the public QGIS plugin
[`eodcgmbh/qgis-geozarr`](https://github.com/eodcgmbh/qgis-geozarr), taken at:

```
commit  68eb5b1c7ff0d3095b6a696eb407710bb73052e7
subject Restrict HTTP reads to http(s), and clear the upload security scan
date    2026-08-26
version 0.2.1  (datacube_viewer/metadata.txt)
```

It is MIT-licensed; `LICENSE` is the upstream file, kept as the license requires.
Original author: Timothy Simons / EODC GmbH.

That commit, `f01754e`, is the pristine import and is tagged `plugin-upstream-0.2.1`.
Everything we have changed since is visible as:

```bash
git diff plugin-upstream-0.2.1 -- datacube_viewer/
```

## Deviations from upstream

| Change | Why |
| --- | --- |
| Renamed to **Datacube Viewer** (`PLUGIN_NAME`, `metadata.txt`, log tag, dialog titles) | Three GeoZarr plugins can share one QGIS profile; two of them answered to nearly the same name. |
| Its own named toolbar instead of `addRasterToolBarIcon()` | An icon on the shared Raster toolbar is indistinguishable from the other plugins'. |
| **Version comes from the git tag**, not `metadata.txt` | See below. `metadata.txt` now holds the placeholder `0.0.0`. |
| `__version__` removed from `datacube_viewer/__init__.py` | It said `0.1.0` while `metadata.txt` said `0.2.1`. One source of truth, and it is not a Python literal. |
| `.github/workflows/ci.yml` | Upstream has no lint/test CI at all. This one lints and tests every branch push once; a `v*` tag then waits for the green run on `main` for that commit, builds the zip, attaches it to a GitHub release and uploads it to plugins.qgis.org. **Tag on `main` after merging**: a tag on a commit that never ran on `main` (squash or merge-commit merges create new hashes) fails the release job. |
| `.github/` and `[tool.qgis-plugin-ci]` deleted | EODC's path to plugins.qgis.org. Its tag-equals-`metadata.txt` check can no longer pass now that the version comes from the tag. Replaced by the workflow above, which uploads with a per-plugin API token (`QGIS_TOKEN`) via `scripts/upload_plugin.py`. |
| Phase-0 spike scripts deleted (`spike_provider.py`, `spike_render.png`) and `make_sample_store.py` | The spike asked whether a pure-Python provider can render at all; the plugin is the answer. `make_sample_store.py` subsets a 13 GB store we do not have, and `scripts/make_dummy_data.py` at the repository root covers the need. |

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
release is keyed on name plus version, and a QGIS client that already installed
`0.0.1` will not fetch a different `0.0.1`. Bump rather than retag.

The copy is deliberate. Upstream is not expected to receive further commits, so
there is no submodule, subtree or remote wired up here -- these are just files we
own now, and they are edited in place like any other code in this repository.

The import was byte-for-byte into a `plugin/` subfolder, and the changes listed above
came afterwards as ordinary commits. Since then the plugin was moved to the top level,
as upstream has it, and merged with the surrounding project: one `pyproject.toml`
(ruff, pytest), one `scripts/` and one `tests/`. `pixi.toml` remains the environment
that has QGIS in it, for the tests that need it.

If upstream ever does publish something worth having, the cheap route is to diff
against a fresh download rather than to reconstruct a git relationship:

```bash
gh repo clone eodcgmbh/qgis-geozarr /tmp/upstream -- --depth 1
diff -ru --strip-trailing-cr /tmp/upstream/geo_geozarr_viewer datacube_viewer
```

The upstream folder is still called `geo_geozarr_viewer`. `--strip-trailing-cr`
matters: the `.gitattributes` normalises to LF, and a fresh clone on Windows will have
CRLF.
