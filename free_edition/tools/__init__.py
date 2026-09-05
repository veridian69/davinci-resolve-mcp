"""Operator scripts for setting up, probing and validating the layer.

Holds the command-line side of the free edition: `setup_inproc.py` (vendors the
runtime dependencies into `.inproc/deps` and prepares the state directory),
`verify_live.py` and `sweep_free_edition.py` (check a running console server),
`fake_console.py` (an offline harness that exercises the real boot end to end
with no Resolve running), the `probe_*.py` scripts, and
`live_subtitles_validation.py`.

These are run as scripts from the checkout root (`python3
free_edition/tools/<name>.py`), not imported. They sit two directories below the
root, so any script deriving the repo root from `__file__` must walk up three
levels - a miscount is silent, producing a plausible-looking path under
`free_edition/` and surfacing much later as a missing dependency. Keeping them
in their own package also keeps them clear of upstream's `tools/`, which must
stay pristine.
"""
