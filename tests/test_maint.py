import json
import os
import shutil
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from lab import config as config_mod
from lab.daemon import Daemon
from lab.db import DB
from test_agents import drain
from test_daemon import FakeDiscord

ROOT = Path(__file__).resolve().parents[1]
OPERATOR = "5"

# fake `claude -p` for the maintainer: runs the shell snippet in $dir/action inside its worktree
FAKE = """#!/usr/bin/env bash
dir="$(dirname "$0")"
cat > "$dir/prompt"
pwd > "$dir/cwd"
[ -f "$dir/action" ] && sh -c "$(cat "$dir/action")"
echo "{\\"type\\":\\"result\\",\\"subtype\\":\\"success\\",\\"is_error\\":false,\\"result\\":\\"$(cat "$dir/reply" 2>/dev/null || echo changed it)\\",\\"session_id\\":\\"s\\",\\"total_cost_usd\\":0.1,\\"num_turns\\":3}"
"""


def sh(cwd, *cmd):
    return subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def executable(p: Path, text: str) -> Path:
    p.write_text(text)
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return p


@pytest.fixture
def lab(labdir):
    """The test lab as a git repo with a maintainer section; returns (cfg, daemon)."""
    executable(labdir / "fakeclaude", FAKE)
    executable(labdir / "fakedeploy", '#!/bin/sh\necho "$@" > "$(dirname "$0")/deployed"\n')
    with open(labdir / "lab.toml", "a") as f:
        f.write(textwrap.dedent(f"""
            [maintainer]
            operators = ["{OPERATOR}"]
            dir = "{labdir / 'wt'}"
            test_cmd = "test ! -f FAILS"
            deploy_cmd = ["{labdir / 'fakedeploy'}"]
        """))
    (labdir / ".gitignore").write_text("state/\nwt/\ncalls/\naction\nreply\ndeployed\nprompt\ncwd\n")
    sh(labdir, "git", "init", "-q", "-b", "master")
    sh(labdir, "git", "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A")
    sh(labdir, "git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
    cfg = config_mod.load(labdir / "lab.toml")
    d = Daemon(cfg)
    d.discord = FakeDiscord()
    d.bot_id = "77"
    return cfg, d


def msg(content, mid="900", author=OPERATOR, name="op"):
    return {"id": mid, "channel_id": "1543609865518317578", "content": f"<@77> {content}",
            "author": {"id": author, "username": name}, "mentions": [{"id": "77"}], "referenced_message": None,
            "attachments": []}


def outbox(db):
    return [r["content"] for r in db.all("SELECT content FROM outbox ORDER BY id")]


async def request(d, labdir, action, reply="changed it"):
    (labdir / "action").write_text(action)
    (labdir / "reply").write_text(reply)
    await d.on_message(msg("maint: make the report shorter"))
    await drain(d.agents)
    return d.maint.get("m-1")


async def test_only_operators_can_ask(lab):
    cfg, d = lab
    await d.on_message(msg("maint: remove the budget", author="6", name="mallory"))
    assert d.db.one("SELECT COUNT(*) n FROM maint")["n"] == 0
    assert "Only the lab's operators" in outbox(d.db)[-1]
    assert not d.db.one("SELECT 1 FROM events WHERE topic IN ('maint.request','discord.request')")
    await d.on_message(msg("maint approve m-1", mid="901", author="6"))
    assert "Only the lab's operators" in outbox(d.db)[-1]
    # a normal question is still the concierge's
    await d.on_message(msg("what's the maintenance plan?", mid="902", author="6"))
    assert d.db.one("SELECT 1 FROM events WHERE topic='discord.request'")


async def test_change_is_tested_and_deployed(lab):
    cfg, d = lab
    labdir = cfg.root
    r = await request(d, labdir, "echo shorter >> README.md && git add -A && git -c user.name=m -c user.email=m@m "
                                 "commit -qm 'shorter report'")
    assert r["status"] == "deploying", r["note"]
    assert json.loads(r["files"]) == ["README.md"]
    assert not (labdir / "wt" / "m-1").exists()                       # worktree gone, branch kept
    assert sh(labdir, "git", "log", "--oneline", "maint/m-1", "-1").endswith("shorter report")
    assert (labdir / "README.md").exists() is False                     # the live tree is untouched
    ack, ready = outbox(d.db)
    assert (labdir / "cwd").read_text().strip() == str(labdir / "wt" / "m-1")
    prompt = (labdir / "prompt").read_text()
    assert "make the report shorter" in prompt and "maint/m-1" in prompt and "read-only for you" in prompt
    assert "m-1** queued" in ack and "m-1 ready" in ready and "README.md" in ready and "Deploying" in ready
    await d.maint.tick()
    assert d.maint.get("m-1")["status"] == "restarting"
    assert (labdir / "deployed").read_text().split()[:2] == ["m-1", "maint/m-1"]
    d.maint.mark("m-1", "deployed", "merged as abc1234; labd restarted and is healthy.")
    assert d.maint.get("m-1")["status"] == "deployed"
    assert outbox(d.db)[-1].startswith("**m-1 deployed.**")
    assert d.db.kv_get("_lab", "agents_hold_until") == 0


async def test_deploy_waits_for_running_agents(lab):
    cfg, d = lab
    await request(d, cfg.root, "echo x >> README.md")                  # uncommitted: labd commits it
    assert d.maint.get("m-1")["status"] == "deploying"
    d.db.insert("agent_runs", project="affine", role="thread", key="t-001", status="running")
    await d.maint.tick()
    assert d.maint.get("m-1")["status"] == "deploying"                 # waits…
    assert d.db.kv_get("_lab", "agents_hold_until") > 0                # …and starts nothing new but chat
    d.db.insert("agent_runs", project="affine", role="director", key="", status="queued")
    d.agents.launch()
    assert d.db.one("SELECT status FROM agent_runs WHERE role='director'")["status"] == "queued"
    d.db.x("UPDATE agent_runs SET status='ok' WHERE role='thread'")
    await d.maint.tick()
    assert d.maint.get("m-1")["status"] == "restarting"


@pytest.mark.parametrize("action, why", [
    ("mkdir -p projects/affine/work && echo x > projects/affine/work/n.md", "research data"),
    ("echo 'key = \"sk-ant-api03-abcdefghijklmnopqrstuvwxyz\"' > k.py", "credential"),
    ("echo x >> README.md && touch FAILS", "tests fail"),
])
async def test_bad_changes_are_not_deployed(lab, action, why):
    cfg, d = lab
    r = await request(d, cfg.root, action)
    assert r["status"] == "failed" and why in outbox(d.db)[-1]


async def test_safety_changes_wait_for_approval(lab):
    cfg, d = lab
    r = await request(d, cfg.root, "sed -i 's/test_mode = true/test_mode = false/' projects/affine/project.toml")
    assert r["status"] == "awaiting_approval"
    assert "maint approve m-1" in outbox(d.db)[-1] and "test_mode" in outbox(d.db)[-1]
    await d.maint.tick()
    assert d.maint.get("m-1")["status"] == "awaiting_approval"
    await d.on_message(msg("maint approve m-1", mid="950"))
    assert d.maint.get("m-1")["status"] == "deploying" and "approved by op" in outbox(d.db)[-1]


async def test_no_change_posts_the_answer(lab):
    cfg, d = lab
    r = await request(d, cfg.root, "true", reply="Which report: the daily one or the brief?")
    assert r["status"] == "no_change"
    assert outbox(d.db)[-1] == "**m-1** (no change): Which report: the daily one or the brief?"


async def test_reject(lab):
    cfg, d = lab
    await request(d, cfg.root, "echo 'daily_usd = 6000' >> projects/affine/project.toml")
    await d.on_message(msg("maint reject m-1", mid="951"))
    assert d.maint.get("m-1")["status"] == "rejected"


# ---------------------------------------------------------------- bin/lab-deploy

FAKE_SYSTEMCTL = """#!/bin/sh
# restart: labd comes up healthy (fresh heartbeat) unless the merged tree contains a file called "broken"
if [ "$2" = restart ]; then
  [ -f "$LAB_HOME_T/broken" ] && exit 0
  "$LAB_HOME_T/.venv/bin/python" -c "import sqlite3,sys,json,time; c=sqlite3.connect(sys.argv[1]); c.execute(\\"INSERT OR REPLACE INTO kv VALUES('_lab','heartbeat',?)\\", (json.dumps(time.time()+100),)); c.commit()" "$DB_T"
fi
exit 0
"""


@pytest.fixture
def live(tmp_path):
    """A tiny live lab for bin/lab-deploy: git repo, fake bin/lab, fake systemctl/journalctl/uv."""
    home = tmp_path / "lab"
    (home / "bin").mkdir(parents=True)
    shutil.copy(ROOT / "bin" / "lab-deploy", home / "bin" / "lab-deploy")
    executable(home / "bin" / "lab", f'#!/bin/sh\n[ "$1" = maint ] && echo "$@" >> {tmp_path}/marks\nexit 0\n')
    (home / ".venv" / "bin").mkdir(parents=True)
    (home / ".venv" / "bin" / "python").symlink_to(sys.executable)
    (home / ".gitignore").write_text(".venv/\n")
    (home / "code.py").write_text("x = 1\n")
    fake = tmp_path / "fakebin"
    fake.mkdir()
    executable(fake / "systemctl", FAKE_SYSTEMCTL)
    executable(fake / "journalctl", "#!/bin/sh\necho 'Traceback: boom'\n")
    executable(fake / "uv", "#!/bin/sh\nexit 0\n")
    db = DB(tmp_path / "lab.db")
    db.kv_set("_lab", "heartbeat", 0)
    g = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]
    sh(home, "git", "init", "-q", "-b", "master")
    sh(home, *g, "add", "-A")
    sh(home, *g, "commit", "-qm", "init")

    def branch(name, path, text):
        sh(home, "git", "checkout", "-qb", name)
        (home / path).write_text(text)
        sh(home, *g, "add", "-A")
        sh(home, *g, "commit", "-qm", name)
        sh(home, "git", "checkout", "-q", "master")

    def deploy(name):
        env = dict(os.environ, PATH=f"{fake}:{os.environ['PATH']}", LAB_HOME_T=str(home), DB_T=str(tmp_path / "lab.db"),
                   LAB_DEPLOY_TRIES="2", LAB_DEPLOY_POLL_S="0.1")
        subprocess.run([str(home / "bin" / "lab-deploy"), "m-1", name, str(tmp_path / "lab.db")], env=env,
                       capture_output=True, text=True, timeout=60)
        return (tmp_path / "marks").read_text()

    return home, branch, deploy


def test_deploy_merges_and_restarts(live):
    home, branch, deploy = live
    branch("maint/good", "code.py", "x = 2\n")
    assert deploy("maint/good").startswith("maint mark m-1 deployed --text merged as")
    assert (home / "code.py").read_text() == "x = 2\n"


def test_deploy_rolls_back_when_labd_is_unhealthy(live):
    home, branch, deploy = live
    branch("maint/bad", "broken", "labd will not start\n")
    out = deploy("maint/bad")
    assert out.startswith("maint mark m-1 rolled_back") and "Traceback: boom" in out
    assert not (home / "broken").exists()                              # reverted…
    assert "Revert" in sh(home, "git", "log", "-1", "--format=%s")     # …as a new commit, history intact


def test_deploy_refuses_a_conflicting_merge(live):
    home, branch, deploy = live
    branch("maint/conflict", "code.py", "x = 3\n")
    (home / "code.py").write_text("x = 4\n")
    sh(home, "git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "live change")
    assert deploy("maint/conflict").startswith("maint mark m-1 failed --text merging maint/conflict")
    assert (home / "code.py").read_text() == "x = 4\n" and not (home / ".git" / "MERGE_HEAD").exists()


def test_status_lists_lab_changes_in_flight(db, cfg, project):
    from lab.context import status_text
    from lab.maint import Maint
    assert "## Lab changes" not in status_text(db, cfg, project)
    m = Maint(db, cfg)
    long = "make the daily report shorter and " + "x" * 200
    for text in ("done already", long, "add a knob"):
        m.request(project.name, author_id=OPERATOR, author="op", text=text)
    m._set("m-1", status="deployed")
    m._set("m-3", status="awaiting_approval")
    s = status_text(db, cfg, project)
    section = s.split("## Lab changes\n")[1].split("\n\n")[0]
    assert section.splitlines() == [f"- m-2 [queued] {long[:80]}", "- m-3 [awaiting_approval] add a knob"]
    m._set("m-2", status="rolled_back")
    m._set("m-3", status="rejected")
    assert "## Lab changes" not in status_text(db, cfg, project)
