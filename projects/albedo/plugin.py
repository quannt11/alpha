"""Albedo (SN97) sentinel sources and World State builder.

Sources are the public surfaces of https://albedo.tech (Cloudflare over the `albedo` R2 bucket; feeds carry
ETags, so polling is cheap). `data/dashboard.json` is the hot path: the reign (5 king slots), the last 200
evals and the last 200 failures. The scoring knobs that are not on the dashboard come from the newest eval's
`request.json` (judge config hash, dataset manifest hash, sample count, GPU request). Not used:
the Hippius mirror (`s3.hippius.com/albedo/…`, stale since 2026-06) and `api.albedo.tech/status`, which
serves whatever king the chat gateway loaded and lags the reign.
"""
from __future__ import annotations

import hashlib
import json
import re

from lab.sentinel import Change, Emit, Source, flatten

SITE = "https://albedo.tech"
DASHBOARD = f"{SITE}/data/dashboard.json"
LLMS = f"{SITE}/llms.txt"
DATASET_META = f"{SITE}/datasets/manifest.meta.json"
EVAL_DIR = SITE + "/albedo-eval-service/submissions/{sid}/eval/{rid}"
GITHUB = "https://api.github.com/repos/unarbos/albedo/commits?per_page=15"
RAW = "https://raw.githubusercontent.com/unarbos/albedo/main/"

# Upstream files whose change can move the score (major) or the admission rules (normal).
SCORING_FILES = (
    "src/albedo_eval_service/judge_core.py",
    "src/albedo_eval_service/judge_api.py",
    "src/albedo_eval_service/judge/prompt_judge.py",
    "src/albedo_eval_service/remote/worker.py",
    "src/albedo_eval_service/remote/generation.py",
    "src/albedo_eval_service/remote/dataset.py",
    "src/albedo_eval_service/shared/loop_check.py",
    "src/albedo_eval_service/shared/observation_format.py",
    "src/albedo_eval_service/shared/dataset_manifest.py",
    "src/albedo_config/config.py",
    "docs/SCORING.md",
)
ADMISSION_FILES = (
    "chain.toml",
    "docs/MINING.md",
    "docs/PRIVATE_UPLOADS.md",
    "src/model_validation/validate_worker.py",
    "src/model_validation/dedup/gate.py",
    "src/sanity_service/dispatcher.py",
    "src/chain_guard/__init__.py",
)
# request.json fields that are per-submission, not rules
REQUEST_SKIP = ("submission_id", "eval_run_id", "artifact_prefix", "challenger.", "previous_king.",
                "dataset.sample_ids", "dataset.sample_seed")

MAJOR_SECTION = re.compile(r"duel|scor|judge|pipeline|validation|pre-eval|sanity|coronation|weights|chain\.toml", re.I)


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


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


def _king(dash: dict) -> dict:
    members = (dash.get("reign") or {}).get("members") or []
    return next((m for m in members if m.get("is_king")), members[0] if members else {})


# ---------------------------------------------------------------- fetchers


def board_fetcher(our_hotkeys: list[str]):
    async def fetch_board(f):
        d = await f.json(DASHBOARD)
        king = _king(d)
        out = {f"king.{k}": king.get(k) for k in ("king_version", "hotkey", "uid", "model_hash", "model_uri",
                                                  "eval_run_id", "score_challenger", "score_king")}
        # there is no crowned_at on the reign: take it from the crowning eval, while it is among the last 200
        crowned = next((e for e in d.get("eval_runs") or [] if e.get("eval_run_id") == king.get("eval_run_id")), {})
        out["king.crowned_at"] = crowned.get("finished_at")
        out["reign"] = [{k: m.get(k) for k in ("king_version", "hotkey", "uid", "weight_bps")}
                        for m in (d.get("reign") or {}).get("members") or []]
        for m in (d.get("reign") or {}).get("members") or []:
            if m.get("hotkey") in our_hotkeys:
                out[f"ours.{m['hotkey']}"] = {k: m.get(k) for k in ("king_version", "uid", "weight_bps", "is_king")}
        return out
    return fetch_board


async def fetch_verdicts(f):
    d = await f.json(DASHBOARD)
    keep = ("finished_at", "hotkey", "uid", "challenger_won", "coronated", "king_version", "score_challenger",
            "score_king", "win_margin", "pass_margins", "required_win_margin", "scored_sample_count",
            "valid_turns", "total_turns", "judge_errors")
    return {e["eval_run_id"]: {"submission_id": e.get("submission_id"), **{k: e.get(k) for k in keep},
                               "vs_king": (e.get("king") or {}).get("king_version")}
            for e in (d.get("eval_runs") or [])[:30] if e.get("eval_run_id")}


async def fetch_fails(f):
    d = await f.json(DASHBOARD)
    return {x["submission_id"]: {"state": x.get("state"), "fault_code": x.get("fault_code"),
                                 "hotkey": x.get("hotkey"), "updated_at": x.get("updated_at"),
                                 "message": (x.get("fault_message") or "")[:300]}
            for x in (d.get("fails") or [])[:30] if x.get("submission_id")}


async def fetch_contract(f):
    """The rules a challenger is judged under: dashboard knobs plus the newest eval's request.json."""
    d = await f.json(DASHBOARD)
    runs = d.get("eval_runs") or []
    last = runs[0] if runs else {}
    out = {"chain.netuid": (d.get("chain") or {}).get("netuid"),
           "chain.judge_models": (d.get("chain") or {}).get("judge_models"),
           "eval.required_win_margin": last.get("required_win_margin"),
           "eval.scoring_mode": last.get("scoring_mode"),
           "eval.sample_count": last.get("sample_count")}
    if last.get("submission_id") and last.get("eval_run_id"):
        req = await f.json(EVAL_DIR.format(sid=last["submission_id"], rid=last["eval_run_id"]) + "/request.json")
        out.update({k: v for k, v in flatten(req).items() if not k.startswith(REQUEST_SKIP)})
    return out


async def fetch_dataset(f):
    d = await f.json(DATASET_META)
    out = {k: d.get(k) for k in ("version", "total_rows", "unique_instances")}
    out.update({f"sampling.{k}": v for k, v in flatten(d.get("sampling") or {}).items()})
    for s in d.get("sources") or []:
        name = s.get("name") or s.get("source") or s.get("id")
        if name:
            out[f"source.{name}"] = _sha(json.dumps(s, sort_keys=True))[:12]
    return out


async def fetch_llms(f):
    return split_sections(await f.text(LLMS))


async def fetch_code(f):
    out = {}
    for path in SCORING_FILES + ADMISSION_FILES:
        try:
            out[path] = await f.text(RAW + path)
        except Exception as e:  # one missing file should not blind the rest
            out[path] = f"(fetch failed: {e!r})"
    return out


async def fetch_github(f):
    d = await f.json(GITHUB)
    return {c["sha"][:12]: {"message": c["commit"]["message"].splitlines()[0][:200],
                            "date": c["commit"]["committer"]["date"]} for c in d}


# ---------------------------------------------------------------- classifiers


def classify_contract(changes: list[Change]) -> list[Emit]:
    keys = [c.key for c in changes]
    major = any(k.startswith(("scoring.", "eval.", "dataset.manifest", "dataset.version", "chain.judge"))
                for k in keys)
    judge = next((c for c in changes if c.key == "scoring.judge_config_hash"), None)
    head = f"judge config {judge.old} → {judge.new}; " if judge else ""
    return [Emit("world.change.contract", f"eval rules changed: {head}{len(changes)} knob(s): "
                 + ", ".join(keys[:12]) + (" …" if len(keys) > 12 else ""), "major" if major else "normal",
                 payload=[c.brief() for c in changes[:60]])]


def classify_llms(changes: list[Change]) -> list[Emit]:
    heads = [c.key for c in changes]
    sev = "major" if any(MAJOR_SECTION.search(h) for h in heads) else "normal"
    return [Emit("world.change.llms", f"llms.txt: {len(heads)} section(s) changed: " + "; ".join(heads[:8]), sev,
                 payload=[c.brief(limit=400) for c in changes[:25]])]


def classify_code(changes: list[Change]) -> list[Emit]:
    sev = "major" if any(c.key in SCORING_FILES for c in changes) else "normal"
    return [Emit("world.change.code", "upstream code changed: " + ", ".join(c.key for c in changes[:15]), sev,
                 payload=[c.brief(limit=200) for c in changes[:20]])]


def classify_dataset(changes: list[Change]) -> list[Emit]:
    v = next((c for c in changes if c.key == "version"), None)
    head = f"dataset {v.old} → {v.new}; " if v else ""
    return [Emit("world.change.corpus", f"{head}eval dataset changed: " + ", ".join(c.key for c in changes[:12]),
                 "major" if v else "normal", payload=[c.brief() for c in changes[:40]])]


def classify_board(changes: list[Change]) -> list[Emit]:
    out: list[Emit] = []
    new = {c.key: c.new for c in changes}
    ver = next((c for c in changes if c.key in ("king.model_hash", "king.king_version")), None)
    if ver:
        old_v = next((c.old for c in changes if c.key == "king.king_version"), None)
        out.append(Emit("board.king", f"new king: #{new.get('king.king_version', '?')} (uid {new.get('king.uid')}, "
                                      f"hotkey {str(new.get('king.hotkey'))[:10]}…), was #{old_v or '?'}",
                        "major", payload=[c.brief() for c in changes]))
    for c in changes:
        if c.key.startswith("ours."):
            if c.kind == "added":
                out.append(Emit("board.ours", f"our hotkey {c.key[5:15]}… is in the reign: {c.new}", "major",
                                payload=c.brief()))
            elif c.kind == "removed":
                out.append(Emit("board.ours", f"our hotkey {c.key[5:15]}… left the reign (no longer paid)",
                                "major", payload=c.brief()))
    if not ver and any(c.key == "reign" for c in changes):
        out.append(Emit("board.payout", "reign slots changed", "info", payload=[c.brief() for c in changes]))
    return out


def classify_verdicts(changes: list[Change]) -> list[Emit]:
    new = [c for c in changes if c.kind == "added"]
    if not new:
        return []
    won = [c for c in new if (c.new or {}).get("challenger_won")]
    best = max((c.new or {}).get("win_margin") or -9 for c in new)
    return [Emit("board.verdict", f"{len(new)} new eval(s), {len(won)} won" + f", best margin {best:+.4f}",
                 "info", payload=[{"id": c.key, **(c.new or {})} for c in new])]


def classify_fails(changes: list[Change]) -> list[Emit]:
    new = [c for c in changes if c.kind == "added"]
    if not new:
        return []
    codes: dict[str, int] = {}
    for c in new:
        code = (c.new or {}).get("fault_code") or "?"
        codes[code] = codes.get(code, 0) + 1
    return [Emit("board.fails", f"{len(new)} submission(s) failed: "
                 + ", ".join(f"{k}×{v}" for k, v in sorted(codes.items(), key=lambda x: -x[1])), "info",
                 payload=[{"id": c.key, **(c.new or {})} for c in new])]


def classify_github(changes: list[Change]) -> list[Emit]:
    new = [c for c in changes if c.kind == "added"]
    if not new:
        return []
    return [Emit("upstream.commit", f"{len(new)} new upstream commit(s): " +
                 "; ".join((c.new or {}).get("message", "")[:80] for c in new[:5]), "info",
                 payload=[{"sha": c.key, **(c.new or {})} for c in new])]


# ---------------------------------------------------------------- plugin API


def sources(project) -> list[Source]:
    cfg = project.extra.get("albedo", {})
    ours = [h for h in cfg.get("our_hotkeys", []) if h]
    iv = project.sentinel.get("intervals", {})
    return [
        Source("board", iv.get("board", 60), board_fetcher(ours), classify_board),
        Source("verdicts", iv.get("verdicts", 60), fetch_verdicts, classify_verdicts),
        Source("fails", iv.get("fails", 300), fetch_fails, classify_fails),
        Source("contract", iv.get("contract", 300), fetch_contract, classify_contract),
        Source("dataset", iv.get("dataset", 900), fetch_dataset, classify_dataset),
        Source("llms", iv.get("llms", 300), fetch_llms, classify_llms),
        Source("code", iv.get("code", 900), fetch_code, classify_code),
        Source("upstream", iv.get("upstream", 900), fetch_github, classify_github),
    ]


def world_lines(w: dict) -> list[str]:
    """The facts every agent prompt shows (after the world_version line)."""
    c = w.get("contract", {})
    keys = [k for k in ("scoring.judge_config_hash", "eval.scoring_mode", "eval.required_win_margin",
                        "eval.sample_count", "scoring.judge_count", "chain.judge_models", "dataset.version",
                        "dataset.manifest_hash", "gpu_request.accelerator", "gpu_request.min_gpus") if k in c]
    fails = w.get("recent_fault_codes") or {}
    return ["eval rules: " + ", ".join(f"{k}={c[k]}" for k in keys),
            f"dataset: {json.dumps(w.get('dataset'))}",
            f"king: {json.dumps(w.get('king'))}",
            f"reign (slot 1 = king, paid weight_bps each): {json.dumps(w.get('reign'))}",
            f"last evals: {json.dumps(w.get('last_evals'))}",
            f"recent failure codes (last 30): {json.dumps(fails)}",
            f"our reign slots: {json.dumps(w.get('ours')) if w.get('ours') else '(none configured / none held)'}"]


def build_world(facts: dict[str, dict]) -> dict:
    contract = facts.get("contract", {})
    board = facts.get("board", {})
    dataset = facts.get("dataset", {})
    rules = {k: contract[k] for k in sorted(contract) if not k.startswith("gpu_request.")}
    rules.update({f"dataset.{k}": v for k, v in dataset.items() if k == "version" or k.startswith("sampling.")})
    csha = _sha("".join(f"{k}={rules[k]}" for k in sorted(rules)))[:8] if rules else "none"
    verdicts = sorted(facts.get("verdicts", {}).values(), key=lambda v: v.get("finished_at") or "", reverse=True)
    codes: dict[str, int] = {}
    for x in facts.get("fails", {}).values():
        codes[x.get("fault_code") or "?"] = codes.get(x.get("fault_code") or "?", 0) + 1
    return {
        # sn97-k<king>-c<rules>: the middle part is not a rules change (context.rules_of drops it)
        "world_version": f"sn97-k{board.get('king.king_version')}-c{csha}",
        "contract": contract,
        "dataset": {k: v for k, v in dataset.items() if not k.startswith("source.")},
        "king": {k[5:]: v for k, v in board.items() if k.startswith("king.")},
        "reign": board.get("reign"),
        "ours": {k[5:]: v for k, v in board.items() if k.startswith("ours.")},
        "last_evals": [{k: v.get(k) for k in ("finished_at", "uid", "challenger_won", "coronated", "win_margin",
                                              "pass_margins", "score_challenger", "score_king")}
                       for v in verdicts[:5]],
        "recent_fault_codes": dict(sorted(codes.items(), key=lambda x: -x[1])),
        "llms": {"sections": list(facts.get("llms", {}).keys())},
        "code": {k: _sha(v)[:12] for k, v in facts.get("code", {}).items()},
    }
