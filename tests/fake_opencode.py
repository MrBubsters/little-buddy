#!/usr/bin/env python3
"""Stand-in for the `opencode` CLI in tests: emits the JSON events `opencode run --format json` prints.

FAKE_OPENCODE picks the scenario: ok (default) | auth | denied | expensive.
FAKE_OPENCODE_LOG, if set, gets one JSON line per invocation with argv, cwd and the stdin prompt.
"""
import json
import os
import sys
import time

prompt = sys.stdin.read()
if os.environ.get("FAKE_OPENCODE_LOG"):
    with open(os.environ["FAKE_OPENCODE_LOG"], "a", encoding="utf-8") as f:
        f.write(json.dumps({"argv": sys.argv[1:], "cwd": os.getcwd(), "prompt": prompt}) + "\n")

scenario = os.environ.get("FAKE_OPENCODE", "ok")
sid = "ses_" + ("resumed" if "--session" in sys.argv else scenario)
msg = "msg_1"


def emit(d):
    print(json.dumps(d), flush=True)


def step_finish(cost):
    emit({"type": "step_finish", "sessionID": sid, "part": {"type": "step-finish", "messageID": msg, "reason": "stop",
          "tokens": {"total": 2000, "input": 1800, "output": 200}, "cost": cost}})


if scenario == "auth":
    emit({"type": "error", "sessionID": sid, "error": {"name": "APIError", "data": {"message": "401 Incorrect API key provided"}}})
    sys.exit(1)
if scenario == "credits":
    emit({"type": "error", "sessionID": sid, "error": {"name": "APIError", "data": {
          "message": "You have no credits remaining. Add credits to continue using the API."}}})
    sys.exit(1)
emit({"type": "step_start", "sessionID": sid, "part": {"type": "step-start", "messageID": msg}})
emit({"type": "tool_use", "sessionID": sid, "part": {"type": "tool", "tool": "bash", "messageID": msg,
      "state": {"status": "completed", "input": {"command": "npm test"}}}})
if scenario == "denied":
    emit({"type": "tool_use", "sessionID": sid, "part": {"type": "tool", "tool": "bash", "messageID": msg,
          "state": {"status": "error", "input": {"command": "gh pr merge 7"},
                    "error": "The user has specified a rule which prevents you from using this specific tool call."}}})
step_finish(0.01)
if scenario == "expensive":
    step_finish(9.99)
    time.sleep(30)
emit({"type": "text", "sessionID": sid, "part": {"type": "text", "messageID": msg,
      "text": "Docs updated and pushed; opened https://github.com/MrBubsters/demo/pull/8."}})
step_finish(0.02)
