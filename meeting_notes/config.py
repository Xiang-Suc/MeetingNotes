import os
from dataclasses import dataclass
from typing import Optional
from pathlib import Path

from dotenv import load_dotenv


@dataclass
class Settings:
    tenant_id: str
    client_id: str
    client_secret: str
    openai_api_key: str
    trello_key: str
    trello_token: str
    trello_list_id_or_board_id: str
    graph_target_user: Optional[str]
    meeting_subject: str = "EDA Library"
    time_window_hours: int = 48
    summary_system_prompt: str = (
        "You are Meeting Markdown Assistant. Produce concise, well-structured Markdown including:"
        " 1) Overview; 2) Key decisions; 3) Action items (owner + due date if present);"
        " 4) Risks/Blockers; 5) Follow-ups. Keep it clear and scannable."
    )


def load_settings() -> Settings:
    load_dotenv()

    # Resolve prompt text from file first, then env var, then default
    def _default_prompt() -> str:
        return (
            "You are Meeting Markdown Assistant. Produce concise, well-structured Markdown including:"
            " 1) Overview; 2) Key decisions; 3) Action items (owner + due date if present);"
            " 4) Risks/Blockers; 5) Follow-ups. Keep it clear and scannable."
        )

    # Determine repository root and default prompt file path
    repo_root = Path(__file__).resolve().parent.parent
    default_prompt_path = repo_root / "prompts" / "summary_system_prompt.md"
    # Allow override via env var SUMMARY_PROMPT_FILE
    prompt_path = Path(os.getenv("SUMMARY_PROMPT_FILE", str(default_prompt_path)))
    prompt_text: Optional[str] = None
    try:
        if prompt_path.exists():
            prompt_text = prompt_path.read_text(encoding="utf-8").strip()
    except Exception:
        # Fall back silently to env/default
        prompt_text = None

    if not prompt_text:
        prompt_text = os.getenv("SUMMARY_SYSTEM_PROMPT", _default_prompt())

    return Settings(
        tenant_id=os.getenv("TENANT_ID", ""),
        client_id=os.getenv("CLIENT_ID", ""),
        client_secret=os.getenv("CLIENT_SECRET", ""),
        openai_api_key=os.getenv("OPENAI_API_KEY", ""),
        trello_key=os.getenv("TRELLO_KEY", ""),
        trello_token=os.getenv("TRELLO_TOKEN", ""),
        trello_list_id_or_board_id=os.getenv("TRELLO_LIST_ID", ""),
        graph_target_user=os.getenv("GRAPH_TARGET_USER", None),
        meeting_subject=os.getenv("MEETING_SUBJECT", "EDA Library"),
        time_window_hours=int(os.getenv("TIME_WINDOW_HOURS", "48")),
        summary_system_prompt=prompt_text,
    )