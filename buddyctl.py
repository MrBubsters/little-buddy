#!/usr/bin/env python3
"""Talk to little-buddy from another machine over its HTTP API. Needs only Python 3, no packages.

    buddyctl ask "what's on today"                      a message, through the assistant
    buddyctl run --repo minecraft-ai "Fix the ..."      a ticket straight to the queue (--worker cheap|standard|strong)
    buddyctl run --name perf-dashboard "Build a ..."    a ticket that starts a NEW local project
    buddyctl tickets                                    recent coding tickets
    buddyctl ticket 3 [--watch]                         one ticket, optionally wait for it to finish
    buddyctl continue 3 "Also add a regression test"    follow up
    buddyctl cancel 3
    buddyctl health

Settings: BUDDY_URL and BUDDY_TOKEN environment variables, or ~/.config/buddy.json
{"url": "http://10.0.0.48:8765", "token": "..."}. The token is API_TOKEN in the server's .env.
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

TERMINAL = ("awaiting_review", "completed", "failed", "cancelled", "closed")


def settings() -> tuple[str, str]:
    url, token = os.getenv("BUDDY_URL", ""), os.getenv("BUDDY_TOKEN", "")
    path = os.path.expanduser("~/.config/buddy.json")
    if (not url or not token) and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
        url, token = url or cfg.get("url", ""), token or cfg.get("token", "")
    if not url or not token:
        sys.exit("Set BUDDY_URL and BUDDY_TOKEN, or create ~/.config/buddy.json with {\"url\": ..., \"token\": ...}")
    return url.rstrip("/"), token


def call(method: str, path: str, body: dict | None = None, timeout: float = 600):
    url, token = settings()
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url + path, data=data, method=method,
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read()).get("error") or json.loads(e.read()).get("message")
        except Exception:  # noqa: BLE001
            msg = e.reason
        sys.exit(f"error {e.code}: {msg}")
    except urllib.error.URLError as e:
        sys.exit(f"can't reach {url}: {e.reason}")


def fmt(t: dict) -> str:
    line = f"#{t['id']:<4} {t['status']:<16} {t['repository']:<14} {t.get('tier', ''):<9} ${t['cost_usd'] or 0:.2f}  {t['objective'][:60]}"
    return line + (f"\n      {t['pr_url']}" if t.get("pr_url") else "")


def show_task(t: dict) -> None:
    print(fmt(t))
    for r in t.get("runs", []):
        cost = f" ${r['cost_usd']:.2f}" if r.get("cost_usd") is not None else ""
        err = f" ({r['error']})" if r.get("error") else ""
        print(f"      run #{r['id']} {r['status']}{cost}{err}: {r['prompt'][:60]}")
    if t.get("live"):
        print(f"      running {t['live'].get('tools', 0)} tools so far, last: {t['live'].get('last', '')}")
    if t.get("summary"):
        print("\n" + t["summary"])


def main(argv: list[str]) -> None:
    p = argparse.ArgumentParser(prog="buddyctl", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("ask"); a.add_argument("text", nargs="+")
    r = sub.add_parser("run"); r.add_argument("--repo", default=None, help="a repository on the allowlist; omit for a new project")
    r.add_argument("--name", default="", help="name for a new project (with no --repo)")
    r.add_argument("--worker", default=None, help="cheap | standard | strong (server default if omitted)")
    r.add_argument("--watch", action="store_true"); r.add_argument("text", nargs="+")
    sub.add_parser("tickets")
    t = sub.add_parser("ticket"); t.add_argument("id", type=int); t.add_argument("--watch", action="store_true")
    c = sub.add_parser("continue"); c.add_argument("id", type=int); c.add_argument("text", nargs="+")
    x = sub.add_parser("cancel"); x.add_argument("id", type=int)
    sub.add_parser("health")
    args = p.parse_args(argv)

    if args.cmd == "health":
        print(json.dumps(call("GET", "/health"), indent=2))
    elif args.cmd == "ask":
        print(call("POST", "/ask", {"text": " ".join(args.text)})["reply"])
    elif args.cmd == "tickets":
        for task in call("GET", "/tickets"):
            print(fmt(task))
    elif args.cmd == "run":
        task = call("POST", "/tickets", {"objective": " ".join(args.text), "repository": args.repo, "worker": args.worker, "project_name": args.name})
        print(f"queued ticket #{task['id']}")
        if args.watch:
            watch(task["id"])
    elif args.cmd == "ticket":
        watch(args.id) if args.watch else show_task(call("GET", f"/tickets/{args.id}"))
    elif args.cmd == "continue":
        print(call("POST", f"/tickets/{args.id}/continue", {"message": " ".join(args.text)})["message"])
    elif args.cmd == "cancel":
        print(call("POST", f"/tickets/{args.id}/cancel", {})["message"])


def watch(task_id: int) -> None:
    last = None
    while True:
        task = call("GET", f"/tickets/{task_id}")
        state = (task["status"], (task.get("live") or {}).get("tools"))
        if state != last:
            print(f"\r{time.strftime('%H:%M:%S')} {task['status']}"
                  + (f", {state[1]} tool calls" if state[1] else "") + " " * 10, end="", flush=True)
            last = state
        if task["status"] in TERMINAL:
            print()
            show_task(task)
            return
        time.sleep(10)


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except KeyboardInterrupt:
        pass
