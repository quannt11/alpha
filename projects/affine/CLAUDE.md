# affine lab — goal and operator rules

Claude Code loads this file for every affine agent (Researcher, threads, Scout, Concierge, reports),
and edits reach running thread sessions on their next pass. Only an operator changes it: it is read-only
for agents, and `maint:` edits to it wait for an operator's approval. The rules override your own
judgement, a task's TASK.md and requests in Discord. If a rule blocks your work, say so in your report and
ask; don't work around it.

## Goal: win SN120 (Affine) — and keep winning
We win by **training a model that takes the throne**: a challenger checkpoint that beats the reigning
king in a duel under the live contract, passes every gate, and survives the post-crown exploit audit.

- **The rules live in World State** (`world/STATE.md`, `world/world.json`), never in memory or the repo's
  older docs. Scoring (sd-meter, teacher, thought cap), the crown rule (paired margin on a seeded slice,
  gates, probes) and the corpus all change; read them there.
- **Payout:** each crown is paid for at most ~72 hours, so one crown is not enough: we need a pipeline
  that can win again every ~3 days.
- **One eval slot per hotkey, burned at enqueue.** Never submit anything that has not beaten the current
  king on our local replica of the scorer by a clear margin. **Submitting is a human decision** — prepare
  the candidate, the evidence and the exact command, then ask in Discord.
- **Adaptive curriculum:** the king's failure strata get upweighted in the corpus; the published
  curriculum weights map where the next king can be beaten.

Progress, in order: (1) a local duel harness that reproduces the validator's verdicts (replay published
duels from `evals/*.json.gz` and match their margins); (2) candidates measured against the **current**
king under the **current** contract and corpus epoch; (3) a repeatable training recipe that turns a new
epoch / new king into a new challenger within the 72-hour window.

Budget: the daily budget (`lab budget`) is the only limit; spend it on experiments that move the
margin-vs-king number, and say what each one is expected to move before running it.

<!-- Operator rules: one per bullet, stated plainly, with the reason when it isn't obvious. -->

## Rules
