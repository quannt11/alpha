# lab — an always-on research lab

`labd` is a control plane (plain Python, no LLM, holds every credential) that watches the
world, wakes Claude agents when something needs judgment, rents GPUs within a budget, and
talks on Discord. Agents are `claude -p` runs per wake; research threads and the Researcher resume
their own session every pass. State lives in SQLite (`~/.local/state/lab/lab.db`) and in files under
`projects/<name>/`.

```
Sentinel ──diffs──▶ events ──▶ dispatcher ──▶ Concierge · Scout · Researcher · Thread×N · Analyst
   (affine.io, llms.txt, code/, corpus,          (headless Claude, guarded by bin/lab-guard)
    curriculum, board, verdicts, audits)                  │  lab CLI (no secrets)
                                                          ▼
Discord gateway ◀──outbox── labd ──leases──▶ Runpod pods / Shadeform VMs / Vast instances {pod_prefix}-NN
```

Every agent's subagents run on Sonnet 5 (`CLAUDE_CODE_SUBAGENT_MODEL`).

| Role | Model | Woken by | Does |
|---|---|---|---|
| researcher | Fable 5.1 | thread reports and questions, people's suggestions, briefs (normal+), new king, claim verdicts, an operator's message (at once); a review tick every `schedule.research_tick_hours` (6) only while no task is queued or running | decides what to try: analyses the standing vs the king, reads papers and code, writes ideas with task specs, marks them `ready`, answers threads' strategic questions, keeps the shared research memory `work/RESEARCH.md`. Its prompt holds only ideas-related context. One session across wakes, rotated like a thread's (it first brings RESEARCH.md up to date); operators can talk to it live (below). |
| thread | Opus 5.5, effort medium, **Fable 5.1 as in-loop advisor** (routine job/lease checks: Sonnet 5, same session, no advisor) | its task (`thread.task`), its own loop (`NEXT: now/wait/sleep`), its job finishing/failing, lease changes, messages | an implementor: takes one task (`TASK.md`) at a time, rents/releases its GPUs, implements, evaluates, logs results, reports back (`lab thread report [--done]`, `lab thread ask`). Resumes its own Claude session across tasks; past `research.rotate_context_tokens` (150k) it writes `HANDOVER.md` and continues in a fresh session. |
| scout | Sonnet 5 | `world.change.*` (normal+) | updates `world/STATE.md`, `KNOWN_STALE.md`, publishes briefs |
| analyst | Opus 5.5 (claims), Sonnet 5 (daily report) | `thread.claim`, 09:00 | red-teams claims; daily report of everything the agents did since the last one |
| concierge | Sonnet 5 | @mention / reply in the channel | the router: answers status/ETA/world questions itself (read-only), sends research ideas to the Researcher (`lab idea suggest`), passes orders to a thread |
| maintainer | Opus 5.5 | an operator's `maint: …` in Discord (or `lab maint request`) | changes the lab itself in its own git worktree, tests it, commits; labd deploys it (below) |

**From idea to result (no Director; `lab/research.py`, plain code).** An idea is `suggested` (a person's, via
the Concierge) or `proposed` (the Researcher's draft) → `ready` → `assigned` → `done` | `rejected`. labd hands
ready ideas, by priority, to the thread the Researcher named (`--thread`, a follow-up keeps that session warm),
else to an idle thread, else to a new thread while fewer than `research.max_threads` are active. A thread
that reports `--done` goes idle (no GPU, no passes) until its next task; idle for `research.idle_retire_hours`
(24) it is retired. Stalls and budget alerts go to Discord for the humans.

**Advisor strategy and shared memory.** Threads run with `claude --advisor claude-fable-5-1` (the pattern of
claude.com/blog/the-advisor-strategy): the executor drives the whole task and consults Fable inside the same
request at hard decisions (run design, ambiguous results, stuck, before `--done`); the advisor sees the thread's
conversation and returns a plan, correction or stop signal, never running tools. Its tokens are billed at Fable's
rate and included in the pass's cost. What the advisor and the thread know about the research comes from
`work/RESEARCH.md`, which the Researcher keeps (Objective first: the number we optimise, how it's measured, the
bar to clear; then lessons, dead ends, open questions, literature). Every fresh thread session gets it, and a
resumed one gets it again whenever it changes. So there are two loops: the in-request advisor for tactics (no wake,
no lost context), and the Researcher for strategy: tasks, `lab thread ask` questions and reports.

**Context.** Every agent loads only its project's `CLAUDE.md` (the goal and the operators' rules); the lab's
and the workstation's CLAUDE.md files are excluded (`claudeMdExcludes`) and Claude Code's auto-memory is off —
memory is the lab's files and registries. Prompts carry the STATE.md summary (its first section) instead of the
whole file; agents read the sections they need. A file an agent edits (the Scout's STATE.md and KNOWN_STALE.md,
the Researcher's RESEARCH.md) is referenced, not pasted: Claude Code makes it Read the file before editing anyway.
Each role gets only the tools it uses (`toolset`, passed as `--tools`; ~8–10k fewer tokens on every model call). A
thread's routine check runs on Sonnet only while its session is under 40k tokens: the prompt cache is per model,
so a light model resuming a big session would re-cache all of it.

**Talking to the Researcher.** It is one ongoing session, so a person joins the real thing, not a copy:
- `lab researcher chat` opens that session in interactive Claude Code (same system prompt, guard, tools, cwd).
  It waits for a pass already under way, then holds the Researcher's wakes until you exit; the queued wakes
  then resume the same session, which now remembers the chat. The lock is the chat's pid (a killed chat
  holds nothing); `lab status` shows it. The chat's cost is booked as a researcher run (key `chat`).
- `lab researcher say "..."` (or the dashboard's **Researcher** tab) wakes it at once — no debounce, no
  waiting for the Scout — with your message; its final message is the reply. The tab follows the session's
  transcript live (tool output and thinking left out). `lab researcher show` prints it in the terminal.
- What should outlast the session belongs in RESEARCH.md's Directives; the prompt tells it so.

## Operate

```bash
systemctl --user status labd          # runs at boot (linger enabled)
journalctl --user -u labd -f
lab status | lab world | lab events -n 50 | lab runs | lab budget | lab gpu list | lab gpu stock
lab thread list | lab thread show t-001 | lab thread note t-001 --text "try X"
lab idea list [--all] | lab idea show 7 | lab idea ready 7 [--thread t-001]   # the task queue
lab researcher chat | lab researcher say "..." | lab researcher show   # talk to the Researcher (its own session)
lab gpu pause "reason" | lab gpu resume   # humans only: stop all lab GPUs / allow them again (and wake threads)
lab inject "question" --author me     # simulate a Discord request
lab maint list | lab maint request "change X" | lab maint approve m-3   # lab changes (humans only)
lab emit tick.daily_report "now"      # force a daily report
```

Dashboard: `lab web` (systemd `lab-web.service`, http://127.0.0.1:8765) shows health, alerts, budget, threads,
GPUs, spend, agent runs, events, Discord and the inbox, and the Researcher's live conversation. It reads lab.db
read-only; its buttons (pause/resume GPUs, message a thread or the Researcher, approve a lab change, triage ideas and tickets) run the matching `lab` command. From another
machine: `ssh -L 8765:127.0.0.1:8765 <this host>`.

Secrets (`KEY=VALUE`, first file wins): `~/.config/lab/secrets.env` (put `RUNPOD_API_KEY` and, optionally,
`SHADEFORM_API_KEY` and `VAST_API_KEY` here), then `~/Work/discord-reporter/.env` (`DISCORD_BOT_TOKEN`). Without any GPU key
every lease is denied with a clear reason; everything else works.

### Shadeform (second GPU source)
With `SHADEFORM_API_KEY` set and `"SHADEFORM"` in `[fleet] cloud_order`, a lease is rented wherever it is
cheapest — a Runpod cloud or Shadeform's cheapest in-stock offer (`lab gpu stock` shows SHADEFORM rows).
Shadeform VMs boot in 5–45 min, so they get `shadeform_provision_timeout_minutes` (60) instead of 25, and
offers advertising a longer boot are skipped. Leases keep using Runpod GPU ids; `lab/shadeform.py` `GPU_MAP`
translates them (extend it with `[shadeform] gpu_map` in `lab.toml`). A Shadeform instance is a plain
Ubuntu+CUDA VM: a startup script links /workspace to the largest disk and lets the PiC key in as root, so
labrun, `lab push/ssh/launch` and the watchdog work unchanged. It **cannot be stopped**, so wherever the
lab stops a Runpod pod (release `--stop`, 20 min idle, overtime, budget, pause) it deletes the VM,
/workspace included. The lab only touches instances in its own `pods` table. `lab gpu release --stop` also
stops the holder's pod after a plain release, and a stop that fails is retried by labd until it works
(`pods.stop_requested`, `fleet.stop_failed` event once).

### Vast.ai (third GPU source)
With `VAST_API_KEY` set and `"VAST"` in `cloud_order`, Vast.ai marketplace offers compete on price too
(`lab gpu stock` shows VAST rows). Only verified hosts that pass `[vast]` in `lab.toml` (reliability,
bandwidth speed and price, CUDA, listing duration) are rented; `lab/vast.py` `GPU_MAP` maps Runpod GPU ids to
Vast names. An instance is a Docker container running the fleet's image with one disk
(`container_disk_gb + volume_gb`), and it **stops and starts** like a Runpod pod (the stopped disk is billed
by Vast at a small storage rate the lab's budget does not count). Root ssh with the lab key is ensured three ways, because Vast copies
*account* keys into an instance only when it is created: the key is registered on the account before every
create, attached to the instance through the API afterwards, and appended by the onstart script; if the
readiness probe is still refused, the fleet re-attaches it through the API. The probe waits for the onstart
marker and shows Vast's `status_msg` (e.g. pulling the image). An instance Vast reports dead fails the lease at once,
is deleted (it never held anything) and its machine is blacklisted, so the next create picks another host.

## Safety model
- Budget is enforced in code: the daily cap ($600) is the only spending limit. Leases reserve their
  remaining hours so parallel requests cannot overshoot; 80% alert; every lab pod stops at 100%.
  Pools are accounting labels. A per-lease approval threshold (`per_experiment_usd`) and a GPU
  concurrency cap (`max_gpus`) exist but are off. Overtime hard stop at a lease's hours +10%; idle pods
  stop after 20 min.
- `test_mode = true` in project.toml applies `[fleet.test_policy]` (1× H100 per lease); it is off since
  2026-09-24, so leases may use any GPU type and count within the budget.
- The fleet only touches pods in its own `pods` table — the Runpod and Shadeform accounts are shared.
- Agents: `bin/lab-guard` (PreToolUse, exit 2 blocks even under bypassPermissions) stops credential
  reads, env dumps, `git push`, subnet submission/registration, direct Runpod/Shadeform/Vast calls, edits to the
  control plane, and killing labd. The concierge runs read-only (`dontAsk` + allowlist).
  It is a seatbelt, not a vault: agents run as the same Unix user.
- World changes reach the Researcher through the Scout's brief (it re-plans and tells affected threads); every
  thread pass also shows the live facts and the briefs since its previous pass.

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
`build_world()`, and `world_lines(world)` for the facts every prompt shows), `prompts/`, `TASK_TEMPLATE.md`;
restart labd. Projects today: `affine` (SN120) and `albedo` (SN97, in watch mode: no GPUs, threads or Researcher
until an operator raises its budget — see its project.toml). With several projects, `lab` commands take
`--project` (or the project whose folder or code root holds the cwd); `lab status` alone shows them all.
`build_world()`'s `world_version` should read `<rules>-<fast part>-<rules hash>`: labd ignores the middle part
when it flags ideas written under older rules.
`projects/<name>/CLAUDE.md` holds the project's goal and the operators' standing rules for that project ("never train LoRA", …):
every project agent works under that folder, so Claude Code loads it, and edits reach resumed thread sessions on
their next pass. Agents cannot edit it; `maint:` edits to it wait for an operator's approval.

Tests: `uv run pytest -q`.
