"""Tool registry: plain Python functions the model can call.

    from typing import Annotated, Literal
    from core.tools import tool

    @tool(writes=True)
    def water_plant(name: Annotated[str, "which plant"], amount: Literal["sip", "soak"] = "sip") -> str:
        '''Log that a plant was watered.'''
        ...

The JSON schema is generated from the signature: type hints give types, `Literal` gives enums,
`Annotated[..., "text"]` gives descriptions, and parameters without defaults are required.
A parameter named `ctx` receives the ToolContext (who is asking, in which mode, ...).
Functions may be sync or async. Return a short string the model can read.
"""
import inspect
import json
import types
import typing
from dataclasses import dataclass, field
from typing import Annotated, Any, Callable, Literal, get_args, get_origin


@dataclass
class ToolContext:
    """Who is talking and how. Passed to tools, prompt sections and hooks."""
    conversation: str = "main"   # chat-history thread id
    speaker: str = ""            # display name of whoever said it
    is_owner: bool = True        # False for someone other than the owner (owner_only tools are hidden)
    mode: str = "text"           # "text", "scheduled" (a job the agent set up), or a mode a front-end adds
    frontend: str = "discord"    # which interface the message came from
    extra: dict = field(default_factory=dict)


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    fn: Callable
    skill: str = ""
    writes: bool = False        # changes state: the result is shown to the user as a receipt
    owner_only: bool = False    # hidden from guests
    enabled: Callable[[], bool] | None = None
    wants_ctx: bool = False
    accepts_any: bool = False

    def schema(self) -> dict:
        return {"type": "function", "function": {
            "name": self.name, "description": self.description, "parameters": self.parameters}}

    def is_enabled(self) -> bool:
        return self.enabled() if self.enabled else True

    def _coerce(self, args: dict) -> dict:
        props = self.parameters.get("properties", {})
        out = {}
        for k, v in (args or {}).items():
            if v in ("", None) or (k not in props and not self.accepts_any):
                continue  # small models often send empty strings or invented arguments
            t = props.get(k, {}).get("type")
            try:
                if t == "integer" and not isinstance(v, int):
                    v = int(str(v).lstrip("#"))
                elif t == "number" and not isinstance(v, (int, float)):
                    v = float(v)
                elif t == "boolean" and isinstance(v, str):
                    v = v.strip().lower() in ("true", "1", "yes")
                elif t == "string" and not isinstance(v, str):
                    v = json.dumps(v) if isinstance(v, (dict, list)) else str(v)
            except (TypeError, ValueError):
                pass  # let the tool report the bad value
            out[k] = v
        return out

    async def run(self, args: dict, ctx: ToolContext) -> str:
        args = self._coerce(args)
        missing = [r for r in self.parameters.get("required", []) if r not in args]
        if missing:
            return f"Error: missing required argument(s): {', '.join(missing)}"
        if self.wants_ctx:
            args["ctx"] = ctx
        result = self.fn(**args)
        if inspect.isawaitable(result):
            result = await result
        if result is None:
            return "OK."
        return result if isinstance(result, str) else json.dumps(result, default=str)


_JSON_TYPES = {str: "string", int: "integer", float: "number", bool: "boolean"}


def _schema_for(hint: Any) -> dict:
    desc = None
    if get_origin(hint) is Annotated:
        hint, *meta = get_args(hint)
        desc = next((m for m in meta if isinstance(m, str)), None)
    if get_origin(hint) in (typing.Union, types.UnionType):  # Optional[T] / T | None
        hint = next(a for a in get_args(hint) if a is not type(None))
    if get_origin(hint) is Literal:
        values = list(get_args(hint))
        s = {"type": _JSON_TYPES.get(type(values[0]), "string"), "enum": values}
    elif get_origin(hint) in (list, tuple) or hint in (list, tuple):
        inner = get_args(hint)
        s = {"type": "array", "items": _schema_for(inner[0]) if inner else {"type": "string"}}
    elif hint is dict or get_origin(hint) is dict:
        s = {"type": "object"}
    else:
        s = {"type": _JSON_TYPES.get(hint, "string")}
    if desc:
        s["description"] = desc
    return s


def build_tool(fn: Callable, *, name: str | None = None, description: str | None = None, writes=False,
               owner_only=False, enabled=None) -> Tool:
    sig = inspect.signature(fn)
    hints = typing.get_type_hints(fn, include_extras=True)
    props, required, wants_ctx, accepts_any = {}, [], False, False
    for p in sig.parameters.values():
        if p.name == "ctx":
            wants_ctx = True
            continue
        if p.kind == p.VAR_KEYWORD:
            accepts_any = True
            continue
        props[p.name] = _schema_for(hints.get(p.name, str))
        if p.default is p.empty:
            required.append(p.name)
    doc = inspect.getdoc(fn) or ""
    description = description or doc.split("\n\n")[0].replace("\n", " ").strip() or fn.__name__
    module = fn.__module__ or ""
    skill = module.split(".")[1] if module.startswith("skills.") and "." in module else module
    return Tool(name or fn.__name__, description, {"type": "object", "properties": props, "required": required},
                fn, skill, writes, owner_only, enabled, wants_ctx, accepts_any)


class Registry:
    def __init__(self):
        self.tools: dict[str, Tool] = {}

    def add(self, t: Tool) -> Tool:
        if t.name in self.tools and self.tools[t.name].fn is not t.fn:
            print(f"[tools] '{t.name}' from {t.skill} replaces the one from {self.tools[t.name].skill}")
        self.tools[t.name] = t
        return t

    def remove_skill(self, skill: str) -> None:
        for n in [n for n, t in self.tools.items() if t.skill == skill]:
            del self.tools[n]

    def available(self, ctx: ToolContext) -> list[Tool]:
        return [t for t in self.tools.values() if t.is_enabled() and (ctx.is_owner or not t.owner_only)]

    def get(self, name: str) -> Tool | None:
        return self.tools.get(name)

    async def run(self, name: str, args: dict, ctx: ToolContext) -> str:
        t = self.tools.get(name)
        if not t or not t.is_enabled():
            return f"Error: unknown tool '{name}'. Available: {', '.join(x.name for x in self.available(ctx))}"
        if t.owner_only and not ctx.is_owner:
            return "Error: only the owner can do that."
        try:
            return await t.run(args, ctx)
        except Exception as e:  # report back so the model can correct itself
            return f"Error: {e.__class__.__name__}: {e}"


REGISTRY = Registry()


def tool(fn: Callable | None = None, **opts):
    """Register a function as a tool. Usable bare (`@tool`) or with options (`@tool(writes=True)`)."""
    def wrap(f):
        REGISTRY.add(build_tool(f, **opts))
        return f
    return wrap(fn) if fn else wrap
