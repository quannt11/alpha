"""Human- and agent-readable snapshots of lab state (shared by the CLI and prompts)."""
from __future__ import annotations

import json
import re
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


_PLUGINS: dict[Path, tuple[float, object]] = {}


def _plugin(project):
    """The project's plugin module (reloaded when plugin.py changes), or None."""
    path = project.dir / "plugin.py"
    if not path.exists():
        return None
    mtime = path.stat().st_mtime
    if path not in _PLUGINS or _PLUGINS[path][0] != mtime:
        from .sentinel import load_plugin
        _PLUGINS[path] = (mtime, load_plugin(path))
    return _PLUGINS[path][1]


def world_facts(project) -> str:
    """The headline facts of world.json. Each plugin knows its own world (`world_lines(world)`);
    without that hook every top-level field is shown, compactly."""
    wj = project.world_dir / "world.json"
    if not wj.exists():
        return "(world.json not built yet — the Sentinel has not completed a first poll)"
    w = json.loads(wj.read_text())
    head = f"world_version: {w.get('world_version')}  (generated {w.get('generated_at')})"
    plugin = _plugin(project)
    if plugin is not None and hasattr(plugin, "world_lines"):
        return "\n".join([head, *plugin.world_lines(w)])
    rest = [f"{k}: {json.dumps(v, default=str)[:600]}" for k, v in w.items()
            if k not in ("world_version", "generated_at")]
    return "\n".join([head, *rest])


STATE_VERSION = re.compile(r"World version:\s*`([^`]+)`")


def live_world_version(project) -> str | None:
    wj = project.world_dir / "world.json"
    try:
        return json.loads(wj.read_text()).get("world_version") if wj.exists() else None
    except ValueError:
        return None


def state_version(project) -> str | None:
    """The version the Scout says STATE.md describes (its `World version: `...`` line)."""
    st = project.world_dir / "STATE.md"
    m = STATE_VERSION.search(st.read_text(errors="replace")[:4000]) if st.exists() else None
    return m.group(1) if m else None


def rules_of(version: str | None) -> str | None:
    """`wvk24-e78-cdc56f848` → `wvk24-cdc56f848`: the contract part, without the (hourly) corpus epoch."""
    if not version:
        return None
    parts = version.split("-")
    return "-".join(parts[:1] + parts[2:]) if len(parts) >= 3 else version


def world_lag(db: DB, project) -> str:
    """A loud note when STATE.md (Scout prose) is behind world.json (live facts), with what changed since."""
    live, st = live_world_version(project), state_version(project)
    if not live or not (project.world_dir / "STATE.md").exists() or st == live:
        return ""
    hist = db.kv_get(project.name, "world_versions", []) or []
    idx = next((i for i, (v, _) in enumerate(hist) if v == st), None)
    since = hist[idx + 1][1] if idx is not None and idx + 1 < len(hist) else now() - 86400
    rows = db.all("SELECT * FROM events WHERE project=? AND topic LIKE 'world.change.%' AND topic != "
                  "'world.change.resync' AND ts>=? ORDER BY id DESC LIMIT 15", (project.name, since - 60))
    changes = "\n".join(f"- #{r['id']} {iso(r['ts'])} [{r['severity']}] {r['topic']}: {r['summary']}"
                        for r in reversed(rows)) or "- (no change events recorded; compare with the live facts)"
    return (f"**⚠ STATE.md is behind the live world.** It describes `{st or 'an unstated version'}`; live is "
            f"`{live}`. The facts above are live: where STATE.md disagrees, trust them and these changes "
            f"(the Scout is asked to catch up):\n{changes}")


def rules_note(db: DB, project: str, stamp: str | None, verb: str) -> str:
    """Flag an idea or charter written under a contract that is no longer live (corpus epochs don't count)."""
    live = db.kv_get(project, "world_version")
    if not stamp or not live or rules_of(stamp) == rules_of(live):
        return ""
    return f" ⚠ {verb} under {stamp.split('-')[0]} rules (`{stamp}`; live `{live}`): re-check against World State"


def world_now(db: DB, project) -> str:
    """Live facts plus the staleness note: what every agent should believe about the world right now."""
    lag = world_lag(db, project)
    return world_facts(project) + (f"\n\n{lag}" if lag else "")


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
        + (f" ({r['reason']})" if r["status"] in ("requested", "provisioning") and r["reason"] else "")
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
                   f"spent ${t['spent_usd'] or 0:.2f}; last pass {ago(t['last_pass_at'])}; session {t['generation'] or 1}"
                   + (f" at {t['context_tokens'] // 1000}k tokens" if t["context_tokens"] else "")
                   + (" (handover next pass)" if t["rotate_pending"] else "")
                   + (f"; on idea {t['task_id']}" if t["task_id"] else f"; idle since {ago(t['idle_since'])}")
                   + rules_note(db, project, t["world_version"], "chartered"))
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
    chat = [r for r in runs if r["role"] == "concierge"]      # their answers are already in the channel
    parts += ["", f"### Agent passes ({len(runs)}; {len(chat)} Concierge answers not listed)"]
    for r in runs:
        if r["role"] == "concierge":
            continue
        summary = " ".join((r["result"] or r["error"] or "").split())[:240]
        parts.append(f"- {iso(r['started_at'])} {r['role']}{'/' + r['key'] if r['key'] else ''} [{r['status']}]: {summary}")
    decisions = db.all("SELECT * FROM events WHERE project=? AND ts>=? AND (topic LIKE 'thread.%' OR topic LIKE "
                       "'idea.%' OR topic LIKE 'ticket.%' OR topic LIKE 'world.brief%' OR topic LIKE 'board.%' OR "
                       "topic LIKE 'budget.%' OR topic LIKE 'operator.%') AND topic NOT IN ('thread.continue', 'thread.log') "
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


def backlog_table(db: DB, project: str, statuses=("suggested", "proposed", "ready", "assigned"), spec: int = 0) -> str:
    """Open ideas; `spec` > 0 adds the first `spec` characters of each one's task text."""
    rows = db.all(f"SELECT * FROM backlog WHERE project=? AND status IN ({','.join('?' * len(statuses))}) "
                  "ORDER BY CASE status WHEN 'assigned' THEN 0 WHEN 'ready' THEN 1 WHEN 'suggested' THEN 2 ELSE 3 END, "
                  "priority, id", (project, *statuses))
    if not rows:
        return "(empty)"
    out = []
    for r in rows:
        line = (f"- idea {r['id']} [{r['status']}{' → ' + r['thread_id'] if r['thread_id'] else ''}"
                f"{' for ' + r['for_thread'] if r['for_thread'] and not r['thread_id'] else ''}] p{r['priority']} "
                f"{r['title']} — by {r['author']}" + (f"; gain: {r['expected_gain']}" if r["expected_gain"] else "")
                + rules_note(db, project, r["world_version"], "written"))
        body = (r["spec"] or r["hypothesis"] or "").strip()
        if spec and body:
            line += "\n  " + (body[:spec] + ("…" if len(body) > spec else "")).replace("\n", "\n  ")
        out.append(line)
    return "\n".join(out)


def research_doc(project) -> Path:
    """The shared research memory: the Researcher writes it, every thread (and its advisor) reads it."""
    return project.work_dir / "RESEARCH.md"


def file_ref(path: Path, what: str) -> str:
    """Point to a file the agent will edit instead of pasting it: Claude Code makes it Read the file before
    an Edit/Write anyway, so a copy in the prompt would be read twice."""
    if not path.exists():
        return f"`{path}` does not exist yet — {what}"
    t = path.read_text(errors="replace")
    return (f"`{path}` ({len(t):,} chars, last changed {iso(path.stat().st_mtime)}) — {what}. "
            "It is not pasted here: Read it (you must before editing it).")


def state_head(project) -> str:
    """STATE.md up to its first section: the version line and the Scout's short summary of the rules."""
    st = project.world_dir / "STATE.md"
    if not st.exists():
        return "(STATE.md does not exist yet)"
    t = st.read_text(errors="replace")
    i = t.find("\n## ")
    head = (t if i < 0 else t[:i]).strip()[:4000]
    return head + f"\n(Full rules by section: `{st}` — read the sections you need.)"


def events_digest(db: DB, project: str, since: float, min_sev: str = "info", limit: int = 80) -> str:
    order = {"info": 0, "minor": 1, "normal": 2, "major": 3}
    rows = db.all("SELECT * FROM events WHERE project=? AND ts>=? ORDER BY id DESC LIMIT 1000", (project, since))
    rows = [r for r in rows if order.get(r["severity"], 0) >= order[min_sev]
            and not r["topic"].startswith(("sentinel.baseline", "agent."))][:limit]
    if not rows:
        return "(no events)"
    return "\n".join(f"- #{r['id']} {iso(r['ts'])} [{r['severity']}] {r['topic']}"
                     f"{' ' + r['key'] if r['key'] else ''}: {r['summary']}" for r in reversed(rows))


MAINT_IN_FLIGHT = ("queued", "working", "awaiting_approval", "deploying", "restarting")


def maint_in_flight(db: DB) -> str:
    """Unfinished changes to the lab's own code (lab-wide, not per project); "" when there are none."""
    rows = db.all(f"SELECT id, status, request FROM maint WHERE status IN ({','.join('?' * len(MAINT_IN_FLIGHT))}) "
                  "ORDER BY created_at", MAINT_IN_FLIGHT)
    return "\n".join(f"- {r['id']} [{r['status']}] {' '.join((r['request'] or '').split())[:80]}" for r in rows)


def status_text(db: DB, cfg, project) -> str:
    b = Budget(db, project, cfg.timezone).summary()
    pools = ", ".join(f"{k} ${v['spent']:.0f}+{v['reserved']:.0f}res" + (f"/{v['cap']:.0f}" if v["cap"] is not None else "")
                      for k, v in b["pools"].items())
    running = db.all("SELECT role, key, started_at FROM agent_runs WHERE project=? AND status='running'", (project.name,))
    queued = db.one("SELECT COUNT(*) n FROM agent_runs WHERE project=? AND status='queued'", (project.name,))["n"]
    backoff = db.kv_get("_lab", "agent_backoff_until", 0) or 0
    agents = ", ".join(f"{r['role']}{'/' + r['key'] if r['key'] else ''} ({ago(r['started_at'])})" for r in running) or "idle"
    paused = db.kv_get(project.name, "gpu_paused")
    changes = maint_in_flight(db)
    return "\n".join([
        f"# {project.name} — status at {iso(now())}"
        + (f"   ** GPUs PAUSED by operator since {iso(paused['at'])} **" if paused else ""),
        f"budget today: spent ${b['spent_today']:.2f}, reserved ${b['reserved']:.2f} of ${b['daily_cap']:.0f} ({pools})",
        f"agents running: {agents}; queued {queued}" + (f"; rate-limit backoff until {iso(backoff)}" if backoff > now() else ""),
        "", "## World", world_now(db, project),
        "", "## Research threads", threads_table(db, project.name),
        "", "## GPU leases", leases_table(db, project.name),
        *(["", "## Open tickets", tickets] if (tickets := tickets_table(db, project.name)) != "(none)" else []),
        *(["", "## Lab changes", changes] if changes else []),
        "", "## Research ideas", backlog_table(db, project.name),
    ])
