"""Sentinel: deterministic change detection over external sources (no LLM).

A project plugin declares Sources. Each poll returns a flat mapping of
fact-key -> value. The engine diffs it against the stored facts and emits
events; the first poll of a source only records a baseline. After any change
the plugin rebuilds world.json (the machine-readable World State) and the
engine flags active experiments whose pinned world dependencies moved.
"""
from __future__ import annotations

import asyncio
import difflib
import hashlib
import importlib.util
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx

from .db import DB, now

log = logging.getLogger("lab.sentinel")

SEVERITY_ORDER = {"info": 0, "minor": 1, "normal": 2, "major": 3}
PARTIAL = "__partial__"   # a fetch may set this key: the facts came from a mirror, merge instead of replace


def h(value: Any) -> str:
    if not isinstance(value, str):
        value = json.dumps(value, sort_keys=True, default=str)
    return hashlib.sha256(value.encode()).hexdigest()


def text_of(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, sort_keys=True, indent=1, default=str)


def unified(old: str, new: str, limit: int = 6000) -> str:
    d = "\n".join(difflib.unified_diff(old.splitlines(), new.splitlines(), "before", "after", lineterm="", n=2))
    return d if len(d) <= limit else d[:limit] + f"\n… (diff truncated, {len(d)} chars)"


@dataclass
class Change:
    key: str
    kind: str          # added | changed | removed
    old: Any
    new: Any

    def brief(self, limit: int = 600) -> dict:
        o, n = text_of(self.old) if self.old is not None else "", text_of(self.new) if self.new is not None else ""
        out = {"key": self.key, "kind": self.kind}
        if len(o) + len(n) <= limit:
            out.update(old=self.old, new=self.new)
        else:
            out["diff"] = unified(o, n)
        return out


@dataclass
class Emit:
    topic: str
    summary: str
    severity: str = "normal"
    key: str | None = None
    payload: Any = None


@dataclass
class Source:
    name: str
    interval_s: int
    fetch: Callable[["Fetcher"], Awaitable[dict[str, Any]]]
    # Turn a list of changes into events. Default: one world.change.<name> event.
    classify: Callable[[list[Change]], list[Emit]] | None = None
    # Keep full values in the store (needed for diffs). False stores only hashes.
    store_values: bool = True


class Fetcher:
    """HTTP with conditional GETs so unchanged documents cost one 304."""

    def __init__(self, client: httpx.AsyncClient | None = None):
        self.client = client or httpx.AsyncClient(timeout=45, follow_redirects=True,
                                                  headers={"User-Agent": "curl/8.5 lab-sentinel"})
        self.etags: dict[str, str] = {}
        self.bodies: dict[str, str] = {}
        self.fell_back = False          # True when the last text()/json() was served by a fallback URL

    async def text(self, url: str, *, fallbacks: tuple[str, ...] = ()) -> str:
        last_err: Exception | None = None
        self.fell_back = False
        for u in (url, *fallbacks):
            self.fell_back = u != url
            headers = {}
            if u in self.etags:
                headers["If-None-Match"] = self.etags[u]
            try:
                r = await self.client.get(u, headers=headers)
            except httpx.HTTPError as e:
                last_err = e
                continue
            if r.status_code == 304 and u in self.bodies:
                return self.bodies[u]
            if r.status_code == 200:
                if et := r.headers.get("etag"):
                    self.etags[u] = et
                self.bodies[u] = r.text
                return r.text
            last_err = RuntimeError(f"{u}: HTTP {r.status_code}")
        raise last_err or RuntimeError(f"fetch failed: {url}")

    async def json(self, url: str, *, fallbacks: tuple[str, ...] = ()) -> Any:
        return json.loads(await self.text(url, fallbacks=fallbacks))


def load_plugin(path: Path):
    spec = importlib.util.spec_from_file_location(f"lab_plugin_{path.parent.name}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def flatten(d: Any, prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    if isinstance(d, dict):
        for k, v in d.items():
            out.update(flatten(v, f"{prefix}{k}."))
    else:
        out[prefix[:-1]] = d
    return out


class Sentinel:
    def __init__(self, db: DB, project, plugin, fetcher: Fetcher | None = None):
        self.db = db
        self.project = project
        self.plugin = plugin
        self.fetcher = fetcher or Fetcher()
        self.sources: list[Source] = plugin.sources(project)
        self.next_due: dict[str, float] = {}
        self.failures: dict[str, int] = {}

    # ---- facts store ---------------------------------------------------------
    def stored(self, source: str) -> dict[str, tuple[str, Any]]:
        rows = self.db.all("SELECT key, hash, value FROM sentinel WHERE project=? AND source=?",
                           (self.project.name, source))
        return {r["key"]: (r["hash"], json.loads(r["value"]) if r["value"] is not None else None) for r in rows}

    def facts(self, source: str) -> dict[str, Any]:
        return {k: v for k, (_, v) in self.stored(source).items()}

    def diff(self, source: Source, new: dict[str, Any]) -> tuple[list[Change], bool]:
        old = self.stored(source.name)
        baseline = not old
        changes: list[Change] = []
        for k, v in new.items():
            hv = h(v)
            if k not in old:
                changes.append(Change(k, "added", None, v))
            elif old[k][0] != hv:
                changes.append(Change(k, "changed", old[k][1], v))
        for k in old.keys() - new.keys():
            changes.append(Change(k, "removed", old[k][1], None))
        return changes, baseline

    def store(self, source: Source, new: dict[str, Any]) -> None:
        p = self.project.name
        self.db.x("BEGIN")
        try:
            self.db.x("DELETE FROM sentinel WHERE project=? AND source=?", (p, source.name))
            for k, v in new.items():
                self.db.insert("sentinel", project=p, source=source.name, key=k, hash=h(v),
                               value=json.dumps(v, default=str) if source.store_values else None,
                               updated_at=now())
            self.db.x("COMMIT")
        except Exception:
            self.db.x("ROLLBACK")
            raise

    # ---- polling ---------------------------------------------------------------
    async def poll(self, source: Source) -> list[Emit]:
        new = await source.fetch(self.fetcher)
        if new.pop(PARTIAL, False):
            # Served by a mirror whose document has a different key set: only update values of keys
            # we already know, never add or remove keys (otherwise primary/mirror flapping reads as change).
            old = self.facts(source.name)
            if old:
                new = {**old, **{k: v for k, v in new.items() if k in old}}
        changes, baseline = self.diff(source, new)
        self.store(source, new)
        if baseline:
            self.db.emit(self.project.name, f"sentinel.baseline.{source.name}",
                         f"baseline recorded for {source.name}: {len(new)} facts", severity="info")
            return []
        if not changes:
            return []
        emits = source.classify(changes) if source.classify else [
            Emit(f"world.change.{source.name}", f"{source.name}: {len(changes)} change(s)",
                 "normal", payload=[c.brief() for c in changes[:40]])]
        for e in emits:
            self.db.emit(self.project.name, e.topic, e.summary, severity=e.severity, key=e.key,
                         payload=e.payload)
        return emits

    def rebuild_world(self) -> dict:
        all_facts = {s.name: self.facts(s.name) for s in self.sources}
        world = self.plugin.build_world(all_facts)
        world["generated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        wd = self.project.world_dir
        wd.mkdir(parents=True, exist_ok=True)
        prev = {}
        wj = wd / "world.json"
        if wj.exists():
            try:
                prev = json.loads(wj.read_text())
            except ValueError:
                prev = {}
        stable = {k: v for k, v in world.items() if k != "generated_at"}
        if stable != {k: v for k, v in prev.items() if k != "generated_at"}:
            tmp = wj.with_suffix(".tmp")
            tmp.write_text(json.dumps(world, indent=2, sort_keys=True, default=str))
            tmp.replace(wj)
            self.db.kv_set(self.project.name, "world_version", world.get("world_version"))
            # when each version went live: lets prompts list what changed since STATE.md's version
            hist = self.db.kv_get(self.project.name, "world_versions", []) or []
            if not hist or hist[-1][0] != world.get("world_version"):
                hist = (hist + [[world.get("world_version"), time.time()]])[-100:]
                self.db.kv_set(self.project.name, "world_versions", hist)
        return world

    async def tick(self) -> None:
        t = time.monotonic()
        changed = False
        for s in self.sources:
            if self.next_due.get(s.name, 0) > t:
                continue
            try:
                emits = await self.poll(s)
                changed |= bool(emits) or not self.world_exists()
                self.failures[s.name] = 0
                self.next_due[s.name] = t + s.interval_s
            except Exception as e:
                n = self.failures.get(s.name, 0) + 1
                self.failures[s.name] = n
                backoff = min(s.interval_s * (2 ** min(n, 4)), 3600)
                self.next_due[s.name] = t + backoff
                log.warning("sentinel %s/%s failed (%d): %r", self.project.name, s.name, n, e)
                if n == 5:
                    self.db.emit(self.project.name, "sentinel.error", f"source {s.name} failing: {e!r}",
                                 severity="normal")
        if changed:
            self.rebuild_world()

    def world_exists(self) -> bool:
        return (self.project.world_dir / "world.json").exists()

    async def run(self, stop: asyncio.Event):
        while not stop.is_set():
            try:
                await self.tick()
            except Exception:
                log.exception("sentinel tick failed")
            try:
                await asyncio.wait_for(stop.wait(), timeout=15)
            except asyncio.TimeoutError:
                pass
