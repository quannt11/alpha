"""labd — the always-on control plane.

It holds every credential, never calls an LLM itself, and runs these loops:
  sentinel   poll external sources, diff, emit events, rebuild world.json
  agents     dispatch events to roles, launch `claude -p` runs by priority
  fleet      schedule experiments, grant/release GPU leases, bill, reap, watchdog
  discord    gateway listener (mentions / replies) + outbox sender + announcer
  scheduler  daily report, director tick, thread continuations, bootstrap
  threads    keep every research thread's loop going: NEXT: now | wait | sleep N
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import signal
import sys
import time

from . import config as config_mod
from .agents import Agents
from .budget import next_local
from .context import live_world_version, state_version
from .db import DB, now, topic_match
from .discord import Destination, DiscordError, DiscordREST, Gateway, is_addressed_to_bot, strip_mention
from .fleet import Fleet
from .maint import Maint
from .runpod import Runpod
from .shadeform import Shadeform
from .vast import Vast
from .sentinel import Sentinel, load_plugin

log = logging.getLogger("labd")

ANNOUNCE = {
    "board.king": "**New king on the board.** {summary}",
    "board.audit": "**Exploit audit:** {summary}",
    "board.crown_expiring": "**Our crown is about to stop earning.** {summary}",
    "budget.alert": "**Budget:** {summary}",
    "agent.ratelimited": "**Agents paused:** {summary}",
    "sentinel.error": "**Sentinel:** {summary}",
    "operator.pause": "**GPUs paused:** {summary}",
    "operator.resume": "**GPUs resumed:** {summary}",
    "thread.start": "**New research thread:** {summary}",
    "thread.retired": "**Thread retired:** {summary}",
    "thread.claim": "**Claimed result (the Analyst will check it):** {summary}",
    "thread.stalled": "**Thread looks stuck:** {summary}",
    "world.change": "**Heads-up — the rules/world changed:** {summary}\nThe Scout is writing a brief.",
}


class Daemon:
    def __init__(self, cfg: config_mod.LabConfig):
        self.cfg = cfg
        self.db = DB(cfg.db_path)
        self.secrets = config_mod.load_secrets(cfg)
        self.stop = asyncio.Event()
        self.discord: DiscordREST | None = None
        self.gateway: Gateway | None = None
        self.bot_id: str | None = None
        self.runpod = Runpod(self.secrets["RUNPOD_API_KEY"]) if self.secrets.get("RUNPOD_API_KEY") else None
        self.shadeform = (Shadeform(self.secrets["SHADEFORM_API_KEY"], gpu_map=cfg.shadeform.get("gpu_map"))
                          if self.secrets.get("SHADEFORM_API_KEY") else None)
        self.vast = Vast(self.secrets["VAST_API_KEY"], cfg=cfg.vast) if self.secrets.get("VAST_API_KEY") else None
        self.sentinels: dict[str, Sentinel] = {}
        self.fleets: dict[str, Fleet] = {}
        self.agents = Agents(self.db, cfg, on_result=self.on_result)
        self.maint = Maint(self.db, cfg)
        self.thread_parent: dict[str, str | None] = {}
        self.thread_wait_check_s = 3600
        for p in cfg.projects.values():
            plugin_path = p.dir / "plugin.py"
            if plugin_path.exists():
                self.sentinels[p.name] = Sentinel(self.db, p, load_plugin(plugin_path))
            self.fleets[p.name] = Fleet(self.db, cfg, p, self.runpod, notify=self._notifier(p),
                                        shadeform=self.shadeform, vast=self.vast)
            for d in (p.world_dir, p.work_dir):
                d.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------ discord out
    def _notifier(self, p):
        async def notify(text: str, reply_to: str | None = None, channel_id: str | None = None):
            self.say(p.name, text, reply_to=reply_to, channel_id=channel_id, role="labd")
        return notify

    def say(self, project: str, text: str, *, reply_to: str | None = None, channel_id: str | None = None,
            role: str = "labd") -> int:
        p = self.cfg.project(project)
        return self.db.insert("outbox", project=project, channel_id=channel_id or p.channel_id, content=text,
                              reply_to=reply_to, files=None, status="pending", created_at=now(), author_role=role)

    async def outbox_loop(self):
        while not self.stop.is_set():
            if self.discord:
                for r in self.db.all("SELECT * FROM outbox WHERE status='pending' ORDER BY id LIMIT 10"):
                    p = self.cfg.projects.get(r["project"])
                    if not p or not p.channel_id:
                        self.db.update("outbox", "id=?", (r["id"],), status="failed", error="project has no channel")
                        continue
                    try:
                        files = json.loads(r["files"]) if r["files"] else None
                        ids = await self.discord.send(Destination(p.guild_id, p.channel_id), r["channel_id"] or p.channel_id,
                                                      r["content"], reply_to=r["reply_to"], files=files)
                        self.db.update("outbox", "id=?", (r["id"],), status="sent", sent_at=now(),
                                       message_ids=json.dumps(ids))
                        for i, mid in enumerate(ids):
                            self.db.x("INSERT OR IGNORE INTO discord_messages(id,project,channel_id,author_id,author_name,"
                                      "is_bot,content,reply_to,ts) VALUES(?,?,?,?,?,?,?,?,?)",
                                      (mid, p.name, r["channel_id"], self.bot_id, f"lab/{r['author_role']}", 1,
                                       r["content"][:4000] if i == 0 else "(continued)", r["reply_to"], now()))
                    except DiscordError as e:
                        status = "failed" if (e.status or 0) in (400, 401, 403, 404) else "unknown"
                        if status == "unknown" and "network" in str(e):
                            status = "pending" if (now() - r["created_at"]) < 600 else "unknown"
                        self.db.update("outbox", "id=?", (r["id"],), status=status, error=str(e)[:500])
                        log.warning("outbox %s: %s", r["id"], e)
                    except Exception as e:
                        self.db.update("outbox", "id=?", (r["id"],), status="failed", error=repr(e)[:500])
                        log.exception("outbox send failed")
            await self._sleep(2)

    async def announce_loop(self):
        for p in self.cfg.projects.values():
            if self.db.kv_get(p.name, "announce_cursor") is None:
                self.db.kv_set(p.name, "announce_cursor", self.db.max_event_id())
        while not self.stop.is_set():
            for p in self.cfg.projects.values():
                cur = int(self.db.kv_get(p.name, "announce_cursor", 0) or 0)
                rows = self.db.all("SELECT * FROM events WHERE project=? AND id>? ORDER BY id LIMIT 200", (p.name, cur))
                for r in rows:
                    tmpl = next((t for k, t in ANNOUNCE.items() if topic_match(r["topic"], [k])), None)
                    loud = r["severity"] == "major" or r["topic"] in (
                        "agent.ratelimited", "sentinel.error", "thread.start", "thread.retired", "thread.stalled")
                    if tmpl and loud:
                        self.say(p.name, tmpl.format(summary=r["summary"]))
                if rows:
                    self.db.kv_set(p.name, "announce_cursor", rows[-1]["id"])
            await self._sleep(5)

    # ------------------------------------------------------------ discord in
    async def _project_for_channel(self, channel_id: str):
        for p in self.cfg.projects.values():
            if channel_id == p.channel_id:
                return p
        if channel_id not in self.thread_parent:
            try:
                ch = await self.discord.channel(channel_id)
                self.thread_parent[channel_id] = str(ch.get("parent_id")) if ch.get("type") in (10, 11, 12) else None
            except DiscordError:
                self.thread_parent[channel_id] = None
        parent = self.thread_parent[channel_id]
        return next((p for p in self.cfg.projects.values() if parent and parent == p.channel_id), None)

    async def on_ready(self, d):
        self.bot_id = str(d["user"]["id"])

    async def on_message(self, m: dict):
        p = await self._project_for_channel(str(m.get("channel_id")))
        if not p:
            return
        author = m.get("author") or {}
        ref = m.get("referenced_message") or {}
        name = (m.get("member") or {}).get("nick") or author.get("global_name") or author.get("username") or "?"
        self.db.x("INSERT OR IGNORE INTO discord_messages(id,project,channel_id,author_id,author_name,is_bot,content,"
                  "reply_to,ts) VALUES(?,?,?,?,?,?,?,?,?)",
                  (m["id"], p.name, m["channel_id"], author.get("id"), name, int(bool(author.get("bot"))),
                   m.get("content", ""), ref.get("id"), now()))
        if not self.bot_id or not is_addressed_to_bot(m, self.bot_id) or author.get("bot"):
            return
        text = strip_mention(m.get("content", ""), self.bot_id)
        # "maint: …" / "maint approve m-N": operators changing the lab itself (checked here, in code)
        if self.maint.handle_message(p, author_id=author.get("id"), author=name, text=text, message_id=m["id"],
                                     channel_id=m["channel_id"], context=ref.get("content") if ref else None):
            return
        payload ={"id": m["id"], "channel_id": m["channel_id"], "author_id": author.get("id"), "author_name": name,
                   "content": text, "reply_to": ref.get("id"), "reply_to_content": ref.get("content") if ref else None,
                   "attachments": [a.get("url") for a in m.get("attachments", [])]}
        self.db.emit(p.name, "discord.request", f"{name}: {text[:300]}", severity="normal", key=m["id"],
                     payload=payload)
        await self.discord.typing(m["channel_id"])

    # ------------------------------------------------------------ agent results
    async def on_result(self, p, role, key, run, res):
        text = (run["result"] or "").strip()
        if role.name == "concierge":
            req = self.db.one("SELECT payload FROM events WHERE topic='discord.request' AND key=? ORDER BY id DESC",
                              (key,))
            payload = json.loads(req["payload"]) if req else {}
            if run["status"] != "ok" or not text:
                text = ("Sorry — I hit an internal error answering that "
                        f"(run {run['id']}, {run['status']}). I've logged it; please try again in a bit.")
            if text.strip() != "NO_REPLY":
                reply_to = None if payload.get("simulated") else key
                self.say(p.name, text, reply_to=reply_to, channel_id=payload.get("channel_id"), role="concierge")
        if role.name == "thread":
            self._continue_thread(p, key, run, text)
        if role.name == "maintainer":
            await self.maint.finish(p, key, run, text)
        if role.name in ("scout", "director", "analyst", "thread"):
            await self._commit_state(p, f"{role.name} run {run['id']}: {text.splitlines()[0][:80] if text else run['status']}")
        if role.name == "analyst":
            ids = json.loads(run["event_ids"] or "[]")
            if ids and self.db.one(f"SELECT 1 FROM events WHERE topic='tick.daily_report' AND id IN "
                                   f"({','.join('?' * len(ids))})", ids):
                self.db.kv_set(p.name, "last_daily_report", now())

    NEXT_RE = re.compile(r"^\s*NEXT:\s*(now|wait|sleep\s+(\d+))", re.I | re.M)

    def _continue_thread(self, p, tid: str, run, text: str) -> None:
        """Autoresearch loop: decide when this thread's next pass runs.
        NEXT: now → right away; NEXT: wait → when its job/lease reports (plus a safety check);
        NEXT: sleep N → in N minutes. A pass that changed nothing backs off and, after several,
        tells the Director the thread is stalled."""
        t = self.db.one("SELECT * FROM threads WHERE id=?", (tid,))
        if not t or t["status"] != "active":
            return
        started = run["started_at"] or now()
        productive = bool(
            self.db.one("SELECT 1 FROM results WHERE thread_id=? AND ts>=?", (tid, started))
            or self.db.one("SELECT 1 FROM events WHERE key=? AND ts>=? AND topic IN ('job.launched','thread.claim',"
                           "'gpu.extended')", (tid, started))
            or self.db.one("SELECT 1 FROM leases WHERE COALESCE(holder, experiment_id)=? AND requested_at>=?",
                           (tid, started)))
        streak = 0 if productive else int(self.db.kv_get(p.name, f"idle_streak:{tid}", 0) or 0) + 1
        self.db.kv_set(p.name, f"idle_streak:{tid}", streak)
        job_running = self.db.one("SELECT 1 FROM leases WHERE COALESCE(holder, experiment_id)=? AND status IN "
                                  "('requested','provisioning','granted') AND COALESCE(job_state,'')='running'", (tid,))
        m = self.NEXT_RE.findall(text or "")
        mode, mins = (m[-1][0].lower().split()[0], m[-1][1]) if m else ("wait" if job_running else "now", "")
        if run["status"] != "ok":
            mode, mins = "sleep", "5"
        if mode == "sleep":
            delay = max(1, min(int(mins or 30), 24 * 60)) * 60
        elif mode == "wait":
            delay = self.thread_wait_check_s          # safety net: look again even if nothing reports
        else:
            delay = 15 if streak == 0 else min(60 * 2 ** (streak - 1), 3600)
        self.db.kv_set(p.name, f"wake:{tid}", now() + delay)
        if streak and streak % 4 == 0:
            self.db.emit(p.name, "thread.stalled", f"{tid}: {streak} passes in a row without a result, job or "
                         f"lease — it may be stuck", severity="normal", key=tid)

    async def _commit_state(self, p, message: str):
        """Commit World State and agent notes to the lab repo: a history of what the lab believed and when."""
        if not (self.cfg.root / ".git").exists():
            return
        paths = [str(p.world_dir.relative_to(self.cfg.root)), str(p.work_dir.relative_to(self.cfg.root))]

        async def git(*args):
            proc = await asyncio.create_subprocess_exec("git", "-C", str(self.cfg.root), *args,
                                                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            out, err = await proc.communicate()
            return proc.returncode, (out + err).decode(errors="replace")

        await git("add", "--", *paths)
        rc, _ = await git("diff", "--cached", "--quiet")
        if rc == 1:
            rc, out = await git("commit", "-q", "-m", f"[{p.name}] {message}")
            if rc:
                log.warning("state commit failed: %s", out[:300])

    # ------------------------------------------------------------ loops
    async def _sleep(self, s: float):
        try:
            await asyncio.wait_for(self.stop.wait(), timeout=s)
        except asyncio.TimeoutError:
            pass

    async def agents_loop(self):
        self.agents.recover()
        while not self.stop.is_set():
            try:
                self.agents.dispatch()
                self.agents.launch()
            except Exception:
                log.exception("agents loop")
            await self._sleep(2)

    async def maint_loop(self):
        while not self.stop.is_set():
            try:
                await self.maint.tick()
            except Exception:
                log.exception("maint loop")
            await self._sleep(5)

    async def fleet_loop(self):
        last_rec = last_wd = 0.0
        while not self.stop.is_set():
            for f in self.fleets.values():
                try:
                    await f.enforce_pause()
                    await f.refresh_stock()
                    await f.process_leases()
                    if time.monotonic() - last_rec > 60:
                        await f.reconcile()
                    if time.monotonic() - last_wd > 120:
                        await f.watchdog()
                except Exception:
                    log.exception("fleet loop (%s)", f.p.name)
            if time.monotonic() - last_rec > 60:
                last_rec = time.monotonic()
            if time.monotonic() - last_wd > 120:
                last_wd = time.monotonic()
            await self._sleep(5)

    async def scheduler_loop(self):
        tz = self.cfg.timezone
        while not self.stop.is_set():
            t = now()
            for p in self.cfg.projects.values():
                n = p.name
                nr = self.db.kv_get(n, "next_daily_report") or next_local(tz, p.report_time)
                if t >= nr:
                    self.db.emit(n, "tick.daily_report", "time for the daily report", severity="normal")
                    nr = next_local(tz, p.report_time, t + 60)
                self.db.kv_set(n, "next_daily_report", nr)
                nl = self.db.kv_get(n, "next_director") or t
                if t >= nl:
                    self.db.emit(n, "tick.director", "periodic portfolio review", severity="normal")
                    nl = t + p.lead_tick_hours * 3600
                self.db.kv_set(n, "next_director", nl)
                # research threads: fire due continuations
                for th in self.db.all("SELECT id FROM threads WHERE project=? AND status='active'", (n,)):
                    due = float(self.db.kv_get(n, f"wake:{th['id']}", 0) or 0)
                    if due and t >= due:
                        self.db.kv_set(n, f"wake:{th['id']}", 0)
                        self.db.emit(n, "thread.continue", f"{th['id']}: next pass", key=th["id"])
                # bootstrap: first world.json → ask the Scout for the first STATE.md
                if (p.world_dir / "world.json").exists() and not (p.world_dir / "STATE.md").exists() \
                        and not self.db.kv_get(n, "bootstrapped"):
                    self.db.kv_set(n, "bootstrapped", t)
                    self.db.emit(n, "world.change.bootstrap", "no STATE.md yet: write the first World State",
                                 severity="normal")
                self._resync_world(p, t)
            self.db.kv_set("_lab", "heartbeat", t)
            await self._sleep(10)

    RESYNC_AFTER_S, RESYNC_EVERY_S = 600, 1800

    def _resync_world(self, p, t: float) -> None:
        """STATE.md must follow world.json. If it stays behind (a Scout run left the version line old, failed,
        or the change was too minor to wake it), wake the Scout again — at most every 30 minutes."""
        n, live, st = p.name, live_world_version(p), state_version(p)
        if not live or not (p.world_dir / "STATE.md").exists() or st == live:
            self.db.kv_set(n, "world_lag_since", 0)
            return
        since = float(self.db.kv_get(n, "world_lag_since", 0) or 0) or t
        self.db.kv_set(n, "world_lag_since", since)
        if t - since < self.RESYNC_AFTER_S or t - float(self.db.kv_get(n, "world_resync_at", 0) or 0) < self.RESYNC_EVERY_S:
            return
        if self.db.one("SELECT 1 FROM agent_runs WHERE project=? AND role='scout' AND status IN ('queued','running')", (n,)):
            return
        self.db.kv_set(n, "world_resync_at", t)
        self.db.emit(n, "world.change.resync", f"STATE.md describes {st or 'no stated version'} but the live world is "
                     f"{live}: bring World State up to date", severity="normal",
                     payload={"state_version": st, "live_version": live})

    async def discord_boot(self):
        token = self.secrets.get("DISCORD_BOT_TOKEN")
        if not token:
            log.warning("no DISCORD_BOT_TOKEN: Discord disabled")
            return
        try:
            self.discord = DiscordREST(token)
            me = await self.discord.whoami()
            self.bot_id = str(me["id"])
            for p in self.cfg.projects.values():
                if p.channel_id:
                    await self.discord.check_destination(Destination(p.guild_id, p.channel_id), p.channel_id)
        except DiscordError as e:
            # A Discord problem must not take the rest of the lab down: run without it and say so.
            log.error("Discord disabled: %s", e)
            for p in self.cfg.projects.values():
                self.db.emit(p.name, "labd.discord_error", f"Discord disabled at boot: {e}", severity="major")
            self.discord = None
            return
        self.gateway = Gateway(token, self.on_message, self.on_ready)

    async def run(self):
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self.stop.set)
        await self.discord_boot()
        tasks = [asyncio.create_task(c) for c in (
            self.agents_loop(), self.fleet_loop(), self.scheduler_loop(), self.outbox_loop(), self.announce_loop(),
            self.maint_loop())]
        tasks += [asyncio.create_task(s.run(self.stop)) for s in self.sentinels.values()]
        if self.gateway:
            tasks.append(asyncio.create_task(self.gateway.run()))
        for p in self.cfg.projects.values():
            self.db.emit(p.name, "labd.started", f"labd started (discord={'on' if self.discord else 'off'}, "
                         f"runpod={'on' if self.runpod else 'off'}, shadeform={'on' if self.shadeform else 'off'}, vast={'on' if self.vast else 'off'})",
                         severity="info")
        log.info("labd running: projects=%s discord=%s runpod=%s shadeform=%s vast=%s", list(self.cfg.projects),
                 bool(self.discord), bool(self.runpod), bool(self.shadeform), bool(self.vast))
        await self.stop.wait()
        log.info("labd stopping")
        if self.gateway:
            self.gateway.stop()
        await self.agents.shutdown()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="labd")
    ap.add_argument("--config")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    cfg = config_mod.load(a.config)
    asyncio.run(Daemon(cfg).run())


if __name__ == "__main__":
    main()
