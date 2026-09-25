"""Operators talk to the Researcher itself: one session across wakes, a terminal chat that holds its wakes,
and dashboard messages that wake it at once."""
import json
import os
import subprocess

import pytest

from lab import cli, research
from lab.agents import Agents
from lab.db import DB, now
from lab.web import action_argv

from test_agents import drain, fake  # noqa: F401  (fixture)


def argv(fake, n):
    return (fake / "calls" / f"{n}.argv").read_text().splitlines()


def wake(db, topic="thread.report", **kw):
    eid = db.emit("affine", topic, "something to think about", severity="normal", **kw)
    db.x("UPDATE events SET ts=? WHERE id=?", (now() - 1000, eid))   # past the debounce
    return eid


async def test_the_researcher_keeps_one_session_across_wakes(db, cfg, fake):
    a = Agents(db, cfg)
    a._cursor("affine", "researcher", "")
    (fake / "cost").write_text("0.50")
    wake(db)
    await drain(a)
    sid = argv(fake, 0)[argv(fake, 0).index("--session-id") + 1]
    s = research.session(db, cfg.project("affine"))
    assert s["passes"] == 1 and s["cost"] == 0.5
    (fake / "cost").write_text("0.80")        # claude reports the session's cost so far
    wake(db)
    await drain(a)
    assert argv(fake, 1)[argv(fake, 1).index("--resume") + 1] == s["id"] and "--session-id" not in argv(fake, 1)
    assert s["id"] in (sid, "sess-0")
    assert "resuming your own session" in (fake / "calls" / "1.prompt").read_text()
    costs = [r["cost_usd"] for r in db.all("SELECT cost_usd FROM agent_runs WHERE role='researcher' ORDER BY id")]
    assert costs == [0.5, 0.3]


async def test_the_researchers_session_rotates_through_research_md(db, cfg, fake):
    p = cfg.project("affine")
    p.rotate_context_tokens = 150_000
    a = Agents(db, cfg)
    a._cursor("affine", "researcher", "")
    (fake / "ctx").write_text("200000")
    wake(db)
    await drain(a)
    assert research.session(db, p)["rotate"]
    wake(db)
    await drain(a)
    assert "--resume" in argv(fake, 1) and "last pass of this session" in (fake / "calls" / "1.prompt").read_text()
    s = research.session(db, p)
    assert s["id"] is None and s["gen"] == 2 and not s["rotate"]
    assert db.one("SELECT 1 FROM events WHERE topic='research.rotated'")
    wake(db)
    await drain(a)
    assert "--session-id" in argv(fake, 2)


async def test_an_operators_message_wakes_the_researcher_at_once(db, cfg, fake):
    a = Agents(db, cfg)
    a._cursor("affine", "researcher", "")
    db.emit("affine", "research.operator", "alice to the Researcher", severity="normal",
            payload={"author": "alice (web)", "text": "Drop the LoRA line; focus on distillation."})
    a.dispatch()            # no debounce, no waiting for the Scout
    assert db.one("SELECT 1 FROM agent_runs WHERE role='researcher' AND status='queued'")
    await drain(a)
    prompt = (fake / "calls" / "0.prompt").read_text()
    assert "An operator is talking to you" in prompt and "Drop the LoRA line" in prompt and "alice (web)" in prompt


async def test_a_live_chat_holds_the_researchers_wakes(db, cfg, fake):
    p = cfg.project("affine")
    a = Agents(db, cfg)
    a._cursor("affine", "researcher", "")
    db.kv_set("affine", research.CHAT_KEY, {"pid": os.getpid(), "by": "alice", "since": now()})
    wake(db)
    a.dispatch()
    a.launch()
    assert db.one("SELECT status FROM agent_runs WHERE role='researcher'")["status"] == "queued"
    assert "live chat with alice" in cli.status_text(db, cfg, p)
    # a chat whose process died no longer holds anything
    dead = subprocess.Popen(["true"])
    dead.wait()
    db.kv_set("affine", research.CHAT_KEY, {"pid": dead.pid, "by": "alice", "since": now()})
    assert research.chat_holder(db, p) is None
    await drain(a)
    assert db.one("SELECT status FROM agent_runs WHERE role='researcher'")["status"] == "ok"


def test_the_chat_is_the_same_session_in_interactive_claude_code(cfg, db, tmp_path):
    p = cfg.project("affine")
    a = Agents(db, cfg)
    cmd = a.command(p, p.roles["researcher"], 7, tmp_path, tmp_path / "s", tmp_path / "t", ("sid-1", True),
                    interactive=True)
    assert "-p" not in cmd and "--output-format" not in cmd
    assert cmd[cmd.index("--resume") + 1] == "sid-1" and cmd[cmd.index("--model") + 1] == "claude-fable-5-1"
    assert cmd[cmd.index("--settings") + 1] == str(tmp_path / "t")      # the same guard and deny list
    assert "--tools" in cmd and "--system-prompt-snapshot" in cmd


def test_chat_records_its_cost_and_releases_the_lock(labdir, cfg, monkeypatch, capsys):
    """`lab researcher chat` runs claude on the session, then books the chat's cost from the transcript."""
    db = DB(cfg.db_path)
    p = cfg.project("affine")
    research.set_session(db, p, id="sid-1", passes=2, cost=1.0, ctx=50_000)
    wd = p.work_dir / "researcher"
    tdir = labdir / "claude" / "projects" / "".join(ch if ch.isalnum() else "-" for ch in str(wd))
    tdir.mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(labdir / "claude"))
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: True, raising=False)
    monkeypatch.setenv("CLAUDE_CODE_CHILD_SESSION", "1")      # run from inside a Claude Code session
    monkeypatch.setenv("CLAUDECODE", "1")
    seen = {}

    def fake_run(cmd, cwd, env):
        seen.update(cmd=cmd, held=research.chat_holder(db, p), role=env["LAB_ROLE"],
                    child=[k for k in env if k.startswith("CLAUDECODE") or k == "CLAUDE_CODE_CHILD_SESSION"])
        (tdir / "sid-1.jsonl").write_text("\n".join(json.dumps(x) for x in [
            {"type": "user", "message": {"role": "user", "content": "drop LoRA"}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "Done."}],
                                              "usage": {"input_tokens": 5, "cache_read_input_tokens": 60_000}}},
            {"type": "cost-state", "totalCostUSD": 1.75}]) + "\n")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    cli.main(["researcher", "chat", "--author", "alice"])
    assert seen["held"]["by"] == "alice" and seen["role"] == "researcher" and "--resume" in seen["cmd"]
    assert seen["child"] == []          # its transcript is saved
    assert research.chat_holder(db, p) is None
    s = research.session(db, p)
    assert s["cost"] == 1.75 and s["ctx"] == 60_005 and s["passes"] == 2
    run = db.one("SELECT * FROM agent_runs WHERE role='researcher' AND key='chat'")
    assert run["status"] == "ok" and run["cost_usd"] == 0.75
    assert db.one("SELECT 1 FROM events WHERE topic='research.chat'")
    cli.main(["researcher", "show"])
    out = capsys.readouterr().out
    assert "drop LoRA" in out and "Done." in out


def test_say_is_for_people_and_wakes_the_researcher(labdir, cfg, monkeypatch):
    db = DB(cfg.db_path)
    cli.main(["researcher", "say", "try distillation first", "--author", "bob"])
    ev = db.one("SELECT * FROM events WHERE topic='research.operator'")
    assert json.loads(ev["payload"]) == {"author": "bob", "text": "try distillation first"}
    assert ev["severity"] == "normal" and "research.operator" in cfg.project("affine").roles["researcher"].wake_on
    monkeypatch.setenv("LAB_RUN_ID", "5")          # an agent cannot pose as an operator
    with pytest.raises(SystemExit):
        cli.main(["researcher", "say", "hi"])


def test_the_dashboard_talks_through_the_cli():
    assert action_argv({"action": "researcher_say", "text": "hello"}) == \
        ["researcher", "say", "--text", "hello", "--author", "operator (web)"]


def test_transcript_keeps_the_conversation_not_the_noise(tmp_path):
    f = tmp_path / "s.jsonl"
    f.write_text("\n".join(json.dumps(x) for x in [
        {"type": "user", "message": {"content": "why did r7 lose?"}},
        {"type": "assistant", "message": {"content": [{"type": "thinking", "thinking": "hmm"},
                                                      {"type": "tool_use", "name": "Bash", "input": {"command": "lab runs"}}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "content": "lots of output"}]}},
        {"type": "assistant", "isSidechain": True, "message": {"content": [{"type": "text", "text": "subagent"}]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "Sampling mismatch."}]}},
    ]) + "\nnot json\n")
    assert [(e["who"], e["text"]) for e in research.transcript(f)] == [
        ("user", "why did r7 lose?"), ("tool", "Bash: lab runs"), ("researcher", "Sampling mismatch.")]


def test_the_dashboard_shows_the_live_session(cfg, db, labdir, monkeypatch):
    from lab.web import Dash
    p = cfg.project("affine")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(labdir / "claude"))
    wd = p.work_dir / "researcher"
    tdir = labdir / "claude" / "projects" / "".join(ch if ch.isalnum() else "-" for ch in str(wd))
    tdir.mkdir(parents=True)
    (tdir / "sid-9.jsonl").write_text(json.dumps({"type": "assistant", "message": {"content": [
        {"type": "text", "text": "Idea 12 is ready for t-004."}]}}) + "\n")
    research.set_session(db, p, id="sid-9", passes=1)
    db.insert("agent_runs", project="affine", role="researcher", key="", status="running", queued_at=now(),
              started_at=now())
    d = Dash(cfg, "affine").researcher()
    assert d["transcript"][-1]["text"] == "Idea 12 is ready for t-004." and d["run"]["status"] == "running"
    assert d["enabled"] and d["chat"] is None
