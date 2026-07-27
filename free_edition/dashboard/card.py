"""The in-process bridge card, and the runtime splice that puts it on the page.

Upstream's dashboard is one Python module holding one giant `HTML` string, and
`Handler._html()` re-encodes that string on every request. `_mcp_status_payload()`
is likewise called by bare global name on every `/api/mcp/status` hit. Those two
facts are the entire integration surface: rebind the module attributes after
import and the next request serves the patched version. No upstream file
imports, mentions, or is edited for any of this.

`install(dash)` does both halves:

  * markup -- a `<style>` before `</head>` and a `<script>` before `</body>`,
    where the script *wraps* `renderMcpDiagnostics` and appends our card to
    `#diagnosticsMcpServer` after upstream has finished rendering into it.
    Appending afterwards is deliberate: upstream replaces the panel's innerHTML
    wholesale and only then attaches its transport-toggle listener, so anything
    that ran earlier would be thrown away, and `insertAdjacentHTML('beforeend')`
    does not reparse the siblings that listener is bound to.

  * payload -- `_mcp_status_payload` gains an `"inproc"` key, which is what the
    script renders.

Both are idempotent and neither is allowed to raise: a diagnostics card is
worth exactly nothing if a missing anchor can take the whole dashboard down.

THE FRAGILE PART, on purpose (risk register #10): the splice matches on upstream
source text -- `</head>`, `</body>`, and the assumption that upstream's inline
script is a *classic* script. Classic is what lets our script see `state`,
`DIAG_ICONS`, `statusPill`, `diagRow`, `escapeHtml` and `renderMcpDiagnostics`
by bare name with zero plumbing; top-level `const`s live in the global
declarative environment shared by every classic script in the document. If
upstream ever adds `type="module"`, or externalizes the page into static assets,
those names vanish. `check_anchors()` states every one of those assumptions as a
value you can print, `install()` logs an ERROR naming whichever one broke, and
the injected JS logs a console warning for the two conditions only the browser
can see. Run `check_anchors()` after every upstream merge.
"""

import logging
from typing import Any, Dict, List

from . import status

logger = logging.getLogger("davinci-resolve-mcp.dashboard.card")

# Set on the dashboard module once install() has run. Dunder-ish on purpose:
# it lands in the namespace of a module we do not own.
SENTINEL = "__free_edition_dashboard_card__"

# Present in the spliced markup, so a re-install can tell "already done" from
# "never done" even if the sentinel was lost (module reloaded, HTML restored).
CSS_MARKER = 'data-free-edition="inproc-card-css"'
SCRIPT_MARKER = 'data-free-edition="inproc-card-js"'


CSS = r"""  <style data-free-edition="inproc-card-css">
    .inproc-log {
      margin: var(--space-2) 0 0;
      padding: var(--space-3);
      background: var(--bg-base);
      border: 1px solid var(--border-subtle);
      border-radius: var(--radius-md);
      max-height: 220px;
      overflow: auto;
      font-family: var(--font-mono, monospace);
      font-size: 11px;
      line-height: 1.5;
      color: var(--text-secondary);
      white-space: pre-wrap;
      word-break: break-word;
    }
  </style>
"""


SCRIPT = r"""  <script data-free-edition="inproc-card-js">
    // Free-edition in-process bridge card, injected at runtime by
    // free_edition/dashboard/card.py. src/analysis_dashboard.py knows nothing
    // about it.
    //
    // Classic script on purpose. The upstream inline script above is classic
    // too, so `state`, `DIAG_ICONS`, `statusPill`, `diagRow`, `escapeHtml` and
    // `renderMcpDiagnostics` all resolve by bare name from here -- no refetch,
    // no re-implementation, no exports to add upstream. Every one of those
    // lookups is guarded, because turning the script above into a module would
    // move all of them into module scope and erase them from here.
    (function () {
      if (window.__freeEditionInprocCard__) { return; }
      window.__freeEditionInprocCard__ = true;

      function buildInprocCard(ip) {
        const title = DIAG_ICONS.connection + 'In-process bridge (free edition)';
        if (!ip || !ip.configured) {
          return `
          <div class="diag-card inproc-card">
            <div class="diag-card-header">
              <div class="diag-card-title">${title}</div>
              ${statusPill('pill-mute', 'Not configured')}
            </div>
            <div class="diag-card-footer">
              <span>No .inproc/transport.json in this checkout. Applies only to
                DaVinci Resolve free-edition installs using the in-process bridge --
                see free_edition/README.md.</span>
            </div>
          </div>`;
        }
        const tone = ip.reachable ? 'pill-ok' : (ip.pid_alive ? 'pill-warn' : 'pill-mute');
        const label = ip.reachable ? 'Serving' : (ip.pid_alive ? 'Process alive, not answering' : 'Not running');
        const startedText = ip.started_at
          ? new Date(ip.started_at * 1000).toLocaleString()
          : '—';
        const logHtml = (ip.log_tail && ip.log_tail.length)
          ? `<pre class="inproc-log">${escapeHtml(ip.log_tail.join(''))}</pre>`
          : '<div class="empty">No log lines yet.</div>';
        return `
          <div class="diag-card inproc-card">
            <div class="diag-card-header">
              <div class="diag-card-title">${title}</div>
              ${statusPill(tone, label)}
            </div>
            <div class="diag-card-rows">
              ${diagRow('URL', ip.url || '—')}
              ${diagRow('Token', ip.token || '—')}
              ${diagRow('PID', ip.pid ?? '—')}
              ${diagRow('Started', startedText)}
            </div>
            <div class="diag-card-footer">
              <span>Read-only: this dashboard runs as a separate process and cannot start
                or stop the bridge. Boot or reload it from Resolve's own console
                (Workspace &gt; Console &gt; Py3) with
                free_edition/boot/resolve_console_boot.py.</span>
            </div>
            <div class="settings-subhead" style="margin-top:16px">Log tail (.inproc/inproc.log)</div>
            ${logHtml}
          </div>`;
      }

      if (typeof renderMcpDiagnostics !== 'function') {
        // Upstream renamed it, or the script above became a module and its
        // declarations are no longer globals. Either way the card cannot render,
        // and this line is the only place that can say so.
        console.warn('[free-edition] renderMcpDiagnostics is not a global function; ' +
                     'the in-process bridge card will not render. See ' +
                     'free_edition/dashboard/card.py.');
        return;
      }

      const renderUpstream = renderMcpDiagnostics;
      window.renderMcpDiagnostics = function () {
        const result = renderUpstream.apply(this, arguments);
        try {
          // Upstream bails early (and leaves a placeholder in the panel) when
          // the status has not loaded or the request failed. Appending a card
          // to that is worse than showing nothing.
          const data = (typeof state === 'undefined' || !state) ? null : state.mcpStatus;
          if (!data || !data.success) { return result; }
          const host = document.getElementById('diagnosticsMcpServer');
          if (!host || host.querySelector('.inproc-card')) { return result; }
          host.insertAdjacentHTML('beforeend', buildInprocCard(data.inproc));
        } catch (err) {
          console.warn('[free-edition] in-process bridge card failed to render:', err);
        }
        return result;
      };
    })();
  </script>
"""


def check_anchors(dash: Any) -> Dict[str, Any]:
    """State every assumption `install()` makes as a value you can print.

    Cheap enough to call from a test or a console one-liner after an upstream
    merge; `install()` calls it too and logs whatever comes back wrong.
    """
    html = getattr(dash, "HTML", None)
    if not isinstance(html, str):
        return {
            "ok": False,
            "html_is_str": False,
            "problems": ["module has no HTML string"],
        }

    # Every content check below asks about UPSTREAM's markup, so ask it of a
    # copy with our own blocks removed. Otherwise this function starts
    # answering questions about itself -- our script mentions
    # renderMcpDiagnostics and diagnosticsMcpServer too, and a comment in it
    # that quoted a module-type script tag would report upstream as broken.
    upstream_html = html.replace(SCRIPT, "").replace(CSS, "")

    checks = {
        "html_is_str": True,
        "head_close": upstream_html.count("</head>"),
        "body_close": upstream_html.count("</body>"),
        "module_script": 'type="module"' in upstream_html,
        "render_fn": "function renderMcpDiagnostics" in upstream_html,
        "host_element": 'id="diagnosticsMcpServer"' in upstream_html,
        "payload_fn": callable(getattr(dash, "_mcp_status_payload", None)),
        "already_installed": bool(getattr(dash, SENTINEL, False)),
        "css_spliced": CSS_MARKER in html,
        "script_spliced": SCRIPT_MARKER in html,
    }

    problems: List[str] = []
    if checks["body_close"] != 1:
        problems.append(
            "expected exactly one </body> in HTML, found %d" % checks["body_close"])
    if checks["head_close"] != 1:
        problems.append(
            "expected exactly one </head> in HTML, found %d" % checks["head_close"])
    if checks["module_script"]:
        problems.append(
            'HTML contains type="module": upstream script may no longer expose '
            "renderMcpDiagnostics/state as globals, so the card will not render")
    if not checks["render_fn"]:
        problems.append("renderMcpDiagnostics is not declared in HTML")
    if not checks["host_element"]:
        problems.append('no element with id="diagnosticsMcpServer" in HTML')
    if not checks["payload_fn"]:
        problems.append("module has no callable _mcp_status_payload")

    checks["problems"] = problems
    checks["ok"] = not problems
    return checks


def install(dash: Any) -> Dict[str, Any]:
    """Attach the in-process bridge card to an already-imported dashboard module.

    `dash` is `src.analysis_dashboard`, imported normally and left pristine on
    disk. Call after importing it and before the HTTP server starts serving:
    the payload wrapper is picked up per request either way, but the markup
    should be in place for the first page load.

    Idempotent -- a second call is a no-op, whether it is the same process
    re-running the boot or a caller that lost track. Never raises: every failure
    is logged and reported in the return value, because a missing diagnostics
    card must not take the dashboard down.

    Returns a report dict: `{"markup", "payload", "already_installed",
    "css_anchor", "anchors", "problems"}`.
    """
    report: Dict[str, Any] = {
        "markup": False,
        "payload": False,
        "already_installed": False,
        "css_anchor": None,
        "anchors": None,
        "problems": [],
    }

    if getattr(dash, SENTINEL, False):
        report["already_installed"] = True
        logger.debug("free-edition: dashboard card already installed, skipping")
        return report

    anchors = check_anchors(dash)
    report["anchors"] = anchors
    if anchors["problems"]:
        # Loud on purpose. These are the upstream-merge tripwires; a silent
        # skip here is a card that quietly stops existing.
        for problem in anchors["problems"]:
            logger.error("free-edition dashboard card: %s", problem)

    report["markup"] = _install_markup(dash, anchors, report)
    report["payload"] = _install_payload(dash, report)

    setattr(dash, SENTINEL, True)
    logger.info(
        "free-edition dashboard card: markup=%s (css in %s) payload=%s",
        report["markup"], report["css_anchor"], report["payload"])
    return report


def _install_markup(dash: Any, anchors: Dict[str, Any],
                    report: Dict[str, Any]) -> bool:
    """Splice CSS before `</head>` and the script before `</body>`."""
    html = getattr(dash, "HTML", None)
    if not isinstance(html, str):
        report["problems"].append("no HTML string to splice into")
        return False

    if SCRIPT_MARKER in html:
        # Sentinel was cleared but the markup survived (module reload of the
        # boot script, say). Splicing again would render the card twice.
        report["css_anchor"] = "(already spliced)"
        return True

    if "</body>" not in html:
        report["problems"].append(
            "no </body> in HTML: cannot inject the in-process bridge card")
        logger.error(
            "free-edition dashboard card: no </body> anchor in "
            "src.analysis_dashboard.HTML; skipping the card. The dashboard "
            "still works; see free_edition/dashboard/card.py.")
        return False

    if anchors.get("head_close") == 1:
        html = html.replace("</head>", CSS + "</head>", 1)
        report["css_anchor"] = "</head>"
    else:
        # A <style> element inside <body> is non-conforming but universally
        # honoured, and one working card beats a conforming missing one.
        html = html.replace("</body>", CSS + "</body>", 1)
        report["css_anchor"] = "</body>"
        report["problems"].append(
            "no unique </head>: put the card's <style> in <body> instead")

    dash.HTML = html.replace("</body>", SCRIPT + "</body>", 1)
    return True


def _install_payload(dash: Any, report: Dict[str, Any]) -> bool:
    """Add the `inproc` key to `/api/mcp/status`."""
    upstream_payload = getattr(dash, "_mcp_status_payload", None)
    if not callable(upstream_payload):
        report["problems"].append(
            "no callable _mcp_status_payload to wrap: the card will render "
            "as 'Not configured'")
        logger.error(
            "free-edition dashboard card: src.analysis_dashboard has no "
            "callable _mcp_status_payload; the in-process bridge status will "
            "be missing from /api/mcp/status.")
        return False

    if getattr(upstream_payload, "__free_edition__", False):
        return True

    def _payload_with_inproc() -> Any:
        payload = upstream_payload()
        try:
            return {**payload, "inproc": status.inproc_status()}
        except Exception:
            # Never let the bridge's status break upstream's status. The card's
            # JS already treats a missing "inproc" key as "not configured".
            logger.exception(
                "free-edition dashboard card: inproc status failed; serving "
                "the upstream payload unchanged")
            return payload

    _payload_with_inproc.__name__ = getattr(upstream_payload, "__name__",
                                            "_mcp_status_payload")
    _payload_with_inproc.__doc__ = (
        "Upstream's MCP status payload plus the free edition's `inproc` key.\n\n"
        "Installed at runtime by free_edition.dashboard.card.install().")
    _payload_with_inproc.__wrapped__ = upstream_payload
    _payload_with_inproc.__free_edition__ = True

    dash._mcp_status_payload = _payload_with_inproc
    return True


# The migration map names this entry point `integrate.install_dashboard_card`;
# `free_edition.integrate` is expected to delegate straight to it.
install_dashboard_card = install
