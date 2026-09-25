"""`lab web` — a local dashboard for operators: what the lab is doing, what it costs, what needs a human.

Reads lab.db read-only. The few actions it offers (pause GPUs, message a thread or the Researcher, approve a
lab change, …) run the `lab` CLI as a human would, so they take exactly the CLI's paths and checks. It binds to
127.0.0.1 and holds no credentials; reach it from elsewhere with an SSH tunnel.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import traceback
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

from . import config as config_mod
from . import research
from .budget import ACTIVE_LEASE, Budget
from .context import MAINT_IN_FLIGHT, live_world_version, state_version, world_facts, world_lag
from .db import DB, now

CODE_ROOT = Path(__file__).resolve().parents[1]
PAGE = Path(__file__).with_name("web.html")
SEV = {"info": 0, "minor": 1, "normal": 2, "major": 3}
WEB_AUTHOR = "operator (web)"


class ReadDB(DB):
    """The DB helpers on a read-only connection: the dashboard can never change state by accident."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=10)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout=10000")


def rows(rs) -> list[dict]:
    return [dict(r) for r in rs]


def jload(s, default=None):
    try:
        return json.loads(s) if s else default
    except ValueError:
        return default


def tail(path: Path, limit: int = 40000) -> str | None:
    if not path.exists():
        return None
    t = path.read_text(errors="replace")
    return t if len(t) <= limit else f"… ({len(t) - limit} earlier chars cut)\n" + t[-limit:]


def head(path: Path, limit: int = 40000) -> str | None:
    if not path.exists():
        return None
    t = path.read_text(errors="replace")
    return t if len(t) <= limit else t[:limit] + f"\n… ({len(t) - limit} more chars)"


_svc_cache: dict = {}


def service_state(unit: str = "labd") -> str:
    """`systemctl --user is-active labd`, cached for a few seconds (every page load asks)."""
    hit = _svc_cache.get(unit)
    if hit and time.time() - hit[0] < 5:
        return hit[1]
    try:
        out = subprocess.run(["systemctl", "--user", "is-active", unit], capture_output=True, text=True,
                             timeout=5).stdout.strip() or "unknown"
    except (OSError, subprocess.TimeoutExpired):
        out = "unknown"
    _svc_cache[unit] = (time.time(), out)
    return out


class Dash:
    """Every view's data, as plain dicts. One instance per request (sqlite connections are per thread)."""

    def __init__(self, cfg, project: str | None):
        self.cfg = cfg
        self.db = ReadDB(cfg.db_path)
        self.p = cfg.project(project) if project else next(iter(cfg.projects.values()))
        self.tz = ZoneInfo(cfg.timezone)

    def close(self):
        self.db.conn.close()

    # ---------------------------------------------------------------- overview

    def overview(self) -> dict:
        db, p = self.db, self.p
        t = now()
        hb = float(db.kv_get("_lab", "heartbeat", 0) or 0)
        b = Budget(db, p, self.cfg.timezone).summary()
        running = rows(db.all("SELECT id, role, key, model, started_at FROM agent_runs WHERE project=? AND "
                              "status='running' ORDER BY id", (p.name,)))
        queued = rows(db.all("SELECT id, role, key, queued_at FROM agent_runs WHERE project=? AND status='queued' "
                             "ORDER BY id", (p.name,)))
        leases = self._active_leases()
        threads = rows(db.all("SELECT id, title, status, passes, last_pass_at, best_value, metric, spent_usd, "
                              "context_tokens, generation, rotate_pending FROM threads WHERE project=? AND "
                              "status='active' ORDER BY id", (p.name,)))
        since = t - 86400
        runs_24h = db.one("SELECT COUNT(*) n, COALESCE(SUM(cost_usd),0) c, SUM(status IN ('error','timeout')) e "
                          "FROM agent_runs WHERE project=? AND queued_at>=?", (p.name, since))
        ds = self._day_start()
        agent_today = db.one("SELECT COALESCE(SUM(cost_usd),0) c FROM agent_runs WHERE project=? AND started_at>=?",
                             (p.name, ds))["c"]
        recent = rows(db.all("SELECT id, ts, topic, severity, key, summary FROM events WHERE project=? AND "
                             "severity IN ('normal','major') AND topic NOT LIKE 'agent.%' ORDER BY id DESC LIMIT 15",
                             (p.name,)))
        schedule = {k: db.kv_get(p.name, k) for k in ("next_research", "next_daily_report")}
        return {
            "project": p.name, "projects": list(self.cfg.projects), "now": t, "timezone": self.cfg.timezone,
            "heartbeat": hb, "service": service_state(),
            "paused": db.kv_get(p.name, "gpu_paused"),
            "backoff_until": db.kv_get("_lab", "agent_backoff_until", 0) or 0,
            "budget": b, "agent_cost_today": agent_today,
            "burn_hr": sum((l["price_hr"] or 0) for l in leases if l["status"] == "granted"),
            "running": running, "queued": queued, "leases": leases, "threads": threads,
            "runs_24h": dict(runs_24h), "recent": recent, "schedule": schedule,
            "world_version": live_world_version(p), "state_version": state_version(p),
            "limits": {"agents": self.cfg.max_concurrent_agents, "threads": self.cfg.max_concurrent_threads,
                       "max_threads": p.max_threads, "test_mode": p.test_mode},
            "counts": {
                "tickets": db.one("SELECT COUNT(*) n FROM tickets WHERE project=? AND status='open'", (p.name,))["n"],
                "ideas": db.one("SELECT COUNT(*) n FROM backlog WHERE project=? AND status IN ('suggested','proposed','ready')", (p.name,))["n"],
                "maint": db.one(f"SELECT COUNT(*) n FROM maint WHERE status IN ({','.join('?' * len(MAINT_IN_FLIGHT))})",
                                MAINT_IN_FLIGHT)["n"],
            },
            "alerts": self.alerts(hb, b, leases),
        }

    def alerts(self, hb: float, b: dict, leases: list[dict]) -> list[dict]:
        """What needs a human, worst first. Each: level (critical|warning|info), text, tab to look at."""
        db, p, t = self.db, self.p, now()
        out: list[dict] = []

        def add(level, text, tab="overview"):
            out.append({"level": level, "text": text, "tab": tab})

        svc = service_state()
        if t - hb > 120:
            add("critical", f"labd heartbeat is {int((t - hb) / 60)} min old — the daemon looks down "
                            f"(service: {svc}). `journalctl --user -u labd -n 50`")
        elif svc not in ("active", "unknown"):
            add("warning", f"systemd reports labd as {svc}")
        cap = b["daily_cap"] or 0
        used = b["spent_today"] + b["reserved"]
        if cap and b["spent_today"] >= cap:
            add("critical", f"Daily budget exhausted: ${b['spent_today']:.0f} of ${cap:.0f} spent — lab pods stop",
                "gpus")
        elif cap and used >= 0.8 * cap:
            add("warning", f"Budget at {used / cap:.0%} of today's ${cap:.0f} (spent ${b['spent_today']:.0f} "
                           f"+ reserved ${b['reserved']:.0f})", "gpus")
        paused = db.kv_get(p.name, "gpu_paused")
        if paused:
            add("warning", f"GPUs paused by operator since {self.fmt(paused.get('at'))}: {paused.get('reason')}",
                "gpus")
        backoff = db.kv_get("_lab", "agent_backoff_until", 0) or 0
        if backoff > t:
            add("warning", f"Claude rate-limit backoff: agents wait until {self.fmt(backoff)}", "runs")
        for l in leases:
            age = t - (l["provisioning_at"] or l["requested_at"] or t)
            if l["status"] == "granted":
                if l["job_state"] != "running":
                    add("warning", f"Lease {l['id']} ({l['holder']}, {l['gpu_count']}×{l['gpu_short']}, "
                                   f"${l['price_hr'] or 0:.2f}/h) has no running job "
                                   f"(job {l['job_name'] or '-'}: {l['job_state'] or 'none'}) — billed while idle",
                        "gpus")
                elif l["heartbeat_at"] and t - l["heartbeat_at"] > 900:
                    add("warning", f"Lease {l['id']} job {l['job_name']} heartbeat is "
                                   f"{int((t - l['heartbeat_at']) / 60)} min old", "gpus")
                if l["expires_at"] and 0 < l["expires_at"] - t < 1800:
                    add("info", f"Lease {l['id']} ({l['holder']}) reaches its hour limit in "
                                f"{int((l['expires_at'] - t) / 60)} min", "gpus")
            elif age > 3600:
                add("warning", f"Lease {l['id']} ({l['holder']}) {l['status']} for {int(age / 60)} min"
                               + (f": {l['reason']}" if l["reason"] else ""), "gpus")
        bad = db.one("SELECT COUNT(*) n FROM agent_runs WHERE project=? AND status IN ('error','timeout') "
                     "AND queued_at>=?", (p.name, t - 6 * 3600))["n"]
        if bad:
            add("warning", f"{bad} agent run(s) failed or timed out in the last 6 h", "runs")
        stuck = db.one("SELECT MIN(queued_at) q FROM agent_runs WHERE project=? AND status='queued'", (p.name,))["q"]
        if stuck and t - stuck > 1800:
            add("warning", f"An agent run has been queued for {int((t - stuck) / 60)} min", "runs")
        failed = db.one("SELECT COUNT(*) n FROM outbox WHERE project=? AND status IN ('failed','unknown') AND "
                        "created_at>=?", (p.name, t - 86400))["n"]
        if failed:
            add("warning", f"{failed} Discord message(s) failed to send in the last 24 h", "discord")
        pend = db.one("SELECT MIN(created_at) c FROM outbox WHERE project=? AND status='pending'", (p.name,))["c"]
        if pend and t - pend > 300:
            add("warning", f"Discord outbox has messages pending for {int((t - pend) / 60)} min", "discord")
        for e in db.all("SELECT id, ts, key, summary FROM events WHERE project=? AND topic='thread.stalled' AND ts>=? "
                        "ORDER BY id DESC LIMIT 3", (p.name, t - 6 * 3600)):
            add("info", f"Stall alarm #{e['id']} ({self.ago(e['ts'])}): {e['summary'][:200]}", "events")
        for m in db.all("SELECT id, request FROM maint WHERE status='awaiting_approval' ORDER BY created_at"):
            add("warning", f"Lab change {m['id']} awaits your approval: {' '.join((m['request'] or '').split())[:140]}",
                "inbox")
        n = db.one("SELECT COUNT(*) n FROM tickets WHERE project=? AND status='open'", (p.name,))["n"]
        if n:
            add("info", f"{n} open ticket(s)", "inbox")
        if world_lag(db, p):
            add("info", f"STATE.md describes {state_version(p) or '?'}; the live world is {live_world_version(p)} "
                        "(the Scout is catching up)", "world")
        order = {"critical": 0, "warning": 1, "info": 2}
        return sorted(out, key=lambda a: order[a["level"]])

    # ---------------------------------------------------------------- threads

    def threads(self) -> dict:
        db, p = self.db, self.p
        ts = rows(db.all("SELECT * FROM threads WHERE project=? ORDER BY status='active' DESC, id DESC", (p.name,)))
        for t in ts:
            r = db.one("SELECT COUNT(*) n, SUM(kept) k FROM results WHERE thread_id=?", (t["id"],))
            t["results"], t["kept"] = r["n"], r["k"] or 0
            t["running"] = bool(db.one("SELECT 1 FROM agent_runs WHERE role='thread' AND key=? AND status IN "
                                       "('running','queued')", (t["id"],)))
            t["leases"] = [l for l in self._active_leases() if l["holder"] == t["id"]]
            t["agent_cost"] = db.one("SELECT COALESCE(SUM(cost_usd),0) c FROM agent_runs WHERE role='thread' AND key=?",
                                     (t["id"],))["c"]
            t["wake"] = db.kv_get(p.name, f"wake:{t['id']}")
        return {"threads": ts, "rotate_tokens": p.rotate_context_tokens}

    def thread(self, tid: str) -> dict:
        db, p = self.db, self.p
        t = db.one("SELECT * FROM threads WHERE id=? AND project=?", (tid, p.name))
        if not t:
            raise LookupError(f"no thread {tid}")
        t = dict(t)
        wd = Path(t["workdir"] or p.work_dir / "threads" / tid)
        files = {name: tail(wd / name) if name == "NOTES.md" else head(wd / name)
                 for name in ("NOTES.md", "program.md", "HANDOVER.md")}
        return {
            "thread": t, "files": {k: v for k, v in files.items() if v is not None}, "workdir": str(wd),
            "results": rows(db.all("SELECT * FROM results WHERE thread_id=? ORDER BY ts", (tid,))),
            "runs": rows(db.all("SELECT id, status, model, queued_at, started_at, ended_at, cost_usd, num_turns, "
                                "substr(COALESCE(result, error, ''), 1, 600) summary FROM agent_runs WHERE role='thread' "
                                "AND key=? ORDER BY id DESC LIMIT 40", (tid,))),
            "leases": self._leases("holder=?", (tid,), 20),
            "events": rows(db.all("SELECT id, ts, topic, severity, summary FROM events WHERE project=? AND key=? AND "
                                  "topic NOT LIKE 'agent.%' ORDER BY id DESC LIMIT 60", (p.name, tid))),
            "spend": db.one("SELECT COALESCE(SUM(usd),0) s FROM ledger WHERE experiment_id=?", (tid,))["s"],
            "agent_cost": db.one("SELECT COALESCE(SUM(cost_usd),0) c FROM agent_runs WHERE role='thread' AND key=?",
                                 (tid,))["c"],
        }

    # ---------------------------------------------------------------- the Researcher (live)

    def researcher(self) -> dict:
        """The Researcher's one session as it happens: its transcript, whether it is mid-pass, and who is
        talking to it (lab/research.py)."""
        db, p = self.db, self.p
        s = research.session(db, p)
        wd = p.work_dir / "researcher"
        return {
            "session": s, "rotate_tokens": p.rotate_context_tokens, "enabled": bool(p.roles["researcher"].wake_on),
            "chat": research.chat_holder(db, p),
            "run": dict(r) if (r := db.one("SELECT id, status, model, queued_at, started_at FROM agent_runs WHERE "
                                           "project=? AND role='researcher' AND status IN ('running','queued') "
                                           "ORDER BY status='running' DESC, id", (p.name,))) else None,
            "transcript": research.transcript(research.session_path(wd, s["id"]), 120),
            "runs": rows(db.all("SELECT id, key, status, model, started_at, ended_at, cost_usd FROM agent_runs WHERE "
                                "project=? AND role='researcher' ORDER BY id DESC LIMIT 12", (p.name,))),
        }

    # ---------------------------------------------------------------- GPUs and money

    def gpus(self) -> dict:
        db, p = self.db, self.p
        stock = db.kv_get(p.name, "stock", {}) or {}
        prices = db.kv_get(p.name, "gpu_prices", {}) or {}
        offers = []
        for k, v in (stock.get("shapes") or {}).items():
            g, n, cl = k.split("|")
            if not v or v == "none":
                continue
            pr = (prices.get(g) or {}).get(cl)
            offers.append({"gpu": g, "count": int(n), "cloud": cl, "stock": v,
                           "price_gpu_hr": pr if isinstance(pr, (int, float)) else None})
        offers.sort(key=lambda o: (o["price_gpu_hr"] is None, o["price_gpu_hr"] or 0, o["gpu"], o["count"]))
        return {
            "active": self._active_leases(),
            "history": self._leases("status NOT IN ('requested','provisioning','granted')", (), 40),
            "pods": rows(db.all("SELECT * FROM pods WHERE project=? ORDER BY terminated, created_at DESC LIMIT 40",
                                (p.name,))),
            "paused": db.kv_get(p.name, "gpu_paused"),
            "budget": Budget(db, p, self.cfg.timezone).summary(),
            "stock_at": stock.get("at"), "offers": offers,
            **self.spend(),
        }

    def spend(self, days: int = 14) -> dict:
        """GPU (ledger) and agent (Claude) dollars per local day, and GPU spend per holder."""
        db, p = self.db, self.p
        start = self._day_start() - (days - 1) * 86400
        buckets = []
        for i in range(days):
            d0 = datetime.fromtimestamp(start, self.tz) + timedelta(days=i)
            a = d0.timestamp()
            b = (d0 + timedelta(days=1)).timestamp()
            gpu = db.one("SELECT COALESCE(SUM(usd),0) s FROM ledger WHERE project=? AND ts>=? AND ts<?",
                         (p.name, a, b))["s"]
            ag = db.one("SELECT COALESCE(SUM(cost_usd),0) s FROM agent_runs WHERE project=? AND started_at>=? AND "
                        "started_at<?", (p.name, a, b))["s"]
            buckets.append({"day": d0.strftime("%m-%d"), "gpu": round(gpu, 2), "agents": round(ag, 2)})
        holders = rows(db.all("SELECT COALESCE(experiment_id,'(idle pods)') holder, SUM(usd) total, "
                              "SUM(CASE WHEN ts>=? THEN usd ELSE 0 END) today FROM ledger WHERE project=? "
                              "GROUP BY holder ORDER BY total DESC LIMIT 20", (self._day_start(), p.name)))
        roles = rows(db.all("SELECT role, COUNT(*) n, COALESCE(SUM(cost_usd),0) total, "
                            "SUM(CASE WHEN started_at>=? THEN cost_usd ELSE 0 END) today "
                            "FROM agent_runs WHERE project=? GROUP BY role ORDER BY total DESC",
                            (self._day_start(), p.name)))
        return {"daily": buckets, "by_holder": holders, "by_role": roles}

    # ---------------------------------------------------------------- runs, events, discord

    def runs(self, q: dict) -> dict:
        where, args = ["project=?"], [self.p.name]
        for col in ("role", "status", "key"):
            if q.get(col):
                where.append(f"{col}=?")
                args.append(q[col])
        if q.get("before"):
            where.append("id<?")
            args.append(int(q["before"]))
        n = min(int(q.get("n") or 100), 500)
        rs = rows(self.db.all(f"SELECT id, role, key, status, model, queued_at, started_at, ended_at, cost_usd, "
                              f"num_turns, substr(COALESCE(result, error, ''), 1, 300) summary FROM agent_runs "
                              f"WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT ?", args + [n]))
        roles = [r["role"] for r in self.db.all("SELECT DISTINCT role FROM agent_runs WHERE project=?", (self.p.name,))]
        return {"runs": rs, "roles": sorted(roles)}

    def run(self, rid: int) -> dict:
        r = self.db.one("SELECT * FROM agent_runs WHERE id=?", (rid,))
        if not r:
            raise LookupError(f"no run {rid}")
        r = dict(r)
        out = {"run": r, "prompt": None, "usage": None, "events": []}
        if r["log_path"]:
            lp = Path(r["log_path"])
            out["prompt"] = head(lp.with_name(lp.name.replace(".out.json", ".prompt.md")), 60000)
            raw = head(lp, 400000)
            j = jload(raw, None) if raw else None
            if isinstance(j, dict):
                out["usage"] = {k: j.get(k) for k in ("duration_ms", "duration_api_ms", "num_turns", "total_cost_usd",
                                                      "terminal_reason", "stop_reason", "is_error", "usage",
                                                      "permission_denials", "subagent_stats")}
            elif raw:
                out["raw_log"] = raw[-20000:]
        ids = jload(r["event_ids"], []) or []
        if ids:
            ids = [int(i) for i in ids[:50]]
            out["events"] = rows(self.db.all(f"SELECT id, ts, topic, severity, key, summary FROM events WHERE id IN "
                                             f"({','.join('?' * len(ids))}) ORDER BY id", ids))
        return out

    def events(self, q: dict) -> dict:
        where, args = ["project=?"], [self.p.name]
        if q.get("topic"):
            where.append("topic LIKE ?")
            args.append(q["topic"].rstrip("*") + "%")
        if q.get("key"):
            where.append("key=?")
            args.append(q["key"])
        if q.get("text"):
            where.append("summary LIKE ?")
            args.append(f"%{q['text']}%")
        if q.get("sev") and q["sev"] in SEV:
            where.append("severity IN (%s)" % ",".join("?" * sum(1 for v in SEV.values() if v >= SEV[q["sev"]])))
            args += [k for k, v in SEV.items() if v >= SEV[q["sev"]]]
        if q.get("hide_agent") == "1":
            where.append("topic NOT LIKE 'agent.%'")
        if q.get("before"):
            where.append("id<?")
            args.append(int(q["before"]))
        n = min(int(q.get("n") or 150), 1000)
        rs = rows(self.db.all(f"SELECT * FROM events WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT ?", args + [n]))
        for r in rs:
            r["payload"] = jload(r["payload"], r["payload"])
        topics = [r["t"] for r in self.db.all("SELECT DISTINCT substr(topic, 1, instr(topic || '.', '.') - 1) t "
                                              "FROM events WHERE project=? ORDER BY t", (self.p.name,))]
        return {"events": rs, "topics": topics}

    def discord(self) -> dict:
        db, p = self.db, self.p
        msgs = rows(db.all("SELECT id, channel_id, author_name, is_bot, content, reply_to, ts FROM discord_messages "
                           "WHERE project=? ORDER BY ts DESC LIMIT 80", (p.name,)))
        outbox = rows(db.all("SELECT id, channel_id, content, reply_to, status, created_at, sent_at, error, author_role, "
                             "message_ids FROM outbox WHERE project=? ORDER BY id DESC LIMIT 80", (p.name,)))
        return {"messages": msgs, "outbox": outbox, "channel_id": p.channel_id}

    def inbox(self) -> dict:
        db, p = self.db, self.p
        return {
            "tickets": rows(db.all("SELECT * FROM tickets WHERE project=? ORDER BY status='open' DESC, id DESC LIMIT 60",
                                   (p.name,))),
            "ideas": rows(db.all("SELECT * FROM backlog WHERE project=? ORDER BY CASE status WHEN 'assigned' THEN 0 "
                                 "WHEN 'ready' THEN 1 WHEN 'suggested' THEN 2 WHEN 'proposed' THEN 3 ELSE 4 END, "
                                 "priority, id DESC LIMIT 80", (p.name,))),
            "maint": rows(db.all("SELECT * FROM maint ORDER BY created_at DESC LIMIT 40")),
        }

    def world(self) -> dict:
        p = self.p
        briefs = sorted((p.world_dir / "briefs").glob("*.md"), reverse=True)[:10] if (p.world_dir / "briefs").exists() else []
        return {
            "facts": world_facts(p), "lag": world_lag(self.db, p),
            "state": head(p.world_dir / "STATE.md", 80000), "known_stale": head(p.world_dir / "KNOWN_STALE.md", 40000),
            "briefs": [{"name": b.name, "text": head(b, 20000)} for b in briefs],
        }

    # ---------------------------------------------------------------- helpers

    def _day_start(self) -> float:
        d = datetime.fromtimestamp(now(), self.tz)
        return d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()

    def _active_leases(self) -> list[dict]:
        return self._leases(f"status IN ({','.join('?' * len(ACTIVE_LEASE))})", ACTIVE_LEASE, 50)

    def _leases(self, where: str, args, limit: int) -> list[dict]:
        rs = rows(self.db.all(f"SELECT * FROM leases WHERE project=? AND {where} ORDER BY id DESC LIMIT ?",
                              (self.p.name, *args, limit)))
        t = now()
        for l in rs:
            l["holder"] = l["holder"] or l["experiment_id"]
            l["gpu_short"] = (l["gpu_type"] or "?").replace("NVIDIA ", "")
            l["job"] = jload(l["job_status"], None)
            l["alternatives"] = jload(l["alternatives"], [])
            if l["granted_at"] and l["price_hr"]:
                end = l["released_at"] or t
                l["hours_used"] = (end - l["granted_at"]) / 3600
                l["cost_so_far"] = l["hours_used"] * l["price_hr"]
        return rs

    def fmt(self, ts) -> str:
        return datetime.fromtimestamp(float(ts), self.tz).strftime("%m-%d %H:%M") if ts else "-"

    @staticmethod
    def ago(ts) -> str:
        d = now() - float(ts or 0)
        return f"{d / 60:.0f} min ago" if d < 5400 else f"{d / 3600:.1f} h ago"


# ---------------------------------------------------------------- actions (through the `lab` CLI)

ID = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")


def _need(body: dict, key: str, pattern=ID) -> str:
    v = str(body.get(key) or "").strip()
    if not v or (pattern and not pattern.match(v)):
        raise ValueError(f"missing or invalid {key!r}")
    return v


def _text(body: dict, key: str = "text", required: bool = True) -> str:
    v = str(body.get(key) or "").strip()
    if required and not v:
        raise ValueError(f"{key} is required")
    if len(v) > 8000:
        raise ValueError(f"{key} is too long")
    return v


def action_argv(body: dict) -> list[str]:
    """Map a dashboard action onto the exact `lab` command an operator would type."""
    a = body.get("action")
    if a == "gpu_pause":
        return ["gpu", "pause", _text(body, "reason", required=False) or "paused from the dashboard"]
    if a == "gpu_resume":
        return ["gpu", "resume"] + ([r] if (r := _text(body, "reason", required=False)) else [])
    if a == "thread_note":
        tid = _need(body, "id")
        return ["thread", "note", tid, "--text", _text(body), "--author", WEB_AUTHOR]
    if a in ("maint_approve", "maint_reject"):
        return ["maint", a.split("_")[1], _need(body, "id")]
    if a == "maint_request":
        return ["maint", "request", _text(body), "--text", WEB_AUTHOR]
    if a in ("idea_accept", "idea_reject", "idea_done"):
        return ["idea", a.split("_")[1], _need(body, "id", re.compile(r"^\d+$"))] + (
            ["--note", n] if (n := _text(body, "note", required=False)) else [])
    if a == "ticket_close":
        return ["ticket", "close", _need(body, "id", re.compile(r"^\d+$")), "--text", _text(body)]
    if a == "researcher_say":
        return ["researcher", "say", "--text", _text(body), "--author", WEB_AUTHOR]
    if a == "daily_report":
        return ["emit", "tick.daily_report", "requested from the dashboard"]
    raise ValueError(f"unknown action {a!r}")


def run_action(cfg_path: str | None, project: str, body: dict) -> dict:
    argv = action_argv(body)
    env = {k: v for k, v in os.environ.items() if k not in ("LAB_ROLE", "LAB_RUN_ID", "LAB_THREAD", "LAB_PROJECT")}
    cmd = [sys.executable, "-m", "lab.cli", *(["--config", cfg_path] if cfg_path else []), "--project", project, *argv]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60, cwd=CODE_ROOT, env=env)
    return {"ok": r.returncode == 0, "output": (r.stdout + r.stderr).strip(), "command": "lab " + " ".join(argv)}


# ---------------------------------------------------------------- HTTP


class Handler(BaseHTTPRequestHandler):
    server_version = "lab-web"
    cfg = None
    cfg_path: str | None = None
    allowed_hosts: set[str] = set()

    def log_message(self, fmt, *args):  # quiet: the page polls every few seconds
        if os.environ.get("LAB_WEB_VERBOSE"):
            super().log_message(fmt, *args)

    def _send(self, code: int, body: bytes, ctype: str = "application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj):
        self._send(code, json.dumps(obj, default=str).encode())

    def _host_ok(self) -> bool:
        """Refuse other Host names (DNS rebinding) — the page is only for this machine or an SSH tunnel."""
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
        return host in self.allowed_hosts

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, b"forbidden host", "text/plain")
        u = urlparse(self.path)
        q = {k: v[-1] for k, v in parse_qs(u.query).items()}
        if u.path in ("/", "/index.html"):
            return self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
        if not u.path.startswith("/api/"):
            return self._send(404, b"not found", "text/plain")
        parts = u.path[5:].strip("/").split("/")
        try:
            d = Dash(self.cfg, q.get("project"))
        except KeyError as e:
            return self._json(400, {"error": str(e)})
        try:
            view = parts[0]
            if view == "overview":
                data = d.overview()
            elif view == "threads" and len(parts) == 1:
                data = d.threads()
            elif view == "threads":
                data = d.thread(parts[1])
            elif view == "gpus":
                data = d.gpus()
            elif view == "runs" and len(parts) == 1:
                data = d.runs(q)
            elif view == "runs":
                data = d.run(int(parts[1]))
            elif view == "events":
                data = d.events(q)
            elif view == "discord":
                data = d.discord()
            elif view == "inbox":
                data = d.inbox()
            elif view == "world":
                data = d.world()
            elif view == "researcher":
                data = d.researcher()
            else:
                return self._json(404, {"error": "unknown view"})
            self._json(200, data)
        except LookupError as e:
            self._json(404, {"error": str(e)})
        except Exception as e:  # show the error in the page instead of a dropped connection
            traceback.print_exc()
            self._json(500, {"error": f"{type(e).__name__}: {e}"})
        finally:
            d.close()

    def do_POST(self):
        # A custom header forces a CORS preflight, which this server never answers: other sites can't post here.
        if not self._host_ok() or self.headers.get("X-Lab-Web") != "1":
            return self._json(403, {"error": "forbidden"})
        origin = self.headers.get("Origin")
        if origin and urlparse(origin).hostname not in self.allowed_hosts:
            return self._json(403, {"error": "forbidden origin"})
        if urlparse(self.path).path != "/api/action":
            return self._json(404, {"error": "not found"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(min(n, 65536)) or b"{}")
            project = self.cfg.project(body.get("project") or next(iter(self.cfg.projects))).name
            self._json(200, run_action(self.cfg_path, project, body))
        except (ValueError, KeyError) as e:
            self._json(400, {"ok": False, "output": str(e)})
        except subprocess.TimeoutExpired:
            self._json(504, {"ok": False, "output": "the lab command timed out"})


def make_server(cfg, cfg_path: str | None, host: str = "127.0.0.1", port: int = 8765,
                extra_hosts: list[str] | None = None) -> ThreadingHTTPServer:
    handler = type("LabHandler", (Handler,), {
        "cfg": cfg, "cfg_path": cfg_path,
        "allowed_hosts": {"127.0.0.1", "localhost", "::1", *(h.lower() for h in extra_hosts or [])},
    })
    srv = ThreadingHTTPServer((host, port), handler)
    srv.daemon_threads = True
    return srv


def main(argv=None):
    ap = argparse.ArgumentParser(prog="lab web", description=__doc__)
    ap.add_argument("--config")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=int(os.environ.get("LAB_WEB_PORT", 8765)))
    ap.add_argument("--allow-host", action="append", help="extra Host name to accept (e.g. a tailnet name)")
    a = ap.parse_args(argv)
    cfg_path = a.config or os.environ.get("LAB_CONFIG")
    cfg = config_mod.load(cfg_path)
    srv = make_server(cfg, cfg_path, a.host, a.port, a.allow_host)
    print(f"lab web: http://{a.host}:{srv.server_address[1]}/  (projects: {', '.join(cfg.projects)})", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
