# Goal: win SN120 (Affine) — and keep winning

We win by **training a model that takes the throne**: a challenger checkpoint that
beats the reigning king in a duel under the live contract, passes every gate, and
survives the post-crown exploit audit.

Read the exact rules from World State (`world/STATE.md`, `world/world.json`), never
from memory or from the repo's older docs. As of the lab's first boot (2026-09-23)
the shape of the game was:

- **Score:** the sd-meter `min(z_R, typ_c, z_A)` in teacher-sd units (wvk 22+, thought
  cap 4,096 since wvk 23), teacher `Qwen/Qwen3.8-27B`, turns drawn from corpus D.
- **Crown rule:** one 1,000-turn seeded slice; paired margin > max(2·SE, δ=0.2 sd),
  plus the thought-length floor and the B gate. Probes at admission.
- **Payout:** each crown is paid for at most **72 hours**; one equal share per paid crown.
  One crown is not enough — we need a pipeline that can win again every ~3 days.
- **One eval slot per hotkey, burned at enqueue.** Submissions are scarce and irreversible:
  never submit anything that has not beaten the current king on our local replica of
  the scorer by a clear margin. **Submitting is a human decision** — prepare the candidate,
  the evidence and the exact command, then ask in Discord.
- **Adaptive curriculum:** the king's failure strata get upweighted in D. The published
  curriculum weights are a map of where the next king can be beaten.

What "progress" means here, in order:
1. A local duel harness that reproduces the validator's verdicts (replay published
   duels from `evals/*.json.gz` and match their margins).
2. Candidates measured against the **current** king on that harness, under the **current**
   contract and corpus epoch.
3. A repeatable training recipe that turns a new epoch / new king into a new
   challenger within the 72-hour window.

Budget: $600/day is the only limit (see `lab budget`); spend it on experiments that move the
margin-vs-king number, and say what each one is expected to move before running it.
