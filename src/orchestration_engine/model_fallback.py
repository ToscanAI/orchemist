"""Model fallback chain for executor-level model escalation.

When the ``OpenClawExecutor`` exhausts all retry attempts on the primary
model (e.g. Sonnet 4.6 is unavailable), ``ModelFallbackChain`` provides
an ordered list of alternative model tiers to try before finally returning
``TaskState.FAILED``.

Usage::

    chain = ModelFallbackChain(["sonnet", "opus"])
    print(chain.current())   # "sonnet"
    chain.advance()
    print(chain.current())   # "opus"
    print(chain.has_next())  # False

If the caller passes ``None`` or an empty list, the default chain
``["sonnet", "opus"]`` is used automatically.

Issue: #347
"""

from __future__ import annotations

import logging
from typing import List, Optional

logger = logging.getLogger(__name__)

#: Default ordered model tier chain applied when the template does not
#: configure ``model_chain`` or passes an empty list.
DEFAULT_MODEL_CHAIN: List[str] = ["sonnet", "opus"]


class ModelFallbackChain:
    """Ordered list of model tiers to iterate over on retry exhaustion.

    The chain starts at index 0 (the primary tier).  When all retry
    attempts on the current tier fail, call :meth:`advance` to move to
    the next tier.  Use :meth:`has_next` to check whether escalation is
    still possible.

    Args:
        tiers: Ordered list of model tier names (``"haiku"``, ``"sonnet"``,
               ``"opus"``).  Pass ``None`` or an empty list to use
               :data:`DEFAULT_MODEL_CHAIN`.

    Example::

        chain = ModelFallbackChain(["sonnet", "opus", "haiku"])
        while True:
            # ... try task on chain.current() model ...
            if succeeded:
                break
            if not chain.has_next():
                # All tiers exhausted → mark as FAILED
                break
            chain.advance()
    """

    def __init__(self, tiers: Optional[List[str]] = None) -> None:
        if not tiers:
            self._tiers: List[str] = list(DEFAULT_MODEL_CHAIN)
        else:
            self._tiers = list(tiers)

        if not self._tiers:
            # Safeguard: should not happen after the above, but be defensive.
            self._tiers = list(DEFAULT_MODEL_CHAIN)

        self._index: int = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def current(self) -> str:
        """Return the model tier name for the current position in the chain.

        Returns:
            A model tier string such as ``"sonnet"`` or ``"opus"``.
        """
        return self._tiers[self._index]

    def has_next(self) -> bool:
        """Return ``True`` if there is at least one more tier to escalate to.

        Returns:
            ``True`` when :meth:`advance` can be called; ``False`` when the
            chain is already at its last entry.
        """
        return self._index < len(self._tiers) - 1

    def advance(self) -> str:
        """Advance to the next model tier and return its name.

        Raises:
            IndexError: If called when :meth:`has_next` returns ``False``.

        Returns:
            The new (advanced-to) model tier name.
        """
        if not self.has_next():
            raise IndexError(
                f"ModelFallbackChain: cannot advance past the last tier "
                f"(index={self._index}, tiers={self._tiers})"
            )
        self._index += 1
        logger.info(
            "ModelFallbackChain: escalating from tier[%d]='%s' → tier[%d]='%s'",
            self._index - 1,
            self._tiers[self._index - 1],
            self._index,
            self._tiers[self._index],
        )
        return self._tiers[self._index]

    def reset(self) -> None:
        """Reset the chain back to the first tier (index 0)."""
        self._index = 0

    @property
    def index(self) -> int:
        """Current zero-based position within the tier list."""
        return self._index

    @property
    def tiers(self) -> List[str]:
        """Return a copy of the full tier list (immutable view)."""
        return list(self._tiers)

    def __repr__(self) -> str:
        return (
            f"ModelFallbackChain(tiers={self._tiers!r}, "
            f"index={self._index}, current={self.current()!r})"
        )
