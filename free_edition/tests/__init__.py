"""Offline tests for the free-edition layer.

Holds the tests that cover only our code - the whisperX backend and its runtime
hooks, the subtitle import planner, and the dashboard status payload - run as
`python3 -m unittest free_edition.tests.<name>` from the checkout root. They
must stay offline: no Resolve, no network, no real transcription.

They live here instead of in `tests/` because that directory is upstream's and
has to stay byte-identical to it, and because several upstream drift tests count
things by file location (the granular tool count, for one), so a test of ours
sitting there would change numbers upstream asserts. Tests that exercise runtime
wiring should go through `free_edition.integrate` rather than calling upstream
helpers directly - otherwise they keep passing against pristine upstream while
the hooks that production depends on are broken.
"""
