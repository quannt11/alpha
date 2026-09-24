"""SQLite store: the durable session log and every registry the lab keeps.

The daemon and the `lab` CLI open the same file (WAL mode). The CLI never holds
credentials: to do anything that needs one (rent a GPU, post to Discord) it
writes a request row and the daemon, which holds the keys, fulfils it.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  project TEXT NOT NULL,
  topic TEXT NOT NULL,
  severity TEXT NOT NULL DEFAULT 'info',
  key TEXT,
  summary TEXT,
  payload TEXT
);
CREATE INDEX IF NOT EXISTS events_topic ON events(project, topic, id);

CREATE TABLE IF NOT EXISTS cursors (
  project TEXT, role TEXT, key TEXT, last_event_id INTEGER, last_run_at REAL,
  PRIMARY KEY (project, role, key)
);

CREATE TABLE IF NOT EXISTS agent_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project TEXT, role TEXT, key TEXT,
  status TEXT,                 -- queued | running | ok | error | ratelimited | timeout
  queued_at REAL, started_at REAL, ended_at REAL,
  event_ids TEXT, session_id TEXT, pid INTEGER,
  result TEXT, error TEXT, cost_usd REAL, num_turns INTEGER, log_path TEXT
);
CREATE INDEX IF NOT EXISTS runs_status ON agent_runs(status, project, role, key);

CREATE TABLE IF NOT EXISTS sentinel (
  project TEXT, source TEXT, key TEXT, hash TEXT, value TEXT, updated_at REAL,
  PRIMARY KEY (project, source, key)
);

CREATE TABLE IF NOT EXISTS discord_messages (
  id TEXT PRIMARY KEY, project TEXT, channel_id TEXT, author_id TEXT, author_name TEXT,
  is_bot INTEGER, content TEXT, reply_to TEXT, ts REAL
);
CREATE INDEX IF NOT EXISTS dm_chan ON discord_messages(project, ts);

CREATE TABLE IF NOT EXISTS outbox (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project TEXT, channel_id TEXT, content TEXT, reply_to TEXT, files TEXT,
  status TEXT DEFAULT 'pending',    -- pending | sent | failed | unknown
  created_at REAL, sent_at REAL, message_ids TEXT, error TEXT, author_role TEXT
);

CREATE TABLE IF NOT EXISTS tickets (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project TEXT, created_at REAL, author TEXT, source_message_id TEXT,
  title TEXT, body TEXT, status TEXT DEFAULT 'open', resolution TEXT, updated_at REAL
);

CREATE TABLE IF NOT EXISTS backlog (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project TEXT, created_at REAL, author TEXT, title TEXT, hypothesis TEXT,
  expected_gain TEXT, est_cost_usd REAL, priority INTEGER DEFAULT 50,
  status TEXT DEFAULT 'proposed', -- suggested | proposed | ready | assigned | done | rejected (lab/research.py)
  experiment_id TEXT, notes TEXT
);

CREATE TABLE IF NOT EXISTS experiments (
  id TEXT PRIMARY KEY, project TEXT, created_at REAL, updated_at REAL,
  title TEXT, spec TEXT, pool TEXT, gpu_type TEXT, gpu_count INTEGER, max_hours REAL,
  est_cost_usd REAL, status TEXT,   -- queued | needs_approval | scheduled | running | done | failed | killed
  stale INTEGER DEFAULT 0, world_version TEXT, world_deps TEXT,
  requested_by TEXT, backlog_id INTEGER, workdir TEXT, approved INTEGER DEFAULT 0,
  result TEXT, conclusion TEXT, spent_usd REAL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS threads (
  id TEXT PRIMARY KEY,              -- t-001: one implementor (a Claude session) working through tasks
  project TEXT, title TEXT, status TEXT,   -- active | retired
  created_at REAL, created_by TEXT, retired_at REAL, retire_reason TEXT,
  session_id TEXT, passes INTEGER DEFAULT 0, last_pass_at REAL, metric TEXT,
  best_value REAL, best_desc TEXT, spent_usd REAL DEFAULT 0, workdir TEXT
);

CREATE TABLE IF NOT EXISTS results (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project TEXT, thread_id TEXT, ts REAL, run TEXT, description TEXT,
  metric TEXT, value REAL, kept INTEGER, cost_usd REAL, notes TEXT
);
CREATE INDEX IF NOT EXISTS results_ts ON results(project, ts);

CREATE TABLE IF NOT EXISTS leases (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project TEXT, experiment_id TEXT,  -- experiment_id: legacy holder column (experiments pipeline)
  status TEXT,   -- requested | provisioning | granted | denied | released | failed
  gpu_type TEXT, gpu_count INTEGER, cloud TEXT, max_hours REAL,
  pod_id TEXT, pod_name TEXT, price_hr REAL, ssh_host TEXT, ssh_port INTEGER,
  requested_at REAL, granted_at REAL, released_at REAL, expires_at REAL,
  reason TEXT, job_state TEXT, job_status TEXT, job_status_at REAL, heartbeat_at REAL,
  last_billed_at REAL, holder TEXT, alternatives TEXT, pool TEXT, est_usd REAL, job_name TEXT
);

CREATE TABLE IF NOT EXISTS pods (
  id TEXT PRIMARY KEY, project TEXT, name TEXT, gpu_type TEXT, gpu_count INTEGER,
  cloud TEXT, price_hr REAL, created_at REAL, state TEXT, last_seen REAL,
  lease_id INTEGER, idle_since REAL, machine_id TEXT, terminated INTEGER DEFAULT 0,
  last_billed_at REAL, last_pool TEXT, last_experiment TEXT, ssh_host TEXT, ssh_port INTEGER
);

CREATE TABLE IF NOT EXISTS ledger (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project TEXT, ts REAL, pool TEXT, experiment_id TEXT, pod_id TEXT, usd REAL, note TEXT
);
CREATE INDEX IF NOT EXISTS ledger_ts ON ledger(project, ts);

CREATE TABLE IF NOT EXISTS maint (
  id TEXT PRIMARY KEY,              -- m-1: one operator request to change the lab's own code
  project TEXT, created_at REAL, updated_at REAL, author_id TEXT, author TEXT,
  message_id TEXT, channel_id TEXT, request TEXT, context TEXT,
  status TEXT,   -- queued | working | awaiting_approval | deploying | restarting | deployed | rolled_back
                 -- | no_change | failed | rejected
  branch TEXT, base_sha TEXT, head_sha TEXT, files TEXT, protected TEXT, summary TEXT, note TEXT,
  deploy_requested_at REAL
);

CREATE TABLE IF NOT EXISTS kv (
  project TEXT, key TEXT, value TEXT, PRIMARY KEY (project, key)
);
"""


def now() -> float:
    return time.time()


class DB:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=30000")
        self.conn.executescript(SCHEMA)
        self._migrate()

    MIGRATIONS = [  # (table, column, type): columns added after a database was first created
        ("leases", "holder", "TEXT"), ("leases", "alternatives", "TEXT"), ("leases", "pool", "TEXT"),
        ("leases", "est_usd", "REAL"), ("leases", "job_name", "TEXT"),
        ("pods", "last_pool", "TEXT"), ("pods", "last_experiment", "TEXT"), ("pods", "ssh_host", "TEXT"),
        ("pods", "ssh_port", "INTEGER"), ("experiments", "approved", "INTEGER DEFAULT 0"),
        ("agent_runs", "model", "TEXT"),
        ("threads", "session_passes", "INTEGER DEFAULT 0"), ("threads", "session_cost", "REAL DEFAULT 0"),
        ("threads", "context_tokens", "INTEGER"), ("threads", "rotate_pending", "INTEGER DEFAULT 0"),
        ("threads", "generation", "INTEGER DEFAULT 1"), ("leases", "provisioning_at", "REAL"),
        ("backlog", "world_version", "TEXT"), ("threads", "world_version", "TEXT"),
        # the Researcher's ideas are tasks for implementor threads
        ("backlog", "spec", "TEXT"), ("backlog", "metric", "TEXT"), ("backlog", "thread_id", "TEXT"),
        ("backlog", "for_thread", "TEXT"), ("backlog", "assigned_at", "REAL"), ("backlog", "done_at", "REAL"),
        ("backlog", "result", "TEXT"), ("backlog", "source_message", "TEXT"),
        ("threads", "task_id", "INTEGER"), ("threads", "idle_since", "REAL"),
    ]

    def _migrate(self) -> None:
        for table, col, typ in self.MIGRATIONS:
            cols = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            if col not in cols:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
                if (table, col) == ("threads", "session_passes"):  # existing sessions keep resuming
                    self.conn.execute("UPDATE threads SET session_passes=passes WHERE session_id IS NOT NULL")
                if (table, col) == ("backlog", "spec"):  # the Director's accepted ideas wait for the Researcher
                    self.conn.execute("UPDATE backlog SET status='proposed' WHERE status='accepted'")

    # ---- generic helpers -------------------------------------------------
    def x(self, sql: str, args: Iterable[Any] = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, tuple(args))

    def one(self, sql: str, args: Iterable[Any] = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, tuple(args)).fetchone()

    def all(self, sql: str, args: Iterable[Any] = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, tuple(args)).fetchall()

    def insert(self, table: str, **cols: Any) -> int:
        keys = ",".join(cols)
        qs = ",".join("?" for _ in cols)
        cur = self.conn.execute(f"INSERT INTO {table} ({keys}) VALUES ({qs})", tuple(cols.values()))
        return cur.lastrowid

    def update(self, table: str, where: str, args: Iterable[Any], **cols: Any) -> int:
        sets = ",".join(f"{k}=?" for k in cols)
        cur = self.conn.execute(f"UPDATE {table} SET {sets} WHERE {where}", (*cols.values(), *args))
        return cur.rowcount

    # ---- events ------------------------------------------------------------
    def emit(self, project: str, topic: str, summary: str = "", *, severity: str = "info",
             key: str | None = None, payload: Any = None) -> int:
        return self.insert(
            "events", ts=now(), project=project, topic=topic, severity=severity, key=key,
            summary=summary[:2000], payload=json.dumps(payload, default=str) if payload is not None else None,
        )

    def events_since(self, project: str, after_id: int, topics: list[str], key: str | None = None,
                     limit: int = 200) -> list[sqlite3.Row]:
        rows = self.all(
            "SELECT * FROM events WHERE project=? AND id>? ORDER BY id LIMIT ?",
            (project, after_id, limit * 5),
        )
        out = [r for r in rows if topic_match(r["topic"], topics) and (key is None or r["key"] == key)]
        return out[:limit]

    def max_event_id(self) -> int:
        r = self.one("SELECT COALESCE(MAX(id),0) AS m FROM events")
        return int(r["m"])

    # ---- kv ----------------------------------------------------------------
    def kv_get(self, project: str, key: str, default: Any = None) -> Any:
        r = self.one("SELECT value FROM kv WHERE project=? AND key=?", (project, key))
        return json.loads(r["value"]) if r else default

    def kv_set(self, project: str, key: str, value: Any) -> None:
        self.x("INSERT INTO kv(project,key,value) VALUES(?,?,?) "
               "ON CONFLICT(project,key) DO UPDATE SET value=excluded.value",
               (project, key, json.dumps(value, default=str)))

    # ---- ids ---------------------------------------------------------------
    def next_thread_id(self, project: str) -> str:
        rows = self.all("SELECT id FROM threads WHERE project=?", (project,))
        n = max((int(r["id"].split("-")[-1]) for r in rows if r["id"].split("-")[-1].isdigit()), default=0)
        return f"t-{n + 1:03d}"

    def next_experiment_id(self, project: str) -> str:
        rows = self.all("SELECT id FROM experiments WHERE project=?", (project,))
        n = max((int(r["id"].split("-")[-1]) for r in rows if r["id"].split("-")[-1].isdigit()), default=0)
        return f"exp-{n + 1:04d}"


def topic_match(topic: str, patterns: list[str]) -> bool:
    """`exp.done` matches `exp.done`, `exp.*` and `*`. A pattern also matches its
    own sub-topics: `world.change` matches `world.change.contract`."""
    for p in patterns:
        if p == "*" or p == topic:
            return True
        if p.endswith(".*") and topic.startswith(p[:-1]):
            return True
        if topic.startswith(p + "."):
            return True
    return False


def row_dict(r: sqlite3.Row | None) -> dict | None:
    return dict(r) if r is not None else None
