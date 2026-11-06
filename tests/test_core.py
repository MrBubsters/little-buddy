"""Engine tests with a scripted fake model: no Ollama, Discord or Google needed.

    .venv\\Scripts\\python -m unittest discover tests
"""
import asyncio
import os
import sys
import tempfile
import unittest
from datetime import timedelta

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
_tmp = tempfile.mkdtemp()
os.environ.update(DB_PATH=os.path.join(_tmp, "test.db"), GCAL_ENABLED="false", WEB_ENABLED="false",
                  PERSONA_FILE=os.path.join(_tmp, "none.md"), TIMEZONE="UTC", EMBED_MODEL="",
                  LLM_CONTEXT="8192", OWNER_NAME="Sam")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from typing import Annotated, Literal  # noqa: E402

from core import agent, db, llm, memory, scheduler, skills  # noqa: E402
from core.app import app  # noqa: E402
from core.llm import Reply  # noqa: E402
from core.tools import REGISTRY, ToolContext, build_tool  # noqa: E402

skills.load()


class FakeLLM(llm.Provider):
    """Replays scripted replies and records what it was sent."""
    name = "fake"

    def __init__(self, *replies: Reply):
        self.replies, self.calls = list(replies), []

    async def chat(self, messages, tools=None, **kw):
        self.calls.append({"messages": [dict(m) for m in messages], "tools": tools})
        return self.replies.pop(0) if self.replies else Reply("ok")


def call(name, **args):
    return {"id": f"c-{name}", "name": name, "arguments": args}


def run(coro):
    return asyncio.run(coro)


class ToolSchemaTest(unittest.TestCase):
    def test_schema_from_signature(self):
        def f(ctx, title: Annotated[str, "the title"], kind: Literal["a", "b"], n: int = 3, flag: bool = False):
            """Do a thing.

            Longer explanation that should not be in the description."""
        t = build_tool(f)
        p = t.parameters
        self.assertEqual(t.description, "Do a thing.")
        self.assertEqual(p["required"], ["title", "kind"])
        self.assertEqual(p["properties"]["kind"], {"type": "string", "enum": ["a", "b"]})
        self.assertEqual(p["properties"]["title"]["description"], "the title")
        self.assertEqual(p["properties"]["n"]["type"], "integer")
        self.assertNotIn("ctx", p["properties"])
        self.assertTrue(t.wants_ctx)

    def test_coercion_and_errors(self):
        seen = {}

        def g(id: int, note: str = ""):
            seen.update(id=id, note=note)
            return "fine"
        t = build_tool(g)
        self.assertEqual(run(t.run({"id": "#7", "note": "", "junk": 1}, ToolContext())), "fine")
        self.assertEqual(seen, {"id": 7, "note": ""})
        self.assertIn("missing required", run(t.run({}, ToolContext())))

    def test_guests_dont_see_owner_only_tools(self):
        guest = {t.name for t in REGISTRY.available(ToolContext(is_owner=False))}
        owner = {t.name for t in REGISTRY.available(ToolContext())}
        self.assertIn("remember", owner)
        self.assertNotIn("remember", guest)
        self.assertIn("calculate", guest)

    def test_disabled_tools_hidden(self):
        self.assertNotIn("web_search", {t.name for t in REGISTRY.available(ToolContext())})


class SkillsTest(unittest.TestCase):
    def test_all_skills_load(self):
        self.assertEqual(skills.FAILED, {})
        for name in ("planner", "checkins", "memory", "jobs", "utilities", "web", "system"):
            self.assertIn(name, skills.SKILLS)

    def test_commands(self):
        out = run(skills.run_command("!help", ToolContext()))
        self.assertIn("!agenda", out)
        self.assertIsNone(run(skills.run_command("hello", ToolContext())))

    def test_calculate(self):
        self.assertIn("= 190.5", run(REGISTRY.run("calculate", {"expression": "1,250*0.15+max(2,3)"}, ToolContext())))
        self.assertIn("Error", run(REGISTRY.run("calculate", {"expression": "__import__('os')"}, ToolContext())))


class AgentTest(unittest.TestCase):
    def setUp(self):
        db.clear_messages(None)

    def test_tool_loop_receipts_and_history(self):
        fake = FakeLLM(Reply("", [call("remember", fact="Sam's sister is Ana.")]), Reply("Got it, noted!"))
        llm.set_provider(fake)
        reply = run(agent.chat("my sister is Ana", ToolContext()))
        self.assertTrue(reply.startswith("Got it, noted!"))
        self.assertIn("-# ✔ Remembered", reply)
        # The second request saw the tool result.
        self.assertEqual(fake.calls[1]["messages"][-1]["role"], "tool")
        self.assertEqual([m["role"] for m in db.recent_messages(5)], ["user", "assistant"])
        self.assertTrue(any("Ana" in m["content"] for m in memory.entries("fact")))

    def test_prompt_is_general_and_includes_skills(self):
        llm.set_provider(FakeLLM(Reply("hi")))
        msgs, schemas = run(agent.build_messages("hello", ToolContext()))
        system, newest = msgs[0]["content"], msgs[-1]["content"]
        self.assertIn("generalist", system)
        self.assertIn("schedule_job", system)                    # jobs guide
        self.assertIn("Calendar for resolving dates", newest)    # planner's live context
        self.assertTrue(newest.startswith("<context>") and newest.endswith("hello"))
        self.assertTrue(any(s["function"]["name"] == "add_item" for s in schemas))

    def test_system_prompt_is_cacheable(self):
        """Nothing that changes between messages may leak into the system prompt (it breaks prompt caching)."""
        llm.set_provider(FakeLLM())
        first = run(agent.build_messages("what's on today?", ToolContext()))[0][0]["content"]
        run(memory.add("Plays the cello on Tuesdays.", "fact"))
        second = run(agent.build_messages("tell me about cello", ToolContext()))[0][0]["content"]
        self.assertEqual(first, second)
        self.assertNotIn(db.now().strftime("%Y-%m-%d"), first)

    def test_guest_message_is_attributed(self):
        fake = FakeLLM(Reply("hey"))
        llm.set_provider(fake)
        run(agent.chat("hi there", ToolContext(is_owner=False, speaker="Jo", conversation="g")))
        sent = fake.calls[0]["messages"]
        self.assertTrue(sent[-1]["content"].endswith("(Jo says) hi there"))
        self.assertIn("NOT Sam", sent[-1]["content"])
        self.assertEqual(db.recent_messages(2, "g")[0]["content"], "(Jo says) hi there")  # history stays clean

    def test_step_limit_forces_answer(self):
        loops = [Reply("", [call("list_jobs")]) for _ in range(20)]
        fake = FakeLLM(*loops)
        llm.set_provider(fake)
        run(agent.chat("loop forever", ToolContext(conversation="loop")))
        self.assertIsNone(fake.calls[-1]["tools"])  # final step had tools withdrawn

    def test_unbacked_promise_gets_one_nudge(self):
        fake = FakeLLM(Reply("Sure, I'll check in with you in 2 hours."),
                       Reply("", [call("schedule_job", instruction="Ask about the slides", at="in 2 hours")]),
                       Reply("Scheduled, talk soon."))
        llm.set_provider(fake)
        reply = run(agent.chat("in 2 hours check on my slides", ToolContext(conversation="p")))
        self.assertIn("Scheduled", reply)
        self.assertIn("System check", fake.calls[1]["messages"][-1]["content"])

    def test_history_is_budgeted(self):
        for i in range(30):
            db.add_message("user", f"message {i} " + "x" * 2000, "big")
            db.add_message("assistant", "ok", "big")
        llm.set_provider(FakeLLM())
        msgs, _ = run(agent.build_messages("hi", ToolContext(conversation="big")))
        total = sum(llm.tokens(m["content"]) for m in msgs)
        self.assertLess(total, 8192)
        self.assertGreater(len(msgs), 2)


class MemoryTest(unittest.TestCase):
    def test_search_and_preview(self):
        run(memory.add("Favourite food is green curry.", "fact"))
        run(memory.add("Pasta recipe: boil water, add salt, cook 9 minutes.", "note", tags="pasta"))
        self.assertTrue(any("curry" in m["content"] for m in run(memory.search("what food do I like curry"))))
        facts, notes, _ = run(memory.for_prompt("how long to cook pasta", 2000))
        self.assertTrue(any("Pasta" in n["content"] for n in notes))

    def test_long_fact_becomes_preview(self):
        mid = run(memory.add("Project list: " + "detail " * 600, "fact"))
        facts, _, complete = run(memory.for_prompt("unrelated", 300))
        self.assertFalse(complete)
        preview = next(f for f in facts if f["id"] == mid)
        self.assertIn(f"recall '#{mid}'", preview["content"])
        self.assertIn("detail detail", run(REGISTRY.run("recall", {"query": f"#{mid}"}, ToolContext())))

    def test_duplicates_merge(self):
        a = run(memory.add("Likes tea.", "fact"))
        self.assertEqual(a, run(memory.add("likes tea.", "fact")))


class SchedulerTest(unittest.TestCase):
    def setUp(self):
        self.sent = []

        async def notify(text):
            self.sent.append(text)
        app.attach("test", notify)

    def test_agent_job_runs_and_repeats(self):
        llm.set_provider(FakeLLM(Reply("How did the week go?")))
        j = scheduler.add("weekly review", "Ask how the week went", db.now() - timedelta(minutes=1),
                          recurrence="weekly")
        run(scheduler.tick())
        self.assertEqual(self.sent, ["How did the week go?"])
        self.assertGreater(scheduler.get(j["id"])["next_run"], db.fmt(db.now()))
        self.assertEqual(scheduler.get(j["id"])["enabled"], 1)

    def test_skip_and_one_shot(self):
        llm.set_provider(FakeLLM(Reply("SKIP")))
        j = scheduler.add("check", "Only tell me if X", db.now() - timedelta(minutes=1))
        run(scheduler.tick())
        self.assertEqual(self.sent, [])
        self.assertEqual(scheduler.get(j["id"])["enabled"], 0)

    def test_schedule_job_tool(self):
        at = db.fmt(db.now() + timedelta(hours=1)).replace("T", " ")
        out = run(REGISTRY.run("schedule_job", {"instruction": "Follow up on the report", "at": at}, ToolContext()))
        self.assertIn("Scheduled", out)
        past = run(REGISTRY.run("schedule_job", {"instruction": "x", "at": "2000-01-01 10:00"}, ToolContext()))
        self.assertIn("past", past)


class NaturalTimeTest(unittest.TestCase):
    def setUp(self):
        from datetime import datetime
        self._now = db.now
        db.now = lambda: datetime(2026, 11, 6, 0, 30)  # a Friday, just after midnight

    def tearDown(self):
        db.now = self._now

    def test_phrases(self):
        cases = {
            "friday 17:00": "2026-11-06 17:00",       # later today
            "next friday 5pm": "2026-11-13 17:00",
            "every friday at 5": "2026-11-06 17:00",
            "someday": None,
            "monday 9am": "2026-11-09 09:00",
            "tomorrow": "2026-11-07",
            "tomorrow at 9": "2026-11-07 09:00",
            "tonight": "2026-11-06 20:00",
            "in 2 hours": "2026-11-06 02:30",
            "noon": "2026-11-06 12:00",
            "12:15am": "2026-11-07 00:15",            # already passed today
            "2026-12-01 08:00": "2026-12-01 08:00",
            "sat": "2026-11-07",
        }
        for phrase, want in cases.items():
            if want is None:
                self.assertRaises(ValueError, db.normalize, phrase)
            else:
                self.assertEqual(db.normalize(phrase), want, phrase)


class PlannerTest(unittest.TestCase):
    def test_add_list_complete(self):
        due = db.fmt(db.now() + timedelta(days=1)).replace("T", " ")
        out = run(REGISTRY.run("add_item", {"title": "Dentist", "kind": "event", "category": "personal",
                                            "due_at": due}, ToolContext()))
        self.assertTrue(out.startswith("Saved:"), out)
        item_id = int(out.split("#")[1].split()[0])
        self.assertIn("Dentist", run(REGISTRY.run("list_items", {}, ToolContext())))
        self.assertIn("✅", run(REGISTRY.run("complete_item", {"id": item_id}, ToolContext())))
        self.assertIn("Error", run(REGISTRY.run("delete_item", {"id": 99999}, ToolContext())))


if __name__ == "__main__":
    unittest.main()
