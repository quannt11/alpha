"""The albedo plugin against small copies of albedo.tech's feeds (offline)."""
import asyncio
import copy
import json
from pathlib import Path

import pytest

from lab.context import rules_of
from lab.sentinel import Change, load_plugin

ROOT = Path(__file__).resolve().parents[1]
PL = load_plugin(ROOT / "projects" / "albedo" / "plugin.py")

KING = {"king_version": 127, "hotkey": "5DFsan", "uid": 242, "weight_bps": 2000, "model_hash": "sha256:9c4d",
        "model_uri": "s3://x/a3@sha256:9c4d", "is_king": True, "eval_run_id": "r-crown",
        "score_challenger": 0.76, "score_king": 0.72}
DASH = {
    "chain": {"netuid": 97, "judge_models": ["z-ai/glm-5.2"]},
    "reign": {"members": [KING, {**KING, "king_version": 126, "hotkey": "5HGq", "uid": 114, "is_king": False,
                                 "model_hash": "sha256:486d", "eval_run_id": "r-old"}]},
    "eval_runs": [
        {"eval_run_id": "r-2", "submission_id": "s-2", "challenger_won": False, "coronated": False,
         "win_margin": -0.0085, "pass_margins": [-0.0085], "required_win_margin": 0.025, "scoring_mode": "graded_20",
         "sample_count": 100, "finished_at": "2026-09-25T02:38:26+00:00", "uid": 70, "king": {"king_version": 127}},
        {"eval_run_id": "r-crown", "submission_id": "s-1", "challenger_won": True, "coronated": True,
         "win_margin": 0.037, "finished_at": "2026-09-20T16:57:37+00:00", "uid": 242},
    ],
    "fails": [{"submission_id": "s-9", "state": "TERMINAL_INVALID", "fault_code": "duplicate",
               "fault_message": "TRIVIAL-EDIT", "hotkey": "5Ck3", "updated_at": "2026-09-24T05:24:26+00:00"}],
}
REQUEST = {"submission_id": "s-2", "eval_run_id": "r-2", "challenger": {"model_hash": "sha256:1754"},
           "previous_king": {"king_version": 168},
           "dataset": {"manifest_hash": "e3cff6", "sample_count": 100, "sample_ids": ["a", "b"], "sample_seed": "0x8",
                       "version": "mini-coder+open-swe+smith-rs+hero-v1"},
           "gpu_request": {"accelerator": "B200", "min_gpus": 8},
           "scoring": {"judge_config_hash": "judge-67c2425-winboth-2026-08-14", "judge_count": 1}}
META = {"version": "mini-coder+open-swe+smith-rs+hero-v1", "total_rows": 426687, "unique_instances": 82364,
        "sampling": {"repo_cap": 2}, "sources": [{"name": "mini-coder", "sha256": "aa"}]}


class Fake:
    def __init__(self, dash=DASH, request=REQUEST):
        self.docs = {PL.DASHBOARD: dash, PL.DATASET_META: META,
                     PL.EVAL_DIR.format(sid="s-2", rid="r-2") + "/request.json": request,
                     PL.GITHUB: [{"sha": "7cbc720aaaaaaaa", "commit": {"message": "fix: x\nbody",
                                                                        "committer": {"date": "2026-09-23"}}}]}
        self.fell_back = False

    async def json(self, url, fallbacks=()):
        return copy.deepcopy(self.docs[url])

    async def text(self, url, fallbacks=()):
        if url == PL.LLMS:
            return "# Albedo\nintro\n## The duel (eval service internals)\nmargin 0.025\n## Links\nx"
        return f"content of {url}"


class P:
    extra = {"albedo": {"our_hotkeys": ["5HGq"]}}
    sentinel = {}


def poll(fake=None):
    fake = fake or Fake()
    return {s.name: asyncio.run(s.fetch(fake)) for s in PL.sources(P())}


def test_world_from_the_feeds():
    facts = poll()
    w = PL.build_world(facts)
    assert w["world_version"].startswith("sn97-k127-c")
    assert w["king"]["crowned_at"] == "2026-09-20T16:57:37+00:00" and w["king"]["model_hash"] == "sha256:9c4d"
    c = w["contract"]
    assert c["scoring.judge_config_hash"].startswith("judge-67c2425") and c["eval.required_win_margin"] == 0.025
    assert c["dataset.sample_count"] == 100   # a knob; the ids and seed are per-submission
    assert not any(k.startswith(("challenger.", "previous_king.", "dataset.sample_ids", "dataset.sample_seed")) for k in c)
    assert w["ours"] == {"5HGq": {"king_version": 126, "uid": 114, "weight_bps": 2000, "is_king": False}}
    assert w["recent_fault_codes"] == {"duplicate": 1} and w["last_evals"][0]["win_margin"] == -0.0085
    lines = "\n".join(PL.world_lines(w))
    assert "judge_config_hash=judge-67c2425" in lines and "our reign slots" in lines
    assert facts["llms"]["The duel (eval service internals)"] == "margin 0.025"
    assert set(facts["code"]) == set(PL.SCORING_FILES + PL.ADMISSION_FILES)


def test_a_new_king_changes_only_the_non_rules_part_of_the_version():
    before = PL.build_world(poll())
    dash = copy.deepcopy(DASH)
    dash["reign"]["members"].insert(0, {**KING, "king_version": 128, "model_hash": "sha256:new", "hotkey": "5New"})
    dash["reign"]["members"][1]["is_king"] = False
    after = PL.build_world(poll(Fake(dash=dash)))
    assert after["world_version"] != before["world_version"]
    assert rules_of(after["world_version"]) == rules_of(before["world_version"])
    req = copy.deepcopy(REQUEST)
    req["scoring"]["judge_config_hash"] = "judge-new"
    assert rules_of(PL.build_world(poll(Fake(request=req)))["world_version"]) != rules_of(before["world_version"])


def test_classifiers():
    ev = PL.classify_board([Change("king.king_version", "changed", 127, 128),
                            Change("king.model_hash", "changed", "a", "b"), Change("king.uid", "changed", 242, 7)])
    assert [e.topic for e in ev] == ["board.king"] and ev[0].severity == "major" and "#128" in ev[0].summary
    ev = PL.classify_board([Change("ours.5HGq", "removed", {"uid": 114}, None)])
    assert ev[0].topic == "board.ours" and ev[0].severity == "major"
    ev = PL.classify_contract([Change("scoring.judge_config_hash", "changed", "judge-a", "judge-b")])
    assert ev[0].severity == "major" and "judge-a → judge-b" in ev[0].summary
    assert PL.classify_contract([Change("gpu_request.accelerator", "changed", "B200", "H200")])[0].severity == "normal"
    assert PL.classify_code([Change("docs/MINING.md", "changed", "a", "b")])[0].severity == "normal"
    assert PL.classify_code([Change("src/albedo_eval_service/judge_core.py", "changed", "a", "b")])[0].severity == "major"
    ev = PL.classify_verdicts([Change("r-3", "added", None, {"challenger_won": True, "win_margin": 0.03}),
                               Change("r-4", "added", None, {"challenger_won": False, "win_margin": -0.01})])
    assert ev[0].summary == "2 new eval(s), 1 won, best margin +0.0300" and ev[0].severity == "info"
    assert PL.classify_dataset([Change("version", "changed", "v1", "v2")])[0].severity == "major"


def test_albedo_project_config_is_in_watch_mode(tmp_path):
    from lab import config as config_mod
    p = config_mod.load_project(ROOT / "projects" / "albedo")
    assert p.pod_prefix == "Pi_albedo" and p.channel_id
    # watch mode: no GPUs, no threads, no Researcher (going live is the operator's call)
    assert p.daily_usd == 0 and p.max_threads == 0 and p.research_tick_hours == 0
    assert p.roles["researcher"].wake_on == [] and p.roles["scout"].wake_on
