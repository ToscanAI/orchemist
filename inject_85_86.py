#!/usr/bin/env python3
"""Inject Issue #85 (Phase Output Inspection) and #86 (HITL) into index.html."""
from pathlib import Path

HTML_PATH = Path("src/orchestration_engine/web/templates/index.html")
html = HTML_PATH.read_text(encoding="utf-8")

# ─────────────────────────────────────────────────────────────────────────────
# 1. CSS — inject before the closing </style> tag (the one before CDN scripts)
# ─────────────────────────────────────────────────────────────────────────────
CSS_INJECT = """
/* === Issue #85: Phase Output Inspection === */
.phase-pill { cursor: pointer; }
.phase-pill:hover { transform: scale(1.05); }

/* === Issue #86: Human-in-the-Loop (HITL) === */
.hitl-modal { position: fixed; top: 0; left: 0; right: 0; bottom: 0; background: rgba(0,0,0,0.8); display: flex; align-items: center; justify-content: center; z-index: 1000; }
.hitl-content { background: #1a1a2e; border: 1px solid #4a9eff; border-radius: 12px; padding: 24px; max-width: 800px; width: 90%; max-height: 80vh; overflow-y: auto; }
.hitl-actions { display: flex; gap: 12px; margin-top: 16px; justify-content: flex-end; }
.btn-approve { background: #22c55e; color: white; border: none; padding: 8px 20px; border-radius: 6px; cursor: pointer; }
.btn-edit { background: #4a9eff; color: white; border: none; padding: 8px 20px; border-radius: 6px; cursor: pointer; }
"""

# Inject before the </style> tag that precedes the CDN <script> tags
CSS_ANCHOR = "</style>\n  <script src=\"https://cdn.jsdelivr.net/npm/dompurify"
assert CSS_ANCHOR in html, f"CSS anchor not found"
html = html.replace(CSS_ANCHOR, CSS_INJECT + CSS_ANCHOR, 1)

# ─────────────────────────────────────────────────────────────────────────────
# 2. HTML — add HITL modal + output-area div just before </div>  <!-- #app -->
# ─────────────────────────────────────────────────────────────────────────────
MODAL_HTML = """
  <!-- Issue #86: HITL Modal -->
  <div id="hitl-modal" class="hitl-modal" style="display:none;" aria-modal="true" role="dialog">
    <div class="hitl-content">
      <h3 id="hitl-title">Pipeline Paused</h3>
      <p id="hitl-message"></p>
      <div class="output-viewer" id="hitl-output-preview"></div>
      <div class="hitl-actions">
        <button class="btn-approve" onclick="hitlApprove()">✅ Approve &amp; Continue</button>
        <button class="btn-edit" onclick="hitlShowEdit()">✏️ Edit Output</button>
      </div>
      <div id="hitl-edit-area" style="display:none;margin-top:12px;">
        <textarea id="hitl-edit-text" style="width:100%;min-height:120px;background:#0d1117;color:#e6edf3;border:1px solid #30363d;border-radius:6px;padding:8px;font-family:monospace;"></textarea>
        <div class="hitl-actions" style="margin-top:8px;">
          <button class="btn-approve" onclick="hitlSaveEdit()">💾 Save &amp; Continue</button>
        </div>
      </div>
    </div>
  </div>

  <!-- Issue #85: output-area for phase output inspection -->
  <div id="output-area" style="display:none;"></div>
"""

# Inject before </div>\n</body> (closing tag of #app)
HTML_ANCHOR = "\n  </div>\n\n  <script>"
assert HTML_ANCHOR in html, f"HTML anchor not found: '{HTML_ANCHOR[:40]}'"
html = html.replace(HTML_ANCHOR, MODAL_HTML + HTML_ANCHOR, 1)

# ─────────────────────────────────────────────────────────────────────────────
# 3. JS — inject before the closing </script> at the very end
# ─────────────────────────────────────────────────────────────────────────────
JS_INJECT = """
/* === Issue #85: Phase Output Inspection === */
let _currentRunId = null;

function showPhaseOutput(phaseId, runId) {
  if (!runId) return;
  fetch('/api/run/' + runId + '/outputs')
    .then(r => r.json())
    .then(outputs => {
      const content = outputs[phaseId] || 'No output available for this phase.';
      const viewer = document.getElementById('output-area');
      if (viewer) {
        viewer.style.display = 'block';
        viewer.innerHTML = '<h3>' + escHtml(phaseId) + '</h3>' +
          '<div class="output-viewer">' +
          (typeof DOMPurify !== 'undefined' && typeof marked !== 'undefined'
            ? DOMPurify.sanitize(marked.parse(String(content)))
            : '<pre>' + escHtml(String(content)) + '</pre>') +
          '</div>';
      }
    })
    .catch(function(e) { console.warn('showPhaseOutput error', e); });
}

/* === Issue #86: Human-in-the-Loop (HITL) === */
let _hitlRunId = null;
let _hitlPhaseId = null;

function hitlShow(runId, phaseId, message, outputPreview) {
  _hitlRunId = runId;
  _hitlPhaseId = phaseId;
  document.getElementById('hitl-title').textContent = 'Pipeline Paused — ' + phaseId;
  document.getElementById('hitl-message').textContent = message || 'Review the output and approve to continue.';
  const prev = document.getElementById('hitl-output-preview');
  if (prev) prev.textContent = outputPreview || '';
  document.getElementById('hitl-edit-area').style.display = 'none';
  document.getElementById('hitl-modal').style.display = 'flex';
}

function hitlHide() {
  document.getElementById('hitl-modal').style.display = 'none';
  _hitlRunId = null;
  _hitlPhaseId = null;
}

function hitlApprove() {
  if (!_hitlRunId) return;
  fetch('/api/run/' + _hitlRunId + '/resume', {method: 'POST'})
    .then(function() { hitlHide(); })
    .catch(function(e) { console.warn('hitlApprove error', e); });
}

function hitlShowEdit() {
  fetch('/api/run/' + _hitlRunId + '/outputs')
    .then(r => r.json())
    .then(outputs => {
      const current = outputs[_hitlPhaseId] || '';
      document.getElementById('hitl-edit-text').value = current;
      document.getElementById('hitl-edit-area').style.display = 'block';
    })
    .catch(function(e) { console.warn('hitlShowEdit error', e); });
}

function hitlSaveEdit() {
  if (!_hitlRunId || !_hitlPhaseId) return;
  const newOutput = document.getElementById('hitl-edit-text').value;
  fetch('/api/run/' + _hitlRunId + '/edit', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({phase_id: _hitlPhaseId, output: newOutput})
  }).then(function() { hitlHide(); })
    .catch(function(e) { console.warn('hitlSaveEdit error', e); });
}
"""

# Also patch: renderProgressPanel to make pills clickable
OLD_PILL = (
    "'<div class=\"phase-pill\" id=\"pill-' + escHtml(p.id) + '\">' + escHtml(p.name) + ' &#x23F3;</div>'"
)
NEW_PILL = (
    "'<div class=\"phase-pill\" id=\"pill-' + escHtml(p.id) + '\" onclick=\"showPhaseOutput(\\'' + escHtml(p.id) + '\\',_currentRunId)\">' + escHtml(p.name) + ' &#x23F3;</div>'"
)
assert OLD_PILL in html, f"Old pill template not found"
html = html.replace(OLD_PILL, NEW_PILL, 1)

# Patch: handleProgressEvent to handle 'paused' event and track runId
OLD_HANDLE_PAUSE = (
    "} catch(e) { console.warn('SSE parse error', e); }\n}"
)
NEW_HANDLE_PAUSE = (
    "} else if (d.type === 'paused') {\n"
    "      hitlShow(_currentRunId, d.phase_id, d.message, d.output_preview || '');\n"
    "    }\n"
    "} catch(e) { console.warn('SSE parse error', e); }\n}"
)
assert OLD_HANDLE_PAUSE in html, f"handleProgressEvent pause anchor not found"
html = html.replace(OLD_HANDLE_PAUSE, NEW_HANDLE_PAUSE, 1)

# Patch: connectSSE to track _currentRunId
OLD_CONNECT = "function connectSSE(runId) {\n      const es = new EventSource('/api/run/' + runId + '/status');"
NEW_CONNECT = "function connectSSE(runId) {\n      _currentRunId = runId;\n      const es = new EventSource('/api/run/' + runId + '/status');"
assert OLD_CONNECT in html, f"connectSSE anchor not found"
html = html.replace(OLD_CONNECT, NEW_CONNECT, 1)

# Also patch handleEvent to handle 'paused' event (the main SSE handler)
OLD_HANDLE_ERROR = (
    "} else if (type === 'error') {\n"
    "        logEntry('error', `Error: ${evt.message}`);\n"
    "        setBadge('error', 'error');\n"
    "        resetRunBtn();\n"
    "        if (activeEventSource) { activeEventSource.close(); activeEventSource = null; }\n"
    "      }\n"
    "    }"
)
NEW_HANDLE_ERROR = (
    "} else if (type === 'error') {\n"
    "        logEntry('error', `Error: ${evt.message}`);\n"
    "        setBadge('error', 'error');\n"
    "        resetRunBtn();\n"
    "        if (activeEventSource) { activeEventSource.close(); activeEventSource = null; }\n"
    "      } else if (type === 'paused') {\n"
    "        logEntry('start', `⏸ Pipeline paused after: ${evt.phase_id}`);\n"
    "        hitlShow(_currentRunId, evt.phase_id, evt.message, evt.output_preview || '');\n"
    "      }\n"
    "    }"
)
assert OLD_HANDLE_ERROR in html, f"handleEvent error anchor not found"
html = html.replace(OLD_HANDLE_ERROR, NEW_HANDLE_ERROR, 1)

# Inject the JS before the closing </script> at the end of the file
JS_ANCHOR = "\n</script>\n</body>\n</html>"
assert JS_ANCHOR in html, f"JS anchor not found"
html = html.replace(JS_ANCHOR, JS_INJECT + JS_ANCHOR, 1)

HTML_PATH.write_text(html, encoding="utf-8")
print(f"✅ Injected Issues #85 and #86 into {HTML_PATH} ({len(html)} bytes)")
print(f"   - Phase output inspection: showPhaseOutput, cursor:pointer, scale(1.05)")
print(f"   - HITL modal: hitl-modal, btn-approve, btn-edit, hitlApprove/hitlSaveEdit")
