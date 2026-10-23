"""Application settings loaded from environment / .env."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
load_dotenv(ROOT / ".env")

BANNER = (
    "Proof of Concept — All sample cases and guidance are synthetic and do not "
    "represent official EY Conflict of Interest policy."
)
LEARNING_STATEMENT = "Human-in-the-loop adaptation using a growing reviewer-approved precedent library."

DEFAULT_MODEL = "openai/gpt-oss-120b"


def _bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    groq_api_key: str = field(default_factory=lambda: os.getenv("GROQ_API_KEY", "").strip())
    groq_model: str = field(default_factory=lambda: os.getenv("GROQ_MODEL", "").strip() or DEFAULT_MODEL)
    demo_mode: bool = field(default_factory=lambda: _bool("DEMO_MODE", False))

    smtp_host: str = field(default_factory=lambda: os.getenv("SMTP_HOST", "").strip())
    smtp_port: int = field(default_factory=lambda: int(os.getenv("SMTP_PORT", "587") or 587))
    smtp_username: str = field(default_factory=lambda: os.getenv("SMTP_USERNAME", "").strip())
    smtp_password: str = field(default_factory=lambda: os.getenv("SMTP_PASSWORD", ""))
    smtp_from_email: str = field(default_factory=lambda: os.getenv("SMTP_FROM_EMAIL", "").strip())
    reviewer_email: str = field(default_factory=lambda: os.getenv("REVIEWER_EMAIL", "").strip() or "coi-reviewer@demo.local")
    app_base_url: str = field(default_factory=lambda: (os.getenv("APP_BASE_URL", "").strip() or "http://localhost:8501").rstrip("/"))

    admin_password_hash: str = field(default_factory=lambda: os.getenv("ADMIN_PASSWORD_HASH", "").strip())

    review_clean_cases: bool = field(default_factory=lambda: _bool("REVIEW_CLEAN_CASES", False))
    prompt_version: str = field(default_factory=lambda: os.getenv("PROMPT_VERSION", "v1"))
    guidance_version: str = field(default_factory=lambda: os.getenv("GUIDANCE_VERSION", "v1"))
    questionnaire_version: str = field(default_factory=lambda: os.getenv("QUESTIONNAIRE_VERSION", "v1"))

    db_path: str = field(default_factory=lambda: os.getenv("COI_DB_PATH", str(ROOT / "data" / "coi_poc.db")))
    uploads_dir: str = field(default_factory=lambda: os.getenv("COI_UPLOADS_DIR", str(ROOT / "uploads")))

    def __post_init__(self) -> None:
        # Google displays App Passwords as "abcd efgh ijkl mnop"; the spaces are not part of the password.
        if self.smtp_host.strip().lower() == "smtp.gmail.com":
            self.smtp_password = self.smtp_password.replace(" ", "").strip()

    @property
    def use_fallback_engine(self) -> bool:
        """Demo fallback is used only when DEMO_MODE is explicitly on."""
        return self.demo_mode

    @property
    def smtp_configured(self) -> bool:
        return bool(self.smtp_host)


def get_settings() -> Settings:
    return Settings()
