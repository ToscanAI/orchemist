# MCP Integration Guide — Claude Code + Cursor Setup

Connect Claude Code or Cursor to Orchemist via the Model Context Protocol (MCP). You'll be up and running in under 5 minutes.

---

## Prerequisites

Before you begin, confirm:

- **Orchemist is installed** and the `orch` entry point is available. Verify with:

  ```bash
  orch --version
  ```

  If this fails, see [Troubleshooting](#troubleshooting) below or follow [GETTING_STARTED.md](GETTING_STARTED.md) to install Orchemist.

- **Your IDE is already installed** — either [Claude Code](https://docs.anthropic.com/en/docs/claude-code) or [Cursor](https://cursor.sh).

> **Note on API keys:** `ANTHROPIC_API_KEY` is not required for MCP connectivity itself. If the variable is unset, the MCP server will print a warning to stderr but will start normally. To configure an API key, see [GETTING_STARTED.md](GETTING_STARTED.md).

---

## Claude Code Setup

Claude Code reads MCP server configuration from a JSON file. You can configure Orchemist at project scope (affects only the current project) or globally (available in every Claude Code session).

### Step 1 — Create the config file

Choose a scope:

**Project scope** — create `.claude/mcp.json` in your project root:

```bash
mkdir -p .claude
```

**Global scope** — use `~/.claude/mcp.json` (applies to all projects).

### Step 2 — Add the Orchemist server entry

Write the following content to whichever file you chose:

```json
{
  "mcpServers": {
    "orchemist": {
      "command": "orch",
      "args": ["mcp", "--transport", "stdio"]
    }
  }
}
```

**What this does:** Claude Code will launch `orch mcp --transport stdio` as a child process and communicate with it over standard input/output. The `--transport stdio` flag is explicit but technically redundant — `stdio` is the default transport. It is included here for clarity.

> **Schema note:** The `mcpServers` JSON schema above follows the format documented at [https://docs.anthropic.com/en/docs/claude-code/mcp](https://docs.anthropic.com/en/docs/claude-code/mcp). Both project-scope (`.claude/mcp.json`) and global scope (`~/.claude/mcp.json`) locations are supported by Claude Code.

> **SSE transport:** `orch mcp` also supports `--transport sse` (with an optional `--port` flag, default `8000`) for networked or remote access. SSE transport is out of scope for this guide.

### Step 3 — Restart Claude Code

Fully restart the IDE so it reads the updated config file.

### Step 4 — Verify

Open Claude Code and look at the MCP tools list (the 🔧 tools panel or equivalent). You should see all three Orchemist tools listed by name:

- `orchemist_launch`
- `orchemist_status`
- `orchemist_logs`

If all three appear, setup is complete.

---

## Cursor Setup

Cursor exposes MCP configuration through its settings UI.

### Step 1 — Open MCP settings

1. Open **Settings** (`Cmd+,` / `Ctrl+,`)
2. Navigate to **Features → MCP**
3. Click **Add new global MCP server** (the button label may vary by Cursor release — it may also appear as **+ Add Server**)

> **Server type:** If Cursor prompts you to select a server type, choose **Command** (stdio) — not SSE or HTTP.

### Step 2 — Enter the server command

In the **Command** field, enter:

```
orch mcp --transport stdio
```

Save and close settings.

### Step 3 — Verify

Restart Cursor if prompted. In the Composer panel (or wherever Cursor surfaces MCP tools), confirm you see all three tools by name:

- `orchemist_launch`
- `orchemist_status`
- `orchemist_logs`

If all three appear, setup is complete.

---

## Verify It Works

After completing either setup, the observable sign of a working integration is:

> **All three tool names — `orchemist_launch`, `orchemist_status`, and `orchemist_logs` — are visible in the IDE's tool list.**

You can also confirm the server starts from the terminal:

```bash
orch mcp --transport stdio
```

Expected output to stderr (verified by running on a system with `orchemist` installed):

```
MCP server started
No API key configured — running without auth
```

The second line appears only when `ANTHROPIC_API_KEY` is not set in the environment; it is a warning, not an error. The server starts normally regardless. The process will remain running, waiting for MCP messages on stdin. Press `Ctrl+C` to stop it.

---

## Troubleshooting

### `orch` not found in PATH

**Symptom:** The IDE reports that the command `orch` cannot be found, or the MCP server fails to start.

**Diagnosis:** Confirm where `orch` is installed:

```bash
which orch
```

If `which orch` returns nothing, try:

```bash
pip show orchemist
```

The output will include a `Location:` line — the `orch` binary will be in a `bin/` directory adjacent to that location (e.g. `/usr/local/lib/python3.11/site-packages` → `/usr/local/bin/orch`).

**Remediation options:**

1. **Add the `bin/` directory to your PATH:**

   ```bash
   export PATH="/path/to/bin:$PATH"
   ```

   Add this to `~/.bashrc` or `~/.zshrc` so it persists.

2. **Use the absolute path in your MCP config:**

   Replace `"command": "orch"` with the absolute path to the binary:

   ```json
   {
     "mcpServers": {
       "orchemist": {
         "command": "/path/to/venv/bin/orch",
         "args": ["mcp", "--transport", "stdio"]
       }
     }
   }
   ```

**Terminal success state:** `orch --version` runs without error, and the IDE MCP setup proceeds without a "command not found" error.

---

### `orch` installed in a virtualenv not active in the IDE shell

**Symptom:** `orch` works in your terminal (where you activate the venv), but the IDE cannot find it.

**Cause:** IDEs launch their own shell processes that do not automatically inherit your terminal's virtualenv activation. The `orch` binary lives inside the venv's `bin/` directory, which is not on the default PATH seen by the IDE.

**Remediation options:**

1. **Use the absolute path to the venv's `orch` binary** (recommended):

   Find it with:

   ```bash
   which orch   # run after activating the venv in your terminal
   ```

   Then update your MCP config's `"command"` field to use that absolute path:

   ```json
   {
     "mcpServers": {
       "orchemist": {
         "command": "/home/you/.venvs/myproject/bin/orch",
         "args": ["mcp", "--transport", "stdio"]
       }
     }
   }
   ```

2. **Configure the IDE's integrated terminal to activate the venv automatically** — consult your IDE's documentation for shell initialization settings.

**Terminal success state:** `orch --version` succeeds when run from the IDE's built-in terminal (not just your external terminal).
