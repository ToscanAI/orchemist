# OpenClaw Gateway API — Tools Invoke

The orchestration engine's OpenClaw executor communicates with the OpenClaw gateway
over HTTP to spawn and manage sub-agent sessions for each pipeline phase.

## Endpoint

```
POST /tools/invoke
Host: localhost:{gateway.port}
Authorization: Bearer {gateway.auth.token}
Content-Type: application/json
```

The gateway exposes a single generic endpoint that can invoke any registered tool.
The orchestrator uses three tools: `sessions_spawn`, `sessions_list`, and `sessions_history`.

## Authentication

Bearer token from `openclaw.json` → `gateway.auth.token`. Set via:

- `--gateway-token` CLI flag
- `OPENCLAW_GATEWAY_TOKEN` env var
- Reads from `~/.openclaw/openclaw.json` automatically (planned)

## Gateway URL

Default: `http://localhost:18789`. Set via:

- `--gateway-url` CLI flag
- `OPENCLAW_GATEWAY_URL` env var

The port is configured in `openclaw.json` → `gateway.port`.

## Tool Invocations

### 1. Spawn a Sub-Agent Session

Starts a new isolated session that executes a prompt and returns results.

**Request:**
```json
{
  "tool": "sessions_spawn",
  "args": {
    "task": "Analyze this code for security vulnerabilities...",
    "model": "anthropic/claude-sonnet-4-6",
    "thinking": "low",
    "runTimeoutSeconds": 600
  }
}
```

**Response:**
```json
{
  "ok": true,
  "result": {
    "content": [
      {
        "type": "text",
        "text": "{\"status\":\"accepted\",\"childSessionKey\":\"agent:main:subagent:abc123\",\"runId\":\"def456\",\"modelApplied\":true}"
      }
    ],
    "details": {
      "status": "accepted",
      "childSessionKey": "agent:main:subagent:abc123",
      "runId": "def456",
      "modelApplied": true
    }
  }
}
```

**Key fields:**
- `details.childSessionKey` — use this to poll for completion
- `details.status` — should be `"accepted"`
- `args.model` — full model string (e.g. `anthropic/claude-sonnet-4-6`)
- `args.thinking` — optional: `"low"`, `"medium"`, `"high"`

### 2. Poll Session Status

Check if a spawned session has completed by fetching its history.

**Request:**
```json
{
  "tool": "sessions_list",
  "args": {
    "kinds": ["sub-agent"],
    "activeMinutes": 30,
    "messageLimit": 1
  }
}
```

**Response contains** a JSON string in `result.content[0].text` with:
```json
{
  "count": 3,
  "sessions": [
    {
      "key": "agent:main:subagent:abc123",
      "totalTokens": 15000,
      "abortedLastRun": false,
      "messages": [
        {
          "role": "assistant",
          "content": [{"type": "text", "text": "...output..."}]
        }
      ]
    }
  ]
}
```

### 3. Get Session Output

Fetch the full conversation history of a completed session.

**Request:**
```json
{
  "tool": "sessions_history",
  "args": {
    "sessionKey": "agent:main:subagent:abc123",
    "limit": 5
  }
}
```

**Response contains** a JSON string in `result.content[0].text` with:
```json
{
  "sessionKey": "agent:main:subagent:abc123",
  "messages": [
    {
      "role": "user",
      "content": [{"type": "text", "text": "...prompt..."}]
    },
    {
      "role": "assistant",
      "content": [{"type": "text", "text": "...output..."}],
      "model": "claude-sonnet-4-6",
      "usage": {"input": 500, "output": 2000, "totalTokens": 2500}
    }
  ]
}
```

## Completion Detection

A session is considered complete when:
1. The session appears in `sessions_list` with `abortedLastRun: false`
2. The last message has `role: "assistant"` with a `stopReason` of `"stop"` or `"end_turn"`

A session has failed when:
1. `abortedLastRun: true`
2. Or no assistant message is present after timeout

## Response Parsing

All tool responses follow the same envelope:

```json
{
  "ok": true,
  "result": {
    "content": [{"type": "text", "text": "<JSON-string>"}],
    "details": { ... }
  }
}
```

- `result.details` — pre-parsed structured data (when available)
- `result.content[0].text` — JSON string that needs `json.loads()` for list/history tools

## Error Responses

```json
{
  "ok": false,
  "error": {
    "type": "not_found",
    "message": "Tool not available: invalid_tool"
  }
}
```

HTTP status codes:
- `200` — success (check `ok` field)
- `401` — invalid/missing bearer token
- `404` — tool not found or not permitted
- `405` — wrong HTTP method (must be POST)
- `400` — tool execution error

## Model Tiers

The orchestrator maps template `model_tier` values to full model strings:

| Tier | Model |
|------|-------|
| `haiku` | `anthropic/claude-haiku-4-5-20251001` |
| `sonnet` | `anthropic/claude-sonnet-4-6` |
| `opus` | `anthropic/claude-opus-4-6` |

## Example: Full Pipeline Phase Execution

```python
# 1. Spawn
resp = http_post("/tools/invoke", {
    "tool": "sessions_spawn",
    "args": {
        "task": "Review this code for bugs...",
        "model": "anthropic/claude-sonnet-4-6",
        "thinking": "low"
    }
})
session_key = resp["result"]["details"]["childSessionKey"]

# 2. Poll until complete
while True:
    resp = http_post("/tools/invoke", {
        "tool": "sessions_history",
        "args": {"sessionKey": session_key, "limit": 2}
    })
    history = json.loads(resp["result"]["content"][0]["text"])
    messages = history["messages"]
    if len(messages) >= 2 and messages[-1]["role"] == "assistant":
        output = messages[-1]["content"][0]["text"]
        break
    time.sleep(3)
```
