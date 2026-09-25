"""Research tasks: the Researcher's ideas, handed to implementor threads by plain code (no LLM).

An idea (a `backlog` row) moves: suggested (a person's, via the Concierge) or proposed (the Researcher's
draft) → ready (queued for implementation, its `spec` is the task) → assigned (a thread works on it) →
done (the thread reported back) | rejected. A thread is long-lived: it implements one task at a time,
reports, goes idle, and gets the next ready task — its session, branch and pod know-how stay warm.
"""
from __future__ import annotations

import json
from pathlib import Path

from .context import live_world_version
from .db import DB, now

OPEN = ("suggested", "proposed", "ready", "assigned")


def release_holder(db: DB, holder: str, *, stop: bool, reason: str, lease_id: int | None = None) -> None:
    q = "SELECT * FROM leases WHERE COALESCE(holder, experiment_id)=? AND status IN ('requested','provisioning','granted')"
    args: list = [holder]
    if lease_id:
        q += " AND id=?"
        args.append(lease_id)
    for l in db.all(q, args):
        if l["status"] == "requested":
            db.update("leases", "id=?", (l["id"],), status="denied", reason=f"cancelled: {reason}")
        else:
            db.update("leases", "id=?", (l["id"],), status="release_requested", reason=reason,
                      job_status=json.dumps({"stop_now": stop}))


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
