import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None or v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on", "да")


def _required(name: str) -> str:
    v = os.getenv(name, "").strip()
    if not v:
        raise SystemExit(f"Не задана переменная окружения {name} (см. .env.example)")
    return v


@dataclass(frozen=True)
class Config:
    bot_token: str
    channel_id: str
    admin_ids: frozenset[int]
    claude_model: str = "claude-opus-5-5"
    claude_effort: str = "medium"
    poll_interval_min: int = 10
    signature: str = "VNIM.AI"
    include_source_link: bool = True
    auto_publish_breaking: bool = True
    max_items_per_run: int = 15
    max_age_hours: int = 24
    learning_min_decisions: int = 30
    learning_target_precision: float = 0.95
    db_path: str = "data/bot.db"
    feeds_file: str = "feeds.txt"


def load_config() -> Config:
    admin_ids = frozenset(
        int(x) for x in _required("ADMIN_IDS").replace(" ", "").split(",") if x
    )
    return Config(
        bot_token=_required("TELEGRAM_BOT_TOKEN"),
        channel_id=_required("CHANNEL_ID"),
        admin_ids=admin_ids,
        claude_model=os.getenv("CLAUDE_MODEL", "claude-opus-5-5"),
        claude_effort=os.getenv("CLAUDE_EFFORT", "medium"),
        poll_interval_min=int(os.getenv("POLL_INTERVAL_MIN", "10")),
        signature=os.getenv("SIGNATURE", "VNIM.AI"),
        include_source_link=_bool("INCLUDE_SOURCE_LINK", True),
        auto_publish_breaking=_bool("AUTO_PUBLISH_BREAKING", True),
        max_items_per_run=int(os.getenv("MAX_ITEMS_PER_RUN", "15")),
        max_age_hours=int(os.getenv("MAX_AGE_HOURS", "24")),
        learning_min_decisions=int(os.getenv("LEARNING_MIN_DECISIONS", "30")),
        learning_target_precision=float(os.getenv("LEARNING_TARGET_PRECISION", "0.95")),
        db_path=os.getenv("DB_PATH", "data/bot.db"),
        feeds_file=os.getenv("FEEDS_FILE", "feeds.txt"),
    )
