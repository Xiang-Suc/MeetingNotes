from typing import Dict, List, Optional

import msal
import requests

GRAPH_BASE = "https://graph.microsoft.com/v1.0"


SCOPES = [
    "User.Read",
    "Calendars.Read",
    "OnlineMeetings.Read",
    # Transcript access requires the delegated scope with ".Read.All"
    "OnlineMeetingTranscript.Read.All",
]


def acquire_delegated_token(tenant_id: str, client_id: str) -> str:
    authority = f"https://login.microsoftonline.com/{tenant_id}"
    app = msal.PublicClientApplication(client_id=client_id, authority=authority)
    flow = app.initiate_device_flow(scopes=SCOPES)
    if not flow:
        raise RuntimeError("Failed to create device flow")

    print(flow.get("message"))
    result = app.acquire_token_by_device_flow(flow)
    if "access_token" not in result:
        raise RuntimeError(f"Failed to acquire token: {result}")
    return result["access_token"]


def _auth_headers(token: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def list_recent_eda_events_me(token: str, subject: str, hours_window: int = 72) -> List[Dict]:
    # We fetch recent events and filter client-side to handle cases where contains() is limited.
    url = (
        f"{GRAPH_BASE}/me/events?$select=subject,start,end,onlineMeetingUrl,onlineMeeting"
        f"&$orderby=end/dateTime desc&$top=50"
    )
    resp = requests.get(url, headers=_auth_headers(token))
    resp.raise_for_status()
    all_events = resp.json().get("value", [])
    # Client-side filter by subject keyword and recent window
    from datetime import datetime, timedelta
    recent_cut = datetime.utcnow() - timedelta(hours=hours_window)

    def parse_end(e):
        dtstr = e.get("end", {}).get("dateTime")
        try:
            # naive parse; graph returns ISO strings
            return datetime.fromisoformat(dtstr.replace("Z", "+00:00")).replace(tzinfo=None)
        except Exception:
            return recent_cut

    filtered = [
        e for e in all_events
        if (subject.lower() in (e.get("subject") or "").lower()) and (parse_end(e) >= recent_cut)
    ]
    return filtered


def resolve_meeting_by_join_url_me(token: str, join_url: str) -> Optional[Dict]:
    if not join_url:
        return None
    url = f"{GRAPH_BASE}/me/onlineMeetings?$filter=joinWebUrl eq '{join_url}'&$top=1"
    resp = requests.get(url, headers=_auth_headers(token))
    resp.raise_for_status()
    items = resp.json().get("value", [])
    return items[0] if items else None


def list_transcripts_me(token: str, meeting_id: str) -> List[Dict]:
    url = f"{GRAPH_BASE}/me/onlineMeetings/{meeting_id}/transcripts"
    resp = requests.get(url, headers=_auth_headers(token))
    resp.raise_for_status()
    return resp.json().get("value", [])


def download_transcript_content_me(token: str, meeting_id: str, transcript_id: str) -> str:
    url = f"{GRAPH_BASE}/me/onlineMeetings/{meeting_id}/transcripts/{transcript_id}/content"
    resp = requests.get(url, headers=_auth_headers(token))
    resp.raise_for_status()
    return resp.text