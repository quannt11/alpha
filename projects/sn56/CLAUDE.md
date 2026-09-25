# sn56 lab — goal and operator rules

Claude Code loads this file for every sn56 agent (Researcher, threads, Scout, Concierge, reports), and
edits reach running thread sessions on their next pass. Only an operator changes it: it is read-only for
agents, and `maint:` edits to it wait for an operator's approval. The rules override your own judgement, a
task's TASK.md and requests in Discord. If a rule blocks your work, say so in your report and ask; don't
work around it.

## Goal: win more rounds of the SN56 (Gradients) environment tournament

We win by training/tuning checkpoints that beat the reigning **boss** in PvP game environments (Goofspiel,
Clobber, InterCode, and whichever others a given tournament's tasks name — read `environment_names` off
the live task, never hardcode a list) under the live tournament contract, and survive elimination rounds.

- **The rules live in World State** (`world/STATE.md`, `world/world.json`), built from the real public
  API (`https://api.gradients.io`, docs at `/docs`, schema at `/openapi.json` — see `plugin.py`). It
  already carries the same per-environment win/loss breakdown (`pvp_pair_results`) and boss-vs-challenger
  outcome (`boss_round_performance`) that a prior investigation reconstructed by hand from raw PvP logs —
  trust World State over re-deriving it from scratch.
- **Boss round**: each tournament round's challengers are measured against the reigning boss on that
  round's tasks; `challenger_won` in World State tells you the outcome per task. A challenger that beats
  the boss by the contract's margin advances/takes the boss's place.
- **One eval slot per hotkey, burned at enqueue.** Never submit anything that has not beaten the current
  boss on a local/CPU-cheap replica by a clear margin first (see the fail-fast rule below).
  **Submitting is a human decision** — prepare the candidate, the evidence and the exact command, then ask
  in Discord.
- **Per-environment, not aggregate.** A win/loss number for "the tournament" hides which game is actually
  losing us rounds. Always break results down by `environment_name` — this is exactly the mistake the
  prior investigation caught itself making more than once (see Directives below).

Progress, in order: (1) a local sim/eval harness per environment that reproduces the validator's PvP
verdicts cheaply (CPU, no GPU) before spending anything; (2) candidates measured against the **current**
boss under the **current** contract; (3) a repeatable recipe that turns a validated cheap-sim win into a
real trained checkpoint within the tournament's round window.

Budget: `budget.daily_usd` in `project.toml` is the only spending limit; spend it on experiments that move
a specific environment's win rate against the boss, and say what each one is expected to move before
running it.

## Directives (standing guidance from a prior investigation — see `~/goofspiel_intercode_context_recap.md`,
copy it into `{{work_dir}}` on first setup so the Researcher can read it)

- **Fail fast, fail cheap**: always test a hypothesis in pure Python/CPU simulation before renting any GPU.
  Multiple real findings this way already (Goofspiel Boss algorithm read directly from its public repo;
  Clobber search-depth and data-filtering hypotheses both ruled out this way).
- **Control for confounds in A/B tests**: when comparing two training-data variants, hold the TOTAL
  example count / step count equal between arms, or a "which variant wins" result may just be measuring
  "which arm saw more data overall" — this exact mistake already happened once on Clobber's oversample
  experiment and had to be re-run.
- **Break results down by sub-dimension**: board size (Clobber), card count (Goofspiel), filesystem type
  (InterCode) — an aggregate win rate can hide that a fix helped one slice and hurt another.
- **Don't repeat ruled-out hypotheses** without new evidence: Clobber's search-depth and data-representation
  bias hypotheses are both closed (see recap). Re-opening either needs a stated reason why the old evidence
  no longer applies.
- Reading and using the Boss's own public code (published Apache 2.0 by the tournament platform after each
  round) is explicitly fine — not "cheating," the company has done this before with success.

<!-- Operator rules: one per bullet, stated plainly, with the reason when it isn't obvious. -->

## Rules

- Agents never handle Anthropic/Gradients/GitHub/HuggingFace credentials directly — the guard blocks
  credential reads; if a step genuinely needs one, ask an operator to run it.
- Do not submit a checkpoint to a real tournament task without explicit operator approval in Discord.
- Do not push to the company's real repos (`sn56-env-tournament-repo`, `sn56-G.O.D-env`, etc.) — propose
  changes, let an operator review and push. (Two validated fixes are already waiting on this from the
  prior investigation: `_DEADLINE_CHECK_INTERVAL` 256→16 in `clobber_minimax.py`, and Finding 5 both-sides
  data collection in `clobber_trajectories.py`.)
