"""The in-process diagnostics card for upstream's analysis dashboard.

Holds the two halves of one feature: `status.py` computes the `inproc` status
payload (is the console server configured, is its transport reachable, what does
its log say) and `card.py` carries the CSS and JavaScript that render it as a
card inside the dashboard's MCP diagnostics panel.

Upstream's `src/analysis_dashboard.py` stays pristine and knows about neither
file. `free_edition.integrate.install_dashboard_card()` attaches them at runtime
instead: it wraps `_mcp_status_payload` to add the `inproc` key and splices the
markup into the module-level `HTML` constant, both of which the request handler
re-reads on every request. That is also why this package is a sibling of the
dashboard rather than part of it - the injection is one-directional, and nothing
upstream may point back here.
"""
