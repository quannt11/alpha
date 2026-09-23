"""Affine (SN120) sentinel sources and World State builder.

Sources are the public surfaces listed in https://affine.io/llms.txt. The
dashboard API is the hot path; the Hippius mirror is the fallback. Note that
data.affine.io/turns/manifest.json is a legacy pointer (epoch 13) — the live
corpus epoch comes from api/v1/dataset.
"""
from __future__ import annotations

import hashlib
import re
import time
from datetime import datetime, timezone

from lab.sentinel import PARTIAL, Change, Emit, Source, flatten

API = "https://affine.io/api/v1"
HIPPIUS = "https://s3.hippius.com/affine-sn120"
LLMS = ("https://affine.io/llms.txt", f"{HIPPIUS}/llms.txt")
GITHUB = "https://api.github.com/repos/AffineFoundation/affine/commits?per_page=15"
CURRICULUM = "https://data.affine.io/curriculum/latest.json"

MAJOR_SECTION = re.compile(r"fork history|upcoming|notice|payout rule|crown rule", re.I)
CODE_LINK = re.compile(r"\((https://s3\.hippius\.com/affine-sn120/code/[^)\s]+)\)")


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def _parse_ts(s: str | None) -> float | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def split_sections(text: str) -> dict[str, str]:
    """`## Heading` sections keyed by heading; the preamble is `(preamble)`."""
    out: dict[str, str] = {}
    cur, buf = "(preamble)", []
    seen: dict[str, int] = {}
    for line in text.splitlines():
        if line.startswith("## "):
            out[cur] = "\n".join(buf).strip()
            head = line[3:].strip()
            seen[head] = seen.get(head, 0) + 1
            cur = head if seen[head] == 1 else f"{head} ({seen[head]})"
            buf = []
        else:
            buf.append(line)
    out[cur] = "\n".join(buf).strip()
    return out


# ---------------------------------------------------------------- fetchers


async def fetch_contract(f):
    c = await f.json(f"{API}/contract", fallbacks=(f"{HIPPIUS}/data/contract.json",))
    out = {k: v for k, v in flatten(c).items()}
    if f.fell_back:
        out[PARTIAL] = True
    return out


async def fetch_llms(f):
    out = split_sections(await f.text(LLMS[0], fallbacks=LLMS[1:]))
    if f.fell_back:
        out[PARTIAL] = True
    return out


async def fetch_code(f):
    text = await f.text(LLMS[0], fallbacks=LLMS[1:])
    urls = sorted(set(CODE_LINK.findall(text)))
    out = {}
    for u in urls:
        path = u.split("/code/", 1)[1]
        try:
            out[path] = await f.text(u)
        except Exception as e:  # one missing file should not blind the rest
            out[path] = f"(fetch failed: {e!r})"
    return out


async def fetch_dataset(f):
    d = await f.json(f"{API}/dataset")
    keep = ("corpus_epoch", "manifest_sha256", "n_turns", "n_trajectories", "n_chunks", "schema_version",
            "view_spec")
    return {k: d.get(k) for k in keep}


async def fetch_curriculum(f):
    d = await f.json(CURRICULUM)
    out = {k: d.get(k) for k in ("for_epoch", "against_epoch", "mode", "rule_version", "ledger_sha256",
                                  "manifest_sha256")}
    for k, v in (d.get("shares_after_clamp") or {}).items():
        out[f"share.{k}"] = round(v, 4) if isinstance(v, float) else v
    return out


def board_fetcher(our_hotkeys: list[str], warn_hours: float):
    async def fetch_board(f):
        s = await f.json(f"{API}/snapshot", fallbacks=(f"{HIPPIUS}/data/dashboard.json",))
        partial = f.fell_back
        king = s.get("king") or {}
        out = {f"king.{k}": king.get(k) for k in ("revision", "reign_number", "hotkey", "crowned_at", "score")}
        pay = s.get("payout") or {}
        out["payout.burn"] = pay.get("burn")
        out["payout.paid"] = sorted(
            [{"reign": p.get("reign_number"), "hotkey": p.get("hotkey"), "share": p.get("share"),
              "paid_until": p.get("paid_until")} for p in pay.get("paid", [])], key=lambda p: p["reign"] or 0)
        members = (s.get("reign") or {}).get("members", [])
        now = time.time()
        for m in members:
            if m.get("hotkey") in our_hotkeys:
                until = _parse_ts(m.get("paid_until"))
                left = (until - now) / 3600 if until else None
                out[f"ours.reign_{m.get('reign_number')}"] = {
                    "paid_until": m.get("paid_until"), "earning": m.get("earning"),
                    "expiring_soon": bool(left is not None and 0 < left < warn_hours),
                    "expired": bool(m.get("expired")),
                }
        if partial:
            out[PARTIAL] = True
        return out
    return fetch_board


async def fetch_verdicts(f):
    d = await f.json(f"{API}/history?limit=30")
    out = {}
    for it in d.get("items", []):
        key = f"{it.get('challenge_id')}:{it.get('event')}"
        out[key] = {k: it.get(k) for k in ("event", "at", "uid", "hotkey", "accepted", "margin", "se", "z",
                                          "score", "score_king", "reign_number", "rejection_reason",
                                          "error_code", "n_paired_turns", "n_forfeit_turns")}
    return out


async def fetch_audits(f):
    d = await f.json(f"{API}/audits", fallbacks=(f"{HIPPIUS}/data/audits.json",))
    partial = f.fell_back
    items = d.get("items", d if isinstance(d, list) else [])
    out = {f"reign_{a.get('reign_number')}": {k: a.get(k) for k in ("status", "exploit", "confidence",
                                                                    "summary", "audited_at")}
           for a in items}
    if partial:
        out[PARTIAL] = True
    return out


async def fetch_github(f):
    d = await f.json(GITHUB)
    return {c["sha"][:12]: {"message": c["commit"]["message"].splitlines()[0][:200],
                            "date": c["commit"]["committer"]["date"]} for c in d}


# ---------------------------------------------------------------- classifiers


def classify_contract(changes: list[Change]) -> list[Emit]:
    wvk = next((c for c in changes if c.key == "subnet.weight_version_key"), None)
    sev = "major" if wvk or any(c.key.startswith(("duel.", "teacher.", "payout.")) for c in changes) else "normal"
    head = f"weight_version_key {wvk.old} → {wvk.new}; " if wvk else ""
    keys = ", ".join(c.key for c in changes[:12]) + (" …" if len(changes) > 12 else "")
    return [Emit("world.change.contract", f"contract changed: {head}{len(changes)} knob(s): {keys}", sev,
                 payload=[c.brief() for c in changes[:60]])]


def classify_llms(changes: list[Change]) -> list[Emit]:
    heads = [c.key for c in changes if c.key != "Table of contents"]
    if not heads:
        return []
    sev = "major" if any(
        MAJOR_SECTION.search(c.key) and (c.kind == "added" or "upcoming" in c.key.lower()) for c in changes
    ) else "normal"
    return [Emit("world.change.llms", f"llms.txt: {len(heads)} section(s) changed: " + "; ".join(heads[:8]), sev,
                 payload=[c.brief(limit=400) for c in changes[:25]])]


def classify_code(changes: list[Change]) -> list[Emit]:
    scoring = ("score.py", "dueling.py", "chat.py", "affine.toml", "terms.py", "vllm_client.py", "dialects.py")
    sev = "major" if any(c.key.endswith(scoring) for c in changes) else "normal"
    return [Emit("world.change.code", "validator code changed: " + ", ".join(c.key for c in changes[:15]), sev,
                 payload=[c.brief(limit=200) for c in changes[:20]])]


def classify_dataset(changes: list[Change]) -> list[Emit]:
    ep = next((c for c in changes if c.key == "corpus_epoch"), None)
    if ep:
        return [Emit("world.change.corpus", f"corpus epoch {ep.old} → {ep.new}", "normal",
                     payload=[c.brief() for c in changes])]
    return [Emit("world.change.corpus", "corpus stats changed: " + ", ".join(c.key for c in changes), "minor",
                 payload=[c.brief() for c in changes])]


def classify_curriculum(changes: list[Change]) -> list[Emit]:
    return [Emit("world.change.curriculum", f"curriculum weights changed ({len(changes)} field(s))", "normal",
                 payload=[c.brief() for c in changes[:40]])]


def classify_board(changes: list[Change]) -> list[Emit]:
    out: list[Emit] = []
    rev = next((c for c in changes if c.key == "king.revision"), None)
    if rev:
        new = {c.key: c.new for c in changes}
        out.append(Emit("board.king", f"new king: reign {new.get('king.reign_number')} "
                                      f"(hotkey {str(new.get('king.hotkey'))[:10]}…), was {str(rev.old)[:12]}",
                        "major", payload=[c.brief() for c in changes]))
    for c in changes:
        if c.key.startswith("ours.") and isinstance(c.new, dict):
            if c.new.get("expiring_soon") and not (c.old or {}).get("expiring_soon"):
                out.append(Emit("board.crown_expiring", f"our crown {c.key[5:]} stops being paid at "
                                                        f"{c.new.get('paid_until')}", "major", payload=c.new))
    pay = [c for c in changes if c.key.startswith("payout.")]
    if pay and not rev:
        out.append(Emit("board.payout", "payout set changed", "info", payload=[c.brief() for c in pay]))
    return out


def classify_verdicts(changes: list[Change]) -> list[Emit]:
    new = [c for c in changes if c.kind == "added"]
    if not new:
        return []
    crowned = [c for c in new if (c.new or {}).get("event") == "crowned"]
    return [Emit("board.verdict", f"{len(new)} new verdict(s)" + (f", {len(crowned)} crowned" if crowned else ""),
                 "info", payload=[{"id": c.key, **(c.new or {})} for c in new])]


def classify_audits(changes: list[Change]) -> list[Emit]:
    out = []
    for c in changes:
        if c.new and c.new.get("exploit"):
            out.append(Emit("board.audit", f"exploit audit REVERT for {c.key}: {str(c.new.get('summary'))[:200]}",
                            "major", payload=c.brief()))
        elif c.kind == "added":
            out.append(Emit("board.audit", f"audit published for {c.key}: {c.new.get('status')}", "info",
                            payload=c.brief()))
    return out


def classify_github(changes: list[Change]) -> list[Emit]:
    new = [c for c in changes if c.kind == "added"]
    if not new:
        return []
    return [Emit("upstream.commit", f"{len(new)} new upstream commit(s): " +
                 "; ".join((c.new or {}).get("message", "")[:80] for c in new[:5]), "info",
                 payload=[{"sha": c.key, **(c.new or {})} for c in new])]


# ---------------------------------------------------------------- plugin API


def sources(project) -> list[Source]:
    cfg = project.extra.get("affine", {})
    ours = [h for h in cfg.get("our_hotkeys", []) if h]
    warn = float(cfg.get("crown_warn_hours", 12))
    iv = project.sentinel.get("intervals", {})
    return [
        Source("contract", iv.get("contract", 300), fetch_contract, classify_contract),
        Source("llms", iv.get("llms", 300), fetch_llms, classify_llms),
        Source("code", iv.get("code", 900), fetch_code, classify_code),
        Source("corpus", iv.get("corpus", 600), fetch_dataset, classify_dataset),
        Source("curriculum", iv.get("curriculum", 900), fetch_curriculum, classify_curriculum),
        Source("board", iv.get("board", 60), board_fetcher(ours, warn), classify_board),
        Source("verdicts", iv.get("verdicts", 120), fetch_verdicts, classify_verdicts),
        Source("audits", iv.get("audits", 600), fetch_audits, classify_audits),
        Source("upstream", iv.get("upstream", 3600), fetch_github, classify_github),
    ]


def build_world(facts: dict[str, dict]) -> dict:
    contract = facts.get("contract", {})
    corpus = facts.get("corpus", {})
    board = facts.get("board", {})
    cur = facts.get("curriculum", {})
    wvk = contract.get("subnet.weight_version_key")
    csha = _sha("".join(f"{k}={contract[k]}" for k in sorted(contract)))[:8] if contract else "none"
    sections = list(facts.get("llms", {}).keys())
    wvk_of = lambda s: int(m.group(1)) if (m := re.search(r"wvk (\d+)", s)) else -1
    forks = sorted((s for s in sections if s.lower().startswith("fork history")), key=wvk_of, reverse=True)
    return {
        "world_version": f"wvk{wvk}-e{corpus.get('corpus_epoch')}-c{csha}",
        "contract": contract,
        "teacher": contract.get("teacher.repo"),
        "corpus": corpus,
        "curriculum": {k: v for k, v in cur.items() if not k.startswith("share.")},
        "curriculum_shares": {k[6:]: v for k, v in cur.items() if k.startswith("share.")},
        "king": {k[5:]: v for k, v in board.items() if k.startswith("king.")},
        "payout": {"burn": board.get("payout.burn"), "paid": board.get("payout.paid")},
        "ours": {k[5:]: v for k, v in board.items() if k.startswith("ours.")},
        "llms": {"latest_fork": forks[0] if forks else None, "sections": sections},
        "code": {k: _sha(v)[:12] for k, v in facts.get("code", {}).items()},
    }
