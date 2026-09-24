# Charter: <direction title>

## Goal
What this thread is trying to achieve, in one or two sentences, and why it matters for winning SN120.

## Rules
The live world version this charter targets (`world_version` from `lab world`, e.g. `wvk24-e78-…`) and
the contract knobs the direction depends on. If they change, the Director re-checks this charter.

## Metric
The single number to optimise (and whether higher or lower is better), how to compute it, and on
what data. E.g. "paired margin vs the current king (sd units) on a 1,000-turn seeded slice scored with
the rl120 harness matching the live contract; higher is better; noise ≈ ±0.05 sd, so only keep changes that move it by > 0.1".

## Baseline
Where to start: repo, branch/commit, the command that reproduces the current number.

## What you may change
Files / knobs in scope (e.g. training data mix, LoRA rank, learning rate, sampling config). Out of scope:
anything that changes how we measure (the scorer), unless that *is* the direction.

## Budget and GPUs
Typical run: <shape> for <hours>. Release GPUs between runs when the next step is long CPU work.

## Stop / report conditions
When to file a claim (`lab thread claim`), when to ask the Director, what would make this direction dead.
