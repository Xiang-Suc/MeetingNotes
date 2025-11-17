from typing import Optional
import os

import requests


API_BASE = "https://api.trello.com/1"


def _params(key: str, token: str, extra: Optional[dict] = None) -> dict:
    p = {"key": key, "token": token}
    if extra:
        p.update(extra)
    return p


def ensure_list_id(key: str, token: str, list_or_board_id: str) -> str:
    # Try list endpoint first
    list_url = f"{API_BASE}/lists/{list_or_board_id}"
    r = requests.get(list_url, params=_params(key, token))
    if r.status_code == 200:
        return list_or_board_id

    # Fallback: treat as board id, pick a reasonable list
    board_lists = requests.get(
        f"{API_BASE}/boards/{list_or_board_id}/lists",
        params=_params(key, token, {"fields": "id,name"}),
    )
    board_lists.raise_for_status()
    items = board_lists.json()
    preferred_names = {"Meeting Notes", "Notes", "To Do", "Inbox"}
    for it in items:
        if it.get("name") in preferred_names:
            return it.get("id")
    return items[0]["id"] if items else list_or_board_id


def create_card(key: str, token: str, list_id: str, name: str, desc: str) -> dict:
    url = f"{API_BASE}/cards"
    params = _params(key, token, {"idList": list_id, "name": name, "desc": desc})
    resp = requests.post(url, params=params)
    resp.raise_for_status()
    return resp.json()


def create_checklist(key: str, token: str, card_id: str, name: str) -> dict:
    """Create a checklist on a card and return its JSON (includes `id`)."""
    url = f"{API_BASE}/cards/{card_id}/checklists"
    params = _params(key, token, {"name": name, "pos": "bottom"})
    resp = requests.post(url, params=params)
    resp.raise_for_status()
    return resp.json()


def add_checkitem(key: str, token: str, checklist_id: str, name: str) -> dict:
    """Add a check item to a checklist (unchecked by default)."""
    url = f"{API_BASE}/checklists/{checklist_id}/checkItems"
    params = _params(key, token, {"name": name, "pos": "bottom"})
    resp = requests.post(url, params=params)
    resp.raise_for_status()
    return resp.json()


def add_attachment_file(key: str, token: str, card_id: str, file_path: str, name: Optional[str] = None) -> dict:
    """Attach a local file to a Trello card.

    Args:
        key: Trello API key
        token: Trello token
        card_id: Target card id
        file_path: Path to the local file to upload
        name: Optional display name for the attachment
    Returns:
        JSON of the created attachment.
    """
    url = f"{API_BASE}/cards/{card_id}/attachments"
    params = _params(key, token, {"name": name} if name else None)
    fn = name or os.path.basename(file_path)
    with open(file_path, "rb") as f:
        files = {"file": (fn, f)}
        resp = requests.post(url, params=params, files=files)
    resp.raise_for_status()
    return resp.json()