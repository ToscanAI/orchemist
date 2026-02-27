# Standalone CLI Test — For René

## What You'll Test
Run a simple 2-phase pipeline (analysis + implementation) using the standalone Anthropic API mode.

## Prerequisites
1. Set your API key: `export ANTHROPIC_API_KEY=sk-ant-...`
2. Be in the orchestration engine directory: `cd /home/toscan/orchestration-engine`

## Step 1: Create a simple test input
```bash
cat > /tmp/test-input.json << 'EOF'
{
  "task_description": "Add a 'hello' CLI command to the orchestration engine that prints 'Hello from the Orchestration Engine v1.0!' when you run 'orch hello'. This is a simple smoke test command.",
  "repository_path": "/home/toscan/orchestration-engine",
  "language": "python",
  "test_framework": "pytest"
}
EOF
```

## Step 2: Run the pipeline (dry-run first)
```bash
# Dry run — no API calls, just validates the template + input
python3 -m orchestration_engine.cli run examples/code-development-pipeline.yaml \
  --input-file /tmp/test-input.json \
  --mode dry-run
```

## Step 3: Run for real (standalone mode)
```bash
python3 -m orchestration_engine.cli run examples/code-development-pipeline.yaml \
  --input-file /tmp/test-input.json \
  --mode standalone
```

## What to Expect
- 5 phases: requirements → implement → code_review → fix → test_generation
- Each phase takes 30-90 seconds
- Total: ~5-10 minutes
- Output files appear in `./output/code-development-pipeline-<timestamp>/`
- Git: a feature branch is created automatically

## Step 4: Check outputs
```bash
ls output/code-development-pipeline-*/
cat output/code-development-pipeline-*/requirements.md
```

## If Something Fails
- Check the error message in the terminal
- Common issues:
  - `ANTHROPIC_API_KEY` not set → standalone mode needs it
  - Dirty working directory → `git stash` first
  - Rate limit → wait 30s and retry
