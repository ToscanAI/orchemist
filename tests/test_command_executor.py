import pytest
from orchestration_engine.command_executor import CommandExecutor, CommandSecurityError
from orchestration_engine.schemas import TaskSpec, TaskType, TaskState, Priority

def _make_task(command=None, prompt=None, **kwargs):
    payload = {}
    if command: payload["command"] = command
    if prompt: payload["prompt"] = prompt
    payload.update(kwargs)
    return TaskSpec(id="test-1", type=TaskType.COMMAND, priority=Priority.NORMAL, payload=payload)

def test_echo():
    r = CommandExecutor().execute(_make_task(command="echo hello"))
    assert r.state == TaskState.SUCCESS
    assert "hello" in r.result["text"]
    assert r.result["exit_code"] == 0

def test_failed_command():
    r = CommandExecutor().execute(_make_task(command="python3 -c 'exit(1)'"))
    assert r.state == TaskState.FAILED
    assert r.result["exit_code"] == 1

def test_timeout():
    r = CommandExecutor(default_timeout=1).execute(_make_task(command="sleep 10"))
    assert r.state == TaskState.FAILED
    assert "TIMEOUT" in r.result["text"]

def test_security_blocks():
    r = CommandExecutor().execute(_make_task(command="rm -rf /"))
    assert r.state == TaskState.FAILED
    assert "SECURITY" in r.result["text"]

def test_security_allows():
    r = CommandExecutor(default_allowed_commands=["echo"]).execute(_make_task(command="echo safe"))
    assert r.state == TaskState.SUCCESS

def test_variable_interpolation():
    r = CommandExecutor().execute(_make_task(command="echo {output_dir}", output_dir="/tmp/test"))
    assert r.state == TaskState.SUCCESS
    assert "/tmp/test" in r.result["text"]

def test_command_prefix():
    r = CommandExecutor().execute(_make_task(prompt="COMMAND: echo prefix"))
    assert r.state == TaskState.SUCCESS
    assert "prefix" in r.result["text"]

def test_no_command_fails():
    with pytest.raises(ValueError):
        CommandExecutor().execute(_make_task())
