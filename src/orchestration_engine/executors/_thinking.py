"""Shared extended-thinking budget map for API executors.

Single source of truth for thinking_level -> budget_tokens. Both the
Anthropic and OpenRouter executors import this; values must stay byte-identical.
"""

from typing import Dict

#: thinking_level -> budget tokens. Keys are the only recognized levels
#: (mirror templates.KNOWN_THINKING_LEVELS). An unknown level resolves to the
#: fail-safe DEFAULT below (thinking disabled), never a silent paid budget.
THINKING_BUDGET: Dict[str, int] = {
    "off" :   0,
    "low": 2048,
    "medium": 8192,
    "high": 32768,
}

#: Fail-safe budget for an unrecognized thinking level (disabled).
DEFAULT_THINKING_BUDGET: int = 0
