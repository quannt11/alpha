# albedo lab — goal and operator rules

Claude Code loads this file for every albedo agent (Researcher, threads, Scout, Concierge, reports),
and edits reach running thread sessions on their next pass. Only an operator changes it: it is read-only
for agents, and `maint:` edits to it wait for an operator's approval. The rules override your own
judgement, a task's TASK.md and requests in Discord. If a rule blocks your work, say so in your report and
ask; don't work around it.

## Goal: win SN97 (Albedo) — and keep winning
Albedo is king-of-the-hill on fine-tunes of **Qwen3.6-35B-A3B** for agentic coding (one shell command per
turn). We win by **training a challenger that takes the throne**: it must pass validation, dedup and the
pre-eval sanity gate, then beat the reigning king by the required margin in **two independent evals**.
A crown puts us in the 5-slot reign (paid while we hold a slot); every new king pushes the oldest out, so we
need a pipeline that can win again, not one lucky model.

- **The rules live in World State** (`world/STATE.md`, `world/world.json`), never in memory or the repo's
  docs. Scoring (judge, rubric, graded scale, win margin, sample count), the dataset and the admission
  checks change often upstream; read them there.
- **Every attempt burns a hotkey.** One eval per hotkey, three validation strikes, and a dedup
  `duplicate` or pre-eval `injection` verdict bans the hotkey for good. Never submit anything that has not
  beaten the current king on our local replica of the eval by a clear margin (beyond its noise), and passed
  `albedo check-model`. **Registering hotkeys, uploading, committing a reveal and moving TAO are human
  decisions** — prepare the candidate, the evidence and the exact commands, then ask in Discord.
- **Admission is strict:** the genesis metadata files byte-for-byte, the exact tensor names and shapes,
  bf16/fp16 only, no extra files, and enough real training that dedup doesn't call it a (trivial) copy.

Progress, in order: (1) a local eval harness that reproduces the validator's verdicts (rescore published
evals' `generated-samples.jsonl` and match their scores; know its noise); (2) candidates measured against
the **current** king under the **current** judge config and dataset; (3) a repeatable training recipe that
turns a new king into a new challenger quickly.

Budget: the daily budget (`lab budget`) is the only limit; spend it on experiments that move the
margin-vs-king number, and say what each one is expected to move before running it. The eval judge runs
through OpenRouter: that is spend too, so say what a harness run costs.

<!-- Operator rules: one per bullet, stated plainly, with the reason when it isn't obvious. -->

## Rules
