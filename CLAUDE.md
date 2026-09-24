# The lab — notes for Claude sessions that change it

This repo is a live, always-on system. `labd` (systemd user service) is running from this working
tree right now: it wakes Claude agents, rents Runpod (and Shadeform, Vast.ai) GPUs against a real budget, and posts to Discord.
Read `README.md` first for the design; this file is how to change it safely.

## Layout
- `lab/` — the control plane (Python, asyncio, no LLM calls): `daemon.py` (loops, Discord in/out),
  `agents.py` (dispatch + `claude -p` runs, thread sessions and rotation), `fleet.py` (GPU leases, pods,
  watchdog), `budget.py`, `sentinel.py` (world polling), `context.py` (prompt sections), `cli.py` (`lab`),
  `db.py` (SQLite schema + `MIGRATIONS`), `config.py` (`DEFAULT_ROLES`, models, config parsing).
- `bin/` — `lab`, `labd`, `lab-guard` (PreToolUse hook for every agent), `labrun` (job wrapper on pods).
- `lab.toml` — global config. `projects/<name>/project.toml`, `plugin.py`, `prompts/*.md`, `GOAL.md`.
  `projects/<name>/CLAUDE.md` = operators' rules for that project (Claude Code loads it for every agent under
  the folder; read-only for agents, approval-gated for `maint:`).
- `projects/*/work/` and `projects/*/world/` — the agents' research notes and World State. labd commits
  them automatically after agent runs (`[affine] …` commits). Don't edit or reformat them.
- State: `~/.local/state/lab/lab.db` (events, runs, threads, leases, ledger, outbox, kv). Secrets:
  `~/.config/lab/secrets.env` and `~/Work/discord-reporter/.env` — never print or copy them.

## Change, test, deploy
1. Make the change; match the surrounding style (short docstrings on the "why", no ceremony).
2. `uv run pytest -q` must pass (fast, offline: fake `claude`, fake Discord, fake Runpod/Shadeform/Vast). Add a test for
   new behaviour. New DB columns go in `DB.MIGRATIONS`, never only in `SCHEMA`.
3. Commit on master with a plain message (local identity "lab" is fine). The research agents commit
   `projects/*/work|world` concurrently — only `git add` the files you changed.
4. `systemctl --user restart labd`, then check `journalctl --user -u labd -n 30` and `lab status`
   (heartbeat under ~15 s). A restart interrupts running agent passes (they are requeued), so prefer
   restarting when `lab status` says "agents running: idle". Remote GPU jobs are not affected.
5. Operators can also change the lab from Discord (`maint: …`): the maintainer agent works in a git
   worktree under `~/.cache/lab-maint/` and labd merges branches `maint/m-N` with `bin/lab-deploy`
   (README, "Changing the lab from Discord"). Those merges and reverts show up in `git log`; if a
   maintainer deploy is in flight (`lab maint list`), let it finish before restarting labd yourself.
6. Prompt-only changes (`projects/*/prompts/*.md`) apply on the next agent run; no restart needed.

## Rules that must not be broken
- GPUs: `lab gpu pause/resume` is a human decision. The Runpod, Shadeform and Vast accounts are shared — the lab only
  touches pods in its own `pods` table (`Pi_affine-NN`); stop, don't terminate, unless asked. Shadeform VMs
  cannot be stopped, so for them "stop" means delete (`Fleet.stop_pod`).
- Budget: `daily_usd` is the only spending limit and is enforced in code; `test_mode` (off since
  2026-09-24) limits each lease to 1× H100. Changing either is the operator's call, not yours.
- Agents must never get credentials: keep `SECRET_ENV` stripping, the `--settings` deny list and
  `bin/lab-guard` intact (it is a seatbelt, not isolation — agents run as the same Unix user).
- Nothing is pushed to `~/Work/affine` remotes (AffineFoundation/affine is public). Submitting to the
  subnet, registering hotkeys or moving TAO is a human decision.
- Models: research threads Fable 5.1 (`claude-fable-5-1`); Director and claim checks Opus 5.5
  (`claude-opus-5-5`); concierge, scout, daily report, routine checks and all subagents Sonnet 5.

## Publishing to github.com/trungvd-zenai/alpha (only when the operator asks)
The public repo is **code only**: never `projects/*/work/`, `projects/*/world/`, `lab.db`, secrets,
people's names or research content. Procedure:
1. Clone alpha into a scratch dir; delete its tracked files; copy this repo's tracked files except
   `projects/*/work/` and `projects/*/world/` (`git ls-files | grep -v '^projects/[^/]*/\(work\|world\)/'`).
2. Keep `projects/*/work/` and `projects/*/world/` in its `.gitignore`.
3. Scan for secrets and personal data (tokens, keys, emails, Discord user names); run the tests there.
4. Commit as `trungvd-zenai <247340075+trungvd-zenai@users.noreply.github.com>` and push — or hand the
   push to the operator if the session's permissions block it.
