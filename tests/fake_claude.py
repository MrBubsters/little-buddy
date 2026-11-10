#!/usr/bin/env python3
"""Stand-in for the `claude` CLI in tests: emits a scripted stream-json session.

FAKE_CLAUDE picks the scenario: ok (default) | error | auth | sleep | big | transient.
FAKE_CLAUDE_LOG, if set, gets one JSON line per invocation with argv, cwd and the prompt.
"""
import json
import os
import sys
import time

prompt = sys.stdin.read()
if os.environ.get("FAKE_CLAUDE_LOG"):
    with open(os.environ["FAKE_CLAUDE_LOG"], "a", encoding="utf-8") as f:
        f.write(json.dumps({"argv": sys.argv[1:], "cwd": os.getcwd(), "prompt": prompt}) + "\n")

scenario = os.environ.get("FAKE_CLAUDE", "ok")
session = "sess-" + ("resumed" if "--resume" in sys.argv else scenario)


def emit(d):
    print(json.dumps(d), flush=True)


emit({"type": "system", "subtype": "init", "session_id": session})
emit({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": "npm test"}}]}})
emit({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Edit", "input": {"file_path": "src/nav.ts"}}]}})
if scenario == "big":  # one event far beyond asyncio's default 64 KiB line limit (a large file write)
    emit({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Write",
          "input": {"file_path": "data/big.json", "content": "x" * (300 * 1024)}}]}})
    scenario = "ok"
if scenario == "sleep":
    time.sleep(30)
if scenario == "ok":
    result, is_error = f"Fixed it and opened https://github.com/MrBubsters/demo/pull/7 for review.", False
elif scenario == "transient":
    result, is_error = "EPROTO: The TLS handshake failed. Does the server speak TLS on this port?", True
elif scenario == "auth":
    result, is_error = "OAuth token has expired. Please run /login.", True
else:
    result, is_error = "I couldn't complete this: the tests fail for unrelated reasons.", True
emit({"type": "result", "subtype": "success" if not is_error else "error", "is_error": is_error, "result": result,
      "num_turns": 3, "total_cost_usd": 0.42, "session_id": session, "permission_denials": []})
