#!/usr/bin/env python3
"""Inject live-progress CSS and JS into index.html for issue #83."""

path = "src/orchestration_engine/web/templates/index.html"

with open(path, encoding="utf-8") as f:
    html = f.read()

print(f"File size: {len(html)} chars")
print(f"</style> count: {html.count('</style>')}")
print(f"</script> count: {html.count('</script>')}")

# ── CSS ────────────────────────────────────────────────────────────────────
progress_css = (
    "\n/* Live Progress (#83) */\n"
    ".progress-bar-fill { background: linear-gradient(90deg, #4a9eff, #7c3aed); height: 100%; transition: width 0.5s ease; border-radius: 4px; }\n"
    ".phase-pill.running { border-color: #4a9eff; animation: pulse 1.5s infinite; }\n"
    ".phase-pill.done { border-color: #22c55e; background: #1a2e1a; }\n"
    ".phase-pill.failed { border-color: #ef4444; background: #2e1a1a; }\n"
    ".totals-row { display: flex; gap: 24px; margin-top: 12px; font-size: 0.9em; color: #aaa; }\n"
    ".totals-row span { color: #fff; }\n"
    "@keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.6; } }\n"
    ".conn-dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; background: #22c55e; margin-right: 6px; }\n"
    ".conn-dot.disconnected { background: #ef4444; }\n"
)
html = html.replace("</style>", progress_css + "\n</style>", 1)
print(f"After CSS inject: </style> count = {html.count('</style>')}")

# ── JS ─────────────────────────────────────────────────────────────────────
# Note: template literals use ${...} which must NOT be interpolated by Python.
# We build the string with concatenation to avoid any f-string / heredoc issues.

progress_js_lines = [
    "",
    "/* Live Progress (#83) */",
    "let _elapsed_timer = null;",
    "let _run_start = null;",
    "",
    "function renderProgressPanel(phases) {",
    "  let pills = phases.map(p =>",
    "    '<div class=\"phase-pill\" id=\"pill-' + escHtml(p.id) + '\">' + escHtml(p.name) + ' &#x23F3;</div>'",
    "  ).join('');",
    "  return '<div class=\"progress-panel\" id=\"progress-panel\">'",
    "    + '<div style=\"display:flex;align-items:center\"><span class=\"conn-dot\" id=\"conn-dot\"></span><strong>Pipeline Running</strong></div>'",
    "    + '<div class=\"progress-bar-container\"><div class=\"progress-bar-fill\" id=\"progress-fill\" style=\"width:0%\"></div></div>'",
    "    + '<div id=\"progress-text\" style=\"font-size:0.85em;color:#aaa\">0 of ' + phases.length + ' phases complete</div>'",
    "    + '<div class=\"phase-timeline\">' + pills + '</div>'",
    "    + '<div class=\"totals-row\">Tokens: <span id=\"total-tokens\">0</span> &nbsp;|&nbsp; Cost: $<span id=\"total-cost\">0.0000</span> &nbsp;|&nbsp; Elapsed: <span id=\"elapsed-time\">0s</span></div>'",
    "    + '</div>';",
    "}",
    "",
    "function handleProgressEvent(evt) {",
    "  try {",
    "    const d = JSON.parse(evt.data);",
    "    if (d.type === 'phase_start') {",
    "      const pill = document.getElementById('pill-' + d.phase_id);",
    "      if (pill) { pill.className = 'phase-pill running'; pill.innerHTML = escHtml(d.phase_name || d.phase_id) + ' [running]'; }",
    "    } else if (d.type === 'phase_complete' || d.type === 'phase') {",
    "      const pill = document.getElementById('pill-' + (d.phase_id || d.id));",
    "      if (pill) { pill.className = 'phase-pill done'; pill.innerHTML = escHtml(d.phase_name || d.phase_id || d.id) + ' [done]'; }",
    "      const ti = document.getElementById('total-tokens');",
    "      const tc = document.getElementById('total-cost');",
    "      if (ti) ti.textContent = parseInt(ti.textContent || '0') + (d.tokens_in||0) + (d.tokens_out||0);",
    "      if (tc) tc.textContent = (parseFloat(tc.textContent || '0') + (d.cost_usd||0)).toFixed(4);",
    "      const completed = document.querySelectorAll('.phase-pill.done').length;",
    "      const total = document.querySelectorAll('.phase-pill').length;",
    "      const fill = document.getElementById('progress-fill');",
    "      const txt = document.getElementById('progress-text');",
    "      if (fill) fill.style.width = (total > 0 ? (completed/total*100) : 0) + '%';",
    "      if (txt) txt.textContent = completed + ' of ' + total + ' phases complete';",
    "    } else if (d.type === 'phase_error') {",
    "      const pill = document.getElementById('pill-' + d.phase_id);",
    "      if (pill) { pill.className = 'phase-pill failed'; pill.innerHTML = escHtml(d.phase_id) + ' [failed]'; }",
    "    } else if (d.type === 'pipeline_complete' || d.type === 'complete') {",
    "      const dot = document.getElementById('conn-dot');",
    "      if (dot) dot.className = 'conn-dot disconnected';",
    "      if (_elapsed_timer) { clearInterval(_elapsed_timer); _elapsed_timer = null; }",
    "    }",
    "  } catch(e) { console.warn('SSE parse error', e); }",
    "}",
    "",
    "function startElapsedTimer() {",
    "  _run_start = Date.now();",
    "  _elapsed_timer = setInterval(function() {",
    "    const el = document.getElementById('elapsed-time');",
    "    if (el) { const s = Math.floor((Date.now() - _run_start)/1000); el.textContent = s < 60 ? s+'s' : Math.floor(s/60)+'m '+s%60+'s'; }",
    "  }, 1000);",
    "}",
    "",
]
progress_js = "\n".join(progress_js_lines)

# Insert before the LAST </script>
last_idx = html.rfind("</script>")
print(f"Last </script> at index: {last_idx}")
if last_idx == -1:
    print("ERROR: no </script> found in file!")
    raise SystemExit(1)

html = html[:last_idx] + progress_js + "\n</script>" + html[last_idx + 9:]
print(f"After JS inject: </script> count = {html.count('</script>')}")

with open(path, "w", encoding="utf-8") as f:
    f.write(html)
print(f"Written {len(html)} chars to {path}")

# Verify all required strings present
checks = [
    "progress-bar-fill", "renderProgressPanel", "handleProgressEvent",
    "startElapsedTimer", "conn-dot", "total-tokens", "total-cost", "elapsed-time",
    "phase-timeline", "phase-pill",
]
all_ok = True
for c in checks:
    found = c in html
    print(f"  {c}: {'OK' if found else 'MISSING'}")
    if not found:
        all_ok = False

print("ALL OK" if all_ok else "SOME MISSING - check above")
