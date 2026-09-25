"""Agents: event-driven wakes of headless Claude Code runs.

Each role is a fresh `claude -p` process per wake (the Ralph pattern from
affine/ralphs/ralph.sh: no memory between passes except the working directory
and the lab's registries); implementor threads and the Researcher resume their own
session. The dispatcher turns new events into queued runs;
the launcher starts them by priority under a global concurrency cap and backs
off everything when the subscription reports a usage/rate limit.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import signal
import time
import uuid
from pathlib import Path

from .config import LAB_ROOT, LIGHT_MAX_CONTEXT, SONNET, LabConfig, Project, RoleConfig
from .context import (activity_digest, backlog_table, file_ref, iso, leases_table, read, research_doc,
                      results_table, state_head, status_text, threads_table, world_now)
from .db import DB, now, topic_match
from .maint import Maint
from . import research

log = logging.getLogger("lab.agents")

SEV = {"info": 0, "minor": 1, "normal": 2, "major": 3}
RESEARCHER_WAITS_FOR_SCOUT_S = 1200
KEYED_ROLES = {"concierge", "thread", "maintainer"}
RATE_RE = re.compile(r"rate.?limit|usage limit|too many requests|\b429\b|overloaded|limit reached|"
                     r"resets? at|quota|out of (extra )?usage", re.I)
SECRET_ENV = ("DISCORD_BOT_TOKEN", "RUNPOD_API_KEY", "SHADEFORM_API_KEY", "VAST_API_KEY", "DISCORD_BOT_TOKEN_ARBOS_BITTENSOR")

READONLY_TOOLS = [
    "Read", "Grep", "Glob", "WebFetch", "WebSearch",
    "Bash(lab status*)", "Bash(lab world*)", "Bash(lab events*)",
    "Bash(lab ticket*)", "Bash(lab idea list*)", "Bash(lab idea show*)", "Bash(lab idea suggest*)", "Bash(lab thread list*)",
    "Bash(lab idea reject*)", "Bash(lab idea clear*)", "Bash(lab thread retire*)",   # operators' orders only (CLI checks)
    "Bash(lab thread show*)", "Bash(lab thread note*)", "Bash(lab maint list*)", "Bash(lab maint show*)",
    "Bash(lab budget*)", "Bash(lab runs*)", "Bash(lab gpu list*)", "Bash(lab gpu stock*)",
    "Bash(ls*)", "Bash(cat *)", "Bash(head *)", "Bash(tail *)", "Bash(grep *)", "Bash(rg *)", "Bash(wc *)",
    "Bash(find *)", "Bash(git log*)", "Bash(git show*)", "Bash(git diff*)", "Bash(git status*)",
    "Bash(curl -s*)", "Bash(curl -sS*)", "Bash(jq *)", "Bash(date*)",
]


def lab_bin_dir(cfg: LabConfig) -> Path:
    return cfg.root / "bin"


def role_workdir(project: Project, role: str, key: str | None, cfg: LabConfig | None = None) -> Path:
    if role == "maintainer" and cfg:
        return cfg.maint.dir / (key or "unknown")     # a git worktree of the lab (Maint.prepare)
    if role == "thread":
        return project.work_dir / "threads" / (key or "unknown")
    if role == "scout":
        return project.world_dir
    return project.work_dir / role


def fmt_events(rows, payload_limit: int) -> str:
    out = []
    for r in rows:
        line = f"- event #{r['id']} [{r['severity']}] {r['topic']}{' key=' + r['key'] if r['key'] else ''}: {r['summary']}"
        if r["payload"] and payload_limit:
            p = r["payload"]
            if len(p) > payload_limit:
                p = p[:payload_limit] + f"… (payload truncated; full: `lab events --id {r['id']}`)"
            line += f"\n  payload: {p}"
        out.append(line)
    return "\n".join(out) or "(no events)"


def directives_text(db, project: str) -> str:
    """Every directive people have given, so a RESEARCH.md trim can never drop one; a later one may withdraw
    or amend an earlier one."""
    rows = db.all("SELECT * FROM events WHERE project=? AND topic='research.directive' ORDER BY id", (project,))
    out = []
    for r in rows:
        d = json.loads(r["payload"] or "{}")
        out.append(f"- [{time.strftime('%Y-%m-%d', time.gmtime(r['ts']))}] {d.get('author') or '?'}"
                   f"{' (msg ' + d['message_id'] + ')' if d.get('message_id') else ''}: {d.get('title') or ''} — "
                   + " ".join((d.get("text") or "").split())[:800])
    return "\n".join(out) or "(none)"


HANDOVER_ASK = """## This is the last pass of this session
Your conversation is {k}k tokens long, and every tool call re-reads all of it. After this pass the lab
starts you in a fresh session. Do this wake's work as usual; then, before your report, (over)write
`HANDOVER.md` in your working directory. The next you reads it first and in full, and remembers nothing else:
- what is running right now (pods, job names, where outputs land, when to check them);
- your current best and its evidence; what you were about to do next, and why;
- dead ends not to retry, and why; open questions and anything you promised people;
- key paths, branches and commits.
Keep it under ~10,000 characters; details belong in NOTES.md. Then end with your NEXT line as usual."""

RESEARCHER_HANDOVER_ASK = """## This is the last pass of this session
Your conversation is {k}k tokens long. After this wake the lab starts you in a fresh session that remembers
nothing but your files. Do this wake's work; then make sure RESEARCH.md holds everything the next you needs:
what you are waiting for from which thread, what you promised people, and what operators told you in live
chats that should outlast this session (standing guidance belongs under Directives, with who and when)."""


def context_tokens(res: dict) -> int | None:
    """Context size of the last model call in a run (what the next call will re-read)."""
    it = (res.get("usage") or {}).get("iterations") or []
    u = it[-1] if it else None
    if not isinstance(u, dict):
        return None
    return sum(int(u.get(k) or 0) for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))


class Agents:
    def __init__(self, db: DB, cfg: LabConfig, on_result=None):
        self.db = db
        self.cfg = cfg
        self.on_result = on_result          # async (project, role, key, run_row, result_dict) -> None
        self.tasks: dict[int, asyncio.Task] = {}
        self._roles: dict[int, tuple[str, str]] = {}   # run id -> (project, role), for slot accounting
        self.maint = Maint(db, cfg)
        self.runs_dir = cfg.state_dir / "runs"
        self.runs_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------ recovery
    def recover(self) -> None:
        """Runs left 'running' by a crashed daemon are requeued once."""
        for r in self.db.all("SELECT * FROM agent_runs WHERE status='running'"):
            self.db.update("agent_runs", "id=?", (r["id"],), status="interrupted", ended_at=now())
            self.db.insert("agent_runs", project=r["project"], role=r["role"], key=r["key"], status="queued",
                           queued_at=now(), event_ids=r["event_ids"])

    # ------------------------------------------------------------ dispatch
    def _cursor(self, project: str, role: str, key: str) -> int:
        r = self.db.one("SELECT last_event_id FROM cursors WHERE project=? AND role=? AND key=?", (project, role, key))
        if r is None:
            # first time we see this role: start from now, do not replay history
            start = self.db.max_event_id() if role not in KEYED_ROLES else 0
            self.db.x("INSERT OR IGNORE INTO cursors(project,role,key,last_event_id) VALUES(?,?,?,?)",
                      (project, role, key, start))
            return start
        return int(r["last_event_id"] or 0)

    def _set_cursor(self, project: str, role: str, key: str, eid: int) -> None:
        self.db.x("INSERT INTO cursors(project,role,key,last_event_id,last_run_at) VALUES(?,?,?,?,?) "
                  "ON CONFLICT(project,role,key) DO UPDATE SET last_event_id=excluded.last_event_id, "
                  "last_run_at=excluded.last_run_at", (project, role, key, eid, now()))

    def _pending(self, project: str, role: str, key: str) -> bool:
        return self.db.one("SELECT 1 FROM agent_runs WHERE project=? AND role=? AND key=? AND status IN "
                           "('queued','running')", (project, role, key)) is not None

    def dispatch(self) -> None:
        for p in self.cfg.projects.values():
            for role in p.roles.values():
                if not role.wake_on:
                    continue
                if role.name in KEYED_ROLES:
                    self._dispatch_keyed(p, role)
                else:
                    self._dispatch_single(p, role)

    def _matching(self, p: Project, role: RoleConfig, after: int):
        """(highest event id scanned, events that wake this role)."""
        rows = self.db.all("SELECT * FROM events WHERE project=? AND id>? ORDER BY id LIMIT 2000", (p.name, after))
        top = rows[-1]["id"] if rows else after
        return top, [r for r in rows if topic_match(r["topic"], role.wake_on)
                     and SEV.get(r["severity"], 0) >= SEV.get(role.min_severity, 0)]

    def _dispatch_single(self, p: Project, role: RoleConfig) -> None:
        key = ""
        if self._pending(p.name, role.name, key):
            return
        cur = self._cursor(p.name, role.name, key)
        _, evs = self._matching(p, role, cur)
        if not evs:
            return
        # an operator talking to the Researcher is waiting for the answer: no debounce, no waiting for the Scout
        live = any(e["topic"] == "research.operator" for e in evs)
        # debounce: wait until the burst has been quiet for debounce_s
        if role.debounce_s and not live and now() - evs[-1]["ts"] < role.debounce_s:
            return
        # the Researcher reasons on World State: let a pending Scout update land first (bounded, so a stuck
        # Scout cannot hold up the threads' questions)
        if role.name == "researcher" and not live and now() - evs[0]["ts"] < RESEARCHER_WAITS_FOR_SCOUT_S \
                and self._scout_pending(p):
            return
        self._queue(p, role, key, evs)

    def _scout_pending(self, p: Project) -> bool:
        scout = p.roles.get("scout")
        if not scout or not scout.wake_on:
            return False
        return self._pending(p.name, "scout", "") or bool(self._matching(p, scout, self._cursor(p.name, "scout", ""))[1])

    def _dispatch_keyed(self, p: Project, role: RoleConfig) -> None:
        """One run per key (message id / experiment id). Keys whose previous run
        is still in flight are deferred and rescanned next time."""
        scan = int(self.db.kv_get(p.name, f"scan:{role.name}", 0) or 0)
        deferred: dict[str, int] = self.db.kv_get(p.name, f"deferred:{role.name}", {}) or {}
        start = min([scan] + [v - 1 for v in deferred.values()])
        top, evs = self._matching(p, role, start)
        by_key: dict[str, list] = {}
        for r in evs:
            if r["key"]:
                by_key.setdefault(r["key"], []).append(r)
        for key, rows in by_key.items():
            cur = self._cursor(p.name, role.name, key)
            fresh = [r for r in rows if r["id"] > cur]
            if not fresh:
                deferred.pop(key, None)
                continue
            if self._pending(p.name, role.name, key):
                deferred[key] = min(deferred.get(key, fresh[0]["id"]), fresh[0]["id"])
                continue
            deferred.pop(key, None)
            if role.name == "thread" and not self._thread_allowed(p, key):
                self._set_cursor(p.name, role.name, key, fresh[-1]["id"])
                continue
            self._queue(p, role, key, fresh)
        self.db.kv_set(p.name, f"scan:{role.name}", max(scan, top))
        self.db.kv_set(p.name, f"deferred:{role.name}", deferred)

    def _thread_allowed(self, p: Project, tid: str) -> bool:
        t = self.db.one("SELECT status FROM threads WHERE id=? AND project=?", (tid, p.name))
        return bool(t and t["status"] == "active")

    def _queue(self, p: Project, role: RoleConfig, key: str, evs) -> int:
        rid = self.db.insert("agent_runs", project=p.name, role=role.name, key=key, status="queued", queued_at=now(),
                             event_ids=json.dumps([r["id"] for r in evs]))
        self._set_cursor(p.name, role.name, key, evs[-1]["id"])
        return rid

    # ------------------------------------------------------------ launch
    def backoff_until(self) -> float:
        return float(self.db.kv_get("_lab", "agent_backoff_until", 0) or 0)

    def slot_class(self, role: str) -> str:
        return role if role in ("thread", "concierge", "maintainer") else "thinker"

    def slot_pool(self, project: str, role: str) -> tuple[str, str]:
        """Every project has its own workers: its slots never go to another project's agents. The maintainer
        changes the shared lab code, so it has one lab-wide slot."""
        cls = self.slot_class(role)
        return ("_lab" if cls == "maintainer" else project, cls)

    def slot_limit(self, cls: str) -> int:
        return {"thread": self.cfg.max_concurrent_threads, "concierge": self.cfg.max_concurrent_concierge,
                "maintainer": 1}.get(cls, self.cfg.max_concurrent_agents)

    def launch(self) -> None:
        """Start queued runs by priority. Each project has its own slot pools (research threads, the concierge,
        the thinking roles), so threads never wait for the Researcher and one project never waits for another."""
        if self.backoff_until() > now():
            return
        rows = self.db.all("SELECT * FROM agent_runs WHERE status='queued' ORDER BY id")
        if not rows:
            return
        # a lab deploy is waiting for the agents to go idle: only people's questions still start
        hold = float(self.db.kv_get("_lab", "agents_hold_until", 0) or 0) > now()
        in_use: dict[tuple[str, str], int] = {}
        for rid, t in self.tasks.items():
            if not t.done():
                pool = self.slot_pool(*self._roles.get(rid, ("_lab", "thinker")))
                in_use[pool] = in_use.get(pool, 0) + 1
        prio = lambda r: (self.cfg.projects[r["project"]].roles[r["role"]].priority
                          if r["project"] in self.cfg.projects and r["role"] in self.cfg.projects[r["project"]].roles
                          else 99, r["id"])
        for r in sorted(rows, key=prio):
            if r["project"] not in self.cfg.projects or r["role"] not in self.cfg.projects[r["project"]].roles:
                self.db.update("agent_runs", "id=?", (r["id"],), status="error", error="unknown project or role")
                continue
            if hold and r["role"] != "concierge":
                continue
            # an operator is in the Researcher's session (`lab researcher chat`): its wakes wait for them
            if r["role"] == "researcher" and research.chat_holder(self.db, self.cfg.projects[r["project"]]):
                continue
            pool = self.slot_pool(r["project"], r["role"])
            if in_use.get(pool, 0) >= self.slot_limit(pool[1]):
                continue
            in_use[pool] = in_use.get(pool, 0) + 1
            self.db.update("agent_runs", "id=?", (r["id"],), status="running", started_at=now())
            self._roles[r["id"]] = (r["project"], r["role"])
            self.tasks[r["id"]] = asyncio.create_task(self._run(r["id"]))

    async def shutdown(self) -> None:
        for t in self.tasks.values():
            t.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)

    # ------------------------------------------------------------ prompts
    def system_prompt(self, p: Project, role: RoleConfig, workdir: Path) -> str:
        parts = []
        # the maintainer works on the lab itself, not on a project: its prompt is lab-wide
        if role.name == "maintainer":
            files = [next((d / "prompts" / "maintainer.md" for d in (self.cfg.root, LAB_ROOT)
                           if (d / "prompts" / "maintainer.md").exists()), LAB_ROOT / "prompts" / "maintainer.md")]
        else:
            files = [p.prompts_dir / "common.md", p.prompts_dir / f"{role.name}.md"]
        for f in files:
            if f.exists():
                parts.append(f.read_text())
        text = "\n\n".join(parts)
        pools = ", ".join(p.pools)
        rules = (f"**Budget: ${p.daily_usd:.0f}/day is the only spending limit.** A lease that would push today's "
                 f"spend plus reservations past it is refused, and every lab pod is stopped when it is reached. "
                 f"Spend is tracked per thread and per pool ({pools}); pools are labels, not separate caps.")
        if p.per_experiment_usd:
            rules += f" Experiments estimated above ${p.per_experiment_usd:.0f} wait for a human to approve them."
        tp = p.fleet.get("test_policy", {}) if p.test_mode else {}
        if tp:
            n, types = tp.get("max_gpus_per_experiment"), tp.get("allowed_gpu_types", [])
            rules += ("\n   **TEST MODE is on:** every GPU lease may have at most "
                      f"{n}× GPU of {', '.join(types) or 'any type'}; other leases are refused. "
                      "Design experiments to fit (smaller slices, LoRA, a quantised teacher). This is temporary.")
        subs = {"project": p.name, "project_root": str(p.root), "lab_root": str(self.cfg.root),
                "world_dir": str(p.world_dir), "work_dir": str(p.work_dir), "workdir": str(workdir),
                "role": role.name, "daily_usd": f"{p.daily_usd:.0f}", "pools": pools, "budget_rules": rules,
                "per_experiment_usd": f"{p.per_experiment_usd:.0f}", "pod_prefix": p.pod_prefix,
                "timezone": self.cfg.timezone, "report_time": p.report_time, "max_threads": str(p.max_threads),
                "rotate_k": str(p.rotate_context_tokens // 1000)}
        for k, v in subs.items():
            text = text.replace("{{" + k + "}}", v)
        return text

    def user_prompt(self, p: Project, role: RoleConfig, key: str, evs) -> str:
        """Each role sees only what its job needs; everything else is one `lab …` command or file away."""
        db, name = self.db, p.name
        since_day = now() - 86400
        head = [f"You are the **{role.name}** of the {name} lab. Wake time: {now():.0f} "
                f"({time.strftime('%Y-%m-%d %H:%M %Z')})."]
        payload_limit = {"scout": 20000, "concierge": 1500, "thread": 4000, "researcher": 6000}.get(role.name, 2500)
        head += ["", "## Why you were woken", fmt_events(evs, payload_limit)]
        if role.name == "concierge":
            ev = evs[-1]
            msg = json.loads(ev["payload"] or "{}")
            hist = db.all("SELECT * FROM discord_messages WHERE project=? ORDER BY ts DESC LIMIT 15", (name,))
            convo = "\n".join(f"[{time.strftime('%m-%d %H:%M', time.localtime(m['ts']))}] "
                              f"{m['author_name']}{' (bot)' if m['is_bot'] else ''} (msg {m['id']}"
                              f"{', reply to ' + m['reply_to'] if m['reply_to'] else ''}): {m['content'][:600]}"
                              for m in reversed(hist))
            head += ["", "## The request you must answer",
                     f"From **{msg.get('author_name')}** (Discord id {msg.get('author_id')}), message id {msg.get('id')}"
                     + (" — **an operator** (checked by labd: their orders to clear ideas or retire threads may be "
                        "carried out)" if msg.get("operator") else "") + ":",
                     "> " + (msg.get("content") or "").replace("\n", "\n> ")]
            files = msg.get("files") or []
            if files:
                head += ["Attached files (saved by labd; pass them on by path — `--file`, never retype or summarise them):"]
                head += [f"- `{f['path']}` ({f.get('filename')}, {f.get('size') or '?'} bytes)" if f.get("path")
                         else f"- {f.get('filename')}: could not be saved ({f.get('error')})" for f in files]
            if msg.get("reply_to_content"):
                head += ["It replies to this earlier bot message:", "> " + msg["reply_to_content"][:3000].replace("\n", "\n> ")]
            head += ["", "## Recent channel conversation (oldest first)", convo or "(none)",
                     "", "## Lab status", status_text(db, self.cfg, p),
                     "", "## World State summary", state_head(p)]
        elif role.name == "scout":
            head += ["", "## Current world.json facts", world_now(db, p),
                     "", "## Your files",
                     "- " + file_ref(p.world_dir / "STATE.md", "World State, which you keep current"),
                     "- " + file_ref(p.world_dir / "KNOWN_STALE.md", "local files that contradict the world"),
                     "", "## STATE.md's summary as it stands", state_head(p),
                     "", "## Threads and their tasks (name any your change affects)", threads_table(db, name)]
        elif role.name == "thread":
            head += self._thread_context(p, key, evs)
        elif role.name == "researcher":
            said = [e for e in evs if e["topic"] == "research.operator"]
            if said:
                head += ["", "## An operator is talking to you (the lab dashboard or `lab researcher say`)",
                         "Answer them in your final message: it is shown to them as your reply. Act on "
                         "what they ask as you would on a directive, and say what you changed."]
                for e in said:
                    m = json.loads(e["payload"] or "{}")
                    head += [f"**{m.get('author') or 'operator'}** ({iso(e['ts'])}):",
                             "> " + (m.get("text") or "").replace("\n", "\n> ")]
            done = db.all("SELECT * FROM backlog WHERE project=? AND status IN ('done','rejected') "
                          "ORDER BY COALESCE(done_at, created_at) DESC LIMIT 8", (name,))
            head += ["", "## The shared research memory (yours to keep; every thread reads it)",
                     file_ref(research_doc(p), "your memory between wakes: read it first"),
                     "", "## People's directives (standing guidance for every idea, newest last)",
                     directives_text(db, name),
                     "", "## Open ideas", backlog_table(db, name, spec=600),
                     "", "## Recently closed ideas (newest first)",
                     "\n".join(f"- idea {r['id']} [{r['status']}] {r['title']}: "
                               + " ".join((r["result"] or r["notes"] or "").split())[:400] for r in done) or "(none)",
                     "", f"## Threads (at most {p.max_threads}; labd hands ready ideas to idle threads)",
                     threads_table(db, name),
                     "", "## Latest results (all threads)", results_table(db, name, limit=15),
                     "", "## World now (live facts)", world_now(db, p),
                     "", "## World State summary", state_head(p)]
            s = research.session(db, p)
            if s["id"] and s["passes"]:
                head += ["", "(You are resuming your own session: your earlier wakes, and any live chats operators "
                             "had with you, are above in this conversation. The tables above are the state now.)"]
                if s["rotate"]:
                    head += ["", RESEARCHER_HANDOVER_ASK.format(k=(s["ctx"] or 0) // 1000)]
        elif role.name == "maintainer":
            m = self.maint.get(key) or {"author": "?", "request": "(no such request)", "context": None,
                                        "branch": None, "base_sha": None}
            head += ["", f"## The request ({key}) from **{m['author']}**",
                     "> " + (m["request"] or "").replace("\n", "\n> ")]
            if m["context"]:
                head += ["It replies to this earlier bot message:", "> " + m["context"][:3000].replace("\n", "\n> ")]
            head += ["", "## Your worktree",
                     f"`{role_workdir(p, role.name, key, self.cfg)}` (your cwd): branch `{m['branch']}`, from the "
                     f"live lab's HEAD {(m['base_sha'] or '')[:10]}. The live lab `{self.cfg.root}` is read-only for you.",
                     "", "## Earlier maintainer requests", self.maint.status_text(),
                     "", "## Lab status", status_text(db, self.cfg, p)]
        elif role.name == "analyst":
            daily = any(r["topic"] == "tick.daily_report" for r in evs)
            head += ["", "## World State summary", state_head(p),
                     "", "## Lab status", status_text(db, self.cfg, p)]
            if daily:
                last = float(db.kv_get(name, "last_daily_report", 0) or since_day)
                head += ["", f"## Everything the lab did since the last report ({time.ctime(last)})",
                         activity_digest(db, self.cfg, p, last)]
            else:
                head += ["", "## Latest results", results_table(db, name, limit=30)]
        return "\n".join(head)

    def _thread_context(self, p: Project, tid: str, evs=()) -> list[str]:
        """Resumed passes already remember everything: give them only what is new (a new task in full).
        A fresh session (first pass, or after rotation) gets its task, handover and notes."""
        db, name = self.db, p.name
        t = db.one("SELECT * FROM threads WHERE id=?", (tid,))
        wd = role_workdir(p, "thread", tid)
        task = f"idea {t['task_id']}" if t["task_id"] else "no task (report done; wait for the next one)"
        live = [f"## Thread {tid} — {task}: {t['title']}  (pass {t['passes'] + 1}, spent ${t['spent_usd'] or 0:.2f}, "
                f"best {t['metric'] or 'metric'} = {t['best_value'] if t['best_value'] is not None else '—'})",
                "", "## Your GPU leases", leases_table(db, name, holder=tid),
                "", "## Your last results", results_table(db, name, thread=tid, limit=8),
                "", "## Budget", status_text(db, self.cfg, p).splitlines()[1]]
        new_task = any(e["topic"] == "thread.task" for e in evs)
        if t["session_id"] and t["session_passes"]:
            out = [""] + live
            if new_task:
                out += ["", "## Your new task (TASK.md)", read(wd / "TASK.md", 12000)]
            if self._research_changed(p, tid):
                out += ["", "## The shared research memory changed since your last pass (RESEARCH.md)",
                        read(research_doc(p), 15000)]
            out += ["", "(You are resuming your own session; your earlier passes are above in this conversation. "
                        "TASK.md and NOTES.md are in your working directory.)"]
            out += self._world_news(p, tid, fresh=False)
            if t["rotate_pending"]:
                out += ["", HANDOVER_ASK.format(k=(t["context_tokens"] or 0) // 1000)]
            return out
        gen = t["generation"] or 1
        fresh = ["", f"(This is a fresh session — generation {gen} of your mind. "
                     + ("Your earlier sessions' memory is in HANDOVER.md (below), NOTES.md, results.tsv and your git "
                        "branch; read NOTES.md in full if you need more.)" if gen > 1 else "Welcome.)")]
        if (wd / "HANDOVER.md").exists():
            fresh += ["", "## Handover from your previous session (HANDOVER.md)", read(wd / "HANDOVER.md", 12000)]
        reports = db.all("SELECT * FROM agent_runs WHERE role='thread' AND key=? AND status='ok' AND result IS NOT NULL "
                         "ORDER BY id DESC LIMIT 2", (tid,))
        charter = wd / "TASK.md" if (wd / "TASK.md").exists() else wd / "program.md"
        self._research_changed(p, tid)       # seen now
        return fresh + ["", "## The shared research memory (RESEARCH.md: what we optimise, what we know)",
                        read(research_doc(p), 15000),
                        "", f"## Your task ({charter.name})", read(charter, 12000),
                "", "## Your notes (NOTES.md, tail)", "\n".join(read(wd / "NOTES.md", 40000).splitlines()[-40:]),
                "", "## Your last pass reports (newest first)",
                "\n\n".join(f"### run {r['id']} ({iso(r['ended_at'])})\n{(r['result'] or '')[:1500]}" for r in reports)
                or "(none)",
                "", "## World State summary", state_head(p)] \
            + self._world_news(p, tid, fresh=True) + ["", ""] + live

    def _research_changed(self, p: Project, tid: str) -> bool:
        """True once per change of RESEARCH.md for this thread (a resumed session never re-reads files by itself)."""
        f = research_doc(p)
        stamp = f.stat().st_mtime_ns if f.exists() else 0
        if not stamp or self.db.kv_get(p.name, f"research_seen:{tid}") == stamp:
            return False
        self.db.kv_set(p.name, f"research_seen:{tid}", stamp)
        return True

    def _world_news(self, p: Project, tid: str, fresh: bool) -> list[str]:
        """The rules move under a long-lived session (a resumed pass never re-reads STATE.md), so every
        pass gets the live facts, and a resumed one also gets the Scout's briefs since its last pass."""
        seen = int(self.db.kv_get(p.name, f"brief_seen:{tid}", 0) or 0)
        briefs = self.db.all("SELECT * FROM events WHERE project=? AND topic='world.brief' AND id>? ORDER BY id",
                             (p.name, seen))
        if briefs:
            self.db.kv_set(p.name, f"brief_seen:{tid}", briefs[-1]["id"])
        out = ["", "## World now (live facts from world.json)", world_now(self.db, p)]
        if fresh or not briefs:
            return out
        out += ["", f"## World changes since your last pass ({len(briefs)} brief(s); STATE.md in "
                    f"{p.world_dir} has the full current rules — check whether your direction or metric is affected)"]
        for b in briefs[-5:]:
            path = json.loads(b["payload"] or "{}").get("path")
            out += ["", f"### brief #{b['id']} ({iso(b['ts'])}, {b['severity']})",
                    read(Path(path), 3000) if path else (b["summary"] or "")]
        return out

    # ------------------------------------------------------------ run
    def _settings(self, role: RoleConfig, p: Project | None = None) -> dict:
        guard = str(lab_bin_dir(self.cfg) / "lab-guard")
        s: dict = {"hooks": {"PreToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": guard}]}]},
                   "permissions": {"deny": [
                       "Read(~/.config/lab/**)", "Read(~/Work/discord-reporter/.env)", "Read(~/.claude/settings.json)",
                       "Read(~/.claude.json)", "Read(~/.claude/.credentials.json)",
                       "Edit(~/Work/lab/lab/**)", "Edit(~/Work/lab/lab.toml)", "Edit(~/Work/lab/bin/**)",
                       "Edit(~/Work/lab/projects/*/project.toml)", "Write(~/Work/lab/lab/**)",
                       "Write(~/Work/lab/lab.toml)", "Write(~/Work/lab/bin/**)", "Write(~/Work/lab/projects/*/project.toml)",
                       "mcp__runpod", "mcp__runpod-docs",
                   ]},
                   # memory is the lab's files and registries: no per-cwd auto-memory to load or write
                   "autoMemoryEnabled": False}
        if p is not None:
            # the only CLAUDE.md an agent needs is its project's (the operators' rules and the goal): skip the
            # ancestors' (the lab's developer notes, the workstation's) that Claude Code would load from the cwd up
            s["claudeMdExcludes"] = [str(d / "CLAUDE.md") for d in p.dir.parents if (d / "CLAUDE.md").exists()]
        return s

    @staticmethod
    def model_for(role: RoleConfig, evs, session_tokens: int | None = None) -> str:
        """The role's light model when every wake event is routine, else its main model. A resumed session
        past LIGHT_MAX_CONTEXT stays on its main model: the light model would have to re-cache all of it."""
        if role.light_model and evs and all(topic_match(e["topic"], role.light_topics) for e in evs) \
                and (session_tokens or 0) <= LIGHT_MAX_CONTEXT:
            return role.light_model
        return role.model

    def command(self, p: Project, role: RoleConfig, run_id: int, workdir: Path, sys_file: Path,
                settings_file: Path, session: tuple[str, bool] | None = None, model: str | None = None,
                interactive: bool = False) -> list[str]:
        """The agent's `claude -p` command line; `interactive` is the same session, prompt, guard and tools
        opened in Claude Code for a person (`lab researcher chat`)."""
        cmd = [self.cfg.claude_bin] + ([] if interactive else ["-p", "--output-format", "json"]) + [
               "--model", model or role.model,
               "--append-system-prompt-file", str(sys_file), "--settings", str(settings_file),
               "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
               "--add-dir", str(p.root), "--add-dir", str(p.dir),
               "--name", f"lab:{p.name}:{role.name}:{'chat' if interactive else run_id}"]
        if role.toolset:
            cmd += ["--tools", ",".join(role.toolset)]
        if role.effort:
            cmd += ["--effort", role.effort]
        if role.advisor and (model or role.model) == role.model:   # routine checks on the light model go without
            cmd += ["--advisor", role.advisor]
        if session:
            sid, resume = session
            cmd += (["--resume", sid] if resume else ["--session-id", sid])
            # re-render the system prompt every request so rule changes reach long-lived minds
            cmd += ["--system-prompt-snapshot", "off"]
        if role.tools == "readonly":
            cmd += ["--permission-mode", "dontAsk", "--permission-prompts", "none",
                    "--allowedTools", " ".join(READONLY_TOOLS)]
        else:
            cmd += ["--permission-mode", "bypassPermissions"]
        return cmd

    def env(self, p: Project, role: RoleConfig, run_id: int, key: str) -> dict:
        env = {k: v for k, v in os.environ.items() if k not in SECRET_ENV}
        env["PATH"] = f"{lab_bin_dir(self.cfg)}:{env.get('PATH', '')}"
        env.update(LAB_PROJECT=p.name, LAB_ROLE=role.name, LAB_RUN_ID=str(run_id), LAB_CONFIG=str(self.cfg.root / "lab.toml"))
        env["CLAUDE_CODE_SUBAGENT_MODEL"] = SONNET   # subagents do lookups and checks
        if role.name == "thread":
            env["LAB_THREAD"] = key
        if role.name == "concierge":   # the CLI lets the Concierge carry out operators' orders only (checked in code)
            req = self.db.one("SELECT payload FROM events WHERE topic='discord.request' AND key=? ORDER BY id DESC", (key,))
            author = str(json.loads(req["payload"] or "{}").get("author_id")) if req else ""
            if author and author in self.cfg.maint.operators:
                env["LAB_OPERATOR"] = "1"
        return env

    async def _run(self, run_id: int) -> None:
        r = self.db.one("SELECT * FROM agent_runs WHERE id=?", (run_id,))
        p = self.cfg.projects[r["project"]]
        role = p.roles[r["role"]]
        key = r["key"] or ""
        ids = json.loads(r["event_ids"] or "[]")
        evs = self.db.all(f"SELECT * FROM events WHERE id IN ({','.join('?' * len(ids))}) ORDER BY id", ids) if ids else []
        try:
            workdir = self.maint.prepare(key) if role.name == "maintainer" else role_workdir(p, role.name, key)
            workdir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            log.exception("workdir failed")
            self.db.update("agent_runs", "id=?", (run_id,), status="error", ended_at=now(), error=f"workdir: {e!r}")
            if role.name == "maintainer" and self.maint.get(key):
                self.maint._set(key, status="failed", note=f"could not make a worktree: {e}"[:500])
                self.maint.say(self.maint.get(key), f"**{key} not deployed:** could not make a worktree: {e}"[:1500])
            return
        base = self.runs_dir / f"{run_id:06d}-{p.name}-{role.name}"
        sys_file, settings_file = base.with_suffix(".system.md"), base.with_suffix(".settings.json")
        prompt_file, out_file = base.with_suffix(".prompt.md"), base.with_suffix(".out.json")
        try:
            sys_file.write_text(self.system_prompt(p, role, workdir))
            settings_file.write_text(json.dumps(self._settings(role, p if role.name != "maintainer" else None), indent=1))
            prompt = self.user_prompt(p, role, key, evs)
            prompt_file.write_text(prompt)
        except Exception as e:
            log.exception("prompt build failed")
            self.db.update("agent_runs", "id=?", (run_id,), status="error", ended_at=now(), error=f"prompt: {e!r}")
            return
        session, rotating = None, False
        if role.name == "thread":
            t = self.db.one("SELECT * FROM threads WHERE id=?", (key,))
            if t and t["session_id"] and t["session_passes"]:
                session = (t["session_id"], True)
                rotating = bool(t["rotate_pending"])
            else:
                session = (str(uuid.uuid4()), False)
                self.db.update("threads", "id=?", (key,), session_id=session[0], session_cost=0)
        elif role.name == "researcher":
            s = research.session(self.db, p)
            if s["id"] and s["passes"]:
                session, rotating = (s["id"], True), bool(s["rotate"])
            else:
                session = (str(uuid.uuid4()), False)
                research.set_session(self.db, p, id=session[0], passes=0, cost=0.0, ctx=None, rotate=False)
        # the handover pass is written by the main model, never by a routine-check model
        resumed_tokens = (t["context_tokens"] or 0) if role.name == "thread" and session and session[1] else 0
        model = role.model if rotating else self.model_for(role, evs, resumed_tokens)
        self.db.update("agent_runs", "id=?", (run_id,), model=model)
        cmd = self.command(p, role, run_id, workdir, sys_file, settings_file, session, model)
        self.db.emit(p.name, "agent.started", f"{role.name}{'/' + key if key else ''} run {run_id}", key=key or None)
        rc, stdout, stderr, timed_out = await self._exec(cmd, prompt, workdir, self.env(p, role, run_id, key),
                                                        role.timeout_s, run_id)
        out_file.write_text(stdout + ("\n--- stderr ---\n" + stderr if stderr else ""))
        res: dict = {}
        try:
            res = json.loads(stdout.strip().splitlines()[-1]) if stdout.strip() else {}
        except (ValueError, IndexError):
            res = {}
        text = res.get("result") or ""
        is_err = bool(res.get("is_error")) or rc != 0
        status = "ok"
        if timed_out:
            status = "timeout"
        elif is_err and (RATE_RE.search(text) or RATE_RE.search(stderr) or "rate" in str(res.get("subtype", ""))):
            status = "ratelimited"
        elif is_err:
            status = "error"
        self.db.update("agent_runs", "id=?", (run_id,), status=status, ended_at=now(), session_id=res.get("session_id"),
                       result=text[:20000], error=(stderr[-2000:] if status != "ok" else None),
                       cost_usd=res.get("total_cost_usd"), num_turns=res.get("num_turns"), log_path=str(out_file))
        lost = session and session[1] and status == "error" and re.search(
            r"no conversation found|session.*not found|could not resume", (stderr + text), re.I)
        if role.name == "thread":
            if lost:  # the session is gone: start a fresh one next pass (notes and results survive)
                self.db.update("threads", "id=?", (key,), session_id=None, session_passes=0, session_cost=0,
                               rotate_pending=0)
                self.db.emit(p.name, "thread.continue", f"{key}: session lost, restarting fresh", key=key)
            elif status in ("ok", "timeout"):
                self._after_thread_pass(p, key, run_id, session, res, status, rotating)
        if role.name == "researcher":
            if lost:  # its memory is RESEARCH.md: the next wake starts fresh from it
                research.set_session(self.db, p, id=None, passes=0, cost=0.0, ctx=None, rotate=False)
            elif status in ("ok", "timeout"):
                self._after_researcher_pass(p, run_id, session, res, status, rotating)
        self.db.emit(p.name, "agent.finished", f"{role.name}{'/' + key if key else ''} run {run_id}: {status}",
                     key=key or None, severity="info" if status == "ok" else "minor")
        if status == "ratelimited":
            self._backoff(run_id, r)
            return
        self.db.kv_set("_lab", "agent_backoff_streak", 0)
        if self.on_result:
            try:
                await self.on_result(p, role, key, self.db.one("SELECT * FROM agent_runs WHERE id=?", (run_id,)), res)
            except Exception:
                log.exception("on_result failed")

    def _after_thread_pass(self, p: Project, tid: str, run_id: int, session, res: dict, status: str,
                           rotating: bool) -> None:
        """Bookkeeping after a thread pass: its own cost, its context size, and session rotation."""
        t = self.db.one("SELECT * FROM threads WHERE id=?", (tid,))
        total = res.get("total_cost_usd")          # claude reports the whole session's cost so far
        if total is not None:
            prev = (t["session_cost"] or 0) if session and session[1] else 0
            self.db.update("agent_runs", "id=?", (run_id,), cost_usd=round(max(0.0, total - prev), 4))
        ctx = context_tokens(res)
        cols = dict(passes=(t["passes"] or 0) + 1, session_passes=(t["session_passes"] or 0) + 1,
                    last_pass_at=now(), session_id=res.get("session_id") or t["session_id"],
                    session_cost=total if total is not None else t["session_cost"],
                    context_tokens=ctx if ctx is not None else t["context_tokens"])
        gen = t["generation"] or 1
        if rotating and status == "ok":           # the handover is written: the next pass starts fresh
            cols.update(session_id=None, session_passes=0, session_cost=0, rotate_pending=0, generation=gen + 1)
            self.db.update("threads", "id=?", (tid,), **cols)
            self.db.emit(p.name, "thread.rotated", f"{tid}: session {gen} closed at {(ctx or 0) // 1000}k tokens; "
                         f"session {gen + 1} starts fresh from HANDOVER.md", key=tid)
            return
        if p.rotate_context_tokens and ctx and ctx >= p.rotate_context_tokens and not t["rotate_pending"]:
            cols["rotate_pending"] = 1
            self.db.emit(p.name, "thread.log", f"{tid}: context {ctx // 1000}k tokens ≥ "
                         f"{p.rotate_context_tokens // 1000}k; next pass writes a handover", key=tid)
        self.db.update("threads", "id=?", (tid,), **cols)

    def _after_researcher_pass(self, p: Project, run_id: int, session, res: dict, status: str,
                               rotating: bool) -> None:
        """The Researcher's session bookkeeping, as for a thread: its cost, context size and rotation."""
        s = research.session(self.db, p)
        total = res.get("total_cost_usd")          # the whole session's cost so far (live chats included)
        if total is not None:
            prev = (s["cost"] or 0) if session and session[1] else 0
            self.db.update("agent_runs", "id=?", (run_id,), cost_usd=round(max(0.0, total - prev), 4))
        ctx = context_tokens(res)
        cols = dict(id=res.get("session_id") or s["id"], passes=(s["passes"] or 0) + 1,
                    cost=total if total is not None else s["cost"], ctx=ctx if ctx is not None else s["ctx"])
        if rotating and status == "ok":           # RESEARCH.md is up to date: the next wake starts fresh
            research.set_session(self.db, p, id=None, passes=0, cost=0.0, ctx=None, rotate=False, gen=s["gen"] + 1)
            self.db.emit(p.name, "research.rotated", f"Researcher session {s['gen']} closed at {(ctx or 0) // 1000}k "
                         f"tokens; session {s['gen'] + 1} starts fresh from RESEARCH.md")
            return
        if p.rotate_context_tokens and ctx and ctx >= p.rotate_context_tokens and not s["rotate"]:
            cols["rotate"] = True
        research.set_session(self.db, p, **cols)

    def _backoff(self, run_id: int, r) -> None:
        streak = int(self.db.kv_get("_lab", "agent_backoff_streak", 0) or 0) + 1
        delay = min(300 * 2 ** (streak - 1), 3600)
        self.db.kv_set("_lab", "agent_backoff_streak", streak)
        self.db.kv_set("_lab", "agent_backoff_until", now() + delay)
        self.db.insert("agent_runs", project=r["project"], role=r["role"], key=r["key"], status="queued",
                       queued_at=now(), event_ids=r["event_ids"])
        self.db.emit(r["project"], "agent.ratelimited", f"usage/rate limit hit; all agents paused {delay // 60:.0f} min",
                     severity="normal")

    async def _exec(self, cmd, prompt, cwd, env, timeout, run_id):
        proc = await asyncio.create_subprocess_exec(*cmd, cwd=str(cwd), env=env, stdin=asyncio.subprocess.PIPE,
                                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                                                    start_new_session=True)
        self.db.update("agent_runs", "id=?", (run_id,), pid=proc.pid)
        try:
            out, err = await asyncio.wait_for(proc.communicate(prompt.encode()), timeout)
            return proc.returncode, out.decode(errors="replace"), err.decode(errors="replace"), False
        except (asyncio.TimeoutError, asyncio.CancelledError) as e:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
                await asyncio.sleep(5)
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            if isinstance(e, asyncio.CancelledError):
                raise
            return 124, "", f"timed out after {timeout}s", True
