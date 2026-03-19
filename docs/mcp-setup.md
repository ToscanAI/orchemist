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

> **Note on API keys:** `ANTHROPIC_API_KEY` is not required for MCP connectivity itself. If the variable is unset, the MCP server will print a warning to stderr but will start normally. To configure an API key for live pipeline runs, see the [Execution Modes](#execution-modes) section below.

---

## Execution Modes

Orchemist pipelines can run in two modes. Choose the one that matches your setup:

### Standalone Mode

**Requires:** `ANTHROPIC_API_KEY` environment variable (or passed as `--api-key` flag on the CLI).

Standalone mode calls the Anthropic API directly using your API key. **OpenClaw is not required.** This is the right mode for any user who has an Anthropic API key and no OpenClaw installation.

Get your API key at: [console.anthropic.com/settings/keys](https://console.anthropic.com/settings/keys)

### OpenClaw Mode

**Requires:** An OpenClaw gateway token (`OPENCLAW_GATEWAY_TOKEN`).

OpenClaw mode routes pipeline execution through the OpenClaw gateway infrastructure. This is the right mode if you're running inside an OpenClaw deployment and already have a gateway token configured.

> **New user without OpenClaw?** Use standalone mode. The rest of this guide covers standalone configuration in full.

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

**Minimal configuration** (no API key — MCP connectivity only):

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

**Standalone configuration** (recommended — includes `ANTHROPIC_API_KEY` for live pipeline runs):

```json
{
  "mcpServers": {
    "orchemist": {
      "command": "orch",
      "args": ["mcp", "--transport", "stdio"],
      "env": {
        "ANTHROPIC_API_KEY": "sk-ant-..."
      }
    }
  }
}
```

Replace `sk-ant-...` with your actual key from [console.anthropic.com/settings/keys](https://console.anthropic.com/settings/keys).

**What this does:** Claude Code will launch `orch mcp --transport stdio` as a child process and communicate with it over standard input/output. The `env` block injects `ANTHROPIC_API_KEY` into the server process, enabling standalone mode pipeline execution without setting the variable globally in your shell.

> **Schema note:** The `mcpServers` JSON schema above follows the format documented at [https://docs.anthropic.com/en/docs/claude-code/mcp](https://docs.anthropic.com/en/docs/claude-code/mcp). Both project-scope (`.claude/mcp.json`) and global scope (`~/.claude/mcp.json`) locations are supported by Claude Code. The `env` key (not `environment` or `envVars`) is the correct field name for injecting environment variables into an MCP server process.

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

### Step 2b — Add the API key (standalone mode)

To enable live pipeline runs from Cursor, add `ANTHROPIC_API_KEY` to the server's environment:

1. In the same MCP server configuration panel (Settings > Features > MCP), locate the **Environment Variables** or **Env** field for the Orchemist server entry you just created.
2. Add a new environment variable:
   - **Name:** `ANTHROPIC_API_KEY`
   - **Value:** `sk-ant-...` (your actual key from [console.anthropic.com/settings/keys](https://console.anthropic.com/settings/keys))
3. Save and close settings.

> **Note:** The exact UI label for the env field may vary by Cursor version. Look for "Environment", "Env vars", or "Env" within the MCP server configuration panel you opened at Settings > Features > MCP.

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

> **Standalone users — no OpenClaw required:** The MCP server (`orch mcp --transport stdio`) starts as a child process of your IDE and communicates over stdin/stdout. It does not connect to any OpenClaw gateway. The tool list check and the terminal command above work identically whether or not OpenClaw is installed or running.

---

## Launching Pipelines from Your IDE

Once the three tools appear in your IDE, you can launch and monitor Orchemist pipelines entirely through natural language — no CLI required.

Type your request into the IDE's chat or composer, and the IDE translates it into the appropriate `orchemist_launch`, `orchemist_status`, or `orchemist_logs` tool call automatically.

### Launching a Coding Pipeline

The coding pipeline template id is `coding-pipeline-standard`. It requires: repo path, branch name, issue title, issue body, issue number, and repo URL.

Example prompt (type this into your IDE chat):

> Launch the coding pipeline for issue #42. The repo is at /home/user/my-project, the branch is feature/fix-login, the issue title is "Fix login timeout bug", the issue number is 42, the repo URL is https://github.com/myorg/my-project, and the issue body is: Users report being logged out after 5 minutes of inactivity. The session timeout is set too aggressively in auth.py. Expected behavior: sessions should remain active for 30 minutes.

The IDE will call `orchemist_launch` with `template_id: "coding-pipeline-standard"` and `mode: "standalone"`, passing your inputs as the `inputs` dict.

### Launching a Content Pipeline

The content pipeline template id is `content-pipeline-v28`. It requires: topic, author name, author facts, voice style, and source material.

Example prompt (type this into your IDE chat):

> Start a content pipeline. Topic: How AI agents are changing software development. Author is René Rivera, a software engineer and AI practitioner with 15 years building production systems. Voice style is direct and builder-focused — for example: "This is broken. Here is why. Here is what I did about it." Source material: [paste or describe your research notes or transcript].

The IDE will call `orchemist_launch` with `template_id: "content-pipeline-v28"` and `mode: "standalone"`.

### Checking Pipeline Status

After launching, ask the IDE to check on your run:

> What is the status of my last pipeline run?

Or, if you have a specific run ID (returned by `orchemist_launch`):

> Show me the status of pipeline run abc12345.

The IDE calls `orchemist_status` and returns current phase, completed phases, elapsed time, and final score.

### Viewing Pipeline Logs

To see what a pipeline is doing:

> Show me the logs for run abc12345.

To see a specific phase's output:

> Show me the output from the spec phase of run abc12345.

The IDE calls `orchemist_logs` (with an optional `phase` argument) and returns the log content.

> **Note:** All prompts above are plain English — you never need to write JSON or tool syntax directly. The IDE infers the correct tool and arguments from your natural language request.

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

---

### Pipelines fail with an authentication error in standalone mode

**Symptom:** The three MCP tools appear correctly, but when you launch a pipeline with `mode: standalone`, it fails with an API authentication error or "No API key configured" message.

**Cause:** `ANTHROPIC_API_KEY` is not set in the environment where the MCP server process runs. Even though the MCP server starts successfully without the key (connectivity does not require it), live pipeline execution in standalone mode requires a valid key.

**Remediation:**

- For Claude Code: add an `"env"` block to your `.claude/mcp.json` (see [Claude Code Setup → Step 2](#step-2--add-the-orchemist-server-entry)).
- For Cursor: add the key in the Env field of the MCP server configuration at Settings > Features > MCP.
- Alternatively, set `ANTHROPIC_API_KEY` globally in your shell profile (`~/.bashrc` or `~/.zshrc`) so all IDE processes inherit it.
