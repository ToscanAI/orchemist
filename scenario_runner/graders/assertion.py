"""Assertion grader — evaluates Python expressions against pipeline output.

Security model: AST-based allowlisting. Only safe node types are permitted.
The expression is parsed into an AST, validated against an allowlist of
node types, and only then evaluated in a restricted namespace.
"""

import ast
from ..models import GradeResult

# Allowed AST node types — anything not on this list is rejected
_ALLOWED_NODES = {
    # Literals and containers
    ast.Expression, ast.Constant, ast.List, ast.Tuple, ast.Dict, ast.Set,
    # Variables
    ast.Name, ast.Load, ast.Store,
    # Operations
    ast.BoolOp, ast.BinOp, ast.UnaryOp, ast.Compare,
    ast.And, ast.Or, ast.Not,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod,
    ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.In, ast.NotIn, ast.Is, ast.IsNot,
    # Subscript and attribute access (needed for output.get, output['key'])
    ast.Subscript, ast.Attribute, ast.Index, ast.Slice,
    # Function calls (restricted to safe builtins at runtime)
    ast.Call, ast.keyword,
    # Conditionals
    ast.IfExp,
    # Starred (for unpacking in function args)
    ast.Starred,
    # Formatted strings
    ast.JoinedStr, ast.FormattedValue,
}

_SAFE_BUILTINS = {
    "len": len,
    "str": str,
    "int": int,
    "float": float,
    "bool": bool,
    "True": True,
    "False": False,
    "None": None,
}

# Attributes that must never be accessed
_BLOCKED_ATTRS = {
    "__import__", "__builtins__", "__class__", "__subclasses__",
    "__globals__", "__locals__", "__code__", "__dict__", "__bases__",
    "__init__", "__new__", "__reduce__", "__mro__", "__getattr__",
    "__setattr__", "__delattr__",
}


def _validate_ast(node: ast.AST) -> str | None:
    """Walk the AST and return an error message if any disallowed node is found."""
    for child in ast.walk(node):
        # Check node type is allowed
        if type(child) not in _ALLOWED_NODES:
            return f"Disallowed syntax: {type(child).__name__}"
        
        # Check attribute access isn't targeting dunder methods
        if isinstance(child, ast.Attribute):
            if child.attr in _BLOCKED_ATTRS:
                return f"Blocked attribute access: {child.attr}"
        
        # Check function calls aren't targeting blocked names
        if isinstance(child, ast.Call) and isinstance(child.func, ast.Name):
            if child.func.id in _BLOCKED_ATTRS:
                return f"Blocked function call: {child.func.id}"
    
    return None


class AssertionGrader:
    """Evaluates a boolean Python expression against a pipeline output dict.

    Uses AST-based allowlisting: the expression is parsed, every node is
    validated against an allowlist, and only then evaluated in a restricted
    namespace with 5 safe builtins + output.
    """

    def grade(self, check_expression: str, output: dict) -> GradeResult:
        """Evaluate *check_expression* against *output*.

        Returns a GradeResult where:
            score = 1.0  if the expression evaluates to truthy
            score = 0.0  if the expression evaluates to falsy or errors
        """
        # Step 1: Parse to AST
        try:
            tree = ast.parse(check_expression, mode="eval")
        except SyntaxError as e:
            return GradeResult(
                passed=False,
                score=0.0,
                details=f"Syntax error in expression: {e}",
                grader_type="assertion",
            )

        # Step 2: Validate AST against allowlist
        error = _validate_ast(tree)
        if error:
            return GradeResult(
                passed=False,
                score=0.0,
                details=f"Blocked expression: {error}",
                grader_type="assertion",
            )

        # Step 3: Compile and eval in restricted namespace
        safe_namespace = {
            "__builtins__": _SAFE_BUILTINS,
            "output": output,
        }

        try:
            code = compile(tree, "<assertion>", "eval")
            result = eval(code, safe_namespace)  # noqa: S307
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
