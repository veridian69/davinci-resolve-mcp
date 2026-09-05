"""Free-edition layer: run this MCP server inside Resolve's own console.

DaVinci Resolve's free edition disables the internal Fusion script server, so
`DaVinciResolveScript.scriptapp("Resolve")` returns None for every external
process. Resolve's built-in console (Workspace -> Console -> Py3) runs inside
Resolve with `resolve` already bound, so this package boots the server on that
side of the door instead of tunnelling through it, and adds the features that
only make sense there (whisperX subtitles, the in-process diagnostics card, the
console tooling).

It is a separate top-level package rather than more code under `src/` because
`src/` is a fork of an upstream project we want to keep merging from. The
invariant that pays for this whole layout: **every file upstream owns stays
byte-identical to upstream** (`git diff <upstream> HEAD -- ':!free_edition'`
must be empty), so no upstream file may ever import, mention, or be edited for
free_edition. Everything upstream needs to know about us is applied at runtime
by `free_edition.integrate`, which the boot scripts call after upstream has been
imported.

Importing this package must stay free of side effects and must not import
`src.*` at module scope: the boot scripts purge both `src.*` and
`free_edition.*` from `sys.modules` and then import in a deliberate order
(shim before upstream, registration before dispatch), and an eager import here
would defeat that.
"""
