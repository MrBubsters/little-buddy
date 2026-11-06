"""Utilities skill: exact arithmetic, so the model doesn't have to do math in its head."""
import ast
import math
import operator
import re
from typing import Annotated

from core.tools import tool

_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.Pow: operator.pow,
        ast.USub: operator.neg, ast.UAdd: operator.pos}
_NAMES = {k: getattr(math, k) for k in ("pi", "e", "tau")}
_FUNCS = {k: getattr(math, k) for k in ("sqrt", "log", "log10", "log2", "exp", "sin", "cos", "tan", "asin",
                                         "acos", "atan", "floor", "ceil", "factorial", "radians", "degrees")}
_FUNCS.update(abs=abs, round=round, min=min, max=max)


def _eval(node):
    if isinstance(node, ast.Expression):
        return _eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        left, right = _eval(node.left), _eval(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > 1000:
            raise ValueError("exponent too large")
        return _OPS[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval(node.operand))
    if isinstance(node, ast.Name) and node.id in _NAMES:
        return _NAMES[node.id]
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FUNCS:
        return _FUNCS[node.func.id](*[_eval(a) for a in node.args])
    raise ValueError(f"unsupported expression: {ast.dump(node)[:60]}")


@tool
def calculate(expression: Annotated[str, "Python-style math, e.g. '(1250 * 0.15) / 12' or 'sqrt(2) * pi'."]) -> str:
    """Evaluate a math expression exactly (tips, budgets, unit conversions, percentages...)."""
    expr = re.sub(r"(?<=\d),(?=\d{3}\b)", "", expression.replace("^", "**").replace("×", "*"))  # 1,250 → 1250
    result = _eval(ast.parse(expr, mode="eval"))
    if isinstance(result, float) and result.is_integer() and abs(result) < 1e15:
        result = int(result)
    return f"{expression} = {result:,}" if isinstance(result, int) else f"{expression} = {result:.10g}"
