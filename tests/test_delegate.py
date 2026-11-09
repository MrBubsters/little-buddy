"""Delegate skill tests: queue -> worktree -> fake `claude` -> notification, plus the HTTP API.

    .venv/bin/python -m unittest tests.test_delegate
"""
import asyncio
import importlib
import json
import os
import subprocess
import sys
import tempfile
import unittest

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
_tmp = tempfile.mkdtemp()
_repo = os.path.join(_tmp, "demo")
os.makedirs(_repo)
for cmd in (["git", "init", "-q", "-b", "main"], ["git", "config", "user.email", "t@example.com"],
            ["git", "config", "user.name", "Test"], ["git", "commit", "-q", "--allow-empty", "-m", "init"]):
    subprocess.run(cmd, cwd=_repo, check=True)
_fake = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_claude.py")
_fake_oc = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_opencode.py")
_log = os.path.join(_tmp, "claude-calls.jsonl")
_oc_log = os.path.join(_tmp, "opencode-calls.jsonl")
_TIERS = "cheap=opencode:openai/gpt-6-luna,standard=claude:sonnet,strong=claude:opus"

os.environ.update(DB_PATH=os.path.join(_tmp, "test.db"), GCAL_ENABLED="false", WEB_ENABLED="false",
                  PERSONA_FILE=os.path.join(_tmp, "none.md"), TIMEZONE="UTC", EMBED_MODEL="", LLM_CONTEXT="8192",
                  OWNER_NAME="Sam", AGENT_REPOS=f"demo={_repo}", CLAUDE_PATH=_fake, API_TOKEN="secret-token",
                  AGENT_WORKTREES=os.path.join(_tmp, "worktrees"), FAKE_CLAUDE_LOG=_log, FAKE_OPENCODE_LOG=_oc_log,
                  OPENCODE_PATH=_fake_oc, AGENT_TIERS=_TIERS)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402

# When run together with test_core, config was imported first with different settings: apply ours.
config.AGENT_REPOS = {"demo": _repo}
config.CLAUDE_PATH = _fake
config.OPENCODE_PATH = _fake_oc
config.AGENT_TIERS = {"cheap": ("opencode", "openai/gpt-6-luna"), "standard": ("claude", "sonnet"), "strong": ("claude", "opus")}
config.AGENT_DEFAULT_TIER = "standard"
config.API_TOKEN = "secret-token"
config.AGENT_WORKTREES = os.path.join(_tmp, "worktrees")
config.AGENT_MAX_BUDGET_USD, config.AGENT_MONTHLY_BUDGET_USD = 5.0, 50.0

from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

from core import api, db, skills  # noqa: E402
from core.app import app  # noqa: E402
from core.tools import REGISTRY, ToolContext  # noqa: E402

skills.load()
delegate = importlib.reload(skills.SKILLS["delegate"])  # re-read AGENT_REPOS into the tool schema
from skills.delegate import runner, store  # noqa: E402

AUTH = {"Authorization": "Bearer secret-token"}
OBJECTIVE = "Fix the pathfinding so the bot stops walking into walls near water; add a regression test."


def run(coro):
    return asyncio.run(coro)


async def drain():
    await runner.tick()
    await runner.wait_all()


def calls() -> list[dict]:
    if not os.path.exists(_log):
        return []
    with open(_log, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


class DelegateTest(unittest.TestCase):
    def setUp(self):
        db.conn.execute("DELETE FROM agent_tasks")
        db.conn.execute("DELETE FROM agent_runs")
        db.conn.commit()
        self.sent = []

        async def notify(text):
            self.sent.append(text)
        app.attach("test", notify)
        os.environ["FAKE_CLAUDE"] = "ok"
        os.environ["FAKE_OPENCODE"] = "ok"
        for f in (_log, _oc_log):
            if os.path.exists(f):
                os.remove(f)
        run(skills.run_hooks("on_start"))

    def delegate(self, **over) -> dict:
        args = {"objective": OBJECTIVE, "repository": "demo", **over}
        out = run(REGISTRY.run("delegate_task", args, ToolContext()))
        self.assertIn("Queued task #", out)
        return store.list_tasks()[0]

    def test_schema_lists_repositories(self):
        schema = REGISTRY.get("delegate_task").schema()["function"]["parameters"]
        self.assertEqual(schema["properties"]["repository"]["enum"], ["demo"])
        self.assertEqual(schema["properties"]["worker"]["enum"], ["cheap", "standard", "strong"])
        self.assertEqual(schema["required"], ["objective", "repository"])
        self.assertIn("delegate_task", [t.name for t in REGISTRY.available(ToolContext())])

    def test_run_completes_in_worktree_and_notifies(self):
        t = self.delegate()
        self.assertEqual(t["status"], "queued")
        run(drain())
        t = store.get_task(t["id"])
        self.assertEqual(t["status"], "awaiting_review")
        self.assertEqual(t["pr_url"], "https://github.com/MrBubsters/demo/pull/7")
        self.assertEqual(t["session_id"], "sess-ok")
        self.assertAlmostEqual(t["cost_usd"], 0.42)
        self.assertTrue(os.path.isdir(t["worktree"]))
        branch = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=t["worktree"],
                                capture_output=True, text=True).stdout.strip()
        self.assertEqual(branch, f"agent/task-{t['id']}")
        self.assertEqual(len(self.sent), 1)
        self.assertIn("pull/7", self.sent[0])
        self.assertIn("👀 Task #", self.sent[0])
        call = calls()[0]
        self.assertEqual(os.path.realpath(call["cwd"]), os.path.realpath(t["worktree"]))
        self.assertEqual(call["prompt"], OBJECTIVE)
        for flag, value in (("--model", "sonnet"), ("--max-budget-usd", "5.00"), ("--output-format", "stream-json")):
            self.assertEqual(call["argv"][call["argv"].index(flag) + 1], value)
        self.assertNotIn("--resume", call["argv"])
        self.assertIn("Bash(gh pr merge*)", call["argv"])
        run_row = store.task_runs(t["id"])[0]
        self.assertEqual((run_row["status"], run_row["tools"], run_row["turns"]), ("completed", 2, 3))

    def test_continue_resumes_session(self):
        t = self.delegate()
        run(drain())
        out = run(REGISTRY.run("continue_task", {"task": t["id"], "message": "Also cover the lava case."}, ToolContext()))
        self.assertIn("resuming", out)
        run(drain())
        argv = calls()[1]["argv"]
        self.assertEqual(argv[argv.index("--resume") + 1], "sess-ok")
        self.assertEqual(calls()[1]["prompt"], "Also cover the lava case.")
        t = store.get_task(t["id"])
        self.assertEqual(len(store.task_runs(t["id"])), 2)
        self.assertEqual(t["session_id"], "sess-resumed")
        self.assertAlmostEqual(t["cost_usd"], 0.84)

    def test_errors_are_classified_and_reported(self):
        os.environ["FAKE_CLAUDE"] = "auth"
        t = self.delegate()
        run(drain())
        self.assertEqual(store.get_task(t["id"])["status"], "failed")
        self.assertEqual(store.task_runs(t["id"])[0]["error"], "auth")
        self.assertIn("log in", self.sent[0])
        os.environ["FAKE_CLAUDE"] = "error"
        t2 = self.delegate()
        run(drain())
        self.assertEqual(store.get_task(t2["id"])["status"], "failed")
        self.assertIn("❌ Task", self.sent[1])

    def test_monthly_budget_blocks_new_runs(self):
        config.AGENT_MONTHLY_BUDGET_USD = 1.0
        try:
            t = self.delegate()
            run(drain())
            self.assertEqual(store.get_task(t["id"])["status"], "failed")
            self.assertIn("budget", self.sent[0].lower())
            self.assertEqual(calls(), [])
        finally:
            config.AGENT_MONTHLY_BUDGET_USD = 50.0

    def test_cancel_queued_and_running(self):
        t = self.delegate()
        self.assertIn("Cancelled", run(runner.cancel(t["id"])))
        self.assertEqual(store.get_task(t["id"])["status"], "cancelled")

        os.environ["FAKE_CLAUDE"] = "sleep"
        t2 = self.delegate()

        async def go():
            await runner.tick()
            await asyncio.sleep(0.5)
            msg = await runner.cancel(t2["id"])
            await runner.wait_all()
            return msg
        self.assertIn("Stopping", run(go()))
        t2 = store.get_task(t2["id"])
        self.assertEqual(t2["status"], "cancelled")
        self.assertEqual(store.task_runs(t2["id"])[0]["status"], "cancelled")
        self.assertIn("🛑 Task", self.sent[-1])

    def test_commands_and_context(self):
        t = self.delegate()
        self.assertIn(f"task #{t['id']}", run(skills.run_command("!tasks", ToolContext())))
        self.assertIn("in progress", delegate.context(ToolContext(), "hi"))
        run(drain())
        self.assertIsNone(delegate.context(ToolContext(), "hi"))
        self.assertIn("pull/7", run(skills.run_command(f"!task {t['id']}", ToolContext())))
        worktree = store.get_task(t["id"])["worktree"]
        self.assertTrue(os.path.isdir(worktree))
        self.assertIn("Closed", run(skills.run_command(f"!close {t['id']}", ToolContext())))
        self.assertFalse(os.path.exists(worktree))
        self.assertEqual(store.get_task(t["id"])["status"], "closed")

    def test_recover_marks_interrupted_runs(self):
        t = self.delegate()
        r = store.task_runs(t["id"])[0]
        store.update_run(r["id"], status="running", started_at=store.ts())
        store.update_task(t["id"], status="running")
        runner.recover()
        self.assertEqual(store.get_task(t["id"])["status"], "failed")
        self.assertEqual(store.task_runs(t["id"])[0]["error"], "interrupted by restart")

    def test_http_api(self):
        async def go():
            async with TestClient(TestServer(api.make_app())) as c:
                self.assertEqual((await c.get("/health")).status, 401)
                self.assertEqual((await c.get("/health", headers={"Authorization": "Bearer nope"})).status, 401)
                self.assertTrue((await (await c.get("/health", headers=AUTH)).json())["ok"])
                r = await c.post("/tasks", json={"objective": "short", "repository": "demo"}, headers=AUTH)
                self.assertEqual(r.status, 400)
                r = await c.post("/tasks", json={"objective": OBJECTIVE, "repository": "demo", "worker": "turbo"}, headers=AUTH)
                self.assertEqual(r.status, 400)
                r = await c.post("/tasks", json={"objective": OBJECTIVE, "repository": "demo", "worker": "strong"}, headers=AUTH)
                self.assertEqual(r.status, 201)
                task = await r.json()
                self.assertEqual((task["origin"], task["tier"], task["backend"], task["model"], task["status"]),
                                 ("api", "strong", "claude", "opus", "queued"))
                r = await c.get(f"/tasks/{task['id']}", headers=AUTH)
                self.assertEqual(len((await r.json())["runs"]), 1)
                self.assertEqual(len(await (await c.get("/tasks", headers=AUTH)).json()), 1)
                r = await c.post("/ask", json={"text": "!tasks"}, headers=AUTH)
                self.assertIn(f"task #{task['id']}", (await r.json())["reply"])
                r = await c.post(f"/tasks/{task['id']}/cancel", headers=AUTH)
                self.assertEqual(r.status, 200)
                self.assertEqual((await (await c.get(f"/tasks/{task['id']}", headers=AUTH)).json())["status"], "cancelled")
                self.assertEqual((await c.get("/tasks/999", headers=AUTH)).status, 404)
        run(go())


def oc_calls() -> list[dict]:
    if not os.path.exists(_oc_log):
        return []
    with open(_oc_log, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


class OpenCodeTest(DelegateTest):
    """The cheap tier runs through OpenCode with the same queue, worktree and reporting."""

    def test_cheap_tier_runs_through_opencode(self):
        t = self.delegate(worker="cheap")
        self.assertEqual((t["tier"], t["backend"], t["model"]), ("cheap", "opencode", "openai/gpt-6-luna"))
        run(drain())
        t = store.get_task(t["id"])
        self.assertEqual(t["status"], "awaiting_review")
        self.assertEqual(t["pr_url"], "https://github.com/MrBubsters/demo/pull/8")
        self.assertEqual(t["session_id"], "ses_ok")
        self.assertAlmostEqual(t["cost_usd"], 0.03)
        self.assertEqual(calls(), [])  # claude was not used
        call = oc_calls()[0]
        self.assertEqual(call["argv"][:4], ["run", "--format", "json", "--model"])
        self.assertEqual(call["argv"][4], "openai/gpt-6-luna")
        self.assertEqual(os.path.realpath(call["argv"][call["argv"].index("--dir") + 1]), os.path.realpath(t["worktree"]))
        self.assertTrue(call["prompt"].endswith(OBJECTIVE))
        self.assertIn("pull request", call["prompt"])  # the brief carries the instructions
        self.assertNotIn("--session", call["argv"])
        r = store.task_runs(t["id"])[0]
        self.assertEqual((r["status"], r["tools"], r["turns"]), ("completed", 1, 2))
        self.assertIn("cheap: openai/gpt-6-luna", self.sent[0])

    def test_continue_passes_session(self):
        t = self.delegate(worker="cheap")
        run(drain())
        run(REGISTRY.run("continue_task", {"task": t["id"], "message": "Also fix the typos in CONTRIBUTING."}, ToolContext()))
        run(drain())
        argv = oc_calls()[1]["argv"]
        self.assertEqual(argv[argv.index("--session") + 1], "ses_ok")
        self.assertEqual(store.get_task(t["id"])["session_id"], "ses_resumed")

    def test_auth_error_and_denials(self):
        os.environ["FAKE_OPENCODE"] = "auth"
        t = self.delegate(worker="cheap")
        run(drain())
        self.assertEqual(store.task_runs(t["id"])[0]["error"], "auth")
        self.assertIn("API key", self.sent[0])
        os.environ["FAKE_OPENCODE"] = "denied"
        t2 = self.delegate(worker="cheap")
        run(drain())
        self.assertEqual(store.get_task(t2["id"])["status"], "awaiting_review")
        self.assertIn("1 tool call(s) were blocked", self.sent[1])

    def test_budget_stops_the_run(self):
        os.environ["FAKE_OPENCODE"] = "expensive"
        t = self.delegate(worker="cheap")
        run(drain())
        t = store.get_task(t["id"])
        self.assertEqual(t["status"], "failed")
        self.assertEqual(store.task_runs(t["id"])[0]["error"], "budget")
        self.assertGreater(t["cost_usd"], 5)
        self.assertIn("budget", self.sent[0])


if __name__ == "__main__":
    unittest.main()
