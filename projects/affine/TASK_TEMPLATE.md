# Task: <idea title>

## Hypothesis
One sentence: what we believe, and why it matters for beating the current king.

## Rules
The live world version this task assumes (`world_version` from `lab world`, e.g. `wvk24-e80-…`) and the
contract knobs it depends on. If they change, the Researcher re-checks the task.

## Metric
The single number to move (higher or lower is better), how to compute it and on what data, its noise, and
the move that counts. E.g. "paired margin vs the current king (sd units) on a 1,000-turn seeded slice with
the rl120 harness matching the live contract; noise ±0.03 sd; keep only moves > 0.1".

## Start from
Repo, branch/commit, the command that reproduces the baseline number; earlier tasks' results to build on.

## In / out of scope
What to change (data mix, LoRA rank, lr, sampling…). Out: anything that changes how we measure (the scorer),
unless that *is* the task.

## GPUs and cost
Typical run: <shape> for <hours>, ≈ $<usd> in total.

## Stop and report
When the task is done (the number measured, or the hypothesis ruled out), what to report, when to ask.
