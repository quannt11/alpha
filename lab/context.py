"""Human- and agent-readable snapshots of lab state (shared by the CLI and prompts)."""
from __future__ import annotations

import json
import time
from pathlib import Path

from .budget import Budget
from .db import DB, now


def ago(ts: float | None) -> str:
    if not ts:
        return "never"
    d = now() - ts
    if d < 90:
        return f"{d:.0f}s ago"
    if d < 5400:
        return f"{d / 60:.0f}m ago"
    if d < 172800:
        return f"{d / 3600:.1f}h ago"
    return f"{d / 86400:.1f}d ago"


def iso(ts: float | None) -> str:
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(ts)) if ts else "-"


def read(path: Path, limit: int = 12000) -> str:
    if not path.exists():
        return f"({path.name} does not exist yet)"
    t = path.read_text(errors="replace")
    return t if len(t) <= limit else t[:limit] + f"\n… (truncated; {len(t)} chars total, read the file for the rest)"


def world_facts(project) -> str:
    wj = project.world_dir / "world.json"
    if not wj.exists():
        return "(world.json not built yet — the Sentinel has not completed a first poll)"
    w = json.loads(wj.read_text())
    c = w.get("contract", {})
    keys = [k for k in ("subnet.weight_version_key", "duel.score_mode", "duel.n_turns", "duel.max_thought_tokens",
                        "duel.ref_max_tokens", "duel.sd_meter.min_margin_sd", "duel.sd_meter.k_sigma",
                        "duel.thought_rendering", "subnet.king_payout_window_hours") if k in c]
    lines = [f"world_version: {w.get('world_version')}  (generated {w.get('generated_at')})",
             f"teacher: {w.get('teacher')}",
             "contract: " + ", ".join(f"{k}={c[k]}" for k in keys),
             f"corpus: {json.dumps(w.get('corpus'))}",
             f"curriculum: {json.dumps(w.get('curriculum'))}",
             f"king: {json.dumps(w.get('king'))}",
             f"payout: {json.dumps(w.get('payout'))}",
             f"our crowns: {json.dumps(w.get('ours')) if w.get('ours') else '(none configured / none held)'}",
             f"latest fork section in llms.txt: {w.get('llms', {}).get('latest_fork')}"]
    return "\n".join(lines)


def leases_table(db: DB, project: str, holder: str | None = None) -> str:
    q = ("SELECT * FROM leases WHERE project=? AND status IN ('requested','provisioning','granted',"
         "'release_requested')")
    args: list = [project]
    if holder:
        q += " AND COALESCE(holder, experiment_id)=?"
        args.append(holder)
    rows = db.all(q + " ORDER BY id", args)
    if not rows:
        return "(no active leases)"
    return "\n".join(
        f"- lease {r['id']} {r['holder'] or r['experiment_id']} [{r['status']}] {r['pod_name'] or '-'} "
        f"{r['gpu_count']}×{r['gpu_type']} ${r['price_hr'] or 0:.2f}/h ≤{r['max_hours']}h "
        f"job={r['job_name'] or '-'}:{r['job_state'] or '-'} heartbeat {ago(r['heartbeat_at'])}"
        + (f" ({r['reason']})" if r["status"] == "requested" and r["reason"] else "")
        for r in rows)


def threads_table(db: DB, project: str, include_retired: bool = False) -> str:
    q = "SELECT * FROM threads WHERE project=?" + ("" if include_retired else " AND status='active'")
    rows = db.all(q + " ORDER BY id", (project,))
    if not rows:
        return "(no research threads)"
    out = []
    for t in rows:
        n = db.one("SELECT COUNT(*) n, SUM(kept) k FROM results WHERE thread_id=?", (t["id"],))
        best = f"{t['best_value']:.4g}" if t["best_value"] is not None else "—"
        out.append(f"- {t['id']} [{t['status']}] {t['title']} — metric {t['metric'] or '?'}, best {best} "
                   f"({t['best_desc'] or ''}); {n['n']} results ({n['k'] or 0} kept); {t['passes']} passes; "
                   f"spent ${t['spent_usd'] or 0:.2f}; last pass {ago(t['last_pass_at'])}")
    return "\n".join(out)


def results_table(db: DB, project: str, thread: str | None = None, since: float | None = None,
                  limit: int = 20) -> str:
    q = "SELECT * FROM results WHERE project=?"
    args: list = [project]
    if thread:
        q += " AND thread_id=?"
        args.append(thread)
    if since:
        q += " AND ts>=?"
        args.append(since)
    rows = db.all(q + " ORDER BY id DESC LIMIT ?", args + [limit])
    if not rows:
        return "(no results yet)"
    return "\n".join(f"- {iso(r['ts'])} {r['thread_id']} {r['run'] or ''}: {r['metric']}={r['value']} "
                     f"{'KEPT' if r['kept'] else 'discarded'} ${r['cost_usd'] or 0:.2f} — {r['description']}"
                     for r in reversed(rows))


def activity_digest(db: DB, cfg, project, since: float) -> str:
    """Everything the lab did since `since`, for the daily report: per-thread results and spend,
    every agent pass with its own summary, Director decisions, people's requests, world changes."""
    name = project.name
    parts = ["### Research threads", threads_table(db, name, include_retired=True),
             "", "### Every result logged", results_table(db, name, since=since, limit=200)]
    spend = db.all("SELECT COALESCE(experiment_id,'(idle pods)') h, SUM(usd) s FROM ledger WHERE project=? AND ts>=? "
                   "GROUP BY h ORDER BY s DESC", (name, since))
    parts += ["", "### GPU spend by holder", "\n".join(f"- {r['h']}: ${r['s']:.2f}" for r in spend) or "(none)"]
    runs = db.all("SELECT * FROM agent_runs WHERE project=? AND started_at>=? ORDER BY id", (name, since))
    parts += ["", f"### Agent passes ({len(runs)})"]
    for r in runs:
        summary = (r["result"] or r["error"] or "").strip().replace("\n", " ")[:400]
        parts.append(f"- {iso(r['started_at'])} {r['role']}{'/' + r['key'] if r['key'] else ''} [{r['status']}]: {summary}")
    decisions = db.all("SELECT * FROM events WHERE project=? AND ts>=? AND (topic LIKE 'thread.%' OR topic LIKE "
                       "'idea.%' OR topic LIKE 'ticket.%' OR topic LIKE 'world.brief%' OR topic LIKE 'board.%' OR "
                       "topic LIKE 'budget.%' OR topic LIKE 'operator.%') AND topic NOT IN ('thread.continue') "
                       "ORDER BY id", (name, since))
    parts += ["", "### Decisions, requests and world changes"]
    parts += [f"- {iso(e['ts'])} {e['topic']}{' ' + e['key'] if e['key'] else ''}: {e['summary'][:300]}"
              for e in decisions] or ["(none)"]
    return "\n".join(parts)


def tickets_table(db: DB, project: str, status: str = "open") -> str:
    rows = db.all("SELECT * FROM tickets WHERE project=? AND status=? ORDER BY id", (project, status))
    if not rows:
        return "(none)"
    return "\n".join(f"- ticket {r['id']} from {r['author']} ({ago(r['created_at'])}): {r['title']}\n    {r['body'][:600]}"
                     for r in rows)


def backlog_table(db: DB, project: str) -> str:
    rows = db.all("SELECT * FROM backlog WHERE project=? AND status IN ('proposed','accepted') "
                  "ORDER BY priority, id", (project,))
    if not rows:
        return "(empty)"
    return "\n".join(f"- idea {r['id']} [{r['status']}] p{r['priority']} {r['title']} — gain: {r['expected_gain']}; "
                     f"est ${r['est_cost_usd'] or 0:.0f}; by {r['author']}" for r in rows)


def events_digest(db: DB, project: str, since: float, min_sev: str = "info", limit: int = 80) -> str:
    order = {"info": 0, "minor": 1, "normal": 2, "major": 3}
    rows = db.all("SELECT * FROM events WHERE project=? AND ts>=? ORDER BY id DESC LIMIT 1000", (project, since))
    rows = [r for r in rows if order.get(r["severity"], 0) >= order[min_sev]
            and not r["topic"].startswith(("sentinel.baseline", "agent."))][:limit]
    if not rows:
        return "(no events)"
    return "\n".join(f"- #{r['id']} {iso(r['ts'])} [{r['severity']}] {r['topic']}"
                     f"{' ' + r['key'] if r['key'] else ''}: {r['summary']}" for r in reversed(rows))


def status_text(db: DB, cfg, project) -> str:
    b = Budget(db, project, cfg.timezone).summary()
    pools = ", ".join(f"{k} ${v['spent']:.0f}+{v['reserved']:.0f}res" + (f"/{v['cap']:.0f}" if v["cap"] is not None else "")
                      for k, v in b["pools"].items())
    running = db.all("SELECT role, key, started_at FROM agent_runs WHERE project=? AND status='running'", (project.name,))
    queued = db.one("SELECT COUNT(*) n FROM agent_runs WHERE project=? AND status='queued'", (project.name,))["n"]
    backoff = db.kv_get("_lab", "agent_backoff_until", 0) or 0
    agents = ", ".join(f"{r['role']}{'/' + r['key'] if r['key'] else ''} ({ago(r['started_at'])})" for r in running) or "idle"
    paused = db.kv_get(project.name, "gpu_paused")
    return "\n".join([
        f"# {project.name} — status at {iso(now())}"
        + (f"   ** GPUs PAUSED by operator since {iso(paused['at'])} **" if paused else ""),
        f"budget today: spent ${b['spent_today']:.2f}, reserved ${b['reserved']:.2f} of ${b['daily_cap']:.0f} ({pools})",
        f"agents running: {agents}; queued {queued}" + (f"; rate-limit backoff until {iso(backoff)}" if backoff > now() else ""),
        "", "## World", world_facts(project),
        "", "## Research threads", threads_table(db, project.name),
        "", "## GPU leases", leases_table(db, project.name),
        "", "## Open tickets", tickets_table(db, project.name),
        "", "## Ideas", backlog_table(db, project.name),
    ])
