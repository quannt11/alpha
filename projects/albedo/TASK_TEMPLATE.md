# Task: <idea title>

## Hypothesis
One sentence: what we believe, and why it matters for beating the current king.

## Rules
The live world version this task assumes (`world_version` from `lab world`, e.g. `sn97-k127-…`) and the
eval knobs (judge config, win margin, dataset) it depends on. If they change, the Researcher re-checks the task.

## Metric
The single number to move (higher or lower is better), how to compute it and on what data, its noise, and
the move that counts. E.g. "judge-score margin vs the current king on 100 seeded samples × 2 rollouts with
our replica of the validator's eval; noise ±0.02; keep only moves > 0.025".

## Start from
Repo, branch/commit, the command that reproduces the baseline number; earlier tasks' results to build on.

## In / out of scope
What to change (data mix, LoRA rank, lr, sampling…). Out: anything that changes how we measure (the scorer),
unless that *is* the task.

## GPUs and cost
Typical run: <shape> for <hours>, ≈ $<usd> in total.

## Stop and report
When the task is done (the number measured, or the hypothesis ruled out), what to report, when to ask.
