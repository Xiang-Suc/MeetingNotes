import sys
import re
import requests
from typing import Optional

from meeting_notes.config import load_settings
from meeting_notes.graph_client import (
    get_app_token,
    find_recent_eda_events_for_user,
    resolve_online_meeting_by_join_url,
    list_meeting_transcripts,
    list_online_meetings_for_user,
)
from meeting_notes.trello_client import ensure_list_id


def _print(name: str, ok: bool, detail: Optional[str] = None) -> None:
    status = "OK" if ok else "FAIL"
    msg = f"[{status}] {name}"
    if detail:
        msg += f" — {detail}"
    print(msg)


def main() -> int:
    settings = load_settings()
    failed = False

    # Env checks
    required = {
        "TENANT_ID": settings.tenant_id,
        "CLIENT_ID": settings.client_id,
        "CLIENT_SECRET": settings.client_secret,
        "OPENAI_API_KEY": settings.openai_api_key,
        "TRELLO_KEY": settings.trello_key,
        "TRELLO_TOKEN": settings.trello_token,
        "TRELLO_LIST_ID": settings.trello_list_id_or_board_id,
        "GRAPH_TARGET_USER": settings.graph_target_user,
    }
    for k, v in required.items():
        ok = bool(v)
        _print(f"ENV {k}", ok)
        failed = failed or not ok

    # Graph token
    token = None
    try:
        token = get_app_token(settings.tenant_id, settings.client_id, settings.client_secret)
        _print("Graph app token", True)
    except requests.HTTPError as e:
        _print("Graph app token", False, f"HTTP {getattr(e.response,'status_code',None)}")
        failed = True

    # Graph: events (Calendars.Read.All)
    meeting_id = None
    join_url = None
    try:
        if token and settings.graph_target_user:
            events = find_recent_eda_events_for_user(
                token,
                settings.graph_target_user,
                settings.meeting_subject,
                hours_window=settings.time_window_hours,
            )
            _print("Graph events access", True, f"found {len(events)} recent events")
            # try get an online meeting join url
            for e in events:
                join_url = e.get("onlineMeeting", {}).get("joinUrl") or e.get("onlineMeetingUrl")
                if not join_url:
                    body = (e.get("body") or {}).get("content") or ""
                    m = re.search(r"https://teams\.microsoft\.com/[^\s\"]+", body)
                    if m:
                        join_url = m.group(0)
                if join_url:
                    break
        else:
            _print("Graph events access", False, "missing token or GRAPH_TARGET_USER")
            failed = True
    except requests.HTTPError as e:
        status = getattr(e.response, "status_code", None)
        _print("Graph events access", False, f"HTTP {status}")
        failed = True

    # Graph: online meeting (OnlineMeetings.Read.All)
    meeting = None
    if token and settings.graph_target_user:
        meeting = None
        if join_url:
            try:
                meeting = resolve_online_meeting_by_join_url(token, settings.graph_target_user, join_url)
            except requests.HTTPError as e:
                status = getattr(e.response, "status_code", None)
                if status == 404:
                    meeting = None  # proceed to fallback
                else:
                    _print("Graph online meeting access", False, f"HTTP {status}")
                    failed = True
        # Fallback: list recent online meetings if unresolved
        if not meeting and not failed:
            try:
                candidates = list_online_meetings_for_user(token, settings.graph_target_user, top=5)
                meeting = candidates[0] if candidates else None
            except requests.HTTPError as e:
                status = getattr(e.response, "status_code", None)
                _print("Graph online meeting access", False, f"HTTP {status} on fallback list")
                failed = True
        if meeting and not failed:
            meeting_id = meeting.get("id")
            _print("Graph online meeting access", True, f"meeting id {meeting_id}")
        elif not failed:
            _print("Graph online meeting access", False, "not found (join url or fallback)")
            failed = True
    else:
        _print("Graph online meeting access", False, "missing token or user")
        failed = True

    # Graph: transcripts (OnlineMeetingTranscript.Read.All)
    try:
        if token and settings.graph_target_user and meeting_id:
            transcripts = list_meeting_transcripts(token, settings.graph_target_user, meeting_id)
            _print("Graph transcript access", True, f"transcripts {len(transcripts)}")
        else:
            _print("Graph transcript access", False, "missing meeting id")
            failed = True
    except requests.HTTPError as e:
        status = getattr(e.response, "status_code", None)
        _print("Graph transcript access", False, f"HTTP {status}")
        failed = True

    # OpenAI
    try:
        from openai import OpenAI

        client = OpenAI(api_key=settings.openai_api_key)
        # lightweight connectivity check
        _ = client.models.list()
        _print("OpenAI connectivity", True)
    except Exception as e:
        _print("OpenAI connectivity", False, str(e)[:120])
        failed = True

    # Trello
    try:
        list_id = ensure_list_id(settings.trello_key, settings.trello_token, settings.trello_list_id_or_board_id)
        _print("Trello access", True, f"list id {list_id}")
    except requests.HTTPError as e:
        _print("Trello access", False, f"HTTP {getattr(e.response,'status_code',None)}")
        failed = True

    print("\nVerification complete.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())