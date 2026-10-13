"""Weighted structured precedent matching (no embeddings, no vector DB).

For each risk_indicator question (weight w from questionnaire.yaml):
    both Yes            -> w
    both not-Yes        -> 0.25 * w      (a hidden question counts as "No")
    disagreement        -> 0
Context fields (opportunity-owner flag, SOE relationship): equal -> full weight, else 0.

The score is normalised against a perfect match for the current case (the current case compared
with itself), so an identical pattern scores 1.00 and every score lies in 0–1.
Ranking: score desc, then REVIEWER before SYNTHETIC, then newer first. Top 3 active with score > 0.
"""
from __future__ import annotations

from .questionnaire import question, risk_indicator_ids

NO_MATCH_FRACTION = 0.25
CONTEXT_WEIGHTS = {"is_owner": 1.0, "soe_relationship": 1.0}
TOP_K = 3


def _yes(answers: dict, qid: str) -> bool:
    return answers.get(qid) == "Yes"


def field_contribution(cur_yes: bool, prec_yes: bool, weight: float) -> float:
    if cur_yes and prec_yes:
        return weight
    if not cur_yes and not prec_yes:
        return NO_MATCH_FRACTION * weight
    return 0.0


def score_precedent(current: dict, precedent_snapshot: dict) -> tuple[float, list[dict]]:
    """current/precedent: {"answers": {...}, "is_owner": "Yes"/"No", "soe_relationship": str}."""
    raw = best = 0.0
    matched: list[dict] = []
    for qid in risk_indicator_ids():
        w = float(question(qid).get("weight", 1))
        c, p = _yes(current["answers"], qid), _yes(precedent_snapshot["answers"], qid)
        contribution = field_contribution(c, p, w)
        raw += contribution
        best += field_contribution(c, c, w)
        if contribution > 0:
            matched.append({"field": qid, "value": "Yes" if c else "No", "points": round(contribution, 2)})
    for key, w in CONTEXT_WEIGHTS.items():
        best += w
        if current.get(key) and current.get(key) == precedent_snapshot.get(key):
            raw += w
            matched.append({"field": key, "value": current[key], "points": w})
    return (raw / best if best else 0.0), matched


def retrieve(current: dict, precedents: list[dict], top_k: int = TOP_K) -> list[dict]:
    """precedents: rows from db.list_precedents (with 'snapshot'). Returns top-k scored, active, score > 0."""
    scored = []
    for p in precedents:
        if not p.get("active"):
            continue
        score, matched = score_precedent(current, p["snapshot"])
        if score <= 0:
            continue
        scored.append({
            "precedent_id": p["id"],
            "source": p["source"],
            "score": round(score, 3),
            "matched": matched,
            "outcome": p["outcome"],
            "rationale": p["rationale"],
            "created_at": p["created_at"],
            "snapshot": p["snapshot"],
        })
    # Stable multi-key sort: newest first, then REVIEWER before SYNTHETIC, then highest score.
    scored.sort(key=lambda s: s["created_at"], reverse=True)
    scored.sort(key=lambda s: 0 if s["source"] == "REVIEWER" else 1)
    scored.sort(key=lambda s: s["score"], reverse=True)
    return scored[:top_k]


def llm_view(retrieved: list[dict]) -> list[dict]:
    """What the LLM sees: anonymised snapshot only (no names or emails)."""
    return [{
        "precedent_id": r["precedent_id"],
        "source": r["source"],
        "similarity_score": r["score"],
        "opportunity_summary": r["snapshot"].get("opportunity_summary"),
        "is_owner": r["snapshot"].get("is_owner"),
        "soe_relationship": r["snapshot"].get("soe_relationship"),
        "relevant_answers": r["snapshot"].get("answers"),
        "final_outcome": r["outcome"],
        "rationale": r["rationale"],
        # Final action(s) recorded for the case: decided by a human reviewer (REVIEWER) or a synthetic example.
        "final_actions": list(r["snapshot"].get("final_actions") or []),
    } for r in retrieved]
