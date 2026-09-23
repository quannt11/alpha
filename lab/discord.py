"""Discord: REST sender (with pinned destinations) and a Gateway listener.

Safety habits borrowed from affine/ops/discord_channel: every destination is
checked against the project's pinned guild + channel (or a thread whose parent
is that channel), @everyone/role pings are disabled, and a send whose outcome
is unknown is not retried blindly.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

import httpx
import websockets

log = logging.getLogger("lab.discord")

API = "https://discord.com/api/v10"
GATEWAY_URL = "wss://gateway.discord.gg/?v=10&encoding=json"
MAX_LEN = 2000

# GUILDS | GUILD_MESSAGES | MESSAGE_CONTENT
INTENTS = (1 << 0) | (1 << 9) | (1 << 15)


class DiscordError(Exception):
    def __init__(self, msg: str, status: int | None = None):
        super().__init__(msg)
        self.status = status


def chunk(text: str, limit: int = MAX_LEN) -> list[str]:
    """Split on paragraph, then line, then hard boundaries; keep code fences balanced."""
    text = text.strip() or "(empty)"
    if len(text) <= limit:
        return [text]
    out: list[str] = []
    budget = limit - 8  # room to close/reopen a fence
    rest = text
    while rest:
        if len(rest) <= limit:
            out.append(rest)
            break
        cut = rest.rfind("\n\n", 0, budget)
        if cut < budget // 2:
            cut = rest.rfind("\n", 0, budget)
        if cut < budget // 2:
            cut = rest.rfind(" ", 0, budget)
        if cut <= 0:
            cut = budget
        piece, rest = rest[:cut].rstrip(), rest[cut:].lstrip("\n")
        if piece.count("```") % 2 == 1:
            piece += "\n```"
            rest = "```\n" + rest
        out.append(piece)
    return out


@dataclass
class Destination:
    guild_id: str
    channel_id: str


class DiscordREST:
    def __init__(self, token: str, client: httpx.AsyncClient | None = None):
        if not token:
            raise DiscordError("no Discord bot token configured")
        self.token = token
        self.client = client or httpx.AsyncClient(timeout=30)
        self._channel_cache: dict[str, dict] = {}
        self.me: dict | None = None

    async def request(self, method: str, route: str, *, json_body=None, files=None, data=None,
                      retries: int = 4):
        headers = {"Authorization": f"Bot {self.token}",
                   "User-Agent": "DiscordBot (https://github.com/local/lab, 0.1)"}
        for attempt in range(retries + 1):
            try:
                r = await self.client.request(method, API + route, headers=headers, json=json_body,
                                              files=files, data=data)
            except httpx.HTTPError as e:
                if method == "GET" and attempt < retries:
                    await asyncio.sleep(2 ** attempt)
                    continue
                raise DiscordError(f"network error: {e}") from None
            if r.status_code == 429 and attempt < retries:
                try:
                    delay = float(r.json().get("retry_after", 1))
                except ValueError:
                    delay = 1.0
                await asyncio.sleep(min(delay, 60) + 0.1)
                continue
            if r.status_code >= 500 and attempt < retries and method == "GET":
                await asyncio.sleep(2 ** attempt)
                continue
            if r.status_code >= 400:
                raise DiscordError(f"Discord {r.status_code}: {r.text[:300]}", r.status_code)
            return r.json() if r.content else None
        raise DiscordError("retries exhausted")

    async def whoami(self) -> dict:
        if self.me is None:
            self.me = await self.request("GET", "/users/@me")
        return self.me

    async def channel(self, channel_id: str) -> dict:
        if channel_id not in self._channel_cache:
            self._channel_cache[channel_id] = await self.request("GET", f"/channels/{channel_id}")
        return self._channel_cache[channel_id]

    async def check_destination(self, dest: Destination, channel_id: str) -> None:
        """Refuse anything that is not the pinned channel or one of its threads."""
        if not re.fullmatch(r"\d{15,22}", channel_id or ""):
            raise DiscordError(f"invalid channel id {channel_id!r}")
        if channel_id == dest.channel_id:
            ch = await self.channel(channel_id)
            if str(ch.get("guild_id")) != dest.guild_id:
                raise DiscordError("pinned channel is not in the pinned guild")
            return
        ch = await self.channel(channel_id)
        if not (str(ch.get("guild_id")) == dest.guild_id and str(ch.get("parent_id")) == dest.channel_id
                and ch.get("type") in (10, 11, 12)):
            raise DiscordError(f"channel {channel_id} is not the project channel or one of its threads")

    async def send(self, dest: Destination, channel_id: str, content: str, *,
                   reply_to: str | None = None, files: list[str] | None = None) -> list[str]:
        await self.check_destination(dest, channel_id)
        parts = chunk(content)
        ids: list[str] = []
        for i, part in enumerate(parts):
            payload: dict = {"content": part,
                             "allowed_mentions": {"parse": ["users"], "replied_user": True}}
            if reply_to and i == 0:
                payload["message_reference"] = {"message_id": reply_to, "fail_if_not_exists": False}
            last = i == len(parts) - 1
            if files and last:
                multipart = {}
                attachments = []
                for j, f in enumerate(files):
                    p = Path(f)
                    multipart[f"files[{j}]"] = (p.name, p.read_bytes())
                    attachments.append({"id": j, "filename": p.name})
                payload["attachments"] = attachments
                msg = await self.request("POST", f"/channels/{channel_id}/messages", files=multipart,
                                         data={"payload_json": json.dumps(payload)}, retries=0)
            else:
                msg = await self.request("POST", f"/channels/{channel_id}/messages", json_body=payload,
                                         retries=2)
            ids.append(msg["id"])
        return ids

    async def history(self, channel_id: str, limit: int = 50, after: str | None = None) -> list[dict]:
        q = f"?limit={min(limit, 100)}" + (f"&after={after}" if after else "")
        msgs = await self.request("GET", f"/channels/{channel_id}/messages{q}")
        return list(reversed(msgs))

    async def typing(self, channel_id: str) -> None:
        try:
            await self.request("POST", f"/channels/{channel_id}/typing", retries=0)
        except DiscordError:
            pass

    async def aclose(self):
        await self.client.aclose()


# ---------------------------------------------------------------------------
# Gateway


def is_addressed_to_bot(msg: dict, bot_id: str) -> bool:
    """A message is a request when it @mentions the bot or replies to one of its messages."""
    if str(msg.get("author", {}).get("id")) == bot_id:
        return False
    if any(str(m.get("id")) == bot_id for m in msg.get("mentions", []) or []):
        return True
    ref = msg.get("referenced_message") or {}
    if str((ref.get("author") or {}).get("id")) == bot_id:
        return True
    return False


def strip_mention(content: str, bot_id: str) -> str:
    return re.sub(rf"<@!?{bot_id}>", "", content or "").strip()


class Gateway:
    """Minimal resilient Gateway client: identify, heartbeat, resume, reconnect."""

    def __init__(self, token: str, on_message: Callable[[dict], Awaitable[None]],
                 on_ready: Callable[[dict], Awaitable[None]] | None = None):
        self.token = token
        self.on_message = on_message
        self.on_ready = on_ready
        self.session_id: str | None = None
        self.resume_url: str | None = None
        self.seq: int | None = None
        self.connected = False
        self._stop = asyncio.Event()

    def stop(self):
        self._stop.set()

    async def run(self):
        backoff = 1.0
        while not self._stop.is_set():
            url = (self.resume_url + "/?v=10&encoding=json") if (self.resume_url and self.session_id) else GATEWAY_URL
            try:
                async with websockets.connect(url, max_size=2 ** 24, ping_interval=None) as ws:
                    backoff = 1.0
                    await self._session(ws)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # network, close codes
                code = getattr(e, "code", None) or getattr(getattr(e, "rcvd", None), "code", None)
                log.warning("gateway disconnected: %r (code=%s)", e, code)
                if code in (4004, 4010, 4011, 4012, 4013, 4014):
                    log.error("gateway fatal close %s (bad token or missing intents); stopping", code)
                    self.connected = False
                    raise
                if code in (4007, 4009):
                    self.session_id = None
            self.connected = False
            await asyncio.sleep(backoff + random.random())
            backoff = min(backoff * 2, 60)

    async def _session(self, ws):
        hello = json.loads(await ws.recv())
        interval = hello["d"]["heartbeat_interval"] / 1000
        acked = True

        async def heartbeat():
            nonlocal acked
            await asyncio.sleep(interval * random.random())
            while True:
                if not acked:
                    await ws.close(4000)
                    return
                acked = False
                await ws.send(json.dumps({"op": 1, "d": self.seq}))
                await asyncio.sleep(interval)

        hb = asyncio.create_task(heartbeat())
        try:
            if self.session_id:
                await ws.send(json.dumps({"op": 6, "d": {"token": self.token, "session_id": self.session_id,
                                                          "seq": self.seq}}))
            else:
                await ws.send(json.dumps({"op": 2, "d": {
                    "token": self.token, "intents": INTENTS,
                    "properties": {"os": "linux", "browser": "lab", "device": "lab"}}}))
            async for raw in ws:
                if self._stop.is_set():
                    return
                ev = json.loads(raw)
                op = ev.get("op")
                if ev.get("s") is not None:
                    self.seq = ev["s"]
                if op == 11:
                    acked = True
                elif op == 1:
                    await ws.send(json.dumps({"op": 1, "d": self.seq}))
                elif op == 7:
                    return  # reconnect + resume
                elif op == 9:
                    if not ev.get("d"):
                        self.session_id = None
                    await asyncio.sleep(1 + random.random() * 4)
                    return
                elif op == 0:
                    t, d = ev.get("t"), ev.get("d")
                    if t == "READY":
                        self.session_id = d["session_id"]
                        self.resume_url = d.get("resume_gateway_url")
                        self.connected = True
                        log.info("gateway ready as %s", d.get("user", {}).get("username"))
                        if self.on_ready:
                            await self.on_ready(d)
                    elif t == "RESUMED":
                        self.connected = True
                        log.info("gateway resumed")
                    elif t == "MESSAGE_CREATE":
                        try:
                            await self.on_message(d)
                        except Exception:
                            log.exception("on_message failed")
        finally:
            hb.cancel()
