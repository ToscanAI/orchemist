#!/usr/bin/env python3
"""
Injection script for Issue #82 (Visual Phase Display) and Issue #84 (Output Viewer).
Modifies src/orchestration_engine/web/templates/index.html in-place.
"""

import re

HTML_PATH = "src/orchestration_engine/web/templates/index.html"

with open(HTML_PATH, "r", encoding="utf-8") as f:
    content = f.read()

# ─── 1. Add marked.js CDN to <head> ─────────────────────────────────────────
MARKED_TAG = '<script src="https://cdn.jsdelivr.net/npm/marked@12.0.0/marked.min.js"></script>'

if MARKED_TAG not in content:
    # Insert just before the first </head>
    content = content.replace("</head>", f"  {MARKED_TAG}\n</head>", 1)
    print("✅  Added marked.js CDN to <head>")
else:
    print("ℹ️   marked.js already present — skipping")

# ─── 2. CSS for Issue #82 (pipeline-viz) ─────────────────────────────────────
CSS_82 = """
/* === Issue #82: Visual Phase Display === */
.pipeline-viz { display: flex; align-items: center; gap: 0; overflow-x: auto; padding: 16px 0; }
.phase-block { background: #1e293b; border: 2px solid #334155; border-radius: 8px; padding: 12px 16px; min-width: 140px; text-align: center; flex-shrink: 0; }
.phase-block .phase-name { font-weight: 600; font-size: 0.9em; }
.phase-block .phase-meta { font-size: 0.75em; color: #94a3b8; margin-top: 4px; }
.phase-connector { color: #4a9eff; font-size: 1.5em; flex-shrink: 0; padding: 0 4px; }
.phase-block.tier-haiku { border-color: #22c55e; }
.phase-block.tier-sonnet { border-color: #4a9eff; }
.phase-block.tier-opus { border-color: #a855f7; }
"""

# ─── 3. CSS for Issue #84 (output-viewer) ────────────────────────────────────
CSS_84 = """
/* === Issue #84: Output Viewer === */
.output-viewer { background: #0d1117; border: 1px solid #30363d; border-radius: 8px; padding: 16px; margin-top: 16px; max-height: 500px; overflow-y: auto; }
.output-viewer h1,.output-viewer h2,.output-viewer h3 { color: #e6edf3; border-bottom: 1px solid #21262d; padding-bottom: 8px; }
.output-viewer code { background: #161b22; padding: 2px 6px; border-radius: 4px; font-size: 0.9em; }
.output-viewer pre { background: #161b22; padding: 12px; border-radius: 6px; overflow-x: auto; }
.output-viewer a { color: #58a6ff; }
.output-viewer ul,.output-viewer ol { padding-left: 24px; }
.phase-output-tab { display: inline-block; padding: 6px 12px; cursor: pointer; border-bottom: 2px solid transparent; color: #8b949e; }
.phase-output-tab.active { border-bottom-color: #4a9eff; color: #e6edf3; }
.output-tabs { border-bottom: 1px solid #30363d; margin-bottom: 12px; }
"""

COMBINED_CSS = CSS_82 + CSS_84

if "pipeline-viz" not in content:
    content = content.replace("</style>", COMBINED_CSS + "\n</style>", 1)
    print("✅  Injected CSS for #82 and #84 before </style>")
else:
    print("ℹ️   pipeline-viz CSS already present — skipping")

# ─── 4. JS for Issue #82 (renderPipelineViz) ─────────────────────────────────
JS_82 = """
// === Issue #82: Visual Phase Display ===
function renderPipelineViz(phases) {
  if (!phases || !phases.length) return '';
  return '<div class="pipeline-viz">' + phases.map((p, i) => {
    let tier = (p.model_tier || 'sonnet').toLowerCase();
    let block = '<div class="phase-block tier-' + escHtml(tier) + '">' +
      '<div class="phase-name">' + escHtml(p.name || p.id) + '</div>' +
      '<div class="phase-meta">' + escHtml(tier) + ' \\u00b7 ' + escHtml(p.thinking_level || 'low') + '</div>' +
    '</div>';
    return (i > 0 ? '<span class="phase-connector">\\u2192</span>' : '') + block;
  }).join('') + '</div>';
}
"""

# ─── 5. JS for Issue #84 (renderOutputViewer + showOutput) ───────────────────
JS_84 = """
// === Issue #84: Output Viewer ===
function renderOutputViewer(phaseOutputs) {
  if (!phaseOutputs || !Object.keys(phaseOutputs).length) return '<div class="output-viewer"><em>No outputs yet. Run the pipeline to see results.</em></div>';
  let tabs = Object.keys(phaseOutputs).map((pid, i) =>
    '<span class="phase-output-tab' + (i===0?' active':'') + '" onclick="showOutput(\\'' + escHtml(pid) + '\\')">' + escHtml(pid) + '</span>'
  ).join('');
  let panels = Object.entries(phaseOutputs).map(([pid, content], i) =>
    '<div class="output-panel" id="output-' + escHtml(pid) + '" style="display:' + (i===0?'block':'none') + '">' +
    '<div class="output-viewer">' + (typeof marked !== 'undefined' ? marked.parse(String(content)) : '<pre>' + escHtml(String(content)) + '</pre>') + '</div></div>'
  ).join('');
  return '<div class="output-tabs">' + tabs + '</div>' + panels;
}

function showOutput(phaseId) {
  document.querySelectorAll('.output-panel').forEach(p => p.style.display = 'none');
  document.querySelectorAll('.phase-output-tab').forEach(t => t.classList.remove('active'));
  const panel = document.getElementById('output-' + phaseId);
  if (panel) panel.style.display = 'block';
  event.target.classList.add('active');
}
"""

COMBINED_JS = JS_82 + JS_84

if "renderPipelineViz" not in content:
    content = content.replace("</script>", COMBINED_JS + "\n</script>", 1)
    print("✅  Injected JS for #82 and #84 before first </script>")
else:
    print("ℹ️   renderPipelineViz JS already present — skipping")

# ─── Write back ──────────────────────────────────────────────────────────────
with open(HTML_PATH, "w", encoding="utf-8") as f:
    f.write(content)

print("✅  index.html updated successfully")

# Quick sanity checks
checks = [
    ("marked.js CDN", "marked@12.0.0"),
    ("pipeline-viz CSS", "pipeline-viz"),
    ("tier-haiku CSS", "tier-haiku"),
    ("tier-sonnet CSS", "tier-sonnet"),
    ("tier-opus CSS", "tier-opus"),
    ("phase-connector CSS", "phase-connector"),
    ("renderPipelineViz JS", "renderPipelineViz"),
    ("output-viewer CSS", "output-viewer"),
    ("phase-output-tab CSS", "phase-output-tab"),
    ("output-tabs CSS", "output-tabs"),
    ("renderOutputViewer JS", "renderOutputViewer"),
    ("showOutput JS", "showOutput"),
]

print("\n── Sanity checks ──")
all_ok = True
with open(HTML_PATH, "r", encoding="utf-8") as f:
    final = f.read()
for label, token in checks:
    ok = token in final
    status = "✅" if ok else "❌"
    print(f"  {status}  {label} ({token!r})")
    if not ok:
        all_ok = False

if all_ok:
    print("\n🎉  All checks passed!")
else:
    print("\n⚠️  Some checks failed — review output above")
