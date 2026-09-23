import asyncio
import json
import os
import stat

import pytest

from lab.agents import Agents
from lab.db import now

FAKE = """#!/usr/bin/env bash
# fake `claude -p`: records argv + prompt, answers per $FAKE_MODE
dir="$(dirname "$0")"
n=$(ls "$dir"/calls/*.argv 2>/dev/null | wc -l)
mkdir -p "$dir/calls"
printf '%s\\n' "$@" > "$dir/calls/$n.argv"
cat > "$dir/calls/$n.prompt"
echo "$LAB_ROLE $LAB_PROJECT ${LAB_EXP:-} ${RUNPOD_API_KEY:-nokey} ${CLAUDE_CODE_SUBAGENT_MODEL:-}" > "$dir/calls/$n.env"
mode=$(cat "$dir/mode" 2>/dev/null || echo ok)
if [ "$mode" = ratelimit ]; then
  echo '{"type":"result","subtype":"error_during_execution","is_error":true,"result":"Claude AI usage limit reached|1790200000","session_id":"s"}'
  exit 1
fi
sleep "${FAKE_SLEEP:-0}"
ctx=$(cat "$dir/ctx" 2>/dev/null || echo 1000)
cost=$(cat "$dir/cost" 2>/dev/null || echo 0.01)
echo "{\\"type\\":\\"result\\",\\"subtype\\":\\"success\\",\\"is_error\\":false,\\"result\\":\\"reply from $LAB_ROLE\\",\\"session_id\\":\\"sess-$n\\",\\"total_cost_usd\\":$cost,\\"num_turns\\":2,\\"usage\\":{\\"iterations\\":[{\\"input_tokens\\":10,\\"cache_read_input_tokens\\":$ctx,\\"cache_creation_input_tokens\\":0}]}}"
"""


@pytest.fixture
def fake(labdir):
    p = labdir / "fakeclaude"
    p.write_text(FAKE)
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return labdir


async def drain(agents, rounds=40):
    for _ in range(rounds):
        agents.dispatch()
        agents.launch()
        await asyncio.sleep(0.05)
        if not any(not t.done() for t in agents.tasks.values()) and \
                not agents.db.one("SELECT 1 FROM agent_runs WHERE status='queued'"):
            agents.dispatch()
            if not agents.db.one("SELECT 1 FROM agent_runs WHERE status='queued'"):
                return
    raise AssertionError("agents did not settle")


def runs(db):
    return [dict(r) for r in db.all("SELECT * FROM agent_runs ORDER BY id")]


async def test_concierge_request_runs_and_calls_back(db, cfg, fake, monkeypatch):
    monkeypatch.setenv("RUNPOD_API_KEY", "secret-should-not-leak")
    got = []

    async def on_result(p, role, key, run, res):
        got.append((role.name, key, run["result"]))

    a = Agents(db, cfg, on_result=on_result)
    db.insert("discord_messages", id="111", project="affine", channel_id="c", author_id="9", author_name="alice",
              is_bot=0, content="what's running?", reply_to=None, ts=now())
    db.emit("affine", "discord.request", "alice: what's running?", severity="normal", key="111",
            payload={"id": "111", "author_name": "alice", "content": "what's running?"})
    await drain(a)
    assert got == [("concierge", "111", "reply from concierge")]
    argv = (fake / "calls" / "0.argv").read_text().splitlines()
    assert "--permission-mode" in argv and argv[argv.index("--permission-mode") + 1] == "dontAsk"
    assert "--strict-mcp-config" in argv
    prompt = (fake / "calls" / "0.prompt").read_text()
    assert "what's running?" in prompt and "alice" in prompt
    env = (fake / "calls" / "0.env").read_text()
    assert "secret-should-not-leak" not in env and env.startswith("concierge affine")


async def test_single_role_debounce_and_batching(db, cfg, fake):
    a = Agents(db, cfg)
    a._cursor("affine", "scout", "")         # scout starts listening now
    for i in range(3):
        db.emit("affine", "world.change.contract", f"change {i}", severity="major")
    db.x("UPDATE events SET ts=?", (now() - 1000,))
    await drain(a)
    scout = [r for r in runs(db) if r["role"] == "scout"]
    assert len(scout) == 1 and len(json.loads(scout[0]["event_ids"])) == 3
    # a fresh burst inside the debounce window waits
    db.emit("affine", "world.change.llms", "another", severity="normal")
    a.dispatch()
    assert len([r for r in runs(db) if r["role"] == "scout"]) == 1


async def test_min_severity_filter(db, cfg, fake):
    a = Agents(db, cfg)
    a._cursor("affine", "scout", "")
    db.emit("affine", "world.change.corpus", "stats", severity="minor")
    db.x("UPDATE events SET ts=?", (now() - 1000,))
    a.dispatch()
    assert not [r for r in runs(db) if r["role"] == "scout"]


def add_thread(db, tid="t-001", status="active", session=None, passes=0):
    db.insert("threads", id=tid, project="affine", title="t " + tid, status=status, created_at=now(),
              passes=passes, session_id=session, workdir="/tmp")


async def test_thread_is_keyed_and_defers_while_pending(db, cfg, fake):
    (fake / "mode").write_text("ok")
    os.environ["FAKE_SLEEP"] = "0.4"
    try:
        a = Agents(db, cfg)
        add_thread(db)
        db.emit("affine", "thread.start", "go", severity="normal", key="t-001")
        a.dispatch()
        a.launch()
        await asyncio.sleep(0.1)
        db.emit("affine", "job.finished", "done", severity="normal", key="t-001")
        a.dispatch()   # first pass still in flight → deferred
        assert len(runs(db)) == 1
        await drain(a)
        rr = runs(db)
        assert len(rr) == 2 and all(r["status"] == "ok" for r in rr)
        assert "job.finished" in (fake / "calls" / "1.prompt").read_text()
        assert (fake / "calls" / "1.env").read_text().split()[0] == "thread"
    finally:
        os.environ.pop("FAKE_SLEEP", None)


async def test_retired_thread_is_not_woken(db, cfg, fake):
    a = Agents(db, cfg)
    add_thread(db, status="retired")
    db.emit("affine", "thread.continue", "go", key="t-001")
    a.dispatch()
    assert runs(db) == []


async def test_thread_session_is_created_then_resumed(db, cfg, fake):
    a = Agents(db, cfg)
    add_thread(db)
    db.emit("affine", "thread.start", "go", key="t-001")
    await drain(a)
    argv0 = (fake / "calls" / "0.argv").read_text().splitlines()
    sid = argv0[argv0.index("--session-id") + 1]
    assert "--resume" not in argv0 and argv0[argv0.index("--model") + 1] == "claude-fable-5-1"
    t = db.one("SELECT * FROM threads")
    assert t["passes"] == 1 and t["session_id"] in (sid, "sess-0")
    db.emit("affine", "thread.continue", "again", key="t-001")
    await drain(a)
    argv1 = (fake / "calls" / "1.argv").read_text().splitlines()
    assert argv1[argv1.index("--resume") + 1] == t["session_id"]
    assert "resuming your own session" in (fake / "calls" / "1.prompt").read_text()
    assert "## Your charter" in (fake / "calls" / "0.prompt").read_text()


def argv_model(fake, n):
    argv = (fake / "calls" / f"{n}.argv").read_text().splitlines()
    return argv[argv.index("--model") + 1]


async def test_routine_checks_resume_the_thread_on_sonnet(db, cfg, fake):
    a = Agents(db, cfg)
    add_thread(db)
    db.emit("affine", "thread.start", "go", key="t-001")
    await drain(a)
    db.emit("affine", "job.check", "r001 running 2h", key="t-001")
    await drain(a)
    assert argv_model(fake, 1) == "claude-sonnet-5"
    argv1 = (fake / "calls" / "1.argv").read_text().splitlines()
    assert "--resume" in argv1                           # same mind, cheaper model
    db.emit("affine", "job.check", "r001 running 3h", key="t-001")
    db.emit("affine", "job.finished", "r001 exit 0", key="t-001")
    await drain(a)
    assert argv_model(fake, 2) == "claude-fable-5-1"     # anything real in the batch -> Fable
    assert [r["model"] for r in runs(db)] == ["claude-fable-5-1", "claude-sonnet-5", "claude-fable-5-1"]
    assert (fake / "calls" / "0.env").read_text().split()[-1] == "claude-sonnet-5"   # subagents


async def test_role_models(db, cfg, fake):
    a = Agents(db, cfg)
    cfg.project("affine").roles["analyst"].debounce_s = 0
    a._cursor("affine", "analyst", "")       # the analyst starts listening now
    db.emit("affine", "discord.request", "hi", key="901", payload={"id": "901", "content": "hi"})
    db.emit("affine", "tick.daily_report", "report")
    await drain(a)
    by_role = {r["role"]: r["model"] for r in runs(db)}
    assert by_role == {"concierge": "claude-sonnet-5", "analyst": "claude-sonnet-5"}
    db.emit("affine", "thread.claim", "t-001 beats the king", key="t-001")
    await drain(a)
    assert runs(db)[-1]["model"] == "claude-opus-5-5"   # red-teaming a claim is not routine


async def test_rate_limit_backoff_requeues(db, cfg, fake):
    (fake / "mode").write_text("ratelimit")
    a = Agents(db, cfg)
    db.emit("affine", "discord.request", "bob: hi", severity="normal", key="222", payload={"id": "222"})
    a.dispatch()
    a.launch()
    await asyncio.gather(*a.tasks.values())
    rr = runs(db)
    assert rr[0]["status"] == "ratelimited" and rr[1]["status"] == "queued"
    assert a.backoff_until() > now() + 200
    a.launch()           # paused
    assert runs(db)[1]["status"] == "queued"


async def test_slot_pools_threads_concierge_thinkers(db, cfg, fake):
    """Thinking roles fill their 2 slots; threads (3 slots) and the concierge (1) still start."""
    os.environ["FAKE_SLEEP"] = "0.3"
    try:
        a = Agents(db, cfg)
        for role in ("director", "scout", "analyst"):
            db.insert("agent_runs", project="affine", role=role, key="", status="queued", queued_at=now(), event_ids="[]")
        for i in range(4):
            add_thread(db, f"t-00{i}")
            db.insert("agent_runs", project="affine", role="thread", key=f"t-00{i}", status="queued",
                      queued_at=now(), event_ids="[]")
        for i in range(2):
            db.insert("agent_runs", project="affine", role="concierge", key=f"m{i}", status="queued",
                      queued_at=now(), event_ids="[]")
        db.insert("agent_runs", project="affine", role="lead", key="", status="queued", queued_at=now(),
                  event_ids="[]")                                    # a removed role: errored, not started
        a.launch()
        running = [r["role"] for r in runs(db) if r["status"] == "running"]
        assert running.count("thread") == 3 and running.count("concierge") == 1
        assert sorted(r for r in running if r not in ("thread", "concierge")) == ["analyst", "scout"]
        assert db.one("SELECT status FROM agent_runs WHERE role='lead'")["status"] == "error"
        await asyncio.gather(*a.tasks.values())
    finally:
        os.environ.pop("FAKE_SLEEP", None)


async def test_prompts_build_for_every_role(db, cfg, project, fake):
    a = Agents(db, cfg)
    add_thread(db)
    ev = db.one("SELECT * FROM events WHERE id=?", (db.emit("affine", "tick.daily_report", "x", payload={"id": "1"}),))
    for name, role in project.roles.items():
        key = "t-001" if name == "thread" else ("1" if name == "concierge" else "")
        text = a.user_prompt(project, role, key, [ev])
        sysp = a.system_prompt(project, role, project.work_dir / name)
        assert "{{" not in sysp, name
        assert name in text and len(text) > 100
    assert set(project.roles) == {"concierge", "scout", "thread", "analyst", "director"}
    assert project.roles["thread"].model == "claude-fable-5-1"
    assert {n: r.model for n, r in project.roles.items()} == {
        "concierge": "claude-sonnet-5", "scout": "claude-sonnet-5", "thread": "claude-fable-5-1",
        "analyst": "claude-opus-5-5", "director": "claude-opus-5-5"}


async def test_thread_pass_cost_is_the_delta_of_the_session_total(db, cfg, fake):
    a = Agents(db, cfg)
    add_thread(db)
    (fake / "cost").write_text("7.79")
    db.emit("affine", "thread.start", "go", key="t-001")
    await drain(a)
    (fake / "cost").write_text("7.95")             # claude reports the session's running total
    db.emit("affine", "thread.continue", "again", key="t-001")
    await drain(a)
    assert [round(r["cost_usd"], 2) for r in runs(db)] == [7.79, 0.16]
    t = db.one("SELECT * FROM threads")
    assert t["passes"] == 2 and t["session_passes"] == 2 and t["context_tokens"] == 1010


async def test_session_rotates_with_a_handover(db, cfg, fake, tmp_path):
    p = cfg.project("affine")
    p.rotate_context_tokens = 150_000
    a = Agents(db, cfg)
    add_thread(db)
    wd = p.work_dir / "threads" / "t-001"
    (fake / "ctx").write_text("200000")
    db.emit("affine", "thread.start", "go", key="t-001")
    await drain(a)
    t = db.one("SELECT * FROM threads")
    assert t["rotate_pending"] == 1 and t["context_tokens"] == 200010
    assert db.one("SELECT 1 FROM events WHERE topic='thread.log'")
    # the next pass resumes, is asked for a handover, and runs on the main model even for a routine check
    db.emit("affine", "job.check", "r001 running", key="t-001")
    await drain(a)
    argv1 = (fake / "calls" / "1.argv").read_text().splitlines()
    assert "--resume" in argv1 and argv_model(fake, 1) == "claude-fable-5-1"
    assert "last pass of this session" in (fake / "calls" / "1.prompt").read_text()
    t = db.one("SELECT * FROM threads")
    assert t["session_id"] is None and t["session_passes"] == 0 and t["generation"] == 2 and t["passes"] == 2
    assert t["rotate_pending"] == 0 and db.one("SELECT 1 FROM events WHERE topic='thread.rotated'")
    # the pass after that starts a fresh session seeded from the handover and recent reports
    wd.mkdir(parents=True, exist_ok=True)
    (wd / "HANDOVER.md").write_text("r007 is running on Pi_affine-01; best +0.12 sd")
    (fake / "ctx").write_text("30000")
    db.emit("affine", "thread.continue", "go on", key="t-001")
    await drain(a)
    argv2 = (fake / "calls" / "2.argv").read_text().splitlines()
    assert "--session-id" in argv2 and "--resume" not in argv2
    prompt = (fake / "calls" / "2.prompt").read_text()
    assert "generation 2" in prompt and "r007 is running on Pi_affine-01" in prompt
    assert "## Your last pass reports" in prompt and "reply from thread" in prompt
    t = db.one("SELECT * FROM threads")
    assert t["session_passes"] == 1 and t["generation"] == 2 and t["rotate_pending"] == 0


async def test_timed_out_handover_pass_does_not_rotate(db, cfg, fake):
    p = cfg.project("affine")
    a = Agents(db, cfg)
    add_thread(db, session="s-old", passes=3)
    db.x("UPDATE threads SET session_passes=3, rotate_pending=1, context_tokens=400000")
    db.x("INSERT INTO agent_runs(project, role, key, status, queued_at, event_ids) VALUES('affine','thread','t-001','queued',?,'[]')", (now(),))
    rid = db.one("SELECT id FROM agent_runs")["id"]
    a._after_thread_pass(p, "t-001", rid, ("s-old", True), {"total_cost_usd": 1.0}, "timeout", True)
    t = db.one("SELECT * FROM threads")
    assert t["session_id"] == "s-old" and t["rotate_pending"] == 1 and t["generation"] == 1
