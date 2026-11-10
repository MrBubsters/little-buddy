# Delegating coding work to Claude Code

little-buddy can hand coding tasks to coding agents running headless on the same machine:
[Claude Code](https://code.claude.com) on your subscription, and [OpenCode](https://opencode.ai) for
any other provider's model (OpenAI, Google, OpenRouter, a local Ollama model, ...) billed per request. You say "fix the pathfinding in the Minecraft bot and open a PR" on Discord (or from
another computer with `buddyctl.py`); the local model writes a brief and queues a **task**; a Claude
Code session works on it in its own git worktree; when it finishes you get the summary and the pull
request link. The local model never runs code or chooses where to work: the repositories, budgets
and permissions are fixed in `.env`.

```
 you (Discord / buddyctl) ──► Qwen writes a brief ──► delegate_task ──► agent_tasks (SQLite)
                                                                             │ every 30 s
                                                      worktree  ◄── runner ──┘
                                                      claude -p --output-format stream-json …
                                                             │ result event: summary, cost, session id
                                                      Discord: "👀 Task #3 done, PR ready" + link
```

## Setup (once, on the machine that runs the bot)

1. **Install Claude Code** for the user the service runs as, and log in: run `claude`, then `/login`.
   For a server without a browser, generate a token on your desktop with `claude setup-token` and set
   `CLAUDE_CODE_OAUTH_TOKEN` in the service's environment (not in `.env`). Pro, Max and Team plans work.
2. **GitHub**: `gh auth login`, then `gh auth setup-git` so pushes over https use the same login.
3. **Clone the repositories** the agent may work on, e.g. `git clone … ~/minecraft-ai`. If their tests need
   Node, install it for that user (nvm works without root; the bot adds `~/.nvm/versions/node/*/bin` to PATH).
4. **OpenCode (optional, for non-Claude tiers)**: `npm install -g opencode-ai`, put the provider key in
   `.env` (e.g. `OPENAI_API_KEY=...`; OpenCode reads the environment the bot passes it), and create
   `~/.config/opencode/opencode.json` with the permissions an unattended run needs. The bot does not
   auto-approve anything; this file is the policy:
   ```json
   {
     "$schema": "https://opencode.ai/config.json",
     "permission": {
       "edit": "allow",
       "webfetch": "allow",
       "bash": {
         "*": "allow",
         "gh pr merge*": "deny", "git push --force*": "deny", "git push -f*": "deny",
         "git reset --hard*": "deny", "git checkout main*": "deny", "git checkout master*": "deny"
       }
     }
   }
   ```
   `opencode models <provider>` lists model ids once the key is set.
5. In `.env`:
   ```
   AGENT_REPOS=minecraft-ai=~/minecraft-ai,little-buddy=~/little-buddy
   AGENT_TIERS=cheap=opencode:openai/gpt-6-luna,standard=claude:sonnet,strong=claude:opus
   AGENT_DEFAULT_TIER=standard
   AGENT_MAX_BUDGET_USD=5
   AGENT_MONTHLY_BUDGET_USD=50
   AGENT_MAX_CONCURRENT=1
   ```
   A tier is `name=backend:model`. Backend `claude` takes `sonnet` or `opus`; backend `opencode` takes
   OpenCode's `provider/model`. Add a tier or swap a model here; nothing else changes.
6. Restart the bot. `!skills` should list `delegate`, and `!status` shows the three extra tools.

Verify the login works for the service user without a terminal:
```
cd ~/minecraft-ai && echo "say hi" | ~/.local/bin/claude -p --output-format json | head -c 400
```

## Using it

| You say / type | What happens |
|---|---|
| "Investigate why the bot walks into walls near water, fix it and open a PR" | The model calls `delegate_task`; you get "Queued task #3 … `!cancel 3` stops it". |
| `!tasks` | Recent tasks with status, cost and PR links. |
| `!task 3` | One task in detail: its runs and the agent's full summary. |
| "how's the pathfinding fix going?" | The model answers from `task_status`. |
| `!continue 3 also add a test for the lava case` | A follow-up run that resumes the same Claude session. |
| `!cancel 3` | Stops a queued or running task. |
| `!close 3` | Removes the worktree after the PR is merged (the branch stays on GitHub). |
| "have GPT write the docs for..." / "use opus for this one" | `worker=cheap` / `worker=strong`. Otherwise the assistant picks: cheap for docs, summaries and small changes, standard for ordinary fixes and features. |

Statuses: `queued → running → awaiting_review` (a PR exists) or `completed` (no PR), `failed`,
`cancelled`, `closed`.

Each task gets its own worktree under `AGENT_WORKTREES` on branch `agent/task-<n>`, based on the
repository's default branch (fetched first). The agent is told to commit, push and open a PR, and is
blocked from `gh pr merge`, force pushes and `git reset --hard`: for Claude Code via `--disallowedTools`,
for OpenCode via the permission file above. The per-run cap is passed to Claude Code as
`--max-budget-usd`; for OpenCode the bot adds up the cost it reports per step and stops the run when it
passes the cap. The monthly cap is checked against recorded run costs before each start.

If an agent finds a clear bug unrelated to its assigned task, it leaves that bug's code unchanged and
creates an issue in the GitHub repository where the bug lives. The issue should include concise evidence
or reproduction steps and impact; the agent checks for duplicates first and reports any repository or
permission blocker instead of claiming an issue was created. The final summary includes links to created issues.

If the bot restarts mid-run, the run is recorded as `interrupted by restart`; `!continue <n> …`
resumes the session.

## From another computer: `buddyctl.py`

Set `API_TOKEN` in `.env` (any long random string) and `API_HOST` to the server's LAN IP or `0.0.0.0`,
restart, then copy `buddyctl.py` anywhere with Python 3:

```
export BUDDY_URL=http://10.0.0.48:8765 BUDDY_TOKEN=<the token>     # or ~/.config/buddy.json
python3 buddyctl.py health
python3 buddyctl.py ask "what's on today"
python3 buddyctl.py run --repo minecraft-ai --watch "Fix the pathfinding near water and add a regression test"
python3 buddyctl.py tasks
python3 buddyctl.py continue 3 "Also handle lava"
```

`ask` goes through the local model like a Discord message (same conversation history). `run` skips the
model and queues the task directly. Results are still announced on Discord. The API is plain HTTP with a
bearer token: keep it on your LAN or behind an SSH tunnel.

## Settings

| Setting | Meaning |
|---|---|
| `AGENT_REPOS` | `name=path,…`. Only these; the model picks a name, never a path. Empty = skill off. |
| `CLAUDE_PATH` | The CLI binary. Default: `~/.local/bin/claude` if present. |
| `AGENT_TIERS` / `AGENT_DEFAULT_TIER` | Worker tiers as `name=backend:model`, and the one used when unspecified. |
| `OPENCODE_PATH` | The OpenCode CLI. Default: found under `~/.nvm`, `~/.opencode/bin` or `~/.local/bin`. |
| `AGENT_MAX_BUDGET_USD` / `AGENT_MONTHLY_BUDGET_USD` | Per run (enforced by Claude Code) / per month (checked before each run). |
| `AGENT_MAX_CONCURRENT` | Parallel runs. Keep 1 on a Pro plan. |
| `AGENT_TIMEOUT_MINUTES` | A run is stopped after this long. |
| `AGENT_PERMISSION_MODE` | Claude Code permission mode for unattended runs (`auto`, `acceptEdits`, `bypassPermissions`, `dontAsk`). Denied tool calls are counted in the result. |
| `AGENT_WORKTREES` | Where worktrees live (default `~/.cache/little-buddy/worktrees`). |
| `API_TOKEN`, `API_HOST`, `API_PORT` | The HTTP API for `buddyctl.py`. Off unless the token is set. |

## Troubleshooting

- **"❌ … credentials are missing or expired"**: for a Claude tier, run `claude` as the service user and `/login`
  (or refresh `CLAUDE_CODE_OAUTH_TOKEN`); for an OpenCode tier, check the provider key in `.env`.
- **OpenCode run ends with "tool call(s) were blocked"**: the permission file denied something it needed.
  Check `!task <n>` for which command, and adjust `~/.config/opencode/opencode.json`.
- **"usage limit was hit"**: the plan's window is exhausted; the task is marked failed. `!continue` it later.
- **Run finished with denied tool calls**: the permission mode blocked something. Check `!task <n>` and
  consider `AGENT_PERMISSION_MODE=acceptEdits` or `bypassPermissions` (the worktree limits the blast radius).
- **Nothing starts**: `!tasks` shows `queued` forever → check the bot log for `[jobs]`/`[skills] delegate` errors,
  and that `AGENT_REPOS` paths exist and are git checkouts.
