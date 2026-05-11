"""Centralised settings, sourced from environment / .env file."""
from __future__ import annotations

from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
UPLOAD_DIR = DATA_DIR / "uploads"
DATA_DIR.mkdir(exist_ok=True)
UPLOAD_DIR.mkdir(exist_ok=True)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(REPO_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-4-6"

    google_oauth_client_secrets: str = str(DATA_DIR / "google_client_secret.json")
    google_sheet_id: str = ""

    local_xlsx_path: str = ""

    host: str = "127.0.0.1"
    port: int = 8765

    db_path: str = str(DATA_DIR / "cardscanner.db")
    google_token_path: str = str(DATA_DIR / "google_token.json")

    # eBay
    ebay_env: str = "sandbox"  # "sandbox" or "production"
    ebay_app_id: str = ""
    ebay_cert_id: str = ""
    ebay_dev_id: str = ""
    ebay_runame: str = ""
    ebay_fulfillment_policy_id: str = ""
    ebay_payment_policy_id: str = ""
    ebay_return_policy_id: str = ""
    ebay_merchant_location_key: str = "cardscanner_default"
    ebay_token_path: str = str(DATA_DIR / "ebay_token.json")

    @property
    def ebay_api_base(self) -> str:
        return (
            "https://api.sandbox.ebay.com"
            if self.ebay_env == "sandbox"
            else "https://api.ebay.com"
        )

    @property
    def ebay_oauth_authorize_url(self) -> str:
        host = (
            "auth.sandbox.ebay.com"
            if self.ebay_env == "sandbox"
            else "auth.ebay.com"
        )
        return f"https://{host}/oauth2/authorize"

    @property
    def ebay_oauth_token_url(self) -> str:
        return f"{self.ebay_api_base}/identity/v1/oauth2/token"


settings = Settings()
