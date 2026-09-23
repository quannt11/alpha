import asyncio
import json

from lab.daemon import Daemon
from lab.db import now


class FakeDiscord:
    def __init__(self):
        self.typed = []

    async def typing(self, ch):
        self.typed.append(ch)

    async def channel(self, cid):
        return {"id": cid, "type": 11, "parent_id": "1543609865518317578", "guild_id": "1480562634783723610"}


def msg(content, mid="900", author="5", mentions=("77",), ref=None, channel="1543609865518317578"):
    return {"id": mid, "channel_id": channel, "content": content, "author": {"id": author, "username": "alice"},
            "mentions": [{"id": m} for m in mentions], "referenced_message": ref, "attachments": []}


def make(cfg):
    d = Daemon(cfg)
    d.discord = FakeDiscord()
    d.bot_id = "77"
    return d


async def test_mention_becomes_request(cfg):
    d = make(cfg)
    await d.on_message(msg("<@77> how much did we spend today?"))
    ev = d.db.one("SELECT * FROM events WHERE topic='discord.request'")
    p = json.loads(ev["payload"])
    assert ev["key"] == "900" and p["content"] == "how much did we spend today?" and p["author_name"] == "alice"
    assert d.discord.typed == ["1543609865518317578"]
    # plain chatter is stored but does not wake anyone
    await d.on_message(msg("unrelated chatter", mid="901", mentions=()))
    assert d.db.one("SELECT COUNT(*) n FROM events WHERE topic='discord.request'")["n"] == 1
    assert d.db.one("SELECT COUNT(*) n FROM discord_messages")["n"] == 2


async def test_reply_to_bot_in_thread_is_request(cfg):
    d = make(cfg)
    await d.on_message(msg("and the epoch?", mid="902", mentions=(), channel="1999999999999999999",
                           ref={"id": "800", "author": {"id": "77"}, "content": "king is reign 21"}))
    ev = d.db.one("SELECT * FROM events WHERE topic='discord.request'")
    assert json.loads(ev["payload"])["reply_to_content"] == "king is reign 21"


async def test_other_channel_ignored(cfg):
    d = make(cfg)
    d.discord.channel = lambda cid: asyncio.sleep(0, result={"id": cid, "type": 0})
    await d.on_message(msg("<@77> hi", channel="123456789012345678"))
    assert d.db.one("SELECT COUNT(*) n FROM discord_messages")["n"] == 0


async def test_concierge_result_goes_to_outbox_as_reply(cfg):
    d = make(cfg)
    p = cfg.project("affine")
    d.db.emit("affine", "discord.request", "x", key="900", payload={"id": "900", "channel_id": p.channel_id})
    rid = d.db.insert("agent_runs", project="affine", role="concierge", key="900", status="ok", result="We spent $12.",
                      event_ids="[]")
    await d.on_result(p, p.roles["concierge"], "900", d.db.one("SELECT * FROM agent_runs WHERE id=?", (rid,)), {})
    o = d.db.one("SELECT * FROM outbox")
    assert o["content"] == "We spent $12." and o["reply_to"] == "900"


async def test_announcer_posts_major_only(cfg):
    d = make(cfg)
    d.db.kv_set("affine", "announce_cursor", d.db.max_event_id())
    d.db.emit("affine", "board.king", "new king: reign 22", severity="major")
    d.db.emit("affine", "board.verdict", "3 new verdicts", severity="info")
    d.db.emit("affine", "world.change.code", "code changed", severity="normal")
    task = asyncio.create_task(d.announce_loop())
    await asyncio.sleep(0.2)
    d.stop.set()
    await task
    posts = [r["content"] for r in d.db.all("SELECT content FROM outbox")]
    assert len(posts) == 1 and "reign 22" in posts[0]


async def test_scheduler_bootstrap_and_ticks(cfg):
    d = make(cfg)
    p = cfg.project("affine")
    (p.world_dir / "world.json").write_text("{}")
    d.db.kv_set("affine", "next_daily_report", now() - 1)
    task = asyncio.create_task(d.scheduler_loop())
    await asyncio.sleep(0.2)
    d.stop.set()
    await task
    topics = [r["topic"] for r in d.db.all("SELECT topic FROM events")]
    assert "tick.daily_report" in topics and "world.change.bootstrap" in topics


def _thread(d, tid="t-001"):
    d.db.insert("threads", id=tid, project="affine", title="x", status="active", created_at=now(), passes=1,
                workdir="/tmp")


def _run(d, tid="t-001", status="ok", started=None):
    rid = d.db.insert("agent_runs", project="affine", role="thread", key=tid, status=status,
                      started_at=started or now() - 5, event_ids="[]")
    return d.db.one("SELECT * FROM agent_runs WHERE id=?", (rid,))


async def test_thread_next_directives(cfg):
    d = make(cfg)
    p = cfg.project("affine")
    _thread(d)
    d.db.insert("results", project="affine", thread_id="t-001", ts=now(), metric="m", value=1, kept=1)
    d._continue_thread(p, "t-001", _run(d), "did a run\nNEXT: now")
    assert 0 < d.db.kv_get("affine", "wake:t-001") - now() <= 16
    d._continue_thread(p, "t-001", _run(d), "launched r002\nNEXT: wait")
    assert abs(d.db.kv_get("affine", "wake:t-001") - now() - 3600) < 5
    d._continue_thread(p, "t-001", _run(d), "nothing to do\nNEXT: sleep 30")
    assert abs(d.db.kv_get("affine", "wake:t-001") - now() - 1800) < 5


async def test_unproductive_passes_back_off_and_report_stall(cfg):
    d = make(cfg)
    p = cfg.project("affine")
    _thread(d)
    delays = []
    for _ in range(4):
        d._continue_thread(p, "t-001", _run(d, started=now() + 1), "thinking\nNEXT: now")
        delays.append(d.db.kv_get("affine", "wake:t-001") - now())
    assert delays[0] < delays[1] < delays[2] < delays[3] <= 3600
    assert d.db.one("SELECT 1 FROM events WHERE topic='thread.stalled' AND key='t-001'")


async def test_scheduler_fires_due_thread_wakes(cfg):
    d = make(cfg)
    _thread(d)
    d.db.kv_set("affine", "wake:t-001", now() - 1)
    task = asyncio.create_task(d.scheduler_loop())
    await asyncio.sleep(0.2)
    d.stop.set()
    await task
    ev = d.db.one("SELECT * FROM events WHERE topic='thread.continue'")
    assert ev and ev["key"] == "t-001" and d.db.kv_get("affine", "wake:t-001") == 0
    assert d.db.one("SELECT 1 FROM events WHERE topic='tick.director'")
