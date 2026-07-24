# Running the MCP server inside DaVinci Resolve

For the **free edition**, which disables the external scripting bridge. The
server runs inside Resolve's own Python console, where the `resolve` object
already exists and the licence gate is never consulted.

Design and evidence: `docs/superpowers/specs/2026-07-24-resolve-inproc-mcp-design.md`.

## Setup on a new machine

### 1. Find the interpreter Resolve's console uses

Open Resolve, then **Workspace -> Console**, pick **Py3** in the dropdown, and
paste:

```
exec(open("/path/to/davinci-resolve-mcp/tools/probe_console.py").read())
```

Read `sys.prefix` out of the output. On macOS with Resolve 21 it is
`/Library/Frameworks/Python.framework/Versions/3.14`. The dependencies must be
built with *that* interpreter, not whatever `python3` your shell resolves to:
the wheels carry ABI tags and the console will refuse mismatched ones.

Note `sys.executable` points at the Resolve binary, not a Python. That is normal
for an embedded interpreter, and it is why the next step needs an explicit path.

### 2. Vendor the dependencies

From a normal shell:

```bash
python3 tools/setup_inproc.py --python /path/from/step/1/bin/python3
```

Installs `mcp[cli]` and its 34 dependencies into `.inproc/deps` (~53 MB). No
sudo, nothing added to the system framework. The console's `sys.path` does not
include the user site-packages directory, so `pip install --user` would be
invisible to it — hence a target directory the boot adds explicitly.

Verify the ABI tags match your console's Python version:

```bash
python3 tools/setup_inproc.py --check
```

### 3. Boot the server

In Resolve's console, Py3 dropdown, one line — substitute your own path:

```
INPROC_REPO="/path/to/davinci-resolve-mcp"; exec(open(INPROC_REPO+"/resolve_console_boot.py").read())
```

You should see 11 self-test checks pass, then a url and a bearer token. The
token is written to `.inproc/token` and reused on every later boot, so client
configs stay valid.

Re-paste the same line any time to reload after editing code: the running server
stops and every module is re-imported.

### 4. Point your MCP client at it

```bash
claude mcp add --transport http davinci-resolve \
  http://127.0.0.1:8765/mcp \
  --header "Authorization: Bearer $(cat .inproc/token)"
```

Then verify from outside Resolve:

```bash
/path/from/step/1/bin/python3 tools/verify_live.py --no-write
```

Drop `--no-write` to also exercise the write path — it creates a timeline and a
marker, reads both back, and removes them.

## Order of operations

Resolve must be running with a project open **before** the console paste, and
the server dies when Resolve quits. Recovery is re-pasting the line.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `INPROC_REPO` | none (required) | checkout path; set by the paste line itself |
| `DAVINCI_MCP_TOKEN` | `.inproc/token` | overrides the persisted bearer token |
| `DAVINCI_MCP_HOST` | `127.0.0.1` | binding off loopback exposes Resolve on the network |
| `DAVINCI_MCP_PORT` | `8765` | |
| `INPROC_LOG_NAME` | `inproc.log` | log filename under `.inproc/` |

Resolve is a GUI application and does not inherit a shell's exported variables,
so in practice the file-based token is the one that matters.

## Troubleshooting

**`UnicodeDecodeError` on the paste.** The console's `open()` defaults to
US-ASCII. `resolve_console_boot.py` is ASCII-only for exactly this reason; if
you edited it, you introduced a non-ASCII character.

**`port 8765 is already bound`.** A server from a previous session is still
alive and the console lost its handle — typically after Resolve reloaded the
console namespace. Quit and reopen Resolve, or boot on another port with
`DAVINCI_MCP_PORT`.

**Boot prints nothing after `modules purged`.** It is still importing. The
granular surface takes under a second normally; longer means Resolve is busy.

**A tool returns `TypeError: 'NoneType' object is not callable`.** The method
does not exist in your edition. The boot installs a truthful `hasattr` so the
repo's own guards catch this first and return a clear message, but only for the
call sites that have a guard.

**Nothing appears in the console but the server works.** Output from background
threads goes to `.inproc/inproc.log`, not the console — Fusion's `fu_stdout` is
bound to the console's thread and raises if written from anywhere else.

## What works on the free edition

Measured on Resolve 21.0.3.7, macOS, via `tools/sweep_free_edition.py`:

- 341 tools registered and advertised
- 60 read-only tools swept, 60 passed
- Writes verified: create timeline, add marker, read back, delete, restore

Not verified: 35 of the 38 tools that depend on Studio-only features
(transcription, neural engine, cloud projects). They are all in the write and
destructive classes, so the sweep does not call them. Note the *getters* for
those features do work — the licence gate is on doing, not on asking.

The per-tool catalog is in `.inproc/cartography/`.
