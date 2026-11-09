"""Planner trash: deleted items are hidden but restorable."""
import os
import sys
import tempfile
import unittest

_tmp = tempfile.mkdtemp()
os.environ.setdefault("DB_PATH", os.path.join(_tmp, "test.db"))
os.environ.update(GCAL_ENABLED="false", WEB_ENABLED="false", TIMEZONE="UTC")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import skills  # noqa: E402
from core.tools import REGISTRY, ToolContext  # noqa: E402

skills.load()
import asyncio  # noqa: E402

from skills.planner import store  # noqa: E402


def run(coro):
    return asyncio.run(coro)


class TrashTest(unittest.TestCase):
    def test_delete_hides_and_restore_brings_back(self):
        item = store.add_item("Buy stamps", "task", category="personal")
        out = run(REGISTRY.run("delete_item", {"item": str(item["id"])}, ToolContext()))
        self.assertIn("Deleted", out)
        self.assertIn("restore_item", out)
        self.assertEqual(store.find_open(str(item["id"])), [])
        self.assertNotIn(item["id"], [i["id"] for i in store.list_items(60)])
        self.assertIn(item["id"], [i["id"] for i in store.list_deleted()])
        self.assertIn("Buy stamps", run(skills.run_command("!trash", ToolContext())))
        out = run(skills.run_command(f"!restore {item['id']}", ToolContext()))
        self.assertIn("Restored", out)
        self.assertEqual(store.find_open(str(item["id"]))[0]["id"], item["id"])
        self.assertIn("isn't in the trash", run(REGISTRY.run("restore_item", {"id": item["id"]}, ToolContext())))

    def test_list_items_can_include_done(self):
        item = store.add_item("Old chore", "task", category="personal")
        store.complete_item(item["id"])
        self.assertNotIn("Old chore", run(REGISTRY.run("list_items", {"days_ahead": 60}, ToolContext())))
        self.assertIn("Old chore", run(REGISTRY.run("list_items", {"days_ahead": 60, "include_done": True}, ToolContext())))


if __name__ == "__main__":
    unittest.main()
