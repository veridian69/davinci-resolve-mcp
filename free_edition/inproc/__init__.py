"""Run the MCP server inside DaVinci Resolve's own Python console.

DaVinci Resolve's free edition disables the internal Fusion script server, so
`DaVinciResolveScript.scriptapp("Resolve")` returns None for any external
process. The console (Workspace -> Console -> Py3) is a different door to the
same room: it runs inside Resolve with `resolve` already bound in its namespace,
never loading fusionscript and never touching the gate.

This package moves the server to that side of the door instead of tunnelling
through it. See ../docs/specs/2026-07-24-resolve-inproc-mcp-design.md.
"""

VERSION = "0.1.0"

__all__ = ["VERSION"]
