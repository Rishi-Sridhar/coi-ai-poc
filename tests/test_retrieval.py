"""Weighted structured precedent matching."""
from coi.db import list_precedents, load_synthetic_seeds
from coi.retrieval import retrieve, score_precedent
from conftest import ALL_NO


def _case(answers, is_owner="No", rel="Client"):
    return {"answers": answers, "is_owner": is_owner, "soe_relationship": rel}


def _prec(pid, answers, source="SYNTHETIC", created="2026-01-01T00:00:00+00:00", outcome="NO CONFLICT", active=1):
    return {"id": pid, "source": source, "active": active, "outcome": outcome, "rationale": "r", "created_at": created,
            "snapshot": _case(answers)}


def test_identical_pattern_scores_one_and_disagreement_zero_contribution():
    a = dict(ALL_NO, RELATIVE_AT_SOE="Yes", RELATIVE_INFLUENCE="No")
    score, matched = score_precedent(_case(a), _case(a))
    assert score == 1.0
    assert {"field": "RELATIVE_AT_SOE", "value": "Yes", "points": 3.0} in matched
    # Influence disagreement removes that field's contribution
    b = dict(a, RELATIVE_INFLUENCE="Yes")
    s2, m2 = score_precedent(_case(a), _case(b))
    assert s2 < 1.0 and not any(m["field"] == "RELATIVE_INFLUENCE" for m in m2)


def test_yes_match_full_weight_no_match_quarter_weight():
    yes, _ = score_precedent(_case(dict(ALL_NO, IMPROPER_INFORMATION="Yes")), _case(dict(ALL_NO, IMPROPER_INFORMATION="Yes")))
    assert yes == 1.0
    _, matched = score_precedent(_case(ALL_NO), _case(ALL_NO))
    imp = next(m for m in matched if m["field"] == "IMPROPER_INFORMATION")
    assert imp["points"] == 0.75  # weight 3 * 0.25


def test_reviewer_precedent_outranks_synthetic_on_tie():
    a = dict(ALL_NO, OTHER_CONFLICT="Yes")
    precs = [_prec("SYN-900", a, created="2026-05-01T00:00:00+00:00"),
             _prec("REV-001", a, source="REVIEWER", created="2026-02-01T00:00:00+00:00")]
    top = retrieve(_case(a), precs)
    assert [t["precedent_id"] for t in top[:2]] == ["REV-001", "SYN-900"]


def test_newer_first_within_same_source_and_inactive_excluded():
    a = dict(ALL_NO, OTHER_CONFLICT="Yes")
    precs = [_prec("SYN-1", a, created="2026-01-01T00:00:00+00:00"), _prec("SYN-2", a, created="2026-03-01T00:00:00+00:00"),
             _prec("SYN-3", a, active=0)]
    ids = [t["precedent_id"] for t in retrieve(_case(a), precs)]
    assert ids == ["SYN-2", "SYN-1"]


def test_top_three_only():
    precs = [_prec(f"SYN-{i}", ALL_NO) for i in range(6)]
    assert len(retrieve(_case(ALL_NO), precs)) == 3


def test_seeds_leave_learning_gap(settings):
    seeds = load_synthetic_seeds()
    assert len(seeds) == 15
    assert all(s["outcome"] in ("CONFLICT", "NO CONFLICT") for s in seeds)
    assert not any(s["answers"].get("RELATIVE_AT_SOE") == "Yes" and s["answers"].get("RELATIVE_INFLUENCE") == "No"
                   for s in seeds), "Synthetic seeds must not encode Learning Case A's pattern"
    assert all(p["source"] == "SYNTHETIC" and p["active"] for p in list_precedents(settings))
