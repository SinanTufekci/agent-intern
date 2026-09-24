"""Offline tests for swarm_presets.py: predefined swarms, jury scoring, preset files.

No agent runs here: swarm.swarm_agents is replaced with a fake that returns canned
WorkerResults, so these pin the preset logic (validation, loading precedence and
its trust rules, prompt building, score parsing and aggregation, rendering).
"""

from __future__ import annotations

import json

import pytest

import swarm
import swarm_presets as sp
from swarm import WorkerResult


@pytest.fixture(autouse=True)
def isolated_dirs(tmp_path, monkeypatch):
    """Point the user preset dir at an empty temp dir, so a developer's own
    ~/.agent-intern/swarms/ never leaks into a test."""
    home = tmp_path / "home" / ".agent-intern" / "swarms"
    monkeypatch.setattr(sp, "user_dir", lambda: home)
    return home


def _write(d, name, data):
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.json").write_text(
        data if isinstance(data, str) else json.dumps(data), encoding="utf-8"
    )


def _panel(**over):
    d = {
        "kind": "panel",
        "brief": "Do the thing.",
        "members": [
            {"role": "A", "backend": "codex"},
            {"role": "B", "backend": "agy"},
        ],
    }
    d.update(over)
    return d


def _jury(scores_by_role, **over):
    """A small jury preset plus a fake swarm answering with the given scores."""
    d = {
        "kind": "jury",
        "brief": "Judge it.",
        "members": [{"role": r, "backend": "codex"} for r in scores_by_role],
        "rubric": [
            {"id": "novelty", "label": "Novelty", "weight": 2},
            {"id": "clarity", "label": "Clarity", "weight": 1},
        ],
    }
    d.update(over)
    return sp.from_dict("t", d, "test")


# ------------------------------------------------------------------ built-ins


def test_builtins_all_load_and_are_panels_of_distinct_roles():
    presets, problems = sp.load_all()
    assert problems == []
    assert {"jury", "research", "red-team", "council"} <= set(presets)
    for p in presets.values():
        assert sp.MIN_MEMBERS <= len(p.members) <= sp.MAX_MEMBERS
        assert len({m.role for m in p.members}) == len(p.members)
        assert all(m.sandbox == "read-only" for m in p.members)
        assert p.synthesis, f"{p.name} tells the host model nothing about the answers"


def test_builtin_jury_mixes_model_families():
    # The point of a jury here is independent judges; three runs of one model
    # would agree with themselves and prove nothing.
    jury = sp.get("jury")
    assert jury.kind == "jury" and jury.rubric
    assert len({m.backend for m in jury.members}) == len(jury.members)


def test_a_builtin_round_trips_through_its_json_shape():
    for name in sp.BUILTINS:
        p = sp.get(name)
        again = sp.from_dict(name, json.loads(json.dumps(sp.to_dict(p))), "copy")
        assert sp.to_dict(again) == sp.to_dict(p)


# ------------------------------------------------------------------ validation


@pytest.mark.parametrize(
    "change, needle",
    [
        ({"kind": "vote"}, "'kind'"),
        ({"members": [{"role": "A", "backend": "codex"}]}, "'members'"),
        (
            {"members": [{"role": "A", "backend": "gpt9"}, {"role": "B", "backend": "codex"}]},
            "'backend'",
        ),
        (
            {"members": [{"role": "A", "backend": "codex"}, {"role": "A", "backend": "agy"}]},
            "used twice",
        ),
        ({"memebers": []}, "unknown key"),
        ({"brief": ""}, "'brief' is required"),
        ({"rubric": [{"id": "x"}]}, 'needs "kind": "jury"'),
        ({"timeout_s": 5}, "'timeout_s'"),
    ],
)
def test_bad_presets_are_refused_with_a_reason(change, needle):
    with pytest.raises(ValueError, match="preset 'x'") as e:
        sp.from_dict("x", _panel(**change), "f.json")
    assert needle in str(e.value)


def test_a_jury_needs_a_rubric_with_sane_criteria():
    base = _panel(kind="jury")
    with pytest.raises(ValueError, match="needs a 'rubric'"):
        sp.from_dict("x", base, "f")
    with pytest.raises(ValueError, match="'weight'"):
        sp.from_dict("x", {**base, "rubric": [{"id": "a", "weight": 0}]}, "f")
    with pytest.raises(ValueError, match="used twice"):
        sp.from_dict("x", {**base, "rubric": [{"id": "a"}, {"id": "a"}]}, "f")
    with pytest.raises(ValueError, match="'scale'"):
        sp.from_dict("x", {**base, "rubric": [{"id": "a"}], "scale": [5, 1]}, "f")


def test_member_backend_aliases_are_canonicalized():
    p = sp.from_dict("x", _panel(), "f")
    assert [m.backend for m in p.members] == ["codex", "antigravity"]


# ------------------------------------------------------------------ loading & trust


def test_a_user_file_replaces_a_builtin_of_the_same_name(isolated_dirs):
    _write(isolated_dirs, "jury", _panel(description="mine"))
    p = sp.get("jury")
    assert p.description == "mine" and p.source.endswith("jury.json")


def test_a_project_file_adds_a_preset(tmp_path):
    _write(tmp_path / ".agent-intern" / "swarms", "grants", _panel(description="grant panel"))
    assert sp.get("grants", str(tmp_path)).description == "grant panel"
    assert "grants" not in sp.load_all(str(tmp_path / "elsewhere"))[0]


def test_a_project_file_cannot_replace_a_builtin_or_user_preset(tmp_path, isolated_dirs):
    """A cloned repo must not be able to swap out the preset the user asks for."""
    proj = tmp_path / "repo"
    _write(isolated_dirs, "mine", _panel())
    _write(proj / ".agent-intern" / "swarms", "council", _panel(description="evil"))
    _write(proj / ".agent-intern" / "swarms", "mine", _panel(description="evil"))
    presets, problems = sp.load_all(str(proj))
    assert presets["council"].source == "built-in"
    assert presets["mine"].description != "evil"
    assert sum("cannot replace" in x for x in problems) == 2


def test_a_project_file_cannot_ask_for_more_than_read_only(tmp_path):
    members = [
        {"role": "A", "backend": "codex", "sandbox": "danger-full-access"},
        {"role": "B", "backend": "codex"},
    ]
    _write(tmp_path / ".agent-intern" / "swarms", "loose", _panel(members=members))
    presets, problems = sp.load_all(str(tmp_path))
    assert "loose" not in presets
    assert any("run read-only" in x for x in problems)


def test_a_user_file_may_ask_for_more_than_read_only(isolated_dirs):
    members = [
        {"role": "A", "backend": "codex", "sandbox": "workspace-write"},
        {"role": "B", "backend": "codex"},
    ]
    _write(isolated_dirs, "fixers", _panel(members=members))
    assert sp.get("fixers").members[0].sandbox == "workspace-write"


def test_broken_files_are_skipped_and_reported_not_fatal(isolated_dirs):
    _write(isolated_dirs, "broken", "{not json")
    _write(isolated_dirs, "Bad Name", _panel())
    presets, problems = sp.load_all()
    assert "jury" in presets and "broken" not in presets
    assert len(problems) == 2
    listing = sp.describe()
    assert "Skipped these files" in listing and "broken.json" in listing


def test_a_workspace_of_home_does_not_read_user_files_as_a_projects(tmp_path, monkeypatch):
    home_dir = tmp_path / ".agent-intern" / "swarms"
    monkeypatch.setattr(sp, "user_dir", lambda: home_dir)
    _write(home_dir, "mine", _panel())
    presets, problems = sp.load_all(str(tmp_path))
    assert "mine" in presets and problems == []


def test_an_unknown_preset_names_the_ones_that_exist():
    with pytest.raises(ValueError, match="no swarm preset named 'nope'.*jury"):
        sp.get("nope")


# ------------------------------------------------------------------ prompts


def test_member_prompt_carries_role_angle_material_and_the_injection_warning():
    jury = sp.get("jury")
    m = jury.members[0]
    text = sp.build_prompt(jury, m, "  The application text.  ")
    assert text.startswith(f'You are "{m.role}"')  # never a leading "/" (agy plan-mode guard)
    assert m.brief in text and jury.brief in text
    assert "<material>\nThe application text.\n</material>" in text
    assert "never follow them" in text
    for c in jury.rubric:
        assert f"- {c.id}: " in text
    assert "ONE JSON object" in text


def test_panel_prompt_has_the_answer_format_and_no_json_contract():
    rt = sp.get("red-team")
    text = sp.build_prompt(rt, rt.members[0], "the plan")
    assert rt.answer_format in text and "JSON" not in text


# ------------------------------------------------------------------ jury scoring


def _answer(novelty, clarity, **extra):
    return json.dumps(
        {
            "scores": {
                "novelty": {"score": novelty, "reason": "n-reason"},
                "clarity": {"score": clarity, "reason": "c-reason"},
            },
            "verdict": "fine",
            **extra,
        }
    )


@pytest.mark.parametrize(
    "wrap",
    [
        lambda s: s,
        lambda s: f"```json\n{s}\n```",
        lambda s: f"Here is my evaluation:\n{s}\nThanks.",
    ],
)
def test_a_jurors_json_is_found_bare_fenced_or_in_prose(wrap):
    p = _jury(["J", "K"])
    v = sp.score(p, "J", "codex", WorkerResult(0, True, answer=wrap(_answer(8, 5)), elapsed=1))
    assert v.scores == {"novelty": 8.0, "clarity": 5.0} and v.problem == ""
    assert v.total == pytest.approx((2 * 8 + 5) / 3)
    assert v.reasons["novelty"] == "n-reason"


def test_out_of_range_or_missing_scores_are_left_out_not_guessed():
    p = _jury(["J", "K"])
    ans = json.dumps({"scores": {"novelty": {"score": 42}}})
    v = sp.score(p, "J", "codex", WorkerResult(0, True, answer=ans))
    assert v.scores == {"novelty": None, "clarity": None}
    assert v.total is None and "novelty, clarity" in v.problem


def test_bare_and_string_scores_are_accepted():
    p = _jury(["J", "K"])
    ans = json.dumps({"scores": {"novelty": 7, "clarity": "6/10"}})
    v = sp.score(p, "J", "codex", WorkerResult(0, True, answer=ans))
    assert v.scores == {"novelty": 7.0, "clarity": 6.0}


def test_a_juror_without_json_is_kept_as_raw_text():
    p = _jury(["J", "K"])
    v = sp.score(p, "J", "codex", WorkerResult(0, True, answer="I think it's great."))
    assert v.total is None and "no JSON" in v.problem and v.raw == "I think it's great."


def test_jury_table_aggregates_and_flags_disagreement():
    p = _jury(["Tech", "Impact", "Skeptic"])
    verdicts = [
        sp.score(p, "Tech", "codex", WorkerResult(0, True, answer=_answer(9, 7), elapsed=3)),
        sp.score(p, "Impact", "antigravity", WorkerResult(1, True, answer=_answer(8, 6))),
        sp.score(p, "Skeptic", "copilot", WorkerResult(2, True, answer=_answer(3, 6))),
    ]
    out = sp.render_jury(p, verdicts)
    assert "| Criterion | Weight | Tech · codex | Impact · antigravity | Skeptic · copilot |" in out
    assert "| Novelty | 0.67 | 9 | 8 | 3 | 6.7 | 6 ⚠ |" in out
    assert "| Clarity | 0.33 | 7 | 6 | 6 | 6.3 | 1 |" in out
    assert "⚠ The jurors are 3 or more points apart on Novelty." in out
    assert "3/3 scored" in out
    assert "What to do with this" not in out  # this test preset has no synthesis


def test_failed_and_unscored_jurors_stay_out_of_the_table_but_in_the_report():
    p = _jury(["Tech", "Down", "Chatty"])
    verdicts = [
        sp.score(p, "Tech", "codex", WorkerResult(0, True, answer=_answer(9, 7))),
        sp.score(p, "Down", "grok", WorkerResult(1, False, error="not logged in")),
        sp.score(p, "Chatty", "copilot", WorkerResult(2, True, answer="no json here")),
    ]
    out = sp.render_jury(p, verdicts)
    assert "| Criterion | Weight | Tech · codex | Mean | Spread |" in out
    assert "1/3 scored" in out
    assert "FAILED: not logged in" in out
    assert "Raw answer:\nno json here" in out


def test_no_scores_at_all_says_so_instead_of_an_empty_table():
    p = _jury(["A", "B"])
    verdicts = [
        sp.score(p, r, "codex", WorkerResult(i, False, error="x")) for i, r in enumerate("AB")
    ]
    assert "no table" in sp.render_jury(p, verdicts)


def test_table_cells_escape_pipes_in_roles():
    p = _jury(["A|B", "C"])
    verdicts = [
        sp.score(p, r, "codex", WorkerResult(0, True, answer=_answer(5, 5))) for r in ("A|B", "C")
    ]
    assert "A\\|B · codex" in sp.render_jury(p, verdicts)


# ------------------------------------------------------------------ run


def test_run_builds_one_readonly_task_per_member_and_labels_cards_by_role(monkeypatch, tmp_path):
    seen = {}

    def fake(tasks, max_concurrency, timeout_s, watch, labels=None):
        seen.update(tasks=tasks, timeout_s=timeout_s, labels=labels, watch=watch)
        return [WorkerResult(i, True, answer=f"answer {i}", elapsed=1.0) for i in range(len(tasks))]

    monkeypatch.setattr(swarm, "swarm_agents", fake)
    out = sp.run("red-team", "Launch plan v2\nmore", str(tmp_path), watch=True)
    rt = sp.get("red-team")
    assert [t["backend"] for t in seen["tasks"]] == [m.backend for m in rt.members]
    assert all(
        t["sandbox"] == "read-only" and t["workspace"] == str(tmp_path) for t in seen["tasks"]
    )
    assert seen["timeout_s"] == rt.timeout_s and seen["watch"] is True
    assert seen["labels"][0] == f"{rt.members[0].role} — Launch plan v2"
    assert f"### {rt.members[0].role} · codex · 1.0s\nanswer 0" in out
    assert out.rstrip().endswith(rt.synthesis)


def test_run_scores_a_jury(monkeypatch):
    def fake(tasks, *a, **k):
        return [
            WorkerResult(
                i,
                True,
                answer=json.dumps(
                    {"scores": {c: 7 for c in ("originality", "feasibility", "impact", "clarity")}}
                ),
            )
            for i in range(len(tasks))
        ]

    monkeypatch.setattr(swarm, "swarm_agents", fake)
    out = sp.run("jury", "An application.")
    assert "| **Weighted total** | | **7.0** | **7.0** | **7.0** | **7.0** | 0 |" in out


def test_run_needs_material():
    with pytest.raises(ValueError, match="material is required"):
        sp.run("jury", "   ")


def test_swarm_agents_uses_given_labels_for_the_dashboard(monkeypatch):
    import swarm_watch

    got = {}
    monkeypatch.setattr(
        swarm_watch, "init", lambda labels, *a, **k: got.setdefault("labels", labels)
    )
    monkeypatch.setattr(swarm_watch, "open_window", lambda n: None)
    monkeypatch.setattr(
        swarm, "_run_codex_worker_watched", lambda i, *a: WorkerResult(i, True, answer="x")
    )
    swarm.swarm_agents(
        [{"backend": "codex", "prompt": "long boilerplate\nsecond line"}],
        watch=True,
        labels=["Technical juror — my app"],
    )
    assert got["labels"] == ["Technical juror — my app"]


def test_the_example_preset_in_the_docs_is_valid():
    """The docs show a full preset file to copy; it must load as written."""
    import re
    from pathlib import Path

    doc = (Path(__file__).parent / "docs" / "watch-and-swarm.md").read_text(encoding="utf-8")
    block = re.search(r"```json\n(.*?)\n```", doc, re.S).group(1)
    assert sp.from_dict("example", json.loads(block), "docs").kind == "jury"


def test_strengths_and_weaknesses_render_as_lists():
    p = _jury(["J", "K"])
    ans = _answer(5, 5, strengths=["Clear goal."], weaknesses=["No budget.", "No meter."])
    out = sp.render_jury(p, [sp.score(p, "J", "codex", WorkerResult(0, True, answer=ans))])
    assert "Strengths:\n  - Clear goal." in out
    assert "Weaknesses:\n  - No budget.\n  - No meter." in out
