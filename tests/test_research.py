"""The Researcher → implementor pipeline: ideas become tasks for threads by plain code."""
import asyncio
import json

import pytest

from lab import cli, research
from lab.agents import Agents
from lab.daemon import Daemon
from lab.db import DB, now


def run(*argv):
    cli.main(list(argv))


def idea(db, title="distil", status="ready", priority=50, for_thread=None):
    return db.insert("backlog", project="affine", created_at=now(), author="researcher", title=title, status=status,
                     priority=priority, spec=f"spec of {title}", metric="margin", for_thread=for_thread)


def test_ready_ideas_go_to_new_then_idle_threads(cfg, db):
    p = cfg.project("affine")
    p.max_threads = 1
    a, b = idea(db, "a", priority=10), idea(db, "b", priority=20)
    assert research.dispatch(db, p) == [f"idea {a} → t-001"]      # one slot: b waits
    t = db.one("SELECT * FROM threads WHERE id='t-001'")
    assert t["task_id"] == a and "spec of a" in (p.work_dir / "threads" / "t-001" / "TASK.md").read_text()
    assert db.one("SELECT status FROM backlog WHERE id=?", (b,))["status"] == "ready"
    assert db.one("SELECT 1 FROM events WHERE topic='thread.task' AND key='t-001'")
    assert research.finish_task(db, p, "t-001", "margin +0.02, noise") == a
    assert db.one("SELECT status, result FROM backlog WHERE id=?", (a,))["result"] == "margin +0.02, noise"
    assert research.dispatch(db, p) == [f"idea {b} → t-001"]      # the idle thread takes the next task
    assert db.one("SELECT COUNT(*) n FROM threads")["n"] == 1


def test_a_follow_up_waits_for_its_thread(cfg, db):
    p = cfg.project("affine")
    p.max_threads = 2
    first = idea(db, "first")
    research.dispatch(db, p)
    follow, other = idea(db, "follow-up", priority=1, for_thread="t-001"), idea(db, "other")
    assert research.dispatch(db, p) == [f"idea {other} → t-002"]  # t-001 is busy: the follow-up waits for it
    research.finish_task(db, p, "t-001", "done")
    assert research.dispatch(db, p) == [f"idea {follow} → t-001"]
    assert first


def test_idle_threads_retire_and_retiring_requeues_the_task(cfg, db):
    p = cfg.project("affine")
    p.max_threads = 2
    idea(db, "a")
    idea(db, "b")
    research.dispatch(db, p)
    research.finish_task(db, p, "t-001", "done")
    db.x("UPDATE threads SET idle_since=? WHERE id='t-001'", (now() - 25 * 3600,))
    assert research.retire_idle(db, p, 24) == ["t-001"]
    research.retire(db, p, "t-002", "dead end", "researcher")
    assert db.one("SELECT status, thread_id FROM backlog WHERE title='b'")["status"] == "ready"   # back in the queue


def test_thread_report_ask_and_suggest(labdir, cfg, monkeypatch, tmp_path):
    run("thread", "start", "--title", "distil", "--metric", "margin", "--text", "do the thing")
    monkeypatch.setenv("LAB_THREAD", "t-001")
    run("thread", "ask", "--text", "which slice?")
    run("thread", "report", "--text", "r001: +0.02 sd (SE 0.03)")
    db = DB(cfg.db_path)
    assert db.one("SELECT task_id FROM threads")["task_id"]           # a plain report keeps the task
    run("thread", "report", "--done", "--text", "r002: ruled out")
    assert db.one("SELECT task_id FROM threads")["task_id"] is None
    assert db.one("SELECT status FROM backlog")["status"] == "done"
    run("idea", "suggest", "--title", "try DPO", "--body", "from Alice", "--author", "Alice", "--message", "42")
    topics = [r["topic"] for r in db.all("SELECT topic FROM events ORDER BY id")]
    assert topics.count("thread.report") == 2 and "thread.question" in topics and "research.suggestion" in topics
    s = db.one("SELECT * FROM backlog WHERE title='try DPO'")
    assert s["status"] == "suggested" and s["source_message"] == "42"
    run("idea", "ready", str(s["id"]), "--spec", "DPO on king failures", "--thread", "t-001")
    s = db.one("SELECT * FROM backlog WHERE id=?", (s["id"],))
    assert s["status"] == "ready" and s["for_thread"] == "t-001" and s["spec"] == "DPO on king failures"
    with pytest.raises(SystemExit):
        run("idea", "ready", "1")                                     # done ideas cannot be requeued


def test_researcher_wakes_on_reports_not_on_its_own_or_ops_events(cfg, db):
    a = Agents(db, cfg)
    role = cfg.project("affine").roles["researcher"]
    a._cursor("affine", "researcher", "")
    role.debounce_s = 0
    for topic in ("idea.new", "thread.result", "budget.alert", "thread.stalled", "ticket.new", "discord.request"):
        db.emit("affine", topic, "x", severity="normal", key="1")
    a.dispatch()
    assert not db.one("SELECT 1 FROM agent_runs WHERE role='researcher'")
    db.emit("affine", "thread.report", "t-001 done", severity="normal", key="t-001")
    a.dispatch()
    assert db.one("SELECT 1 FROM agent_runs WHERE role='researcher'")


def test_researcher_prompt_is_about_ideas_only(cfg, db):
    p = cfg.project("affine")
    a = Agents(db, cfg)
    p.work_dir.mkdir(parents=True, exist_ok=True)
    (p.work_dir / "RESEARCH.md").write_text("king trails on sci_code")
    idea(db, "distil")
    ev = db.one("SELECT * FROM events WHERE id=?", (db.emit("affine", "thread.question", "which slice?", key="t-001",
                                                           payload={"text": "which slice?"}),))
    text = a.user_prompt(p, p.roles["researcher"], "", [ev])
    assert "RESEARCH.md" in text and "spec of distil" in text and "which slice?" in text
    assert "budget today" not in text and "channel conversation" not in text and "GPU leases" not in text


def test_only_the_project_claude_md_is_loaded(cfg, project, tmp_path):
    (cfg.root / "CLAUDE.md").write_text("lab developer notes")
    s = Agents(DB(cfg.db_path), cfg)._settings(project.roles["thread"], project)
    assert str(cfg.root / "CLAUDE.md") in s["claudeMdExcludes"]
    assert str(project.dir / "CLAUDE.md") not in s["claudeMdExcludes"]
    assert s["autoMemoryEnabled"] is False
    assert "Goal" in (project.dir / "CLAUDE.md").read_text()         # GOAL.md lives in the project's CLAUDE.md


async def test_daemon_dispatches_and_ticks_the_researcher_only_when_idle(cfg):
    d = Daemon(cfg)
    p = cfg.project("affine")
    d._research(p, now())
    assert d.db.one("SELECT 1 FROM events WHERE topic='tick.research'")         # nothing to do: review
    idea(d.db, "a")
    d.db.kv_set("affine", "next_research", 0)
    d._research(p, now())
    assert d.db.one("SELECT task_id FROM threads WHERE id='t-001'")["task_id"]
    assert d.db.one("SELECT COUNT(*) n FROM events WHERE topic='tick.research'")["n"] == 1   # busy: no tick
    # a thread whose task is done is not continued
    research.finish_task(d.db, p, "t-001", "done")
    rid = d.db.insert("agent_runs", project="affine", role="thread", key="t-001", status="ok", started_at=now(),
                      event_ids="[]")
    d._continue_thread(p, "t-001", d.db.one("SELECT * FROM agent_runs WHERE id=?", (rid,)), "NEXT: now")
    assert d.db.kv_get("affine", "wake:t-001") == 0
    await asyncio.sleep(0)


def test_idea_edit_keeps_the_status(labdir, cfg):
    run("idea", "add", "--title", "draft", "--spec", "v1")
    run("idea", "edit", "1", "--spec", "v2", "--priority", "5", "--note", "tightened")
    r = DB(cfg.db_path).one("SELECT * FROM backlog")
    assert (r["status"], r["spec"], r["priority"]) == ("proposed", "v2", 5) and "tightened" in r["notes"]


async def test_threads_consult_a_fable_advisor_except_on_routine_checks(db, cfg, tmp_path):
    p = cfg.project("affine")
    a = Agents(db, cfg)
    role = p.roles["thread"]
    argv = a.command(p, role, 1, tmp_path, tmp_path / "s", tmp_path / "t")
    assert argv[argv.index("--advisor") + 1] == "claude-fable-5-1"
    assert "--advisor" not in a.command(p, role, 1, tmp_path, tmp_path / "s", tmp_path / "t", model=role.light_model)
    assert "--advisor" not in a.command(p, p.roles["researcher"], 1, tmp_path, tmp_path / "s", tmp_path / "t")


def test_threads_read_the_shared_research_memory_and_see_its_changes(cfg, db):
    import os
    p = cfg.project("affine")
    a = Agents(db, cfg)
    idea(db, "a")
    research.dispatch(db, p)
    doc = p.work_dir / "RESEARCH.md"
    doc.write_text("## Objective\nmargin vs king on the e80 slice; bar +0.1 sd")
    task = [db.one("SELECT * FROM events WHERE topic='thread.task'")]
    assert "bar +0.1 sd" in a.user_prompt(p, p.roles["thread"], "t-001", task)          # fresh session
    db.x("UPDATE threads SET session_id='s', session_passes=1")
    cont = [db.one("SELECT * FROM events WHERE id=?", (db.emit("affine", "thread.continue", "go", key="t-001"),))]
    assert "bar +0.1 sd" not in a.user_prompt(p, p.roles["thread"], "t-001", cont)      # unchanged: not repeated
    doc.write_text("## Objective\nbar +0.2 sd now")
    os.utime(doc, ns=(doc.stat().st_mtime_ns + 10**9,) * 2)
    text = a.user_prompt(p, p.roles["thread"], "t-001", cont)
    assert "research memory changed" in text and "bar +0.2 sd" in text


def test_the_researchers_notebook_becomes_the_shared_memory(cfg):
    p = cfg.project("affine")
    (p.work_dir / "researcher").mkdir(parents=True)
    (p.work_dir / "researcher" / "NOTEBOOK.md").write_text("objective: margin")
    Daemon(cfg)
    assert (p.work_dir / "RESEARCH.md").read_text() == "objective: margin"


def test_no_tasks_are_handed_out_while_gpus_are_paused(cfg, db):
    p = cfg.project("affine")
    idea(db, "a")
    db.kv_set("affine", "gpu_paused", {"at": now(), "by": "operator"})
    assert research.dispatch(db, p) == [] and not db.one("SELECT 1 FROM threads")
    db.kv_set("affine", "gpu_paused", None)
    assert research.dispatch(db, p) == ["idea 1 → t-001"]


def test_agents_get_only_the_tools_they_use(db, cfg, tmp_path):
    p = cfg.project("affine")
    a = Agents(db, cfg)
    tools = lambda role: (lambda v: v[v.index("--tools") + 1].split(","))(a.command(p, p.roles[role], 1, tmp_path,
                                                                                    tmp_path / "s", tmp_path / "t"))
    assert tools("thread") == ["Bash", "Read", "Edit", "Write", "Grep", "Glob", "WebFetch", "WebSearch", "Agent"]
    assert "Edit" not in tools("concierge") and "Agent" not in tools("concierge")
    assert all("--tools" in a.command(p, r, 1, tmp_path, tmp_path / "s", tmp_path / "t") for r in p.roles.values())


def test_routine_checks_stay_on_the_main_model_once_the_session_is_big(cfg):
    role = cfg.project("affine").roles["thread"]
    check = [{"topic": "job.check"}]
    assert Agents.model_for(role, check, 20_000) == "claude-sonnet-5"
    assert Agents.model_for(role, check, 130_000) == "claude-opus-5-5"     # Sonnet would re-cache 130k tokens


def test_files_an_agent_edits_are_referenced_not_pasted(cfg, db):
    p = cfg.project("affine")
    a = Agents(db, cfg)
    p.world_dir.mkdir(parents=True, exist_ok=True)
    p.work_dir.mkdir(parents=True, exist_ok=True)
    (p.world_dir / "STATE.md").write_text("# World\nWorld version: `v1`\n\n## Scoring\nSECTION-BODY " * 1 + "x" * 5000)
    research_doc = p.work_dir / "RESEARCH.md"
    research_doc.write_text("## Objective\nRESEARCH-BODY")
    ev = [db.one("SELECT * FROM events WHERE id=?", (db.emit("affine", "world.change.contract", "x", severity="normal"),))]
    scout = a.user_prompt(p, p.roles["scout"], "", ev)
    assert "SECTION-BODY" not in scout and "World version: `v1`" in scout and str(p.world_dir / "STATE.md") in scout
    researcher = a.user_prompt(p, p.roles["researcher"], "", ev)
    assert "RESEARCH-BODY" not in researcher and str(research_doc) in researcher


def test_operator_orders_and_verbatim_files(labdir, cfg, monkeypatch, tmp_path):
    notes = tmp_path / "field-notes.md"
    notes.write_text("# winner's log\nexact bytes ✓\n")
    run("idea", "add", "--title", "old", "--spec", "x")
    monkeypatch.setenv("LAB_ROLE", "concierge")
    monkeypatch.setattr(cli, "ROLE", "concierge")
    with pytest.raises(SystemExit):
        run("idea", "clear", "--note", "not an operator")                     # anyone: refused in code
    monkeypatch.setenv("LAB_OPERATOR", "1")
    run("idea", "clear", "--note", "operator: start over")
    run("idea", "suggest", "--title", "winner's log", "--body", "update the state from this, verbatim",
        "--author", "Op", "--message", "9", "--file", str(notes))
    db = DB(cfg.db_path)
    assert db.one("SELECT status FROM backlog WHERE title='old'")["status"] == "rejected"
    s = db.one("SELECT * FROM backlog WHERE title=\"winner's log\"")
    assert str(notes) in s["spec"] and "update the state from this, verbatim" in s["spec"]
    ev = db.one("SELECT * FROM events WHERE topic='research.suggestion'")
    assert str(notes) in ev["payload"]


async def test_attachments_are_saved_and_the_operator_is_flagged(cfg):
    from tests.test_daemon import make, msg
    d = make(cfg)

    async def download(url, max_bytes):
        return b"# notes\nverbatim"
    d.discord.download = download
    cfg.maint.operators = ["5"]
    m = msg("<@77> read this", mid="950")
    m["attachments"] = [{"url": "https://cdn/x", "filename": "affine notes.md", "size": 16}]
    await d.on_message(m)
    p = json.loads(d.db.one("SELECT payload FROM events WHERE topic='discord.request'")["payload"])
    assert p["operator"] is True and p["files"][0]["path"].endswith("950-affine_notes.md")
    assert open(p["files"][0]["path"], "rb").read() == b"# notes\nverbatim"
    a = Agents(d.db, cfg)
    proj = cfg.project("affine")
    ev = [d.db.one("SELECT * FROM events WHERE topic='discord.request'")]
    text = a.user_prompt(proj, proj.roles["concierge"], "950", ev)
    assert "an operator" in text and p["files"][0]["path"] in text
    assert a.env(proj, proj.roles["concierge"], 1, "950").get("LAB_OPERATOR") == "1"
