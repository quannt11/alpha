"""The maintainer: operators change the lab's own code from Discord.

"@bot maint: <what to change>" from an operator (lab.toml `[maintainer].operators`) becomes a request
m-N. The maintainer agent makes the change in its own git worktree on branch `maint/m-N` and commits it.
labd then checks the branch in plain code — nothing under projects/*/work or world, nothing that looks
like a credential, the test suite passes — and deploys it: `bin/lab-deploy`, started under systemd-run
so that restarting labd does not kill it, merges the branch into the live lab, restarts labd and reverts
the merge if labd does not come back healthy. Changes that touch the lab's safety machinery (guard,
budget, secrets, operators, this file) wait for "maint approve m-N".
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import subprocess
from pathlib import Path

from .config import LabConfig, Project
from .db import DB, now

REQUEST_RE = re.compile(r"^\s*maint\s*[:：]\s*(?P<text>\S.*)$", re.S | re.I)
COMMAND_RE = re.compile(r"^\s*maint\s+(?P<cmd>approve|reject|status|list)\b\s*(?P<id>m-\d+)?\s*$", re.I)

# never deployed: research notes and World State belong to the agents, not to code changes
FORBIDDEN_PATH = re.compile(r"^projects/[^/]+/(work|world)/")
# deployed only after an operator approves
PROTECTED_PATH = re.compile(r"^(bin/lab-guard|bin/lab-deploy|lab/maint\.py|lab/budget\.py|lab\.toml|systemd/)")
PROTECTED_LINE = re.compile(r"daily_usd|test_mode|test_policy|max_gpus|allowed_gpu_types|per_experiment_usd|"
                            r"approval_over_usd|operators|RUNPOD_API_KEY|SHADEFORM_API_KEY|DISCORD_BOT_TOKEN|SECRET_ENV|"
                            r"secrets_files|load_secrets|\"permissions\"|bypassPermissions")
SECRET_LIKE = re.compile(r"sk-ant-[\w-]{20,}|ghp_\w{20,}|github_pat_\w{20,}|rpa_\w{20,}|xox[bp]-[\w-]{10,}|"
                         r"-----BEGIN [A-Z ]*PRIVATE KEY|\b[MN][\w-]{23,25}\.[\w-]{6}\.[\w-]{27,}")
SECRET_ENV = ("DISCORD_BOT_TOKEN", "RUNPOD_API_KEY", "SHADEFORM_API_KEY", "DISCORD_BOT_TOKEN_ARBOS_BITTENSOR", "CLAUDE_CODE_OAUTH_TOKEN",
              "ANTHROPIC_API_KEY", "VIRTUAL_ENV")
DONE = ("deployed", "rolled_back", "no_change", "failed", "rejected")


class MaintError(Exception):
    pass


def git(cwd: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
    if r.returncode:
        raise MaintError(f"git {' '.join(args)}: {(r.stderr or r.stdout).strip()[:400]}")
    return r.stdout.strip()


class Maint:
    def __init__(self, db: DB, cfg: LabConfig):
        self.db = db
        self.cfg = cfg
        self.mc = cfg.maint

    # ------------------------------------------------------------ registry
    def get(self, mid: str):
        return self.db.one("SELECT * FROM maint WHERE id=?", (mid,))

    def _set(self, mid: str, **cols) -> None:
        self.db.update("maint", "id=?", (mid,), updated_at=now(), **cols)

    def is_operator(self, author_id) -> bool:
        return str(author_id) in self.mc.operators

    def say(self, r, text: str) -> None:
        """Reply to the request's Discord message (or post to the channel when it came from the terminal)."""
        p = self.cfg.projects.get(r["project"])
        if not p:
            return
        self.db.insert("outbox", project=p.name, channel_id=r["channel_id"] or p.channel_id, content=text,
                       reply_to=r["message_id"], files=None, status="pending", created_at=now(),
                       author_role="maintainer")

    def request(self, project: str, *, author_id: str, author: str, text: str, message_id: str | None = None,
                channel_id: str | None = None, context: str | None = None) -> str:
        rows = self.db.all("SELECT id FROM maint")
        mid = f"m-{max((int(r['id'][2:]) for r in rows), default=0) + 1}"
        self.db.insert("maint", id=mid, project=project, created_at=now(), updated_at=now(), author_id=author_id,
                       author=author, message_id=message_id, channel_id=channel_id, request=text, context=context,
                       status="queued")
        self.db.emit(project, "maint.request", f"{mid} from {author}: {text[:300]}", severity="normal", key=mid)
        return mid

    # ------------------------------------------------------------ Discord
    def handle_message(self, p: Project, *, author_id, author: str, text: str, message_id: str, channel_id: str,
                       context: str | None = None) -> bool:
        """True when the message was for the maintainer (then nobody else should answer it)."""
        m, c = REQUEST_RE.match(text or ""), COMMAND_RE.match(text or "")
        if not (m or c):
            return False
        r = {"project": p.name, "channel_id": channel_id, "message_id": message_id}
        if not self.is_operator(author_id):
            self.say(r, "Only the lab's operators can change the lab itself (`maint: …`). "
                        "Ask one of them — or ask me without `maint` and I'll answer or pass it on as an idea.")
            return True
        if m:
            mid = self.request(p.name, author_id=str(author_id), author=author, text=m["text"].strip(),
                               message_id=message_id, channel_id=channel_id, context=context)
            self.say(r, f"**{mid}** queued. The maintainer will make the change on its own branch, test it and "
                        f"report back here{' before deploying' if not self.mc.auto_deploy else ''}.")
        else:
            self.say(r, self.command(c["cmd"].lower(), c["id"], author))
        return True

    def command(self, cmd: str, mid: str | None, by: str) -> str:
        if cmd in ("status", "list"):
            return self.status_text(mid)
        if not mid:
            return f"Which one? `maint {cmd} m-N`."
        return self.approve(mid, by) if cmd == "approve" else self.reject(mid, by)

    def approve(self, mid: str, by: str) -> str:
        r = self.get(mid)
        if not r:
            return f"There is no {mid}."
        if r["status"] != "awaiting_approval":
            return f"{mid} is {r['status']}, not waiting for approval."
        self._set(mid, status="deploying", deploy_requested_at=now(), note=f"approved by {by}")
        return (f"**{mid}** approved by {by}. Deploying when the agents are idle (at most "
                f"{self.mc.idle_wait_s // 60} min); labd restarts and I'll report back.")

    def reject(self, mid: str, by: str) -> str:
        r = self.get(mid)
        if not r:
            return f"There is no {mid}."
        if r["status"] not in ("queued", "awaiting_approval", "deploying"):
            return f"{mid} is {r['status']}; it can no longer be rejected."
        self._set(mid, status="rejected", note=f"rejected by {by}")
        self.cleanup(mid)
        return f"**{mid}** rejected by {by}. Nothing was deployed (branch `{r['branch'] or '-'}` is kept)."

    def status_text(self, mid: str | None = None, limit: int = 8) -> str:
        rows = ([self.get(mid)] if mid else
                self.db.all("SELECT * FROM maint ORDER BY created_at DESC LIMIT ?", (limit,)))
        rows = [r for r in rows if r]
        if not rows:
            return "No maintainer requests yet."
        return "\n".join(f"- **{r['id']}** [{r['status']}] {r['request'][:120]} — {r['author']}"
                         + (f"; {r['note'][:200]}" if r["note"] else "") for r in rows)

    # ------------------------------------------------------------ worktree
    def worktree(self, mid: str) -> Path:
        return self.mc.dir / mid

    def prepare(self, mid: str) -> Path:
        """The maintainer's own checkout: branch maint/m-N from the live lab's HEAD."""
        r, wt, root = self.get(mid), self.worktree(mid), self.cfg.root
        branch = f"maint/{mid}"
        if not (wt / ".git").exists():
            wt.parent.mkdir(parents=True, exist_ok=True)
            git(root, "worktree", "prune")
            if subprocess.run(["git", "-C", str(root), "rev-parse", "--verify", "-q", branch],
                              capture_output=True).returncode == 0:
                git(root, "worktree", "add", "-q", str(wt), branch)
            else:
                git(root, "worktree", "add", "-q", "-b", branch, str(wt), "HEAD")
        base = r["base_sha"] or git(root, "rev-parse", "HEAD")
        self._set(mid, status="working", branch=branch, base_sha=base)
        return wt

    def cleanup(self, mid: str) -> None:
        wt = self.worktree(mid)
        if wt.exists():
            subprocess.run(["git", "-C", str(self.cfg.root), "worktree", "remove", "--force", str(wt)],
                           capture_output=True)
        subprocess.run(["git", "-C", str(self.cfg.root), "worktree", "prune"], capture_output=True)

    # ------------------------------------------------------------ after the agent
    def review(self, files: list[str], diff: str) -> tuple[list[str], list[str]]:
        """(reasons it must not be deployed, reasons it needs an operator's approval)."""
        changed = [ln[1:] for ln in diff.splitlines() if ln[:1] in "+-" and ln[:3] not in ("+++", "---")]
        bad = [f"`{f}` (research data belongs to the agents)" for f in files if FORBIDDEN_PATH.match(f)]
        if any(SECRET_LIKE.search(ln) for ln in changed):
            bad.append("something that looks like a credential")
        protected = [f"`{f}`" for f in files if PROTECTED_PATH.match(f)]
        code_diff = re.split(r"^diff --git ", diff, flags=re.M)
        hits = sorted({m.group(0) for part in code_diff if not re.match(r"a/\S+\.md\b", part)
                       for ln in part.splitlines() if ln[:1] in "+-" and ln[:3] not in ("+++", "---")
                       for m in [PROTECTED_LINE.search(ln)] if m})
        protected += [f"`{h}`" for h in hits]
        return bad, protected

    async def run_tests(self, wt: Path) -> tuple[int, str]:
        env = {k: v for k, v in os.environ.items() if not k.startswith("LAB_") and k not in SECRET_ENV}
        proc = await asyncio.create_subprocess_shell(self.mc.test_cmd, cwd=str(wt), env=env, start_new_session=True,
                                                     stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), 1800)
        except asyncio.TimeoutError:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            return 124, "tests timed out after 30 min"
        return proc.returncode, out.decode(errors="replace")

    async def finish(self, p: Project, mid: str, run, text: str) -> None:
        """labd, not the agent, decides what happens to the branch."""
        r, wt = self.get(mid), self.worktree(mid)
        if not r:
            return
        text = (text or "").strip()

        def fail(why: str, detail: str = ""):
            self._set(mid, status="failed", note=why[:500])
            self.say(r, f"**{mid} not deployed:** {why}" + (f"\n{detail}" if detail else ""))
            self.cleanup(mid)

        if run["status"] != "ok" or not wt.exists():
            return fail(f"the maintainer run ended with `{run['status']}` (run {run['id']}).", text[:1200])
        try:
            if git(wt, "status", "--porcelain"):
                git(wt, "add", "-A")
                git(wt, "-c", "user.name=lab maintainer", "-c", "user.email=lab@localhost", "commit", "-q",
                    "-m", f"{mid}: {r['request'].splitlines()[0][:70]}")
            head = git(wt, "rev-parse", "HEAD")
            files = [f for f in git(wt, "diff", "--name-only", f"{r['base_sha']}..{head}").splitlines() if f]
            diff = git(wt, "diff", f"{r['base_sha']}..{head}")
            stat = git(wt, "diff", "--stat", f"{r['base_sha']}..{head}")
        except MaintError as e:
            return fail(str(e))
        if not files:
            self._set(mid, status="no_change", summary=text[:4000], note="no code change")
            self.say(r, f"**{mid}** (no change): {text[:1800] or '(the maintainer did not say why)'}")
            return self.cleanup(mid)
        self._set(mid, head_sha=head, files=json.dumps(files), summary=text[:4000])
        bad, protected = self.review(files, diff)
        if bad:
            return fail("the change touches " + ", ".join(bad) + ".")
        rc, out = await self.run_tests(wt)
        if rc:
            return fail(f"the tests fail (`{self.mc.test_cmd}`, exit {rc}).", "```\n" + out[-900:] + "\n```")
        stat_lines = stat.splitlines()
        stat = "\n".join(stat_lines if len(stat_lines) <= 12 else stat_lines[:10] + ["…", stat_lines[-1]])
        msg = f"**{mid} ready** (tests pass)\n{text[:1100]}\n```\n{stat}\n```"
        if protected or not self.mc.auto_deploy:
            why = f"it touches {', '.join(protected[:6])}" if protected else "auto-deploy is off"
            self._set(mid, status="awaiting_approval", protected=json.dumps(protected))
            msg += f"\nWaiting for approval because {why}: reply `maint approve {mid}` or `maint reject {mid}`."
        else:
            self._set(mid, status="deploying", deploy_requested_at=now())
            msg += (f"\nDeploying when the agents are idle (at most {self.mc.idle_wait_s // 60} min): merge, restart "
                    "labd, and roll back automatically if it does not come up healthy.")
        self.say(r, msg)
        self.cleanup(mid)          # the branch keeps the commits; the deploy merges the branch

    # ------------------------------------------------------------ deploy
    def deploy_command(self, r) -> list[str]:
        base = self.mc.deploy_cmd or [
            "systemd-run", "--user", "--collect", "--quiet", f"--unit=lab-deploy-{r['id']}-{int(now())}",
            f"--setenv=PATH={os.environ.get('PATH', '')}", str(self.cfg.root / "bin" / "lab-deploy")]
        return [*base, r["id"], r["branch"], str(self.cfg.db_path)]

    async def tick(self) -> None:
        """Start the next approved deploy once the agents are idle. The deploy script reports back
        through `lab maint mark`."""
        busy = self.db.one("SELECT * FROM maint WHERE status='restarting'")
        if busy:
            if now() - (busy["updated_at"] or 0) > 900:
                self._set(busy["id"], status="failed", note="the deploy script never reported back")
                self.db.kv_set("_lab", "agents_hold_until", 0)
                self.say(busy, f"**{busy['id']}:** the deploy script never reported back. Check "
                               "`journalctl --user -u 'lab-deploy-*'` and `git log` in the lab.")
            return
        r = self.db.one("SELECT * FROM maint WHERE status='deploying' ORDER BY deploy_requested_at LIMIT 1")
        if not r:
            return
        # no new background runs while we wait: running ones finish, then labd restarts
        self.db.kv_set("_lab", "agents_hold_until", now() + 120)
        running = self.db.one("SELECT COUNT(*) n FROM agent_runs WHERE status='running' AND role!='concierge'")["n"]
        if running and now() - (r["deploy_requested_at"] or 0) < self.mc.idle_wait_s:
            return
        self.db.kv_set("_lab", "agents_hold_until", now() + 900)
        self._set(r["id"], status="restarting",
                  note=f"deploy started ({running} agent run(s) were still going)" if running else "deploy started")
        proc = await asyncio.create_subprocess_exec(*self.deploy_command(r), stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.STDOUT)
        out, _ = await proc.communicate()
        if proc.returncode:
            self.mark(r["id"], "failed", f"could not start the deploy: {out.decode(errors='replace')[-500:]}")

    def mark(self, mid: str, status: str, text: str) -> None:
        """The deploy's outcome (called by bin/lab-deploy through `lab maint mark`)."""
        r = self.get(mid)
        if not r:
            raise MaintError(f"no {mid}")
        self._set(mid, status=status, note=text[:1000])
        self.db.kv_set("_lab", "agents_hold_until", 0)
        self.db.emit(r["project"], f"maint.{status}", f"{mid}: {text[:300]}", key=mid,
                     severity="normal" if status == "deployed" else "major")
        head = {"deployed": f"**{mid} deployed.**", "rolled_back": f"**{mid} rolled back.**"}.get(
            status, f"**{mid} failed to deploy.**")
        self.say(r, f"{head} {text[:1500]}")
