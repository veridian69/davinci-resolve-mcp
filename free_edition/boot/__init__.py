"""Console boot scripts: the paste-into-Resolve entry points.

Holds the three scripts an operator pastes into Resolve's Py3 console -
`resolve_console_boot.py` (granular server), `compound_console_boot.py`
(compound server) and `dashboard_console_boot.py` (analysis dashboard). Each one
guards the environment, points `sys.path` at the vendored deps and the checkout,
purges `src.*` and `free_edition.*` so a re-paste really does reload, installs
the in-process shim before any upstream import, and only then imports upstream
and calls `free_edition.integrate` to wire our features in.

These files are `exec()`d from a console namespace, not imported: they have no
`__file__`, which is why they take the checkout root from the `INPROC_REPO`
variable set on the paste line and never derive it from their own location.
The package exists to give them one stable home next to the rest of the layer,
not to make them importable.
"""
