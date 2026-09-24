"""`lab` — the one CLI humans and agents use to act on the lab.

It never holds credentials. Anything that needs one (post to Discord, rent a
GPU) is written as a request row; labd fulfils it.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
import re
import tomllib
from pathlib import Path

from . import config as config_mod
from .budget import Budget
from .context import (ago, backlog_table, events_digest, iso, leases_table, live_world_version, read,
                      results_table, status_text, threads_table, tickets_table, world_now)
from .db import DB, now
from .fleet import ssh_base
from . import research
from .maint import Maint
from .research import text_or_file
from .sentinel import flatten

ROLE = os.environ.get("LAB_ROLE", "human")
VALID_POOLS_HINT = "agenda | explore | request"


def die(msg: str, code: int = 1):
    print(f"lab: {msg}", file=sys.stderr)
    sys.exit(code)


class Ctx:
    def __init__(self, args):
        self.cfg = config_mod.load(getattr(args, "config", None))
        self.db = DB(self.cfg.db_path)
        name = getattr(args, "project", None) or os.environ.get("LAB_PROJECT")
        if not name:
            if len(self.cfg.projects) != 1:
                die(f"which project? pass --project ({', '.join(self.cfg.projects)})")
            name = next(iter(self.cfg.projects))
        self.p = self.cfg.project(name)


# ---------------------------------------------------------------- status / world / events


def cmd_status(c: Ctx, a):
    hb = float(c.db.kv_get("_lab", "heartbeat", 0) or 0)
    print(f"labd heartbeat: {ago(hb)}" + ("  (DAEMON DOWN?)" if now() - hb > 120 else ""))
    print(status_text(c.db, c.cfg, c.p))


def cmd_world(c: Ctx, a):
    if a.json:
        print((c.p.world_dir / "world.json").read_text() if (c.p.world_dir / "world.json").exists() else "{}")
        return
    print(world_now(c.db, c.p))
    print()
    print(read(c.p.world_dir / "STATE.md", 20000))


def cmd_world_brief(c: Ctx, a):
    src = Path(a.file)
    if not src.exists():
        die(f"no such file {src}")
    briefs = c.p.world_dir / "briefs"
    briefs.mkdir(parents=True, exist_ok=True)
    dst = briefs / (time.strftime("%Y%m%d-%H%M%S") + ".md")
    shutil.copyfile(src, dst)
    text = src.read_text()
    eid = c.db.emit(c.p.name, "world.brief", a.title or text.strip().splitlines()[0][:200], severity=a.severity,
                    payload={"path": str(dst)})
    if a.post:
        c.db.insert("outbox", project=c.p.name, channel_id=c.p.channel_id, content=text, status="pending",
                    created_at=now(), author_role=ROLE)
    print(f"brief saved to {dst} (event #{eid}){' and queued for Discord' if a.post else ''}")


def cmd_events(c: Ctx, a):
    if a.id:
        r = c.db.one("SELECT * FROM events WHERE id=?", (a.id,))
        if not r:
            die("no such event")
        print(json.dumps({**dict(r), "payload": json.loads(r["payload"]) if r["payload"] else None}, indent=1,
                         default=str))
        return
    since = now() - a.hours * 3600
    rows = c.db.all("SELECT * FROM events WHERE project=? AND ts>=? ORDER BY id DESC LIMIT ?",
                    (c.p.name, since, 5000))
    order = {"info": 0, "minor": 1, "normal": 2, "major": 3}
    rows = [r for r in rows if order.get(r["severity"], 0) >= order[a.min_severity]
            and (not a.topic or r["topic"].startswith(a.topic))][: a.n]
    for r in reversed(rows):
        print(f"#{r['id']} {iso(r['ts'])} [{r['severity']}] {r['topic']}{' ' + r['key'] if r['key'] else ''}: "
              f"{r['summary']}")


def cmd_emit(c: Ctx, a):
    payload = json.loads(a.payload) if a.payload else None
    eid = c.db.emit(c.p.name, a.topic, a.summary, severity=a.severity, key=a.key, payload=payload)
    print(f"event #{eid}")


def cmd_budget(c: Ctx, a):
    print(json.dumps(Budget(c.db, c.p, c.cfg.timezone).summary(), indent=1))


def cmd_runs(c: Ctx, a):
    if a.id:
        r = c.db.one("SELECT * FROM agent_runs WHERE id=?", (a.id,))
        if not r:
            die("no such run")
        d = dict(r)
        print(json.dumps(d, indent=1, default=str))
        return
    for r in c.db.all("SELECT * FROM agent_runs WHERE project=? ORDER BY id DESC LIMIT ?", (c.p.name, a.n)):
        dur = f"{(r['ended_at'] or now()) - (r['started_at'] or now()):.0f}s" if r["started_at"] else "-"
        cost = f" ${r['cost_usd']:.2f}" if r["cost_usd"] else ""
        print(f"run {r['id']} {r['role']}{'/' + r['key'] if r['key'] else ''} [{r['status']}] "
              f"queued {ago(r['queued_at'])} dur {dur}{cost}  {(r['result'] or r['error'] or '')[:100]!r}")


# ---------------------------------------------------------------- discord


def cmd_say(c: Ctx, a):
    text = a.text if a.text != "-" else sys.stdin.read()
    if a.file_text:
        text = Path(a.file_text).read_text()
    files = [str(Path(f).resolve()) for f in (a.F or [])]
    for f in files:
        if not Path(f).exists():
            die(f"attachment not found: {f}")
    oid = c.db.insert("outbox", project=c.p.name, channel_id=a.channel_id or c.p.channel_id, content=text,
                      reply_to=a.reply_to, files=json.dumps(files) if files else None, status="pending",
                      created_at=now(), author_role=ROLE)
    if a.wait:
        for _ in range(60):
            r = c.db.one("SELECT status, error FROM outbox WHERE id=?", (oid,))
            if r["status"] != "pending":
                print(f"outbox {oid}: {r['status']} {r['error'] or ''}")
                return
            time.sleep(1)
        print(f"outbox {oid}: still pending (is labd running?)")
    else:
        print(f"queued outbox {oid}")


def cmd_inject(c: Ctx, a):
    """Simulate a Discord request (for testing the Concierge without a human)."""
    key = f"sim-{int(now())}"
    payload = {"id": key, "channel_id": c.p.channel_id, "author_id": "0", "author_name": a.author,
               "content": a.text, "reply_to": None, "simulated": True}
    c.db.insert("discord_messages", id=key, project=c.p.name, channel_id=c.p.channel_id, author_id="0",
                author_name=a.author, is_bot=0, content=a.text, reply_to=None, ts=now())
    eid = c.db.emit(c.p.name, "discord.request", f"{a.author}: {a.text[:300]}", severity="normal", key=key,
                    payload=payload)
    print(f"injected request {key} (event #{eid})")


# ---------------------------------------------------------------- tickets / ideas


def cmd_ticket(c: Ctx, a):
    if a.action == "new":
        tid = c.db.insert("tickets", project=c.p.name, created_at=now(), updated_at=now(), author=a.author or ROLE,
                          source_message_id=a.message, title=a.title, body=a.body or "", status="open")
        c.db.emit(c.p.name, "ticket.new", f"ticket {tid} from {a.author or ROLE}: {a.title}", severity="normal",
                  key=str(tid), payload={"ticket": tid, "message_id": a.message, "body": a.body})
        print(f"ticket {tid} opened")
    elif a.action == "list":
        print(tickets_table(c.db, c.p.name, "open" if not a.all else "closed"))
    elif a.action in ("close", "note"):
        t = c.db.one("SELECT * FROM tickets WHERE id=? AND project=?", (a.id, c.p.name))
        if not t:
            die("no such ticket")
        if a.action == "close":
            c.db.update("tickets", "id=?", (a.id,), status="closed", resolution=a.text, updated_at=now())
            c.db.emit(c.p.name, "ticket.closed", f"ticket {a.id} closed: {a.text[:200]}", key=str(a.id))
        else:
            c.db.update("tickets", "id=?", (a.id,), body=(t["body"] or "") + f"\n[{iso(now())} {ROLE}] {a.text}",
                        updated_at=now())
        print("ok")


def cmd_backlog(c: Ctx, a):
    """Ideas are the Researcher's tasks for implementor threads (lifecycle in lab/research.py)."""
    db, p = c.db, c.p
    if a.action in ("add", "suggest"):
        if not a.title:
            die(f"idea {a.action} needs --title")
        suggest = a.action == "suggest"
        status = "suggested" if suggest else ("ready" if a.ready else "proposed")
        bid = db.insert("backlog", project=p.name, created_at=now(), author=a.author or ROLE, title=a.title,
                        hypothesis=a.hypothesis, expected_gain=a.gain, est_cost_usd=a.cost, priority=a.priority,
                        status=status, world_version=live_world_version(p), spec=text_or_file(a.spec or a.body) or None,
                        metric=a.metric, for_thread=a.thread, source_message=a.message)
        if suggest:   # a person's idea: the Researcher weighs it
            db.emit(p.name, "research.suggestion", f"idea {bid} suggested by {a.author or ROLE}: {a.title}",
                    severity="normal", key=str(bid), payload={"idea": bid, "message_id": a.message})
        else:
            db.emit(p.name, "idea.new", f"idea {bid} [{status}] by {ROLE}: {a.title}", key=str(bid))
        print(f"idea {bid} {status}" + (" — labd hands it to a thread" if status == "ready" else ""))
    elif a.action == "list":
        print(backlog_table(db, p.name, statuses=("suggested", "proposed", "ready", "assigned", "done", "rejected")
                            if a.all else ("suggested", "proposed", "ready", "assigned")))
    elif a.action == "show":
        r = db.one("SELECT * FROM backlog WHERE id=? AND project=?", (a.id, p.name)) or die("no such idea")
        for k in ("id", "status", "title", "author", "priority", "metric", "expected_gain", "hypothesis", "thread_id",
                  "for_thread", "world_version", "notes", "spec", "result"):
            if r[k] is not None:
                print(f"{k}: {r[k]}")
    else:
        r = db.one("SELECT * FROM backlog WHERE id=? AND project=?", (a.id, p.name)) or die("no such idea")
        status = {"accept": "ready", "ready": "ready", "reject": "rejected", "done": "done", "edit": r["status"]}[a.action]
        if a.action in ("ready", "accept", "edit") and r["status"] not in ("suggested", "proposed", "ready"):
            die(f"idea {a.id} is {r['status']}; only suggested/proposed/ready ideas can be changed or queued")
        cols = dict(status=status)
        if a.action == "edit":
            cols.update({k: v for k, v in (("title", a.title), ("expected_gain", a.gain), ("est_cost_usd", a.cost),
                                           ("hypothesis", a.hypothesis)) if v is not None})
            if a.priority != 50:
                cols["priority"] = a.priority
        if a.note:
            cols["notes"] = ((r["notes"] or "") + f"\n[{iso(now())} {ROLE}] {a.note}").strip()
        if a.spec or a.body:
            cols["spec"] = text_or_file(a.spec or a.body)
        if a.thread:
            cols["for_thread"] = a.thread
        if a.metric:
            cols["metric"] = a.metric
        db.update("backlog", "id=?", (a.id,), **cols)
        print(f"idea {a.id} {status}" + (" — labd hands it to a thread" if status == "ready" else ""))


# ---------------------------------------------------------------- research threads


def _holder(c: Ctx, a) -> str:
    h = getattr(a, "holder", None) or os.environ.get("LAB_THREAD")
    if not h:
        die("which thread? (agents have $LAB_THREAD; humans pass --holder t-001)")
    if not c.db.one("SELECT 1 FROM threads WHERE id=? AND project=?", (h, c.p.name)):
        die(f"unknown thread {h}")
    return h


_text_or_file = text_or_file


def cmd_thread(c: Ctx, a):
    db, p = c.db, c.p
    if a.action == "start":   # humans: a thread with a first task (labd starts threads for ready ideas itself)
        n = db.one("SELECT COUNT(*) n FROM threads WHERE project=? AND status='active'", (p.name,))["n"]
        if n >= p.max_threads:
            die(f"{n} thread(s) already active; the limit is {p.max_threads}. Retire one first "
                f"(`lab thread retire t-00N --text why`) or queue the task (`lab idea add --ready`).")
        if not a.title or not a.text:
            die("thread start needs --title and --text (the task: a file path or text)")
        bid = db.insert("backlog", project=p.name, created_at=now(), author=a.author or ROLE, title=a.title,
                        status="ready", world_version=live_world_version(p), spec=_text_or_file(a.text), metric=a.metric)
        tid = research.start_thread(db, p, a.title, ROLE, a.metric)
        research.assign(db, p, db.one("SELECT * FROM backlog WHERE id=?", (bid,)), tid)
        print(f"{tid} started on idea {bid} (workdir {p.work_dir / 'threads' / tid}); its first pass begins now")
    elif a.action == "list":
        print(threads_table(db, p.name, include_retired=a.all))
    elif a.action == "show":
        t = db.one("SELECT * FROM threads WHERE id=? AND project=?", (a.id, p.name))
        if not t:
            die(f"no thread {a.id}")
        print(json.dumps(dict(t), indent=1, default=str))
        print("\nleases:\n" + leases_table(db, p.name, holder=a.id))
        print("\nresults:\n" + results_table(db, p.name, thread=a.id, limit=30))
    elif a.action == "note":
        targets = [r["id"] for r in db.all("SELECT id FROM threads WHERE project=? AND status='active'", (p.name,))] \
            if a.id == "all" else [a.id]
        if not a.text:
            die("thread note needs --text")
        me = os.environ.get("LAB_THREAD") if os.environ.get("LAB_ROLE") == "thread" else None
        for tid in targets:
            if not db.one("SELECT 1 FROM threads WHERE id=? AND status='active'", (tid,)):
                die(f"no active thread {tid}")
            if tid == me:  # a note to yourself must not wake you again
                db.emit(p.name, "thread.log", f"{tid} note to self: {a.text[:300]}", key=tid,
                        payload={"from": ROLE, "text": _text_or_file(a.text)})
                print(f"{tid}: logged (a note to yourself does not wake you; keep your notes in NOTES.md)")
                continue
            db.emit(p.name, "thread.message", f"message for {tid} from {a.author or ROLE}: {a.text[:300]}",
                    severity="normal", key=tid, payload={"from": a.author or ROLE, "text": _text_or_file(a.text)})
        if targets != [me]:
            print(f"sent to {', '.join(t for t in targets if t != me) or '(no active threads)'}")
    elif a.action == "retire":
        t = db.one("SELECT * FROM threads WHERE id=? AND project=?", (a.id, p.name))
        if not t or t["status"] != "active":
            die(f"no active thread {a.id}")
        research.retire(db, p, a.id, a.text or "", ROLE)
        print(f"{a.id} retired; its GPUs are released")
    elif a.action in ("report", "ask"):
        h = _holder(c, a)
        text = _text_or_file(a.text)
        if not text:
            die(f"thread {a.action} needs --text (text or a file path)")
        t = db.one("SELECT * FROM threads WHERE id=?", (h,))
        topic = "thread.report" if a.action == "report" else "thread.question"
        db.emit(p.name, topic, f"{h}{' (task done)' if a.done else ''} on idea {t['task_id'] or '-'}: {text[:300]}",
                severity="normal", key=h, payload={"idea": t["task_id"], "text": text, "done": bool(a.done)})
        if a.done:
            idea = research.finish_task(db, p, h, text)
            print(f"reported; idea {idea or '-'} is done and you are free for the next task. Release GPUs you no "
                  f"longer need (`lab gpu release --stop`).")
        else:
            print("sent to the Researcher" + ("; its answer arrives as a message that wakes you" if a.action == "ask" else ""))
    elif a.action == "claim":
        h = _holder(c, a)
        if not a.text:
            die("thread claim needs --text (what you beat, by how much, with evidence paths)")
        db.emit(p.name, "thread.claim", f"{h} claims: {_text_or_file(a.text)[:300]}", severity="major", key=h,
                payload={"text": _text_or_file(a.text)})
        print("claim filed; the Analyst will red-team it")


def cmd_maint(c: Ctx, a):
    """Operators change the lab's code from Discord ("maint: …"); this is the same from the terminal."""
    m = Maint(c.db, c.cfg)
    if a.action not in ("list", "show") and ROLE != "human":
        die("changing the lab's own code is for operators; agents can only `lab maint list|show`.")
    if a.action == "list":
        print(m.status_text(limit=20))
    elif a.action == "show":
        r = m.get(a.id or "") or die(f"no maintainer request {a.id}")
        for k in r.keys():
            if r[k] is not None:
                print(f"{k}: {r[k]}")
    elif a.action == "request":
        if not a.id:
            die('maint request "what to change"')
        mid = m.request(c.p.name, author_id="terminal", author=a.text or "operator (terminal)", text=a.id)
        print(f"queued {mid}; the maintainer's report goes to Discord (`lab maint show {mid}`)")
    elif a.action == "mark":
        if not (a.id and a.status):
            die("maint mark m-N deployed|rolled_back|failed --text ...")
        m.mark(a.id, a.status, a.text)
        print(f"{a.id}: {a.status}")
    else:
        if not a.id:
            die(f"maint {a.action} m-N")
        print((m.approve if a.action == "approve" else m.reject)(a.id, "operator (terminal)"))


def cmd_result(c: Ctx, a):
    db, p = c.db, c.p
    h = _holder(c, a)
    kept = a.kept.lower() in ("y", "yes", "true", "1", "kept", "keep")
    rid = db.insert("results", project=p.name, thread_id=h, ts=now(), run=a.run, description=a.desc,
                    metric=a.metric, value=a.value, kept=int(kept), cost_usd=a.cost, notes=a.notes)
    t = db.one("SELECT * FROM threads WHERE id=?", (h,))
    better = a.best or (t["best_value"] is None and kept)
    if better:
        db.update("threads", "id=?", (h,), best_value=a.value, best_desc=a.desc[:300], metric=t["metric"] or a.metric)
    wd = Path(t["workdir"])
    with open(wd / "results.tsv", "a") as f:
        f.write("\t".join(str(x) for x in (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), a.run or "",
                                            a.metric, a.value, "keep" if kept else "discard", a.cost or "",
                                            a.desc.replace("\t", " ").replace("\n", " "))) + "\n")
    topic = "thread.result" if (kept or better) else "result.discarded"
    db.emit(p.name, topic, f"{h}: {a.metric}={a.value} {'KEPT' if kept else 'discarded'}"
            f"{' NEW BEST' if better else ''} — {a.desc[:200]}", severity="normal" if kept else "info", key=h)
    print(f"result {rid} recorded{' (new best)' if better else ''}")


# ---------------------------------------------------------------- GPUs (leases are held by threads)


_release_holder = research.release_holder


def _release_all(db: DB, exp_id: str, *, stop: bool, reason: str):
    _release_holder(db, exp_id, stop=stop, reason=reason)


def _lease_for(c: Ctx, holder: str, lease_id: int | None = None):
    q = "SELECT * FROM leases WHERE COALESCE(holder, experiment_id)=? AND status='granted'"
    args: list = [holder]
    if lease_id:
        q += " AND id=?"
        args.append(lease_id)
    l = c.db.one(q + " ORDER BY id DESC", args)
    if not l:
        die(f"{holder} holds no granted lease{' ' + str(lease_id) if lease_id else ''} (see `lab gpu list`)")
    return l


def _key(c: Ctx) -> Path:
    return config_mod.expand(c.cfg.runpod.get("ssh_key", "~/.ssh/runpod_pic"))


def _test_policy(p) -> dict:
    return p.fleet.get("test_policy", {}) if p.test_mode else {}


RANK = {"High": 0, "Medium": 1, "Low": 2}


def cmd_gpu_stock(c: Ctx, a):
    """Stock from labd's feed. Asking for a shape it does not watch yet makes labd fetch it now."""
    db, p = c.db, c.p
    ids = list((db.kv_get(p.name, "gpu_prices", {}) or {}).keys())
    pat = (a.pattern or "").lower().replace("nvidia", "").strip()
    match = [g for g in ids if pat and pat in g.lower()] if pat else []
    if pat and not match:
        die(f"no GPU id matches {a.pattern!r}" + (f" (known: {len(ids)} ids; e.g. {', '.join(sorted(ids)[:6])})"
                                              if ids else " — labd has not fetched prices yet"))
    want_count = a.count_pos
    counts = [want_count] if want_count else [1, 2, 4, 8]
    cache = db.kv_get(p.name, "stock", {}) or {}
    have = cache.get("shapes", {})
    # fetch on demand only for an explicit shape; otherwise show what the feed already watches
    want = [(g, n) for g in match for n in counts] if want_count else []
    missing = [(g, n) for g, n in want if not any(k.startswith(f"{g}|{n}|") for k in have)]
    if missing or (want and now() - float(cache.get("at", 0)) > 600):
        before = float(cache.get("at", 0))
        req = db.kv_get(p.name, "stock_requests", []) or []
        db.kv_set(p.name, "stock_requests", req + [list(x) for x in want])
        for _ in range(45):
            cache = db.kv_get(p.name, "stock", {}) or {}
            if float(cache.get("at", 0)) > before:
                break
            time.sleep(1)
        else:
            print("(labd did not refresh within 45s — showing cached data; is labd running?)")
        have = cache.get("shapes", {})
    prices = db.kv_get(p.name, "gpu_prices", {}) or {}
    rows = []
    for k, v in have.items():
        g, n, cl = k.split("|")
        if (not match or g in match) and (not want_count or int(n) == want_count):
            pr = (prices.get(g) or {}).get(cl)
            rows.append((RANK.get(v, 9), g, int(n), cl, v or "none", pr))
    if not rows:
        print("no stock data for that shape yet")
        return
    print(f"stock as of {ago(float(cache.get('at', 0)))} (Runpod COMMUNITY/SECURE, Shadeform SHADEFORM, Vast VAST; "
          "none = cannot be rented right now)")
    for _, g, n, cl, v, pr in sorted(rows, key=lambda r: (r[0], r[1], r[2])):
        price = f"${pr:.2f}/gpu/h" if pr else ""
        print(f"  {v:<7} {n}× {g:<28} {cl:<9} {price}")


def cmd_gpu(c: Ctx, a):
    db, p = c.db, c.p
    if a.action in ("pause", "resume"):
        if os.environ.get("LAB_RUN_ID"):
            die(f"gpu {a.action} is for humans only")
        if a.action == "pause":
            db.kv_set(p.name, "gpu_paused", {"at": now(), "reason": a.pattern or "operator"})
            db.emit(p.name, "operator.pause", f"all GPU work paused: {a.pattern or 'operator'}", severity="major")
            print("GPUs paused: labd stops every lab pod and rents nothing until `lab gpu resume`")
        else:
            db.kv_set(p.name, "gpu_paused", None)
            db.emit(p.name, "operator.resume", f"GPU work resumed{': ' + a.pattern if a.pattern else ''}",
                    severity="major")
            print("GPUs resumed")
        return
    if a.action == "stock":
        return cmd_gpu_stock(c, a)
    if a.action == "list":
        print(leases_table(db, p.name))
        print("\npods owned by the lab:")
        for r in db.all("SELECT * FROM pods WHERE project=? AND terminated=0", (p.name,)):
            print(f"  {r['name']} ({r['id']}) {r['state']} {r['gpu_count']}×{r['gpu_type']} ${r['price_hr'] or 0:.2f}/h "
                  f"lease={r['lease_id'] or '-'} holder={r['last_experiment'] or '-'} seen {ago(r['last_seen'])}")
        return
    h = _holder(c, a)
    if a.action == "lease":
        if not a.gpu:
            die("gpu lease needs --gpu TYPE (see `lab gpu stock`)")
        tp = _test_policy(p)
        alts = a.alt or []
        if tp.get("max_gpus_per_experiment") and a.count > int(tp["max_gpus_per_experiment"]):
            die(f"test mode: at most {tp['max_gpus_per_experiment']} GPU(s) per lease")
        allowed = tp.get("allowed_gpu_types", [])
        if allowed and (a.gpu not in allowed or any(x not in allowed for x in alts)):
            die(f"test mode: GPU types must be among {allowed}")
        lid = db.insert("leases", project=p.name, holder=h, experiment_id=h, status="requested", gpu_type=a.gpu,
                        gpu_count=a.count, max_hours=a.hours, alternatives=json.dumps(alts), pool=a.pool or
                        next(iter(p.pools)), requested_at=now())
        print(f"lease {lid} requested for {h}: {a.count}× {a.gpu} (alternatives {alts or 'none'}) for ≤{a.hours}h")
        deadline = now() + a.wait
        while True:
            l = db.one("SELECT * FROM leases WHERE id=?", (lid,))
            if l["status"] == "granted":
                print(f"GRANTED: lease {lid} on {l['pod_name']} ({l['gpu_type']}) ${l['price_hr']:.2f}/h. "
                      f"Use `lab ssh`, `lab push`, `lab launch --job NAME`.")
                return
            if l["status"] in ("denied", "failed"):
                die(f"lease {lid} {l['status']}: {l['reason']}", 3)
            if now() >= deadline:
                what, mins = (("a Shadeform VM", p.fleet.get("shadeform_provision_timeout_minutes", 60))
                              if l["cloud"] == "SHADEFORM" else
                              ("a Vast instance", p.fleet.get("vast_provision_timeout_minutes", 40))
                              if l["cloud"] == "VAST" else
                              ("a Runpod pod", p.fleet.get("provision_timeout_minutes", 25)))
                boot = (f" — still booting ({what} may take up to {mins} min): don't release or re-request "
                        "it; end the pass with `NEXT: wait`") if l["status"] == "provisioning" else ""
                print(f"lease {lid} is {l['status']}{' (' + l['reason'] + ')' if l['reason'] else ''}; "
                      f"labd will wake you with a gpu.lease event when it changes{boot}")
                return
            time.sleep(10)
    if a.action == "release":
        _release_holder(db, h, stop=a.stop, reason=f"released by {ROLE}", lease_id=a.lease)
        print("release requested" + (" (pod will be stopped now)" if a.stop else " (pod stops after 20 min idle)"))
    if a.action == "extend":
        l = _lease_for(c, h, a.lease)
        from .budget import Budget
        extra = (l["price_hr"] or 0) * a.hours
        ok, why = Budget(db, p, c.cfg.timezone).check(l["pool"] or next(iter(p.pools)), extra)
        if not ok:
            die(f"cannot extend: {why}", 3)
        db.update("leases", "id=?", (l["id"],), max_hours=l["max_hours"] + a.hours,
                  expires_at=(l["expires_at"] or now()) + a.hours * 3600)
        db.emit(p.name, "gpu.extended", f"{h}: lease {l['id']} extended by {a.hours}h (+${extra:.2f})", key=h)
        print(f"lease {l['id']} now allows {l['max_hours'] + a.hours}h in total")


def cmd_ssh(c: Ctx, a):
    l = _lease_for(c, _holder(c, a), a.lease)
    base = ssh_base(_key(c), l["ssh_host"], l["ssh_port"])
    cmd = a.cmd[1:] if a.cmd and a.cmd[0] == "--" else a.cmd
    if cmd:
        base.append(" ".join(cmd) if len(cmd) > 1 else cmd[0])
    os.execvp(base[0], base)


def _rsync(c: Ctx, a, push: bool):
    l = _lease_for(c, _holder(c, a), a.lease)
    e = " ".join(shlex.quote(x) for x in ssh_base(_key(c), l["ssh_host"], l["ssh_port"])[:-1])
    remote = f"root@{l['ssh_host']}:"
    args = ["rsync", "-az", "--partial", "-e", e, *(a.rsync_args or [])]
    args += [a.src, remote + a.dst] if push else [remote + a.src, a.dst]
    sys.exit(subprocess.call(args))


def cmd_push(c: Ctx, a):
    _rsync(c, a, True)


def cmd_pull(c: Ctx, a):
    _rsync(c, a, False)


def cmd_launch(c: Ctx, a):
    h = _holder(c, a)
    l = _lease_for(c, h, a.lease)
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", a.job or ""):
        die("launch needs --job NAME (letters, digits, . _ -), e.g. --job r007-lr3e-4")
    key = _key(c)
    labrun = c.cfg.root / "bin" / "labrun"
    base = ssh_base(key, l["ssh_host"], l["ssh_port"])
    scp = ["scp", "-P", str(l["ssh_port"]), "-i", str(key), "-o", "StrictHostKeyChecking=no", "-o",
           "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR", str(labrun), f"root@{l['ssh_host']}:/tmp/labrun"]
    if subprocess.call(base + ["mkdir -p /workspace/lab/bin"]) or subprocess.call(scp) or \
            subprocess.call(base + ["install -m 755 /tmp/labrun /workspace/lab/bin/labrun"]):
        die("could not install labrun on the pod")
    cmd = a.cmd[1:] if a.cmd and a.cmd[0] == "--" else a.cmd
    if not cmd:
        die("usage: lab launch --job NAME [--cwd DIR] -- <command>")
    c.db.update("leases", "id=?", (l["id"],), job_name=a.job, job_state=None, heartbeat_at=None)
    remote = (f"/workspace/lab/bin/labrun {shlex.quote(a.job)} --cwd {shlex.quote(a.cwd)} -- "
              f"{shlex.quote(' '.join(cmd))}")
    rc = subprocess.call(base + [remote])
    if rc == 0:
        c.db.emit(c.p.name, "job.launched", f"{h}: launched {a.job} on {l['pod_name']}", key=h,
                  payload={"job": a.job, "cmd": " ".join(cmd)})
    sys.exit(rc)


# ---------------------------------------------------------------- misc


def cmd_doctor(c: Ctx, a):
    s = config_mod.load_secrets(c.cfg)
    ok = lambda b: "ok " if b else "MISSING"
    print(f"config           {c.cfg.root / 'lab.toml'}")
    print(f"db               {c.cfg.db_path}")
    print(f"projects         {', '.join(c.cfg.projects)}")
    print(f"discord token    {ok('DISCORD_BOT_TOKEN' in s)}")
    print(f"runpod key       {ok('RUNPOD_API_KEY' in s)}  (GPU leases are denied without a Runpod, Shadeform or Vast key)")
    print(f"shadeform key    {ok('SHADEFORM_API_KEY' in s)}  (optional: SHADEFORM in cloud_order; cheapest offer wins)")
    print(f"vast key         {ok('VAST_API_KEY' in s)}  (optional: VAST in cloud_order; cheapest offer wins)")
    print(f"claude CLI       {ok(shutil.which(c.cfg.claude_bin) is not None)} {shutil.which(c.cfg.claude_bin) or ''}")
    print(f"ssh key          {ok(_key(c).exists())} {_key(c)}")
    for p in c.cfg.projects.values():
        print(f"[{p.name}] channel {p.channel_id} guild {p.guild_id}; root {p.root} {ok(p.root.exists())}; "
              f"budget ${p.daily_usd:.0f}/day pools {p.pools}; test_mode={'ON' if p.test_mode else 'off'}")
    hb = float(c.db.kv_get("_lab", "heartbeat", 0) or 0)
    print(f"labd heartbeat   {ago(hb)}")


def cmd_ralph(c: Ctx, a):
    script = c.cfg.root / "vendor" / "ralph.sh"
    env = {**os.environ, "RALPH_AGENT": os.environ.get("RALPH_AGENT", "claude")}
    os.execvpe("bash", ["bash", str(script), *a.args], env)


def cmd_web(c: Ctx, a):
    from .web import main as web_main
    web_main([*(["--config", a.config] if a.config else []), *a.args])


def cmd_daemon(c: Ctx, a):
    from .daemon import main as dmain
    dmain([] if not a.config else ["--config", a.config])


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="lab", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project")
    ap.add_argument("--config")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="everything at a glance").set_defaults(fn=cmd_status)
    s = sub.add_parser("world", help="World State: facts + STATE.md")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_world)
    s = sub.add_parser("brief", help="(scout) publish a Change Brief")
    s.add_argument("--file", required=True)
    s.add_argument("--title")
    s.add_argument("--severity", default="normal", choices=["info", "minor", "normal", "major"])
    s.add_argument("--post", action="store_true", help="also post the brief to Discord")
    s.set_defaults(fn=cmd_world_brief)
    s = sub.add_parser("events", help="the event log")
    s.add_argument("-n", type=int, default=40)
    s.add_argument("--topic")
    s.add_argument("--id", type=int)
    s.add_argument("--hours", type=float, default=48)
    s.add_argument("--min-severity", default="info", choices=["info", "minor", "normal", "major"])
    s.set_defaults(fn=cmd_events)
    s = sub.add_parser("emit", help="append an event")
    s.add_argument("topic")
    s.add_argument("summary")
    s.add_argument("--severity", default="normal", choices=["info", "minor", "normal", "major"])
    s.add_argument("--key")
    s.add_argument("--payload")
    s.set_defaults(fn=cmd_emit)
    sub.add_parser("budget", help="today's spend by pool").set_defaults(fn=cmd_budget)
    s = sub.add_parser("runs", help="agent runs")
    s.add_argument("-n", type=int, default=20)
    s.add_argument("--id", type=int)
    s.set_defaults(fn=cmd_runs)

    s = sub.add_parser("say", help="post to the project's Discord channel (via labd)")
    s.add_argument("text", nargs="?", default="-")
    s.add_argument("--file-text", help="post the contents of this file")
    s.add_argument("-F", action="append", help="attach a file")
    s.add_argument("--reply-to")
    s.add_argument("--channel-id", help="a thread of the project channel")
    s.add_argument("--wait", action="store_true")
    s.set_defaults(fn=cmd_say)
    s = sub.add_parser("inject", help="simulate a Discord request (testing)")
    s.add_argument("text")
    s.add_argument("--author", default="tester")
    s.set_defaults(fn=cmd_inject)

    s = sub.add_parser("ticket")
    s.add_argument("action", choices=["new", "list", "close", "note"])
    s.add_argument("id", nargs="?", type=int)
    s.add_argument("--title")
    s.add_argument("--body")
    s.add_argument("--author")
    s.add_argument("--message")
    s.add_argument("--text")
    s.add_argument("--all", action="store_true")
    s.set_defaults(fn=cmd_ticket)

    s = sub.add_parser("idea", aliases=["backlog"], help="research ideas: the Researcher's tasks for threads")
    s.add_argument("action", choices=["add", "suggest", "list", "show", "edit", "ready", "accept", "reject", "done"])
    s.add_argument("id", nargs="?", type=int)
    s.add_argument("--title")
    s.add_argument("--hypothesis")
    s.add_argument("--gain")
    s.add_argument("--cost", type=float)
    s.add_argument("--priority", type=int, default=50)
    s.add_argument("--note")
    s.add_argument("--spec", help="the task for a thread: text or a file path")
    s.add_argument("--body", help="suggest: what the person asked, with context")
    s.add_argument("--metric", help="the number the task should move")
    s.add_argument("--ready", action="store_true", help="add: queue it for a thread now")
    s.add_argument("--thread", help="hand it to this thread (a follow-up keeps its session warm)")
    s.add_argument("--author")
    s.add_argument("--message", help="suggest: the Discord message id it came from")
    s.add_argument("--all", action="store_true")
    s.set_defaults(fn=cmd_backlog)

    s = sub.add_parser("thread", help="implementor threads (one session each, one task at a time)")
    s.add_argument("action", choices=["start", "list", "show", "note", "retire", "claim", "report", "ask"])
    s.add_argument("id", nargs="?", help="thread id (note: an id or 'all')")
    s.add_argument("--title")
    s.add_argument("--text", help="charter / message / reason / claim (text or a file path)")
    s.add_argument("--metric", help="start: the number this thread optimises")
    s.add_argument("--author")
    s.add_argument("--holder")
    s.add_argument("--done", action="store_true", help="report: the task is finished")
    s.add_argument("--all", action="store_true")
    s.set_defaults(fn=cmd_thread)

    s = sub.add_parser("maint", help="changes to the lab's own code (the maintainer)")
    s.add_argument("action", choices=["list", "show", "request", "approve", "reject", "mark"])
    s.add_argument("id", nargs="?", help="m-N (request: the change, in words)")
    s.add_argument("status", nargs="?", choices=["deployed", "rolled_back", "failed"], help="mark: the outcome")
    s.add_argument("--text", default="")
    s.set_defaults(fn=cmd_maint)

    s = sub.add_parser("result", help="(thread) record one experiment result")
    s.add_argument("action", choices=["add"])
    s.add_argument("--metric", required=True)
    s.add_argument("--value", type=float, required=True)
    s.add_argument("--kept", required=True, help="yes | no")
    s.add_argument("--desc", required=True, help="what was changed/tried")
    s.add_argument("--run", help="job name")
    s.add_argument("--cost", type=float)
    s.add_argument("--notes")
    s.add_argument("--best", action="store_true", help="this is the thread's new best")
    s.add_argument("--holder")
    s.set_defaults(fn=cmd_result)

    s = sub.add_parser("gpu", help="GPU leases (held by threads) and Runpod/Shadeform/Vast stock")
    s.add_argument("action", choices=["lease", "release", "extend", "list", "stock", "pause", "resume"])
    s.add_argument("pattern", nargs="?", help="stock: GPU name pattern (e.g. H100); pause/resume: reason")
    s.add_argument("count_pos", nargs="?", type=int, help="stock: GPUs per pod")
    s.add_argument("--gpu", help="lease: Runpod GPU id, e.g. 'NVIDIA H100 80GB HBM3'")
    s.add_argument("--count", type=int, default=1)
    s.add_argument("--hours", type=float, default=4.0)
    s.add_argument("--alt", action="append", help="lease: alternative GPU id (repeatable)")
    s.add_argument("--pool")
    s.add_argument("--wait", type=int, default=0, help="lease: seconds to wait for the grant")
    s.add_argument("--lease", type=int, help="release/extend: a specific lease id")
    s.add_argument("--stop", action="store_true", help="release: stop the pod now")
    s.add_argument("--holder")
    s.set_defaults(fn=cmd_gpu)
    s = sub.add_parser("ssh", help="ssh into your leased pod")
    s.add_argument("--lease", type=int)
    s.add_argument("--holder")
    s.add_argument("cmd", nargs=argparse.REMAINDER)
    s.set_defaults(fn=cmd_ssh)
    for name, fn in (("push", cmd_push), ("pull", cmd_pull)):
        s = sub.add_parser(name, help=f"rsync {'to' if name == 'push' else 'from'} your leased pod")
        s.add_argument("--lease", type=int)
        s.add_argument("--holder")
        s.add_argument("src")
        s.add_argument("dst")
        s.add_argument("rsync_args", nargs=argparse.REMAINDER)
        s.set_defaults(fn=fn)
    s = sub.add_parser("launch", help="start a job on your pod under labrun (watchdog-visible)")
    s.add_argument("--job", required=True)
    s.add_argument("--cwd", default="/workspace")
    s.add_argument("--lease", type=int)
    s.add_argument("--holder")
    s.add_argument("cmd", nargs=argparse.REMAINDER)
    s.set_defaults(fn=cmd_launch)

    sub.add_parser("doctor", help="check configuration").set_defaults(fn=cmd_doctor)
    s = sub.add_parser("ralph", help="run the vendored ralph.sh loop (claude backend)")
    s.add_argument("args", nargs=argparse.REMAINDER)
    s.set_defaults(fn=cmd_ralph)
    s = sub.add_parser("web", help="local dashboard (http://127.0.0.1:8765; --port, --host, --allow-host)")
    s.add_argument("args", nargs=argparse.REMAINDER)
    s.set_defaults(fn=cmd_web)
    sub.add_parser("daemon", help="run labd in the foreground").set_defaults(fn=cmd_daemon)
    return ap


def main(argv=None):
    a = build_parser().parse_args(argv)
    if a.cmd == "ticket" and a.action == "new" and not a.title:
        die("ticket new needs --title")
    if a.cmd == "ticket" and a.action in ("close", "note") and (not a.id or not a.text):
        die(f"ticket {a.action} needs ID and --text")
    if a.cmd in ("idea", "backlog") and a.action == "add" and not (a.title and (a.hypothesis or a.spec)):
        die("idea add needs --title and --spec (or --hypothesis)")
    if a.cmd in ("idea", "backlog") and a.action in ("accept", "ready", "edit", "show", "reject", "done") and not a.id:
        die(f"idea {a.action} needs ID")
    if a.cmd == "thread" and a.action in ("show", "note", "retire") and not a.id:
        die(f"thread {a.action} needs a thread id")
    a.fn(Ctx(a), a)


if __name__ == "__main__":
    main()
