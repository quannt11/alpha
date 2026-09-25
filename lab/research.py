"""Research tasks: the Researcher's ideas, handed to implementor threads by plain code (no LLM).

An idea (a `backlog` row) moves: suggested (a person's, via the Concierge) or proposed (the Researcher's
draft) → ready (queued for implementation, its `spec` is the task) → assigned (a thread works on it) →
done (the thread reported back) | rejected. A thread is long-lived: it implements one task at a time,
reports, goes idle, and gets the next ready task — its session, branch and pod know-how stay warm.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from .context import live_world_version
from .db import DB, now

OPEN = ("suggested", "proposed", "ready", "assigned")


def release_holder(db: DB, holder: str, *, stop: bool, reason: str, lease_id: int | None = None) -> int:
    """Release the holder's leases; with `stop`, also stop its pods that are already unleased (a plain release
    followed by `--stop`, or a stop that failed). Returns how many leases and pods it acted on."""
    q = ("SELECT * FROM leases WHERE COALESCE(holder, experiment_id)=? AND status IN "
         "('requested','provisioning','granted','release_requested')")
    args: list = [holder]
    if lease_id:
        q += " AND id=?"
        args.append(lease_id)
    n = 0
    for l in db.all(q, args):
        if l["status"] == "requested":
            db.update("leases", "id=?", (l["id"],), status="denied", reason=f"cancelled: {reason}")
        elif l["status"] == "release_requested":
            if stop:
                db.update("leases", "id=?", (l["id"],), job_status=json.dumps({"stop_now": True}))
        else:
            db.update("leases", "id=?", (l["id"],), status="release_requested", reason=reason,
                      job_status=json.dumps({"stop_now": stop}))
        n += 1
    if stop:
        pq = "SELECT id FROM pods WHERE terminated=0 AND lease_id IS NULL AND state!='EXITED' AND "
        pq += "id=(SELECT pod_id FROM leases WHERE id=? AND COALESCE(holder, experiment_id)=?)" if lease_id else \
              "last_experiment=?"
        for r in db.all(pq, [lease_id, holder] if lease_id else [holder]):
            db.update("pods", "id=?", (r["id"],), stop_requested=1)   # labd stops it (Fleet.process_stops)
            n += 1
    return n


def start_thread(db: DB, p, title: str, by: str, metric: str | None = None) -> str:
    tid = db.next_thread_id(p.name)
    wd = p.work_dir / "threads" / tid
    wd.mkdir(parents=True, exist_ok=True)
    (wd / "NOTES.md").write_text(f"# {tid}\n\nMy notes across tasks and passes (newest last).\n")
    (wd / "results.tsv").write_text("ts\trun\tmetric\tvalue\tkept\tcost_usd\tdescription\n")
    db.insert("threads", id=tid, project=p.name, title=title, status="active", created_at=now(), created_by=by,
              passes=0, metric=metric, workdir=str(wd), world_version=live_world_version(p), idle_since=now())
    db.emit(p.name, "thread.start", f"{tid} started by {by}: {title}", severity="normal", key=tid,
            payload={"title": title, "metric": metric})
    return tid


def assign(db: DB, p, idea, tid: str) -> None:
    """Give a ready idea to a thread: TASK.md is its brief; the thread.task event wakes it."""
    wd = p.work_dir / "threads" / tid
    wd.mkdir(parents=True, exist_ok=True)
    (wd / "TASK.md").write_text(f"# Task: idea {idea['id']} — {idea['title']}\n\n{idea['spec'] or idea['hypothesis'] or ''}\n")
    db.update("backlog", "id=?", (idea["id"],), status="assigned", thread_id=tid, assigned_at=now())
    db.update("threads", "id=?", (tid,), task_id=idea["id"], idle_since=None, title=idea["title"][:200],
              metric=idea["metric"] or None, best_value=None, best_desc=None, world_version=live_world_version(p))
    db.kv_set(p.name, f"idle_streak:{tid}", 0)
    db.emit(p.name, "thread.task", f"{tid} takes idea {idea['id']}: {idea['title']}", severity="normal", key=tid,
            payload={"idea": idea["id"], "path": str(wd / "TASK.md")})


def dispatch(db: DB, p) -> list[str]:
    """Pair ready ideas with threads: the one the Researcher named, else an idle thread, else a new one
    while there is room (research.max_threads). What cannot be placed waits, and nothing is handed out while
    GPUs are paused."""
    done: list[str] = []
    if db.kv_get(p.name, "gpu_paused"):   # an operator paused GPU work: no new tasks until `lab gpu resume`
        return done
    for idea in db.all("SELECT * FROM backlog WHERE project=? AND status='ready' ORDER BY priority, id", (p.name,)):
        active = db.all("SELECT * FROM threads WHERE project=? AND status='active' ORDER BY id", (p.name,))
        idle = [t for t in active if not t["task_id"]]
        want = idea["for_thread"]
        if want and any(t["id"] == want for t in active):
            tid = want if any(t["id"] == want for t in idle) else None
        else:
            # a thread another ready idea is waiting for is not free
            named = {r["for_thread"] for r in db.all("SELECT for_thread FROM backlog WHERE project=? AND "
                                                     "status='ready' AND id!=?", (p.name, idea["id"]))}
            free = [t for t in idle if t["id"] not in named]
            tid = free[0]["id"] if free else None
            if not tid and len(active) < p.max_threads:
                tid = start_thread(db, p, idea["title"][:200], "labd", idea["metric"])
        if tid:
            assign(db, p, db.one("SELECT * FROM backlog WHERE id=?", (idea["id"],)), tid)
            done.append(f"idea {idea['id']} → {tid}")
    return done


def finish_task(db: DB, p, tid: str, summary: str) -> int | None:
    """The thread reported its task done: the idea closes and the thread waits for the next one."""
    t = db.one("SELECT * FROM threads WHERE id=?", (tid,))
    if not t or not t["task_id"]:
        return None
    db.update("backlog", "id=?", (t["task_id"],), status="done", done_at=now(), result=summary[:4000])
    db.update("threads", "id=?", (tid,), task_id=None, idle_since=now())
    db.kv_set(p.name, f"wake:{tid}", 0)
    return t["task_id"]


def retire(db: DB, p, tid: str, reason: str, by: str) -> None:
    t = db.one("SELECT * FROM threads WHERE id=?", (tid,))
    release_holder(db, tid, stop=True, reason=f"thread retired by {by}")
    if t and t["task_id"]:   # an unfinished task goes back to the queue
        db.update("backlog", "id=? AND status='assigned'", (t["task_id"],), status="ready", thread_id=None, for_thread=None)
    db.update("threads", "id=?", (tid,), status="retired", retired_at=now(), retire_reason=reason, task_id=None)
    db.emit(p.name, "thread.retired", f"{tid} retired by {by}: {reason}", severity="normal", key=tid)


def retire_idle(db: DB, p, hours: float) -> list[str]:
    """Threads without a task (and without a lease or a ready idea naming them) for `hours` are retired."""
    if not hours:
        return []
    out = []
    for t in db.all("SELECT * FROM threads WHERE project=? AND status='active' AND task_id IS NULL AND "
                    "idle_since IS NOT NULL AND idle_since<?", (p.name, now() - hours * 3600)):
        if db.one("SELECT 1 FROM leases WHERE COALESCE(holder, experiment_id)=? AND status IN "
                  "('requested','provisioning','granted','release_requested')", (t["id"],)) or \
                db.one("SELECT 1 FROM backlog WHERE for_thread=? AND status='ready'", (t["id"],)):
            continue
        retire(db, p, t["id"], f"no task for {hours:g}h", "labd")
        out.append(t["id"])
    return out


def research_idle(db: DB, p) -> bool:
    """Nothing for implementors to do: no thread on a task and no ready idea."""
    return not db.one("SELECT 1 FROM threads WHERE project=? AND status='active' AND task_id IS NOT NULL", (p.name,)) \
        and not db.one("SELECT 1 FROM backlog WHERE project=? AND status='ready'", (p.name,))


def text_or_file(v: str | None) -> str:
    try:
        if v and len(v) < 4096 and Path(v).expanduser().is_file():
            return Path(v).expanduser().read_text()
    except OSError:   # a long one-line text is not a path ("File name too long")
        pass
    return v or ""


# ---------------------------------------------------------------- the Researcher's one session and live chats
# The Researcher keeps one Claude session across wakes (rotated like a thread's), so an operator can talk to
# the real mind: `lab researcher chat` opens that session in interactive Claude Code, and `lab researcher say`
# (the dashboard's chat box) wakes it with a message. While a terminal chat holds the lock labd starts no
# Researcher pass; the wakes wait and then resume the same session, which now remembers the chat.

SESSION_KEY, CHAT_KEY = "researcher_session", "researcher_chat"


def session(db: DB, p) -> dict:
    """{id, passes, cost, ctx, rotate, gen}; id None = the next wake starts a fresh session."""
    return {"id": None, "passes": 0, "cost": 0.0, "ctx": None, "rotate": False, "gen": 1,
            **(db.kv_get(p.name, SESSION_KEY, {}) or {})}


def set_session(db: DB, p, **kw) -> dict:
    s = {**session(db, p), **kw}
    db.kv_set(p.name, SESSION_KEY, s)
    return s


def _alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


def chat_holder(db: DB, p) -> dict | None:
    """The live terminal chat holding the Researcher ({pid, by, since, run}), or None. A dead holder's lock
    counts as released, so a killed `lab researcher chat` never blocks the Researcher."""
    c = db.kv_get(p.name, CHAT_KEY)
    return c if c and _alive(c.get("pid")) else None


def session_path(workdir: Path, sid: str | None) -> Path | None:
    """Claude Code's transcript of a session: ~/.claude/projects/<cwd, non-alphanumerics as '-'>/<id>.jsonl."""
    if not sid:
        return None
    base = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "projects"
    f = base / "".join(ch if ch.isalnum() else "-" for ch in str(workdir)) / f"{sid}.jsonl"
    return f if f.exists() else next(base.glob(f"*/{sid}.jsonl"), None)


def _entries(path: Path | None):
    if not path:
        return
    with open(path, errors="replace") as fh:
        for line in fh:
            try:
                yield json.loads(line)
            except ValueError:
                continue


def session_totals(path: Path | None) -> tuple[float | None, int | None]:
    """(the session's cost so far, the context size of its last model call), from its transcript."""
    cost = ctx = None
    for d in _entries(path):
        if d.get("type") == "cost-state":
            cost = d.get("totalCostUSD", cost)
        elif d.get("type") == "assistant" and not d.get("isSidechain"):
            u = (d.get("message") or {}).get("usage") or {}
            if u:
                ctx = sum(int(u.get(k) or 0) for k in ("input_tokens", "cache_read_input_tokens",
                                                        "cache_creation_input_tokens"))
    return cost, ctx


def transcript(path: Path | None, limit: int = 80, clip: int = 6000) -> list[dict]:
    """The conversation as the dashboard shows it: people's and labd's messages, the Researcher's text and
    its tool calls (tool output and thinking left out), oldest first."""
    out: list[dict] = []
    for d in _entries(path):
        kind, m = d.get("type"), d.get("message") or {}
        if kind not in ("user", "assistant") or d.get("isSidechain") or d.get("isMeta"):
            continue
        c = m.get("content")
        blocks = [{"type": "text", "text": c}] if isinstance(c, str) else (c or [])
        for b in blocks:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text" and (b.get("text") or "").strip():
                t = b["text"]
                if kind == "user" and t.lstrip().startswith("<"):   # local command echoes, reminders
                    continue
                out.append({"ts": d.get("timestamp"), "who": "researcher" if kind == "assistant" else "user",
                            "text": t if len(t) <= clip else t[:clip] + f"\n… ({len(t) - clip} more chars)"})
            elif b.get("type") == "tool_use":
                inp = b.get("input") or {}
                arg = inp.get("command") or inp.get("file_path") or inp.get("pattern") or inp.get("url") \
                    or inp.get("query") or inp.get("description") or ""
                out.append({"ts": d.get("timestamp"), "who": "tool", "text": f"{b.get('name')}: {str(arg)[:300]}"})
    return out[-limit:]
