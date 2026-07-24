# Running the MCP server on DaVinci Resolve **free edition**

The upstream server talks to Resolve through the external scripting bridge,
which the free edition disables. This fork adds a second way in: the server runs
**inside Resolve's own Python console**, where the `resolve` object already
exists and the licence gate is never consulted.

Nothing about the upstream path changes. If you have Studio and
`Preferences > General > External scripting using = Local` works for you, use
the [main README](README.md) — it is simpler. This document is for everyone
whose `GetResolve()` returns `None`.

Design, evidence, and measurements:
[`docs/superpowers/specs/2026-07-24-resolve-inproc-mcp-design.md`](docs/superpowers/specs/2026-07-24-resolve-inproc-mcp-design.md).

---

## Why the free edition needs this

Resolve free ships without Fusion's internal script server, so
`DaVinciResolveScript.scriptapp("Resolve")` returns `None` for **any external
process** — a venv, a shell, an MCP server, anything. No amount of environment
variables fixes it, because the gate is not about paths.

The console sits on the other side of that gate. Open
**Workspace → Console → Py3** and type `resolve.GetProductName()`: it answers.
That code runs inside Resolve's process, so it never asks the bridge for a
handle — it already has one.

So the server moves there. `src/inproc/` registers a fake
`DaVinciResolveScript` module in `sys.modules` **before** any repo import, which
hands the repo the console's live handle. The 341 granular tools then work
unmodified, and a normal HTTP MCP server on loopback serves them to any client.

---

## Daily startup

Once setup is done (see below), starting the server is one paste:

1. Open Resolve, open a project.
2. **Workspace → Console**, pick **Py3** in the dropdown.
3. Paste this one line — substitute your own checkout path:

```
INPROC_REPO="/path/to/davinci-resolve-mcp"; exec(open(INPROC_REPO+"/resolve_console_boot.py").read())
```

> This goes in **Resolve's console**, not a terminal. Pasted into zsh it fails
> with ``zsh: parse error near `)' ``.

The path is named once and read back because `exec(open(...).read())` leaves no
`__file__` to derive it from. Hardcoding one in the file would work on exactly
one machine.

You should see 11 self-test checks, then a url and a token:

```
DaVinci Resolve MCP -- in-process boot
  repo    : /path/to/davinci-resolve-mcp
  log     : /path/to/davinci-resolve-mcp/.inproc/inproc.log
  modules purged: 0
  truthful hasattr installed in 12 modules

  utf-8 default encoding  ok    open() default was US-ASCII, now utf-8
  shim installed          ok    DaVinciResolveScript resolves to the shim
  handle identity         ok    granular.resolve is the console handle
  resolve responds        ok    DaVinci Resolve 21.0.3.7
  tools registered        ok    341 tools registered
  threaded dispatch       ok    341 tools wrapped for threaded dispatch
  truthful hasattr        ok    PyRemoteObject exposes 34 methods; missing ones now report False
  live API call           ok    current project: New Project 1
  server thread           ok    serving on http://127.0.0.1:8765
  rejects anonymous       ok    anonymous request got 401, wanted 401
  accepts token           ok    authenticated request accepted

SELF-TEST PASSED -- 11 checks

  url   : http://127.0.0.1:8765
  token : <your token>   (file)
  state : /path/to/davinci-resolve-mcp/.inproc/transport.json

  re-paste the same exec(...) line to reload after edits
```

If any line reads `FAIL`, the last line names which checks failed. The server
still starts — the self-test reports, it does not gate.

The server lives and dies with Resolve. Quit Resolve and it is gone; re-paste to
get it back. There is no daemon, no launchd job, nothing to clean up.

---

## First-time setup on a new machine

### 1. Find the interpreter Resolve's console uses

In the console (Py3), paste:

```
exec(open("/path/to/davinci-resolve-mcp/tools/probe_console.py").read())
```

Read `sys.prefix` out of the output. On macOS with Resolve 21 it is
`/Library/Frameworks/Python.framework/Versions/3.14`.

The dependencies must be built with **that** interpreter, not whatever `python3`
your shell resolves to — the wheels carry ABI tags and the console refuses
mismatched ones.

`sys.executable` points at the Resolve binary rather than a Python. That is
normal for an embedded interpreter, and it is why the next step needs an
explicit path instead of reusing `sys.executable`.

### 2. Vendor the dependencies

From a normal shell, in the repo root:

```bash
python3 tools/setup_inproc.py --python /path/from/step/1/bin/python3
```

Installs `mcp[cli]` and its 34 dependencies into `.inproc/deps` (~53 MB). No
sudo, nothing added to the system framework, nothing outside the repo.

`.inproc/` is gitignored in full — deps, logs, transport state, and token all
stay local.

> `pip install --user` will **not** work here: the console's `sys.path` does not
> include the user site-packages directory, so the install would be invisible to
> it. Hence a target directory the boot adds explicitly.

Confirm the ABI tags match:

```bash
python3 tools/setup_inproc.py --check
```

### 3. Boot the server

The paste line from [Daily startup](#daily-startup) above.

The bearer token is generated on the first boot, written to `.inproc/token` with
mode `600`, and reused on every boot after that. A token that rotated every boot
would invalidate every client config, and Resolve is a GUI app so it never sees
a shell's exported variables — the file is what makes the config stay valid.

### 4. Point a client at it

The MCP endpoint is the url from the boot output **plus `/mcp`**:
`http://127.0.0.1:8765/mcp`.

**Claude Code** — run this from the directory you want the server available in:

```bash
claude mcp add --transport http davinci-resolve \
  http://127.0.0.1:8765/mcp \
  --header "Authorization: Bearer $(cat /path/to/davinci-resolve-mcp/.inproc/token)"
```

`claude mcp add` defaults to **local scope**, so the server appears only in
sessions started from that directory. Pass `--scope user` to get it everywhere.
Restart the session for the tools to show up.

**Claude Desktop** — it speaks stdio only, so it needs `mcp-remote` as a bridge.
Requires Node. Edit
`~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "davinci-resolve": {
      "command": "npx",
      "args": [
        "-y", "mcp-remote",
        "http://127.0.0.1:8765/mcp",
        "--header", "Authorization: Bearer YOUR_TOKEN_HERE"
      ]
    }
  }
}
```

Substitute the contents of `.inproc/token` for `YOUR_TOKEN_HERE`, then restart
Claude Desktop.

**claude.ai in a browser cannot reach this.** The server is bound to loopback on
your machine and the web app runs on Anthropic's. Exposing it through a tunnel
would put control of your Resolve install on the public internet behind nothing
but a bearer token — don't.

### 5. Verify from outside Resolve

```bash
/path/from/step/1/bin/python3 tools/verify_live.py --no-write
```

Reads `.inproc/transport.json` for the endpoint and token, connects as a real
MCP client, and exercises read-only tools.

Drop `--no-write` to also run the write path: it creates a timeline, adds a
marker, reads both back, then deletes them and confirms the project returned to
its baseline. It writes to whatever project is currently open, so open a scratch
project first if that matters to you.

---

## Reloading after a code edit

Re-paste the same line. The boot stops the running server, purges every `src.*`
module from `sys.modules`, and re-imports. Existing MCP clients reconnect on
their own.

The purge is why editing `src/inproc/` does not require restarting Resolve. It
also means a syntax error in your edit surfaces as a failed boot with a
traceback, leaving no server running — re-paste after fixing.

---

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `INPROC_REPO` | none (required) | checkout path; set by the paste line itself |
| `DAVINCI_MCP_TOKEN` | `.inproc/token` | overrides the persisted bearer token |
| `DAVINCI_MCP_HOST` | `127.0.0.1` | **binding off loopback exposes Resolve on the network** |
| `DAVINCI_MCP_PORT` | `8765` | |
| `INPROC_LOG_NAME` | `inproc.log` | log filename under `.inproc/` |
| `INPROC_STATE_NAME` | `transport.json` | endpoint file clients read |
| `INPROC_TOKEN_NAME` | `token` | token filename |

Resolve does not inherit a shell's exported variables, so in practice the
file-based token is the one that matters. The last three exist so the offline
harness cannot overwrite the real server's identity.

---

## Troubleshooting

**`UnicodeDecodeError` on the paste.** The console's `open()` defaults to
US-ASCII, because `locale.getencoding()` returns `US-ASCII` in Resolve's
environment. `resolve_console_boot.py` is deliberately ASCII-only for exactly
this reason — it gets read before any encoding fix can exist. If you edited it,
you introduced a non-ASCII character. Modules imported later are exempt: Python
always reads source files as UTF-8.

**`no 'resolve' in the console namespace`.** Wrong dropdown. Pick **Py3**, not
Lua and not Py2.

**`dependencies missing at .../.inproc/deps`.** Step 2 was skipped, or
`INPROC_REPO` points at a different checkout than the one you installed into.

**`port 8765 is already bound`.** A server from a previous session is still
alive and the console lost its handle — typically after Resolve reloaded the
console namespace. Quit and reopen Resolve, or boot on another port with
`DAVINCI_MCP_PORT` (and update your client config to match).

**Boot prints nothing after `modules purged`.** It is still importing. The
granular surface takes under a second normally; longer means Resolve is busy
with something else.

**A tool returns `TypeError: 'NoneType' object is not callable`.** That method
does not exist in your edition. Resolve's `PyRemoteObject` returns `None` for
any unknown attribute instead of raising, which is why `hasattr()` is always
`True` and every version guard in the repo silently takes the "new API" branch.
The boot installs a truthful `hasattr` so guarded call sites return a clear
message instead — but only the call sites that have a guard.

**Nothing appears in the console but the server works.** Output from background
threads goes to `.inproc/inproc.log`, not the console. Fusion's `fu_stdout` is
bound to the console's thread through a PyCapsule and raises `SystemError` when
written from anywhere else, so the boot routes off-thread writes to the file.
When a tool misbehaves, that log is where the traceback is.

**Client connects but lists no tools.** Restart the client session. Both Claude
Code and Claude Desktop enumerate tools at startup only.

---

## What works on the free edition

Measured on Resolve 21.0.3.7, macOS, via `tools/sweep_free_edition.py`:

- **341 tools** registered and advertised
- **60 / 60** read-only tools swept, all passed
- Writes verified end to end: create timeline → add marker → read back → delete
  marker → delete timeline by `unique_id` → confirm baseline restored
- Calls serialized under a single lock, because the Resolve API is not
  thread-safe; verified by a test that fails when the lock is removed

**Not verified: 35 of the 38 tools that depend on Studio-only features** —
transcription, neural engine (Magic Mask, Smart Reframe), voice isolation, Super
Scale, cloud projects, Dolby Vision, stereoscopic 3D. They are all in the write
and destructive classes, so the sweep does not call them, and they were not
tested by hand either.

The *getters* for those features do work. The licence gate is on doing, not on
asking.

---

## Repo layout

| Path | What it is |
|---|---|
| `resolve_console_boot.py` | the paste target; ASCII-only by necessity |
| `src/inproc/shim.py` | fake `DaVinciResolveScript` in `sys.modules` |
| `src/inproc/encoding.py` | UTF-8 default for `open()` |
| `src/inproc/streams.py` | thread-safe console output |
| `src/inproc/dispatch.py` | serializes tool calls onto a worker thread |
| `src/inproc/launcher.py` | uvicorn in a daemon thread, bearer auth |
| `src/inproc/api_probe.py` | `hasattr` that tells the truth about remote objects |
| `src/inproc/selftest.py` | the 11 boot checks |
| `tools/probe_console.py` | console probe (interpreter, threads, sockets) |
| `tools/setup_inproc.py` | vendors deps into `.inproc/deps` |
| `tools/verify_live.py` | external MCP client; read and write verification |
| `tools/sweep_free_edition.py` | screens tool names, sweeps the safe ones |
| `tools/fake_console.py` | offline harness; no Resolve needed |

The only change to the repo proper is `_launch_resolve()` in
`src/granular/common.py`, which would otherwise open a **second** Resolve
instance when the in-process handle goes stale.
