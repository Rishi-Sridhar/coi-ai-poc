"""Demo opportunity presets and scenario presets (DEMO ONLY — synthetic people and organisations)."""
from __future__ import annotations

from functools import lru_cache

import yaml

from .config import CONFIG_DIR


@lru_cache(maxsize=1)
def opportunities() -> dict[str, dict]:
    data = yaml.safe_load((CONFIG_DIR / "opportunities.yaml").read_text(encoding="utf-8"))
    return {o["key"]: o for o in data["opportunities"]}


@lru_cache(maxsize=1)
def scenarios() -> dict[str, dict]:
    data = yaml.safe_load((CONFIG_DIR / "demo_scenarios.yaml").read_text(encoding="utf-8"))
    return {s["key"]: s for s in data["scenarios"]}


def scenario_context(key: str) -> dict:
    """Opportunity + respondent context for a scenario, in the submission-form shape."""
    s = scenarios()[key]
    o = opportunities()[s["opportunity"]]
    r = s["respondent"]
    return {
        "opportunity_id": o["opportunity_id"], "opportunity_name": o["opportunity_name"], "client": o["client"],
        "soe": o["soe"], "soe_relationship": o["soe_relationship"], "description": o["description"].strip(),
        "respondent_name": r["name"], "respondent_email": r["email"], "respondent_role": r["role"],
        "is_owner": r["is_owner"],
    }


def scenario_answers(key: str) -> dict:
    return {k: str(v).strip() for k, v in scenarios()[key]["answers"].items()}
