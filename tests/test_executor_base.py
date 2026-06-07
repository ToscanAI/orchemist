"""Tests for the shared executor base class and single _PRICING (Issue #927).

Pins:
- AC1-AC5: every concrete executor is an ``isinstance`` of the ``TaskExecutor``
  ABC (forward-looking guarantee; nothing in production dispatches on it today).
- AC7/AC8: ``_PRICING`` lives in ``_common`` and is re-exported by the three
  executors that used to own their own module-scope instance — all the SAME object.
- AC9 (scoped): re-importing the executor modules constructs exactly ONE
  ``PricingTable`` in ``_common`` — NOT a process-global call count (other tests
  and ``CostTracker.__init__`` legitimately build their own instances).
- ``BaseExecutor`` invariant: defines no ``__init__`` and stays abstract, so no
  executor needs ``super().__init__()`` and every init chain is unchanged.
"""

import importlib
from unittest.mock import MagicMock, patch

import pytest

from orchestration_engine.runner import TaskExecutor
from orchestration_engine.executors._common import BaseExecutor, _PRICING
from orchestration_engine.cost_tracker import PricingTable
from orchestration_engine.executors.anthropic_executor import (
    AnthropicExecutor,
    _PRICING as _ANTHROPIC_PRICING,
)
from orchestration_engine.executors.openrouter_executor import (
    OpenRouterExecutor,
    _PRICING as _OPENROUTER_PRICING,
)
from orchestration_engine.executors.gemini_cli_executor import GeminiCliExecutor
from orchestration_engine.executors.claudecode_executor import ClaudeCodeExecutor
from orchestration_engine.openclaw_executor import (
    OpenClawExecutor,
    _PRICING as _OPENCLAW_PRICING,
)


def _make_executors():
    """Construct one of every concrete executor (no real I/O on construction)."""
    mcp = MagicMock()
    mcp.get_context = MagicMock()
    return [
        AnthropicExecutor(api_key="sk-ant-test"),
        OpenRouterExecutor(api_key="or-test"),
        GeminiCliExecutor(),
        ClaudeCodeExecutor(mcp_server=mcp),
        OpenClawExecutor(),
    ]


class TestExecutorABCStandardization:
    """AC1-AC5: all five executors are TaskExecutor instances/subclasses."""

    def test_base_executor_subclasses_task_executor(self):
        assert issubclass(BaseExecutor, TaskExecutor)

    @pytest.mark.parametrize(
        "cls",
        [
            AnthropicExecutor,
            OpenRouterExecutor,
            GeminiCliExecutor,
            ClaudeCodeExecutor,
            OpenClawExecutor,
        ],
    )
    def test_class_is_subclass_of_task_executor(self, cls):
        assert issubclass(cls, BaseExecutor)
        assert issubclass(cls, TaskExecutor)

    def test_instances_are_isinstance_task_executor(self):
        for ex in _make_executors():
            assert isinstance(ex, TaskExecutor), f"{type(ex).__name__} not a TaskExecutor"
            assert isinstance(ex, BaseExecutor)


class TestBaseExecutorInvariants:
    """BaseExecutor must add no __init__ and stay abstract."""

    def test_base_executor_defines_no_init(self):
        # If a future BaseExecutor.__init__ is added it would silently require
        # super().__init__() calls that the executors do not make.
        assert "__init__" not in BaseExecutor.__dict__

    def test_base_executor_is_abstract(self):
        # It implements none of the three abstract methods, so it cannot be
        # instantiated directly.
        with pytest.raises(TypeError):
            BaseExecutor()

    def test_shared_static_helpers(self):
        # _resolve_task_id: truthy id passes through; falsy -> fresh uuid.
        good = MagicMock()
        good.id = "task-123"
        assert BaseExecutor._resolve_task_id(good) == "task-123"

        empty = MagicMock()
        empty.id = ""
        generated = BaseExecutor._resolve_task_id(empty)
        assert generated and generated != ""

        class _NoId:
            pass

        assert BaseExecutor._resolve_task_id(_NoId())  # falls back to uuid

        # _capture_start_time returns a datetime.
        from datetime import datetime

        assert isinstance(BaseExecutor._capture_start_time(), datetime)


class TestSinglePricingInstance:
    """AC7/AC8/AC9: one shared PricingTable, re-exported, constructed once."""

    def test_pricing_is_a_pricing_table(self):
        assert isinstance(_PRICING, PricingTable)

    def test_executors_reexport_the_same_object(self):
        # AC8: the re-export shims expose the identical _common object.
        assert _ANTHROPIC_PRICING is _PRICING
        assert _OPENROUTER_PRICING is _PRICING
        assert _OPENCLAW_PRICING is _PRICING

    def test_importing_common_constructs_pricing_exactly_once(self):
        # AC9 (scoped per adversary MINOR): re-importing the _common module must
        # build exactly ONE PricingTable. We do NOT assert a process-global
        # call-count==1 (CostTracker.__init__ and other tests build their own).
        import orchestration_engine.executors._common as common_mod

        with patch.object(
            PricingTable, "__init__", autospec=True, return_value=None
        ) as init_spy:
            importlib.reload(common_mod)
            try:
                assert init_spy.call_count == 1, (
                    f"reloading _common constructed PricingTable "
                    f"{init_spy.call_count} times; expected exactly 1"
                )
            finally:
                # Restore a real module state so downstream tests see a valid
                # _PRICING (the patched reload left a half-built instance).
                importlib.reload(common_mod)
