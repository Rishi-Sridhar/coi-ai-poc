"""Offline synthetic evaluation (leave-one-out).

Each synthetic precedent is assessed as if it were a new case, with itself excluded from retrieval.
Reports agreement between the preliminary verdict and the synthetic outcome. Uses the live LLM when
configured, otherwise the demo fallback (DEMO_MODE=true). Synthetic data only — this is a smoke-level
consistency check, not a validation of real-world accuracy.

Run:  python scripts/evaluate_synthetic.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from coi import db  # noqa: E402
from coi.config import get_settings  # noqa: E402
from coi.guardrails import apply_g5  # noqa: E402
from coi.pipeline import run_ai  # noqa: E402
from coi.retrieval import retrieve  # noqa: E402

EXPECTED = {"CONFLICT": "POTENTIAL CONFLICT IDENTIFIED", "NO CONFLICT": "NO CONFLICT APPARENT"}


def main() -> None:
    settings = get_settings()
    settings.db_path = str(Path(tempfile.mkdtemp()) / "eval.db")  # never touches the demo database
    db.init_db(settings)
    precedents = db.list_precedents(settings)
    agree = 0
    print(f"Engine: {'demo fallback' if settings.use_fallback_engine else settings.groq_model}\n")
    for p in precedents:
        snap = p["snapshot"]
        context = {"opportunity_id": p["id"], "opportunity_name": snap["title"], "client": "Synthetic client",
                   "soe": "Synthetic SOE", "soe_relationship": snap["soe_relationship"],
                   "description": snap["opportunity_summary"], "respondent_role": "Synthetic", "is_owner": snap["is_owner"]}
        rule_results = {"validation": "PASSED", "is_owner": snap["is_owner"],
                        "risk_indicators_yes": [k for k, v in snap["answers"].items() if v == "Yes"]}
        current = {"answers": snap["answers"], "is_owner": snap["is_owner"], "soe_relationship": snap["soe_relationship"]}
        retrieved = retrieve(current, [x for x in precedents if x["id"] != p["id"]])
        ai = run_ai(settings, context, snap["answers"], rule_results, retrieved)
        verdict = apply_g5(ai.verdict, ai.quality_issues)[0] if ai.state == "COMPLETED" else ai.verdict
        ok = verdict == EXPECTED[p["outcome"]]
        agree += ok
        print(f"{'✔' if ok else '✘'} {p['id']:8} expected {EXPECTED[p['outcome']]:30} got {verdict}")
    print(f"\nAgreement: {agree}/{len(precedents)}")


if __name__ == "__main__":
    main()
