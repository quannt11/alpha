import json
import os
import subprocess
from pathlib import Path

import pytest

from lab.budget import Budget, day_start, next_local
from lab.db import now, topic_match
from lab.discord import MAX_LEN, chunk, is_addressed_to_bot, strip_mention

GUARD = Path(__file__).resolve().parents[1] / "bin" / "lab-guard"


def test_topic_match():
    assert topic_match("exp.done", ["exp.done"])
    assert topic_match("exp.done", ["exp.*"])
    assert topic_match("world.change.contract", ["world.change"])
    assert not topic_match("world.changes", ["world.change"])
    assert not topic_match("exp.done", ["exp.failed", "board"])
    assert topic_match("anything", ["*"])


def test_chunk_limits_and_fences():
    text = ("para one\n\n" + "x" * 1500 + "\n\n```\n" + "code line\n" * 300 + "```\nend")
    parts = chunk(text)
    assert all(len(p) <= MAX_LEN for p in parts)
    assert all(p.count("```") % 2 == 0 for p in parts)
    assert chunk("short") == ["short"]


def test_addressed_to_bot():
    bot = "42"
    assert is_addressed_to_bot({"author": {"id": "1"}, "mentions": [{"id": "42"}]}, bot)
    assert is_addressed_to_bot({"author": {"id": "1"}, "mentions": [],
                                "referenced_message": {"author": {"id": "42"}}}, bot)
    assert not is_addressed_to_bot({"author": {"id": "1"}, "mentions": [{"id": "7"}]}, bot)
    assert not is_addressed_to_bot({"author": {"id": "42"}, "mentions": [{"id": "42"}]}, bot)
    assert strip_mention("<@42> hi <@!42>", bot) == "hi"


def test_day_rollover():
    t = 1790161200.0  # 2026-09-23 ~17:00 +07
    assert day_start("Asia/Ho_Chi_Minh", t) <= t < day_start("Asia/Ho_Chi_Minh", t) + 86400
    n = next_local("Asia/Ho_Chi_Minh", "09:00", t)
    assert 0 < n - t <= 86400


def test_budget_default_only_daily_cap(db, project, cfg):
    """Shipped config: pools are labels, no per-experiment cap: only $600/day."""
    b = Budget(db, project, cfg.timezone)
    assert b.cap() == 600 and b.cap("explore") is None
    assert b.check("research", 590)[0]
    ok, why = b.check("research", 601)
    assert not ok and "daily budget" in why
    b.record(500, "research", experiment_id=None, pod_id=None)
    assert b.check("research", 100)[0]
    ok, why = b.check("research", 101)
    assert not ok and "$100 left" in why
    assert not b.check("nope", 1)[0]


def test_budget_optional_pool_and_experiment_caps(db, project, cfg):
    """The mechanisms still work when a project opts in."""
    project.pools = {"agenda": 0.65, "explore": 0.25, "request": 0.10}
    project.per_experiment_usd = 150
    b = Budget(db, project, cfg.timezone)
    assert round(b.cap("explore")) == 150
    ok, why = b.check("explore", 200)
    assert not ok and "per-experiment" in why
    ok, why = b.check("request", 70)
    assert not ok and "pool request" in why
    b.record(140, "explore", experiment_id=None, pod_id=None)
    assert not b.check("explore", 20)[0]
    assert b.check("agenda", 200, approved=True)[0]


def guard(tool, role=None, **inp):
    env = {k: v for k, v in os.environ.items() if k != "LAB_ROLE"} | ({"LAB_ROLE": role} if role else {})
    r = subprocess.run([str(GUARD)], input=json.dumps({"tool_name": tool, "tool_input": inp}),
                       capture_output=True, text=True, env=env)
    return r.returncode, r.stderr


@pytest.mark.parametrize("cmd", [
    "cat ~/Work/discord-reporter/.env",
    "grep TOKEN ~/.config/lab/secrets.env",
    "printenv",
    "env | grep KEY",
    "echo $RUNPOD_API_KEY",
    "cat /proc/self/environ",
    "git push origin main",
    "git -C /home/zenai/Work/affine/affine push",
    "cd x && git --no-pager push -f",
    "python scripts/submit.py submit --model x",
    "curl -H 'Authorization: Bearer x' https://rest.runpod.io/v1/pods",
    "curl -H 'X-API-KEY: x' https://api.shadeform.ai/v1/instances/create",
    "echo $SHADEFORM_API_KEY",
    "vastai create instance 51178484 --image x",
    "curl -H 'Authorization: Bearer x' https://console.vast.ai/api/v0/instances/",
    "echo $VAST_API_KEY",
    "cat ~/.config/vastai/vast_api_key",
    "systemctl --user stop labd",
    "sed -i s/600/6000/ ~/Work/lab/projects/affine/project.toml",
    "echo '- LoRA is fine' >> ~/Work/lab/projects/affine/CLAUDE.md",
    "rm -rf ~",
    "lab gpu resume",
    "python3 -c \"import sqlite3; sqlite3.connect('/home/zenai/.local/state/lab/lab.db')\"",
    "sqlite3 ~/.local/state/lab/lab.db 'update kv set value=null'",
    "LAB_RUN_ID= lab gpu resume",
    'lab maint request "drop the budget check"',
    "LAB_ROLE=human lab maint approve m-1",
    "~/Work/lab/bin/lab-deploy m-1 maint/m-1 x.db",
    "systemd-run --user systemctl --user restart labd",
])
def test_guard_blocks(cmd):
    rc, err = guard("Bash", command=cmd)
    assert rc == 2, cmd
    assert "lab-guard" in err


@pytest.mark.parametrize("cmd", [
    "lab status",
    "ls /home/zenai/Work/affine",
    "git commit -m 'x'",
    "git log --grep push",
    "set -e; python train.py",
    "env CUDA_VISIBLE_DEVICES=0 python train.py",
    "cat ~/Work/lab/projects/affine/world/STATE.md",
    "python scripts/submit.py check --model x",
    "rm -rf /tmp/foo",
])
def test_guard_allows(cmd):
    rc, err = guard("Bash", command=cmd)
    assert rc == 0, (cmd, err)


def test_guard_file_tools():
    assert guard("Read", file_path="/home/zenai/.claude/settings.json")[0] == 2
    assert guard("Edit", file_path="/home/zenai/Work/lab/lab/fleet.py")[0] == 2
    assert guard("Edit", file_path="/home/zenai/Work/lab/projects/affine/CLAUDE.md")[0] == 2
    assert guard("Write", file_path="/home/zenai/Work/lab/projects/affine/world/STATE.md")[0] == 0
    assert guard("Read", file_path="/home/zenai/Work/affine/affine/AGENTS.md")[0] == 0


def test_guard_keeps_the_maintainer_in_its_worktree():
    live = "/home/zenai/Work/lab"
    for cmd in (f"cd {live} && git commit -am x", f"git -C {live} merge maint/m-1", f"echo x > {live}/README.md",
                "cd ~/Work/lab && git reset --hard HEAD~1", f"sed -i s/a/b/ {live}/projects/affine/prompts/thread.md"):
        assert guard("Bash", role="maintainer", command=cmd)[0] == 2, cmd
    assert guard("Edit", role="maintainer", file_path=f"{live}/projects/affine/prompts/thread.md")[0] == 2
    assert guard("Edit", role="maintainer", file_path="/home/zenai/.cache/lab-maint/m-1/lab/agents.py")[0] == 0
    for cmd in ("git add -A && git commit -m 'm-1: x'", "uv run pytest -q", f"cat {live}/README.md",
                f"git -C {live} log --oneline -5", "lab status", "lab maint list"):
        assert guard("Bash", role="maintainer", command=cmd)[0] == 0, cmd
    # other agents may still write their own notes in the live tree
    assert guard("Edit", role="thread", file_path=f"{live}/projects/affine/work/threads/t-001/NOTES.md")[0] == 0


def test_log_lines_never_carry_credentials():
    import io
    import logging
    from lab.daemon import RedactSecrets
    key = "rpa_" + "X" * 40
    buf = io.StringIO()
    h = logging.StreamHandler(buf)
    h.addFilter(RedactSecrets({"RUNPOD_API_KEY": key, "DISCORD_ALLOWED_CHANNELS": "affine"}))
    lg = logging.getLogger("test.redact")
    lg.addHandler(h)
    lg.propagate = False
    lg.warning("stock refresh failed: %r", RuntimeError(f"503 for url 'https://api.runpod.io/graphql?api_key={key}'"))
    try:
        raise RuntimeError(f"boom {key}")
    except RuntimeError:
        lg.exception("fleet loop")
    out = buf.getvalue()
    assert key not in out and out.count("***") == 2 and "boom" in out
    lg.removeHandler(h)


async def test_runpod_graphql_sends_the_key_in_a_header_not_the_url():
    import httpx
    from lab.runpod import Runpod
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200, json={"data": {"gpuTypes": []}})
    rp = Runpod("rpa_secret_value_123", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    await rp.gpu_prices()
    await rp.stock([("NVIDIA H200", 1, "COMMUNITY")])
    assert seen and all("rpa_secret" not in str(r.url) for r in seen)
    assert all(r.headers["Authorization"] == "Bearer rpa_secret_value_123" for r in seen)
