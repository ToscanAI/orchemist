"""Assertion grader — evaluates Python expressions against pipeline output.

Security model: restricted eval with only safe builtins exposed.
No __builtins__, no import, no os access.
"""

from ..models import GradeResult

# Patterns that are explicitly blocked regardless of other restrictions
_BLOCKED_PATTERNS = (
    "__import__",
    "__builtins__",
    "__class__",
    "__subclasses__",
    "__globals__",
    "__locals__",
    "__code__",
    "__dict__",
    "__bases__",
    "import ",
    "exec(",
    "eval(",
    "open(",
    "compile(",
    "getattr(",
    "setattr(",
    "delattr(",
    "vars(",
    "dir(",
    "globals(",
    "locals(",
)

_SAFE_BUILTINS = {
    "len": len,
    "str": str,
    "int": int,
    "float": float,
    "bool": bool,
}


class AssertionGrader:
    """Evaluates a boolean Python expression against a pipeline output dict.

    Uses a restricted eval environment — only the five safe builtins
    (len, str, int, float, bool) plus ``output`` are in scope.
    Attempts to use anything else raise a ValueError.
    """

    def grade(self, check_expression: str, output: dict) -> GradeResult:
        """Evaluate *check_expression* against *output*.

        Returns a GradeResult where:
            score = 1.0  if the expression evaluates to truthy
            score = 0.0  if the expression evaluates to falsy or errors
        """
        # Pre-scan for blocked patterns
        for pattern in _BLOCKED_PATTERNS:
            if pattern in check_expression:
                return GradeResult(
                    passed=False,
                    score=0.0,
                    details=f"Blocked expression: contains '{pattern}'",
                    grader_type="assertion",
                )

        # Build safe namespace — __builtins__ restricted to known-safe set
        safe_namespace = {
            "__builtins__": _SAFE_BUILTINS,
            "output": output,
        }

        try:
            result = eval(check_expression, safe_namespace)  # noqa: S307
            passed = bool(result)
            return GradeResult(
                passed=passed,
                score=1.0 if passed else 0.0,
                details=f"Expression evaluated to {result!r}",
                grader_type="assertion",
            )
        except Exception as exc:
            return GradeResult(
                passed=False,
                score=0.0,
                details=f"Eval error: {type(exc).__name__}: {exc}",
                grader_type="assertion",
            )
