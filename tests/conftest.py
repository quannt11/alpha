import json
import shutil
import textwrap
from pathlib import Path

import pytest

from lab import config as config_mod
from lab.db import DB

ROOT = Path(__file__).resolve().parents[1]
FIX = Path(__file__).parent / "fixtures"


@pytest.fixture
def labdir(tmp_path, monkeypatch):
    """A self-contained lab: config, one project (affine plugin + prompts), private state dir."""
    (tmp_path / "projects" / "affine").mkdir(parents=True)
    (tmp_path / "bin").symlink_to(ROOT / "bin")
    (tmp_path / "lab.toml").write_text(textwrap.dedent(f"""
        [lab]
        state_dir = "{tmp_path / 'state'}"
        secrets_files = ["{tmp_path / 'secrets.env'}"]
        max_concurrent_agents = 2
        max_concurrent_threads = 3
        max_concurrent_concierge = 1
        timezone = "Asia/Ho_Chi_Minh"
        [claude]
        bin = "{tmp_path / 'fakeclaude'}"
        [runpod]
        ssh_key = "{tmp_path / 'key'}"
    """))
    (tmp_path / "key").write_text("PRIVATE")
    (tmp_path / "key.pub").write_text("ssh-ed25519 AAAA test")
    pdir = tmp_path / "projects" / "affine"
    src = ROOT / "projects" / "affine"
    toml = (src / "project.toml").read_text().replace('root = "~/Work/affine"', f'root = "{tmp_path / "code"}"')
    (pdir / "project.toml").write_text(toml)
    shutil.copy(src / "plugin.py", pdir / "plugin.py")
    shutil.copytree(src / "prompts", pdir / "prompts")
    shutil.copy(src / "GOAL.md", pdir / "GOAL.md")
    shutil.copy(src / "CLAUDE.md", pdir / "CLAUDE.md")
    (tmp_path / "code").mkdir()
    monkeypatch.setenv("LAB_CONFIG", str(tmp_path / "lab.toml"))
    for var in ("LAB_PROJECT", "LAB_ROLE", "LAB_RUN_ID", "LAB_THREAD"):   # tests also run inside agent sessions
        monkeypatch.delenv(var, raising=False)
    return tmp_path


@pytest.fixture
def cfg(labdir):
    return config_mod.load(labdir / "lab.toml")


@pytest.fixture
def db(cfg):
    return DB(cfg.db_path)


@pytest.fixture
def project(cfg):
    return cfg.project("affine")


class FakeFetcher:
    """Serves fixture files for the affine plugin's URLs."""

    def __init__(self):
        self.overrides: dict[str, str] = {}
        self.fell_back = False
        self.mirror = False          # simulate the primary being down: serve from the mirror

    def _body(self, url: str) -> str:
        for k, v in self.overrides.items():
            if k in url:
                return v
        if "llms.txt" in url:
            return (FIX / "llms.txt").read_text()
        if "/contract" in url:
            return (FIX / "contract.json").read_text()
        if "/snapshot" in url or "dashboard.json" in url:
            return (FIX / "snap.json").read_text()
        if "/history" in url:
            return (FIX / "history.json").read_text()
        if "/dataset" in url:
            return (FIX / "dataset.json").read_text()
        if "curriculum" in url:
            return (FIX / "curriculum.json").read_text()
        if "/audits" in url:
            return (FIX / "audits.json").read_text()
        if "api.github.com" in url:
            return json.dumps([{"sha": "a" * 40, "commit": {"message": "first", "committer": {"date": "2026-09-22"}}}])
        if "/code/" in url:
            return f"# code for {url.rsplit('/code/', 1)[1]}\n"
        raise RuntimeError(f"no fixture for {url}")

    async def text(self, url, *, fallbacks=()):
        self.fell_back = bool(self.mirror and fallbacks)
        return self._body(fallbacks[0] if self.fell_back else url)

    async def json(self, url, *, fallbacks=()):
        return json.loads(await self.text(url, fallbacks=fallbacks))


@pytest.fixture
def fetcher():
    return FakeFetcher()
