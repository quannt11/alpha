# Task: <idea title>

## Environment
Which `environment_name` this task is about (Goofspiel / Clobber / InterCode / ...). One environment per
task — see CLAUDE.md's "one environment at a time" rule.

## Hypothesis
One sentence: what we believe, and why it matters for beating the current boss on this environment.

## World state
The live `world_version` this task assumes (`world_version` from `lab world`, e.g. `t<tournament_id>`) and
the tournament round/status it depends on. If they change, the Researcher re-checks the task.

## Metric
The single number to move (higher or lower is better), how to compute it and on what data, its noise
(state n games / sample size — small n means noisy, see the sn56 Clobber A/B experiments' lesson in the
recap), and the move that counts. E.g. "win rate vs boss on Clobber's 5x6 board, n≥200 games, controlled
for equal total training examples between arms; keep only moves > 2x the binomial SE".

## Start from
Repo, branch/commit, the command that reproduces the baseline number; earlier tasks' results to build on
(check `{{work_dir}}/RESEARCH.md`'s per-environment section first — a fair amount of this environment's
groundwork may already exist).

## In / out of scope
What to change (data mix, LoRA rank, sampling, search depth...). Out: anything that changes how we
measure (the eval harness), unless that *is* the task.

## GPUs and cost
Typical run: <shape> for <hours>, ≈ $<usd> in total. Default to a CPU-only run first (see CLAUDE.md's
fail-fast rule) — only budget GPU cost once that's shown a real signal.

## Stop and report
When the task is done (the number measured, or the hypothesis ruled out — both are valid outcomes), what
to report, when to ask.
