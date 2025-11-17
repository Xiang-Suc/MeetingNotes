import datetime as dt
from typing import Dict, List, Optional

import requests

GRAPH_BASE = "https://graph.microsoft.com/v1.0"


def get_app_token(tenant_id: str, client_id: str, client_secret: str) -> str:
    token_url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
    data = {
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "client_credentials",
        "scope": "https://graph.microsoft.com/.default",
    }
    resp = requests.post(token_url, data=data)
    resp.raise_for_status()
    return resp.json()["access_token"]


def _auth_headers(token: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def get_user(token: str, user_id_or_upn: str) -> Dict:
    url = f"{GRAPH_BASE}/users/{user_id_or_upn}"
    resp = requests.get(url, headers=_auth_headers(token))
    resp.raise_for_status()
    return resp.json()


def find_recent_eda_events_for_user(
    token: str,
    user_id_or_upn: str,
    subject: str,
    hours_window: int = 48,
    ) -> List[Dict]:
    now = dt.datetime.utcnow()
    window_start = (now - dt.timedelta(hours=hours_window)).isoformat() + "Z"

    # Filter by ended after window_start; optional subject contains
    # Note: contains() support can vary; keep logic tolerant.
    filter_parts = [f"end/dateTime ge '{window_start}'"]
    if subject:
        filter_parts.insert(0, f"contains(subject,'{subject}')")
    filter_expr = " and ".join(filter_parts)

    url = (
        f"{GRAPH_BASE}/users/{user_id_or_upn}/events"
        f"?$filter={filter_expr}&$orderby=end/dateTime desc&$top=10"
        f"&$select=subject,end,onlineMeetingUrl,body"
    )
    # Prefer text body to simplify link extraction from the event content
    resp = requests.get(url, headers={**_auth_headers(token), "Prefer": 'outlook.body-content-type="text"'})
    resp.raise_for_status()
    return resp.json().get("value", [])


def list_online_meetings_for_user(token: str, user_id_or_upn: str, top: int = 10) -> List[Dict]:
    url = f"{GRAPH_BASE}/users/{user_id_or_upn}/onlineMeetings?$orderby=creationDateTime desc&$top={top}"
    resp = requests.get(url, headers=_auth_headers(token))
    resp.raise_for_status()
    return resp.json().get("value", [])


def resolve_online_meeting_by_join_url(
    token: str, user_id_or_upn: str, join_url: str
) -> Optional[Dict]:
    # OnlineMeetings supports filtering by joinWebUrl equality
    url = (
        f"{GRAPH_BASE}/users/{user_id_or_upn}/onlineMeetings"
        f"?$filter=joinWebUrl eq '{join_url}'&$top=1"
    )
    resp = requests.get(url, headers=_auth_headers(token))
    resp.raise_for_status()
    items = resp.json().get("value", [])
    return items[0] if items else None


def list_meeting_transcripts(
    token: str, user_id_or_upn: str, meeting_id: str
) -> List[Dict]:
    url = f"{GRAPH_BASE}/users/{user_id_or_upn}/onlineMeetings/{meeting_id}/transcripts"
    resp = requests.get(url, headers=_auth_headers(token))
    resp.raise_for_status()
    return resp.json().get("value", [])


def download_transcript_content(
    token: str, user_id_or_upn: str, meeting_id: str, transcript_id: str
) -> str:
    url = (
        f"{GRAPH_BASE}/users/{user_id_or_upn}/onlineMeetings/{meeting_id}/transcripts/{transcript_id}/content"
    )
    resp = requests.get(url, headers=_auth_headers(token))
    resp.raise_for_status()
    return resp.text