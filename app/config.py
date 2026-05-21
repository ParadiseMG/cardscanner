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

    # Ollama (local vision model on home server)
    vision_backend: str = "auto"  # "auto" | "ollama" | "http" | "cli"
    ollama_base_url: str = "http://100.71.106.53:11434"
    ollama_vision_model: str = "llama3.2-vision:11b"

    # Video scanning
    video_retake_photo_threshold: float = 10.0  # suggest retaking photos above this value
    video_max_upload_mb: int = 500
    video_motion_threshold: float = 6.0  # per-pixel diff threshold for "still" detection
    video_min_still_seconds: float = 0.4  # minimum pause duration to count as a still window
    video_min_sharpness: float = 10.0  # skip frames below this Laplacian variance

    # Captcha notification (ntfy.sh push notification when captcha detected)
    ntfy_topic: str = ""  # e.g. "cardscanner-connor" — leave blank to disable
    ntfy_server: str = "https://ntfy.sh"

    # Pricing strategy (applies when sourcing prices from eBay Browse API
    # active listings). "Undercut the lowest active asking by N%" — typical
    # range is 5-7. Lower = sells faster but leaves money on the table; higher
    # = better margin but slower turnover.
    pricing_undercut_pct: float = 6.0

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
