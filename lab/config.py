"""Configuration: the global lab.toml plus one project.toml per project.

Secrets never live in these files. They are read at runtime from the env files
listed in `[lab].secrets_files`, and only the daemon process loads them.
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

LAB_ROOT = Path(__file__).resolve().parents[1]


def expand(p: str | Path) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(str(p)))).resolve()


@dataclass
class RoleConfig:
    name: str
    model: str = "sonnet"
    effort: str | None = None
    timeout_s: int = 1800
    priority: int = 50            # lower runs first
    debounce_s: int = 0
    wake_on: list[str] = field(default_factory=list)
    tools: str = "full"           # "readonly" | "full"
    # the only tools the agent is given (`claude --tools`): every other built-in tool's schema costs ~8–10k
    # tokens on every model call; None = Claude Code's default set
    toolset: list[str] | None = None
    min_severity: str = "info"    # ignore wake events below this severity
    light_model: str | None = None  # used when every wake event is a routine check (light_topics)
    light_topics: list[str] = field(default_factory=list)
    advisor: str | None = None      # `claude --advisor`: a stronger model the agent consults in-loop at hard decisions


@dataclass
class Project:
    name: str
    dir: Path                      # lab/projects/<name>
    root: Path                     # the project's code, e.g. ~/Work/affine
    pod_prefix: str
    guild_id: str
    channel_id: str
    daily_usd: float
    pools: dict[str, float | None]   # pool -> fraction of the daily cap, or None = label only (no cap)
    per_experiment_usd: float        # 0 = no per-experiment cap
    approval_over_usd: float         # 0 = never ask a human
    test_mode: bool
    max_threads: int                 # implementor threads alive at once (labd starts them for ready ideas)
    rotate_context_tokens: int       # a thread whose context passes this gets a fresh session (0 = never)
    report_time: str
    research_tick_hours: float       # the Researcher's review tick, fired only while no task is queued or running
    idle_retire_hours: float         # a thread without a task this long is retired (0 = never)
    fleet: dict
    roles: dict[str, RoleConfig]
    sentinel: dict
    extra: dict

    @property
    def world_dir(self) -> Path:
        return self.dir / "world"

    @property
    def work_dir(self) -> Path:
        return self.dir / "work"

    @property
    def prompts_dir(self) -> Path:
        return self.dir / "prompts"


@dataclass
class MaintConfig:
    operators: list[str]            # Discord user ids allowed to change the lab from Discord
    auto_deploy: bool               # False: every change waits for "maint approve"
    dir: Path                       # git worktrees of in-flight changes
    test_cmd: str                   # must pass (in the worktree) before anything is deployed
    idle_wait_s: int                # how long a deploy waits for running agents to finish
    deploy_cmd: list[str] | None    # None: bin/lab-deploy under systemd-run (tests override it)


@dataclass
class LabConfig:
    root: Path
    state_dir: Path
    db_path: Path
    secrets_files: list[Path]
    max_concurrent_agents: int      # scout, researcher, analyst share these slots
    max_concurrent_threads: int     # research threads are independent: each works on its own pods
    max_concurrent_concierge: int   # people in Discord never wait behind background work
    timezone: str
    claude_bin: str
    runpod: dict
    shadeform: dict                 # optional [shadeform] gpu_map = {"<Runpod GPU id>" = ["H100", "sxm5"]}
    vast: dict                      # optional [vast]: host filters, image, gpu_map = {"<Runpod GPU id>" = ["H100 SXM", 0]}
    projects: dict[str, Project]
    maint: MaintConfig

    def project(self, name: str) -> Project:
        if name not in self.projects:
            raise KeyError(f"unknown project {name!r}; known: {', '.join(self.projects) or '(none)'}")
        return self.projects[name]


CORE_TOOLS = ["Bash", "Read", "Edit", "Write", "Grep", "Glob", "WebFetch", "WebSearch", "Agent"]
READ_TOOLS = ["Bash", "Read", "Grep", "Glob", "WebFetch", "WebSearch"]
LIGHT_MAX_CONTEXT = 40_000   # the prompt cache is per model: a light model re-caching a bigger session costs more

OPUS = "claude-opus-5-5"
FABLE = "claude-fable-5-1"
SONNET = "claude-sonnet-5"   # chat, reading and routine checks; also every agent's subagents

DEFAULT_ROLES: dict[str, dict] = {
    # people in Discord; read-only: answers status/ETA/world questions itself, routes research ideas
    "concierge": {"model": SONNET, "priority": 10, "timeout_s": 600, "tools": "readonly", "toolset": READ_TOOLS,
                  "wake_on": ["discord.request"]},
    # turns detected world changes into World State + briefs
    "scout":     {"model": SONNET, "toolset": CORE_TOOLS, "priority": 20, "timeout_s": 1200, "debounce_s": 60,
                  "wake_on": ["world.change"], "min_severity": "normal"},
    # the research mind: analyses where we stand, reads papers, writes ideas that labd hands to threads,
    # answers the threads' questions and reads their reports. Its context is about ideas only.
    "researcher": {"model": FABLE, "toolset": CORE_TOOLS, "priority": 25, "timeout_s": 3600, "debounce_s": 90,
                   "wake_on": ["thread.report", "thread.question", "research.suggestion", "world.brief",
                               "board.king", "thread.claim.verdict", "tick.research"], "min_severity": "normal"},
    # an implementor: takes one task at a time, rents its GPUs, implements, evaluates, reports back;
    # consults Fable in-loop (the advisor strategy) at decisions it can't reasonably make alone
    "thread":    {"model": OPUS, "toolset": CORE_TOOLS, "effort": "medium", "advisor": FABLE, "priority": 30, "timeout_s": 5400,
                  "wake_on": ["thread.task", "thread.continue", "thread.message", "gpu.lease", "gpu.waiting",
                              "job.finished", "job.anomaly", "job.check", "job.idle"],
                  # a pass woken only by a routine job/lease check resumes the same session on Sonnet
                  "light_model": SONNET, "light_topics": ["job.check", "job.idle", "gpu.waiting"]},
    # red-teams claims (Opus), writes the daily report (Sonnet)
    "analyst":   {"model": OPUS, "toolset": CORE_TOOLS, "priority": 40, "timeout_s": 1800, "debounce_s": 60,
                  "wake_on": ["thread.claim", "tick.daily_report"],
                  "light_model": SONNET, "light_topics": ["tick.daily_report"]},
    # changes the lab's own code on an operator's request ("maint: …" in Discord), in a git worktree
    "maintainer": {"model": OPUS, "toolset": CORE_TOOLS, "priority": 15, "timeout_s": 3600, "wake_on": ["maint.request"]},
}


def _load_toml(path: Path) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


def _pools(v) -> dict[str, float | None]:
    """`pools = ["agenda", "explore"]` are accounting labels; a table `{agenda = 0.65}` caps them."""
    if isinstance(v, dict):
        return {k: (float(x) if x is not None else None) for k, x in v.items()}
    return {k: None for k in v}


def load_project(pdir: Path) -> Project:
    raw = _load_toml(pdir / "project.toml")
    roles: dict[str, RoleConfig] = {}
    overrides = raw.get("agents", {})
    for name, base in DEFAULT_ROLES.items():
        merged = {**base, **overrides.get(name, {})}
        roles[name] = RoleConfig(name=name, **merged)
    budget = raw.get("budget", {})
    sched = raw.get("schedule", {})
    discord = raw.get("discord", {})
    return Project(
        name=raw["name"],
        dir=pdir,
        root=expand(raw["root"]),
        pod_prefix=raw.get("pod_prefix", f"Pi_{raw['name']}"),
        guild_id=str(discord.get("guild_id", "")),
        channel_id=str(discord.get("channel_id", "")),
        daily_usd=float(budget.get("daily_usd", 0)),
        pools=_pools(budget.get("pools", ["agenda"])),
        per_experiment_usd=float(budget.get("per_experiment_usd", 0)),
        approval_over_usd=float(budget.get("approval_over_usd", 0)),
        test_mode=bool(raw.get("test_mode", False)),
        max_threads=int(raw.get("research", {}).get("max_threads", 1)),
        rotate_context_tokens=int(raw.get("research", {}).get("rotate_context_tokens", 150_000)),
        report_time=sched.get("daily_report", "09:00"),
        research_tick_hours=float(sched.get("research_tick_hours", 6)),
        idle_retire_hours=float(raw.get("research", {}).get("idle_retire_hours", 24)),
        fleet=raw.get("fleet", {}),
        roles=roles,
        sentinel=raw.get("sentinel", {}),
        extra=raw,
    )


def load(path: str | Path | None = None) -> LabConfig:
    path = expand(path or os.environ.get("LAB_CONFIG") or LAB_ROOT / "lab.toml")
    raw = _load_toml(path)
    lab = raw.get("lab", {})
    maint = raw.get("maintainer", {})
    root = path.parent
    state_dir = expand(lab.get("state_dir", "~/.local/state/lab"))
    projects: dict[str, Project] = {}
    pdir_root = root / "projects"
    for pdir in sorted(pdir_root.iterdir()) if pdir_root.exists() else []:
        if (pdir / "project.toml").exists():
            p = load_project(pdir)
            projects[p.name] = p
    return LabConfig(
        root=root,
        state_dir=state_dir,
        db_path=expand(lab.get("db", state_dir / "lab.db")),
        secrets_files=[expand(p) for p in lab.get("secrets_files", [])],
        max_concurrent_agents=int(lab.get("max_concurrent_agents", 3)),
        max_concurrent_threads=int(lab.get("max_concurrent_threads", lab.get("max_concurrent_runners", 8))),
        max_concurrent_concierge=int(lab.get("max_concurrent_concierge", 2)),
        timezone=lab.get("timezone", "UTC"),
        claude_bin=raw.get("claude", {}).get("bin", "claude"),
        runpod=raw.get("runpod", {}),
        shadeform=raw.get("shadeform", {}),
        vast=raw.get("vast", {}),
        projects=projects,
        maint=MaintConfig(
            operators=[str(x) for x in maint.get("operators", [])],
            auto_deploy=bool(maint.get("auto_deploy", True)),
            dir=expand(maint.get("dir", "~/.cache/lab-maint")),
            test_cmd=maint.get("test_cmd", "uv run pytest -q"),
            idle_wait_s=int(float(maint.get("idle_wait_minutes", 20)) * 60),
            deploy_cmd=maint.get("deploy_cmd"),
        ),
    )


def load_secrets(cfg: LabConfig) -> dict[str, str]:
    """Parse KEY=VALUE env files. Later files do not override earlier ones;
    the process environment wins over all of them."""
    out: dict[str, str] = {}
    for f in cfg.secrets_files:
        if not f.exists():
            continue
        for line in f.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip().removeprefix("export ").strip()
            v = v.strip().strip('"').strip("'")
            if v and k not in out:
                out[k] = v
    for k in ("DISCORD_BOT_TOKEN", "RUNPOD_API_KEY", "SHADEFORM_API_KEY", "VAST_API_KEY"):
        if os.environ.get(k):
            out[k] = os.environ[k]
    return out
