import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from coi import db  # noqa: E402
from coi.config import Settings  # noqa: E402
from prompts import quality as quality_prompt  # noqa: E402


def make_settings(tmp_path, **overrides) -> Settings:
    base = dict(groq_api_key="test-key", groq_model="openai/gpt-oss-120b", demo_mode=False, smtp_host="",
                reviewer_email="reviewer@example.com", app_base_url="http://localhost:8501", review_clean_cases=False,
                db_path=str(tmp_path / "test.db"), uploads_dir=str(tmp_path / "uploads"))
    base.update(overrides)
    s = Settings(**base)
    db.init_db(s)
    return s


@pytest.fixture
def settings(tmp_path):
    return make_settings(tmp_path)


@pytest.fixture
def demo_settings(tmp_path):
    return make_settings(tmp_path, demo_mode=True, groq_api_key="")


class FakeLLM:
    """Scripted stand-in for GroqJSONClient. Each script item is a dict (returned) or Exception (raised)."""

    def __init__(self, quality=None, assessment=None):
        self.quality = list(quality or [])
        self.assessment = list(assessment or [])
        self.calls = []

    def call_json(self, system, user, schema):
        import copy
        import json
        self.calls.append(user)
        queue = self.quality if schema is quality_prompt.OUTPUT_SCHEMA else self.assessment
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, Exception):
            raise item
        item = copy.deepcopy(item)
        if item.get("precedent_analysis") == "AUTO":
            # Analyse exactly the precedents that were supplied in the prompt (as a compliant model would).
            payload = json.loads(user.split("<case_data>")[1].split("</case_data>")[0])
            item["precedent_analysis"] = [
                {"precedent_id": p["precedent_id"], "relationship_to_current_case": "DISTINGUISHED",
                 "reason": "test", "material_fact": "test"} for p in payload["precedents"]]
        return item, json.dumps(item)


DEFAULT_ACTIONS = {
    "NO CONFLICT APPARENT": [{"action_code": "NO_FURTHER_ACTION",
                              "reason": "No conflict indicators were disclosed in the submitted answers."}],
    "POTENTIAL CONFLICT IDENTIFIED": [{"action_code": "HOLD_OPPORTUNITY",
                                       "reason": "Pause the opportunity until Compliance determines the mitigation."}],
    "INSUFFICIENT INFORMATION": [{"action_code": "REQUEST_ADDITIONAL_INFORMATION",
                                  "reason": "Clarify the incomplete answer before a decision is made."}],
}


def assessment_result(verdict="NO CONFLICT APPARENT", contributing=None, mitigating=None, precedents="AUTO",
                      more_info=False, actions=None):
    """precedents="AUTO" = analyse every supplied precedent exactly once; pass a list to override.
    actions=None = a valid catalogue recommendation for the verdict; pass a list to override."""
    return {
        "verdict": verdict, "overall_explanation": "test", "contributing_factors": contributing or [],
        "mitigating_factors": mitigating or [], "recommended_action": "test", "quality_data_issues": [],
        "more_information_required": more_info, "precedent_analysis": precedents,
        "recommended_actions": DEFAULT_ACTIONS[verdict] if actions is None else actions,
    }


CONTEXT = {
    "opportunity_id": "OPP-1", "opportunity_name": "Smart meter tender", "client": "Eastvale Power",
    "soe": "Eastvale Power", "soe_relationship": "Client", "description": "Smart-meter advisory tender.",
    "respondent_name": "Jane Respondent", "respondent_email": "jane.respondent@example.com",
    "respondent_role": "Manager", "is_owner": "No",
}
ALL_NO = {"RELATIVE_AT_SOE": "No", "THIRD_PARTY_INTEREST": "No", "PRIOR_SOE_EMPLOYMENT": "No",
          "IMPROPER_INFORMATION": "No", "OTHER_CONFLICT": "No"}
