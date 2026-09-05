# Upstream bugs this layer works around

Two tools in `src/granular/project.py` raise on **every** call in pristine
upstream. Neither has anything to do with the free edition — they are broken in
Studio too — but a fork that reverted its fixes purely to win a clean diff would
be knowingly shipping two broken tools.

So they are repaired at runtime, in `free_edition/integrate.py`
(`fix_upstream_project_bugs`), like every other hook in this layer. That keeps
`src/granular/project.py` byte-identical to upstream and conflict-free on merge,
while our users still get working tools.

**Both deserve an upstream pull request.** They are one-line fixes there, and
until such a PR lands every non-forked install of this project has them.

---

## 1. `close_project` — `NameError` on every call

`close_project` binds `pm, current_project = get_current_project()` and then
calls:

```python
result = project_manager.CloseProject(current_project)
```

`project_manager` is neither a local in that function nor a module global. The
only other uses of that name in the file are unrelated locals inside
`open_project` and `create_project`. So the call raises `NameError`, which the
function's own broad `except Exception` catches and returns as:

```
Error closing project: name 'project_manager' is not defined
```

It reads like a connection failure. It has never closed a project.

**Upstream fix:** use `pm`, already bound two lines above.

```diff
-    result = project_manager.CloseProject(current_project)
+    result = pm.CloseProject(current_project)
```

Verified by running pristine upstream against a stubbed Resolve: before the
patch the call returns the `NameError` string above and `CloseProject` is never
reached; after it, `CloseProject` is called and the tool returns
`Successfully closed project '<name>'`.

---

## 2. `load_cloud_project_tool` — `TypeError` on every call

`load_cloud_project_tool` calls the bare global `load_cloud_project`, intending
the `src.utils.cloud_operations` helper that `from src.granular.common import *`
brings into the namespace at line 3:

```python
return load_cloud_project(get_resolve(), project_name=project_name,
                          project_media_path=project_media_path, sync_mode=sync_mode)
```

But a separate `@mcp.tool()` **later in the same file** is also named
`load_cloud_project`, with the signature
`(project_name, project_media_path, sync_mode="proxy")` — no leading
`resolve_obj`. Module-level names bind in source order, so by the time the
module finishes importing, that global points at the later definition.

The call therefore passes `get_resolve()` into its `project_name` slot *and*
`project_name=` as a keyword:

```
TypeError: load_cloud_project() got multiple values for argument 'project_name'
```

Both tools are legitimate and already published under their public MCP names, so
renaming either would break existing callers.

**Upstream fix:** import the helper under an alias so the two definitions no
longer share a Python name.

```diff
 from src.granular.common import *  # noqa: F401,F403
+from src.utils.cloud_operations import load_cloud_project as _load_cloud_project

-    return load_cloud_project(get_resolve(), project_name=project_name,
-                              project_media_path=project_media_path, sync_mode=sync_mode)
+    return _load_cloud_project(get_resolve(), project_name=project_name,
+                               project_media_path=project_media_path, sync_mode=sync_mode)
```

---

## Why runtime patching, and not just carrying the diff

The whole point of `free_edition/` is that `git merge upstream/main` produces
zero conflicts, which requires zero modifications to upstream files. These two
were the last holdouts: unlike everything else in the layer, they are not
free-edition-specific, so "move them into our package" does not apply.

Patching them at boot costs about forty lines and keeps both properties — clean
merges *and* working tools. If upstream ever fixes them,
`fix_upstream_project_bugs` raises `UpstreamAnchorMissing` naming the function
that changed, rather than silently shadowing a fix that no longer needs
shadowing.
