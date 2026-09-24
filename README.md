# lab — an always-on research lab

`labd` is a control plane (plain Python, no LLM, holds every credential) that watches the
world, wakes Claude agents when something needs judgment, rents GPUs within a budget, and
talks on Discord. Agents are `claude -p` runs per wake; research threads resume their own
session every pass. State lives in SQLite (`~/.local/state/lab/lab.db`) and in files under
`projects/<name>/`.

```
Sentinel ──diffs──▶ events ──▶ dispatcher ──▶ Concierge · Scout · Director · Thread×N · Analyst
   (affine.io, llms.txt, code/, corpus,          (headless Claude, guarded by bin/lab-guard)
    curriculum, board, verdicts, audits)                  │  lab CLI (no secrets)
                                                          ▼
Discord gateway ◀──outbox── labd ──leases──▶ Runpod pods {pod_prefix}-NN (labrun + watchdog)
```

Every agent's subagents run on Sonnet 5 (`CLAUDE_CODE_SUBAGENT_MODEL`).

| Role | Model | Woken by | Does |
|---|---|---|---|
| thread | Fable 5.1 (routine job/lease checks: Sonnet 5, same session) | its own loop (`NEXT: now/wait/sleep`), its job finishing/failing, lease changes, messages | one autonomous research mind: owns a direction, edits code, rents/releases its own GPUs, runs experiments, keeps or reverts, logs `results.tsv` — forever, like autoresearch. Resumes the same Claude session every pass; past `research.rotate_context_tokens` (150k) it writes `HANDOVER.md` and continues in a fresh session. |
| director | Opus 5.5 | results, stalls, claims, people's ideas/tickets, briefs, new king, hourly | portfolio: starts / steers / retires threads (max `research.max_threads`), weighs human ideas |
| scout | Sonnet 5 | `world.change.*` (normal+) | updates `world/STATE.md`, `KNOWN_STALE.md`, publishes briefs |
| analyst | Opus 5.5 (claims), Sonnet 5 (daily report) | `thread.claim`, 09:00 | red-teams claims; daily report of everything the agents did since the last one |
| concierge | Sonnet 5 | @mention / reply in the channel | answers (read-only), forwards guidance to a thread, files ideas for the Director |
| maintainer | Opus 5.5 | an operator's `maint: …` in Discord (or `lab maint request`) | changes the lab itself in its own git worktree, tests it, commits; labd deploys it (below) |

## Operate

```bash
systemctl --user status labd          # runs at boot (linger enabled)
journalctl --user -u labd -f
lab status | lab world | lab events -n 50 | lab runs | lab budget | lab gpu list | lab gpu stock
lab thread list | lab thread show t-001 | lab thread note t-001 --text "try X"
lab gpu pause "reason" | lab gpu resume   # humans only: stop all lab GPUs / allow them again
lab inject "question" --author me     # simulate a Discord request
lab maint list | lab maint request "change X" | lab maint approve m-3   # lab changes (humans only)
lab emit tick.daily_report "now"      # force a daily report
```

Secrets (`KEY=VALUE`, first file wins): `~/.config/lab/secrets.env` (put `RUNPOD_API_KEY` here),
then `~/Work/discord-reporter/.env` (`DISCORD_BOT_TOKEN`). Without a Runpod key every GPU lease
is denied with a clear reason; everything else works.

## Safety model
- Budget is enforced in code: the daily cap ($600) is the only spending limit. Leases reserve their
  remaining hours so parallel requests cannot overshoot; 80% alert; every lab pod stops at 100%.
  Pools are accounting labels. A per-lease approval threshold (`per_experiment_usd`) and a GPU
  concurrency cap (`max_gpus`) exist but are off. Overtime hard stop at a lease's hours +10%; idle pods
  stop after 20 min.
- `test_mode = true` in project.toml applies `[fleet.test_policy]` (currently 1× H100 per lease).
- The fleet only touches pods in its own `pods` table — the Runpod account is shared.
- Agents: `bin/lab-guard` (PreToolUse, exit 2 blocks even under bypassPermissions) stops credential
  reads, env dumps, `git push`, subnet submission/registration, direct Runpod calls, edits to the
  control plane, and killing labd. The concierge runs read-only (`dontAsk` + allowlist).
  It is a seatbelt, not a vault: agents run as the same Unix user.
- World changes reach threads through the Scout's brief → the Director, who notes or retires affected threads.

## Changing the lab from Discord (the maintainer)
Operators (`lab.toml` `[maintainer].operators`, Discord user ids — checked in code) write
`@bot maint: <what to change>`. Anyone else gets a refusal; plain questions still go to the concierge.
1. labd opens request `m-N` and wakes the maintainer (Opus) in a git worktree (`~/.cache/lab-maint/m-N`,
   branch `maint/m-N` from the live HEAD). `bin/lab-guard` keeps it out of the live tree; it cannot push,
   restart labd or deploy. It edits, runs the tests and commits — or makes no commit and asks a question.
2. labd (plain code) checks the branch: nothing under `projects/*/work|world`, nothing credential-shaped,
   and `[maintainer].test_cmd` (`uv run pytest -q`) passes. Then it posts the agent's summary and diffstat.
3. Changes touching the guard, budget/test mode, secret handling, operators, `lab.toml`, `systemd/`, the
   deploy script or `lab/maint.py` wait for `maint approve m-N` (or `maint reject m-N`); so does everything
   when `auto_deploy = false`. Other changes deploy automatically.
4. Deploy: labd stops starting background agents, waits for running ones (≤ `idle_wait_minutes`), then
   starts `bin/lab-deploy` under `systemd-run` (outside labd's cgroup): merge `--no-ff` into the live
   master, `uv sync` if dependencies changed, restart labd, and require a fresh heartbeat plus a working
   `lab status`. If labd is not healthy it reverts the merge (a new commit), restarts, and reports
   `rolled_back`. The outcome is posted as a reply to the request.
`maint status` / `maint list` in Discord, or `lab maint list|show|request|approve|reject` in the terminal.
`lab status` (and so every agent prompt that includes it) lists unfinished requests under "Lab changes".

## Add a project
Create `projects/<name>/` with `project.toml` (copy affine's), `plugin.py` (`sources()` +
`build_world()`), `prompts/`, `GOAL.md`; restart labd.

Tests: `uv run pytest -q`.
