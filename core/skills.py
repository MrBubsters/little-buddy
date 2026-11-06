"""Skill loader and hooks. A skill is a module or package in skills/ (or a SKILL_PATHS folder).

Everything in a skill is optional:

    from core.tools import tool                 # @tool functions the model can call
    from core.skills import command, Forward    # @command("!name") chat commands

    GUIDE = "When to use these tools..."         # prompt text, included while its tools are available
    def context(ctx, query) -> str | None: ...   # live info for the system prompt (keep it short)
    def preprocess(text, ctx, meta) -> str: ...  # rewrite an incoming message before the agent sees it
    async def on_start(): ...                     # once, after the front-end connects
    async def on_tick(): ...                      # every ~30 s (reminders, polling, ...)

Turn skills off with SKILLS_DISABLED=name1,name2. A skill that fails to load is skipped with a
log line, so one broken plug-in doesn't take the assistant down.
"""
import importlib
import importlib.util
import inspect
import os
import sys
import traceback
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Callable

import config
from core.tools import REGISTRY, ToolContext

SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"
SKILLS: dict[str, ModuleType] = {}
FAILED: dict[str, str] = {}


@dataclass
class Forward:
    """Returned by a command to hand (possibly rewritten) text on to the agent."""
    text: str


@dataclass
class Command:
    name: str
    help: str
    fn: Callable
    skill: str


COMMANDS: dict[str, Command] = {}


def command(name: str, help: str = ""):
    """Register a chat command. Handler: fn(args: str, ctx) -> str | Forward | None (sync or async)."""
    def wrap(fn):
        mod = fn.__module__ or ""
        skill = mod.split(".")[1] if mod.startswith("skills.") else mod
        COMMANDS[name.lower()] = Command(name.lower(), help, fn, skill)
        return fn
    return wrap


async def run_command(text: str, ctx: ToolContext):
    """Run `!command args` if registered. Returns its result, or None if it isn't a command."""
    if not text.startswith("!"):
        return None
    name, _, args = text.partition(" ")
    cmd = COMMANDS.get(name.lower())
    if not cmd:
        return None
    result = cmd.fn(args.strip(), ctx)
    if inspect.isawaitable(result):
        result = await result
    return result


def _discover() -> list[tuple[str, Path]]:
    found = []
    for base in [SKILLS_DIR, *map(Path, config.SKILL_PATHS)]:
        if not base.is_dir():
            if base != SKILLS_DIR:
                print(f"[skills] SKILL_PATHS folder not found: {base}")
            continue
        for p in sorted(base.iterdir()):
            if p.name.startswith(("_", ".")):
                continue
            if p.suffix == ".py":
                found.append((p.stem, p))
            elif p.is_dir() and (p / "__init__.py").exists():
                found.append((p.name, p / "__init__.py"))
    return found


def _import(name: str, path: Path) -> ModuleType:
    full = f"skills.{name}"
    if full in sys.modules:
        return sys.modules[full]
    if path.parent == SKILLS_DIR or path.parent.parent == SKILLS_DIR:
        return importlib.import_module(full)
    # A skill from an outside folder: load it under the skills.* namespace.
    is_pkg = path.name == "__init__.py"
    spec = importlib.util.spec_from_file_location(
        full, path, submodule_search_locations=[str(path.parent)] if is_pkg else None)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[full] = mod
    spec.loader.exec_module(mod)
    return mod


def load() -> dict[str, ModuleType]:
    importlib.import_module("skills")  # the namespace outside skills are loaded into
    for name, path in _discover():
        if name.lower() in config.SKILLS_DISABLED or name in SKILLS:
            continue
        try:
            SKILLS[name] = _import(name, path)
        except Exception as e:
            FAILED[name] = f"{e.__class__.__name__}: {e}"
            REGISTRY.remove_skill(name)
            for k in [k for k, c in COMMANDS.items() if c.skill == name]:
                del COMMANDS[k]
            print(f"[skills] '{name}' failed to load and was skipped: {FAILED[name]}")
            if os.getenv("SKILLS_DEBUG"):
                traceback.print_exc()
    return SKILLS


def hooks(attr: str):
    """(skill name, hook) for every loaded skill that defines `attr`."""
    for name, mod in SKILLS.items():
        fn = getattr(mod, attr, None)
        if fn is not None:
            yield name, fn


async def call(fn, *args):
    result = fn(*args)
    return await result if inspect.isawaitable(result) else result


async def run_hooks(attr: str, *args) -> None:
    """Call a hook on every skill, logging (not raising) failures."""
    for name, fn in hooks(attr):
        try:
            await call(fn, *args)
        except Exception as e:
            print(f"[skills] {name}.{attr} failed: {e!r}")
