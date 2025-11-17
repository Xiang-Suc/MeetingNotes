import datetime as dt
import os
import sys
import requests
import argparse
import re

from meeting_notes.config import load_settings
from meeting_notes.graph_client import (
    get_app_token,
    get_user,
    find_recent_eda_events_for_user,
    resolve_online_meeting_by_join_url,
    list_meeting_transcripts,
    download_transcript_content,
    list_online_meetings_for_user,
)
from meeting_notes.graph_delegated_client import (
    acquire_delegated_token,
    list_recent_eda_events_me,
    resolve_meeting_by_join_url_me,
    list_transcripts_me,
    download_transcript_content_me,
)
from meeting_notes.summarize import summarize_markdown
from meeting_notes.trello_client import ensure_list_id, create_card, create_checklist, add_checkitem


def main() -> int:
    parser = argparse.ArgumentParser(description="Summarize meeting transcript and post to Trello")
    parser.add_argument(
        "--transcript-file",
        type=str,
        help="Path to a local transcript file (bypass Graph; useful without admin consent)",
    )
    parser.add_argument(
        "--summary-file",
        type=str,
        help="Path to a pre-generated Markdown summary (skip OpenAI summarization)",
    )
    parser.add_argument(
        "--use-delegated",
        action="store_true",
        help="Use delegated auth (/me endpoints) via device code flow",
    )
    parser.add_argument(
        "--list-meetings",
        action="store_true",
        help="List recent 'EDA Library' meetings (no changes)",
    )
    # App-mode testing flags: list meetings/transcripts using application permissions
    parser.add_argument(
        "--app-list-meetings",
        action="store_true",
        help="[App mode] List recent online meetings for GRAPH_TARGET_USER",
    )
    parser.add_argument(
        "--app-user-info",
        action="store_true",
        help="[App mode] Show Graph user info for GRAPH_TARGET_USER",
    )
    parser.add_argument(
        "--app-list-events",
        action="store_true",
        help="[App mode] List recent events for GRAPH_TARGET_USER (calendar access check)",
    )
    parser.add_argument(
        "--app-resolve-joinurl",
        action="store_true",
        help="[App mode] Resolve an online meeting by joinUrl from recent events",
    )
    parser.add_argument(
        "--app-meeting-index",
        type=int,
        help="[App mode] Select meeting index when listing transcripts",
    )
    parser.add_argument(
        "--app-list-transcripts",
        action="store_true",
        help="[App mode] List transcripts for the selected meeting",
    )
    parser.add_argument(
        "--app-transcript-index",
        type=int,
        help="[App mode] Select transcript index (0=latest) to verify content length",
    )
    parser.add_argument(
        "--meeting-index",
        type=int,
        help="Select meeting by index from the --list-meetings output",
    )
    parser.add_argument(
        "--transcript-index",
        type=int,
        help="Select transcript index (0=latest) from selected meeting",
    )
    parser.add_argument(
        "--output-md",
        type=str,
        help="Fallback path to write summary Markdown if Trello fails",
    )
    args = parser.parse_args()

    settings = load_settings()

    def _strip_fences(text: str) -> str:
        t = text.strip()
        if t.startswith("```") and t.endswith("```"):
            # Remove leading/trailing triple backticks block
            t = t[3:]
            # If language marker present (e.g., ```markdown), strip until first newline
            if "\n" in t:
                first_nl = t.find("\n")
                t = t[first_nl + 1 :]
            # Remove closing fence
            if t.endswith("```"):
                t = t[:-3]
        return t.strip()

    def transform_summary_for_trello(summary_md: str):
        """
        Parse the summary into:
        - card_title: from first '# ' heading line
        - desc_text: everything up to '## Actions & Follow-Up' (exclusive), with fences removed
        - checklists: list of (name, [items]) for 'Actions & Follow-Up' and all subsequent sections
        """
        clean = _strip_fences(summary_md)
        lines = clean.splitlines()
        card_title = None
        # Find first H1
        for ln in lines:
            s = ln.strip()
            if s.startswith("# "):
                card_title = s[2:].strip()
                break
        # Section indices
        section_starts = []
        for i, ln in enumerate(lines):
            s = ln.strip()
            if s.startswith("## "):
                name = s[3:].strip()
                section_starts.append((i, name))

        # Helper to get section content by name
        def get_section(name: str):
            idx = next((i for i, n in section_starts if n.lower() == name.lower()), None)
            if idx is None:
                return []
            # find next section start
            next_indices = [i for i, _ in section_starts if i > idx]
            end = min(next_indices) if next_indices else len(lines)
            return lines[idx:end]

        # Description keeps everything before Actions & Follow-Up
        actions_idx = next((i for i, n in section_starts if n.lower().startswith("actions") or n.lower() == "actions & follow-up"), None)
        if actions_idx is not None:
            desc_lines = lines[:actions_idx]
        else:
            desc_lines = lines

        # Build checklists from Actions & Follow-Up and all subsequent sections
        checklist_defs = []
        if actions_idx is not None:
            # Gather sections from actions_idx onwards
            tail_sections = [(i, n) for i, n in section_starts if i >= actions_idx]
            for i, name in tail_sections:
                # section content until next heading
                next_indices = [j for j, _ in section_starts if j > i]
                end = min(next_indices) if next_indices else len(lines)
                sec_lines = lines[i:end]
                # Extract bullet items
                items = []
                for ln in sec_lines[1:]:  # skip the heading line
                    s = ln.strip()
                    if s.startswith(("- ", "* ")):
                        items.append(s[2:].strip())
                    elif re.match(r"^\d+\.\s+", s):
                        items.append(re.sub(r"^\d+\.\s+", "", s).strip())
                if items:
                    checklist_defs.append((name, items))

        # Remove any leading/trailing blank lines in desc
        # Also drop the H1 line from description (card title will use it)
        if desc_lines and desc_lines[0].strip().startswith("# "):
            desc_lines = desc_lines[1:]
        # Trim surrounding blank lines
        while desc_lines and desc_lines[0].strip() == "":
            desc_lines = desc_lines[1:]
        while desc_lines and desc_lines[-1].strip() == "":
            desc_lines = desc_lines[:-1]
        desc_text = "\n".join(desc_lines).strip()

        return card_title, desc_text, checklist_defs

    # If a pre-generated summary is provided, bypass OpenAI and post directly to Trello
    if args.summary_file:
        try:
            with open(args.summary_file, "r", encoding="utf-8") as f:
                summary_md = f.read()
        except FileNotFoundError:
            print(f"Summary file not found: {args.summary_file}")
            return 2

        try:
            list_id = ensure_list_id(
                settings.trello_key, settings.trello_token, settings.trello_list_id_or_board_id
            )
            card_name = f"{settings.meeting_subject} Meeting Notes – {dt.date.today().isoformat()}"
            card = create_card(settings.trello_key, settings.trello_token, list_id, card_name, summary_md)
            print(f"Created Trello card: {card.get('shortUrl')}")
        except requests.HTTPError as e:
            print(f"Trello failed ({e}). Writing Markdown to file.")
            path = args.summary_file or f"EDA_Library_Meeting_Notes_{dt.date.today().isoformat()}.md"
            with open(path, "w", encoding="utf-8") as w:
                w.write(summary_md)
            print(f"Summary written to {path}")
        return 0

    # If a local transcript is provided, bypass Graph and proceed directly
    if args.transcript_file:
        if not os.path.exists(args.transcript_file):
            print(f"Transcript file not found: {args.transcript_file}")
            return 2

        ext = os.path.splitext(args.transcript_file)[1].lower()
        if ext == ".docx":
            try:
                from meeting_notes.docx_utils import extract_text_from_docx
                transcript_text = extract_text_from_docx(args.transcript_file)
            except RuntimeError as e:
                print(str(e))
                return 2
        else:
            try:
                with open(args.transcript_file, "r", encoding="utf-8") as f:
                    transcript_text = f.read()
            except Exception as e:
                print(f"Failed to read transcript file: {e}")
                return 2

        summary_md = summarize_markdown(
            settings.openai_api_key,
            transcript_text,
            settings.summary_system_prompt,
        )
        try:
            list_id = ensure_list_id(
                settings.trello_key, settings.trello_token, settings.trello_list_id_or_board_id
            )
            card_title, desc_text, checklist_defs = transform_summary_for_trello(summary_md)
            card_name = card_title or f"{settings.meeting_subject or 'Meeting'} – {dt.date.today().isoformat()}"
            card = create_card(settings.trello_key, settings.trello_token, list_id, card_name, desc_text)
            # Add checklists
            for cl_name, items in checklist_defs:
                cl = create_checklist(settings.trello_key, settings.trello_token, card.get("id"), cl_name)
                cl_id = cl.get("id")
                for it in items:
                    add_checkitem(settings.trello_key, settings.trello_token, cl_id, it)
            print(f"Created Trello card: {card.get('shortUrl')}")
        except requests.HTTPError as e:
            print(f"Trello failed ({e}). Writing Markdown to file.")
            path = args.output_md or f"EDA_Library_Meeting_Notes_{dt.date.today().isoformat()}.md"
            with open(path, "w", encoding="utf-8") as w:
                w.write(summary_md)
            print(f"Summary written to {path}")
        return 0

    # Delegated flow: list/select meeting and transcript via /me endpoints
    if args.use_delegated:
        token = acquire_delegated_token(settings.tenant_id, settings.client_id)

        events = list_recent_eda_events_me(token, settings.meeting_subject, settings.time_window_hours)
        if args.list_meetings:
            for i, e in enumerate(events):
                print(f"[{i}] {e.get('subject')} | end={e.get('end',{}).get('dateTime')} ")
            return 0
        if not events:
            print("No matching events found in the time window.")
            return 0
        idx = args.meeting_index or 0
        if idx < 0 or idx >= len(events):
            print(f"meeting-index out of range (0..{len(events)-1})")
            return 2
        target_event = events[idx]
        join_url = target_event.get("onlineMeeting", {}).get("joinUrl") or target_event.get("onlineMeetingUrl")
        meeting = resolve_meeting_by_join_url_me(token, join_url)
        if not meeting:
            print("Online meeting not found for selected event.")
            return 4
        meeting_id = meeting.get("id")
        transcripts = list_transcripts_me(token, meeting_id)
        if not transcripts:
            print("No transcripts available; ensure transcription was enabled for the meeting.")
            return 0
        transcripts.sort(key=lambda t: t.get("createdDateTime", ""), reverse=True)
        t_idx = args.transcript_index or 0
        if t_idx < 0 or t_idx >= len(transcripts):
            print(f"transcript-index out of range (0..{len(transcripts)-1})")
            return 2
        transcript_id = transcripts[t_idx].get("id")
        transcript_text = download_transcript_content_me(token, meeting_id, transcript_id)

        summary_md = summarize_markdown(
            settings.openai_api_key,
            transcript_text,
            settings.summary_system_prompt,
        )
        try:
            list_id = ensure_list_id(settings.trello_key, settings.trello_token, settings.trello_list_id_or_board_id)
            card_title, desc_text, checklist_defs = transform_summary_for_trello(summary_md)
            card_name = card_title or f"{settings.meeting_subject or 'Meeting'} – {dt.date.today().isoformat()}"
            card = create_card(settings.trello_key, settings.trello_token, list_id, card_name, desc_text)
            for cl_name, items in checklist_defs:
                cl = create_checklist(settings.trello_key, settings.trello_token, card.get("id"), cl_name)
                cl_id = cl.get("id")
                for it in items:
                    add_checkitem(settings.trello_key, settings.trello_token, cl_id, it)
            print(f"Created Trello card: {card.get('shortUrl')}")
        except requests.HTTPError as e:
            print(f"Trello failed ({e}). Writing Markdown to file.")
            path = args.output_md or f"EDA_Library_Meeting_Notes_{dt.date.today().isoformat()}.md"
            with open(path, "w", encoding="utf-8") as w:
                w.write(summary_md)
            print(f"Summary written to {path}")
        return 0

    # App-mode testing helpers: list meetings and transcripts directly
    if (
        args.app_list_meetings
        or args.app_list_transcripts is True
        or args.app_user_info
        or args.app_list_events
        or args.app_resolve_joinurl
    ):
        try:
            token = get_app_token(settings.tenant_id, settings.client_id, settings.client_secret)
        except requests.HTTPError as e:
            status = getattr(e.response, "status_code", None)
            print(f"Failed to acquire app token (HTTP {status})")
            return 2

        # Debug: show user info to confirm identity resolution
        if args.app_user_info:
            try:
                info = get_user(token, settings.graph_target_user)
                print(
                    "User info:\n"
                    f"  id={info.get('id')}\n"
                    f"  userPrincipalName={info.get('userPrincipalName')}\n"
                    f"  displayName={info.get('displayName')}\n"
                )
            except requests.HTTPError as e:
                status = getattr(e.response, "status_code", None)
                body = getattr(e.response, "text", "")
                print(f"Get user failed (HTTP {status})\n{body[:400]}")
                return 4
            return 0

        # Debug: list events to ensure calendar application permission is working
        if args.app_list_events:
            try:
                events = find_recent_eda_events_for_user(
                    token,
                    settings.graph_target_user,
                    settings.meeting_subject,
                    hours_window=settings.time_window_hours,
                )
            except requests.HTTPError as e:
                status = getattr(e.response, "status_code", None)
                body = getattr(e.response, "text", "")
                print(f"List events failed (HTTP {status})\n{body[:400]}")
                return 4
            if not events:
                print("No matching events found in the time window.")
            else:
                for i, e in enumerate(events):
                    print(f"[{i}] {e.get('subject')} | end={e.get('end',{}).get('dateTime')} ")
            return 0

        if args.app_resolve_joinurl:
            try:
                events = find_recent_eda_events_for_user(
                    token,
                    settings.graph_target_user,
                    settings.meeting_subject,
                    hours_window=settings.time_window_hours,
                )
            except requests.HTTPError as e:
                status = getattr(e.response, "status_code", None)
                body = getattr(e.response, "text", "")
                print(f"List events failed (HTTP {status})\n{body[:400]}")
                return 4

            # Pick first event with an online meeting join URL
            join_url = None
            for e in events:
                join_url = e.get("onlineMeeting", {}).get("joinUrl") or e.get("onlineMeetingUrl")
                if not join_url:
                    # Fallback: extract from event body content if present
                    body = (e.get("body") or {}).get("content") or ""
                    m = re.search(r"https://teams\.microsoft\.com/[^\s\"]+", body)
                    if m:
                        join_url = m.group(0)
                if join_url:
                    break
            if not join_url:
                print("No event contains an online meeting join URL in the time window.")
                return 0
            try:
                meeting = resolve_online_meeting_by_join_url(token, settings.graph_target_user, join_url)
            except requests.HTTPError as e:
                status = getattr(e.response, "status_code", None)
                body = getattr(e.response, "text", "")
                print(f"Resolve meeting by joinUrl failed (HTTP {status})\n{body[:400]}")
                return 4
            if not meeting:
                print("Meeting not resolved by joinUrl (empty result).")
            else:
                print(f"Resolved meeting id: {meeting.get('id')} | subject={meeting.get('subject')} | created={meeting.get('creationDateTime')} ")
            return 0

        try:
            meetings = list_online_meetings_for_user(token, settings.graph_target_user, top=10)
        except requests.HTTPError as e:
            status = getattr(e.response, "status_code", None)
            body = getattr(e.response, "text", "")
            print(f"List online meetings failed (HTTP {status})\n{body[:400]}")
            return 4

        if args.app_list_meetings:
            if not meetings:
                print("No recent online meetings returned. If this should succeed, ensure Application Access Policy is granted to the organizer and wait for propagation.")
            else:
                for i, m in enumerate(meetings):
                    print(f"[{i}] subject={m.get('subject')} | created={m.get('creationDateTime')} | id={m.get('id')}")
            return 0

        # If we are listing transcripts, select a meeting
        idx = args.app_meeting_index or 0
        if idx < 0 or (meetings and idx >= len(meetings)):
            print(f"app-meeting-index out of range (0..{max(0, len(meetings)-1)})")
            return 2
        if not meetings:
            print("No meetings to list transcripts for.")
            return 0

        target_meeting = meetings[idx]
        meeting_id = target_meeting.get("id")

        try:
            transcripts = list_meeting_transcripts(token, settings.graph_target_user, meeting_id)
        except requests.HTTPError as e:
            status = getattr(e.response, "status_code", None)
            body = getattr(e.response, "text", "")
            print(f"List transcripts failed (HTTP {status})\n{body[:400]}")
            return 4

        if not transcripts:
            print("No transcripts available; ensure transcription was enabled for the meeting.")
            return 0

        transcripts.sort(key=lambda t: t.get("createdDateTime", ""), reverse=True)
        for i, t in enumerate(transcripts):
            print(f"[{i}] created={t.get('createdDateTime')} | id={t.get('id')}")

        # Optionally verify a transcript content length without printing sensitive content
        t_idx = args.app_transcript_index if args.app_transcript_index is not None else 0
        if t_idx < 0 or t_idx >= len(transcripts):
            print(f"app-transcript-index out of range (0..{len(transcripts)-1})")
            return 2

        transcript_id = transcripts[t_idx].get("id")
        try:
            content = download_transcript_content(token, settings.graph_target_user, meeting_id, transcript_id)
        except requests.HTTPError as e:
            status = getattr(e.response, "status_code", None)
            body = getattr(e.response, "text", "")
            print(f"Download transcript content failed (HTTP {status})\n{body[:400]}")
            return 4

        print(f"Transcript content length: {len(content)} characters (not printing full text)")
        return 0

    # Application permissions flow (background automation): requires GRAPH_TARGET_USER
    if not settings.graph_target_user:
        print("GRAPH_TARGET_USER is not set in .env; set a UPN or user id.")
        return 2
    token = get_app_token(settings.tenant_id, settings.client_id, settings.client_secret)

    try:
        events = find_recent_eda_events_for_user(
            token,
            settings.graph_target_user,
            settings.meeting_subject,
            hours_window=settings.time_window_hours,
        )
    except requests.HTTPError as e:
        status = getattr(e.response, "status_code", None)
        if status == 403:
            print(
                "Graph returned 403 Forbidden for calendar access. "
                "Ensure the app has application permissions with admin consent: "
                "Calendars.Read.All, OnlineMeetings.Read.All, OnlineMeetingTranscript.Read.All."
            )
            return 5
        raise
    if not events:
        print("No matching events found in the time window.")
        return 0

    target_event = events[0]
    join_url = target_event.get("onlineMeeting", {}).get("joinUrl") or target_event.get("onlineMeetingUrl")
    if not join_url:
        print("Event does not contain an online meeting join URL; cannot resolve meeting id.")
        return 3

    meeting = resolve_online_meeting_by_join_url(token, settings.graph_target_user, join_url)
    if not meeting:
        # Fallback: list recent online meetings and try to pick by subject or latest
        candidates = list_online_meetings_for_user(token, settings.graph_target_user, top=10)
        pick = None
        if settings.meeting_subject:
            for m in candidates:
                if settings.meeting_subject.lower() in (m.get("subject") or "").lower():
                    pick = m
                    break
        if not pick and candidates:
            pick = candidates[0]
        if not pick:
            print("Online meeting not found by join URL, and no recent meetings to fallback.")
            return 4
        meeting = pick

    meeting_id = meeting.get("id")
    transcripts = list_meeting_transcripts(token, settings.graph_target_user, meeting_id)
    if not transcripts:
        print("No transcripts available; ensure transcription was enabled for the meeting.")
        return 0

    transcripts.sort(key=lambda t: t.get("createdDateTime", ""), reverse=True)
    # In app mode we still pick the most recent transcript
    transcript_id = transcripts[0].get("id")
    transcript_text = download_transcript_content(token, settings.graph_target_user, meeting_id, transcript_id)

    summary_md = summarize_markdown(
        settings.openai_api_key,
        transcript_text,
        settings.summary_system_prompt,
    )
    try:
        list_id = ensure_list_id(settings.trello_key, settings.trello_token, settings.trello_list_id_or_board_id)
        card_title, desc_text, checklist_defs = transform_summary_for_trello(summary_md)
        card_name = card_title or f"{settings.meeting_subject or 'Meeting'} – {dt.date.today().isoformat()}"
        card = create_card(settings.trello_key, settings.trello_token, list_id, card_name, desc_text)
        for cl_name, items in checklist_defs:
            cl = create_checklist(settings.trello_key, settings.trello_token, card.get("id"), cl_name)
            cl_id = cl.get("id")
            for it in items:
                add_checkitem(settings.trello_key, settings.trello_token, cl_id, it)
        print(f"Created Trello card: {card.get('shortUrl')}")
    except requests.HTTPError as e:
        print(f"Trello failed ({e}). Writing Markdown to file.")
        path = args.output_md or f"EDA_Library_Meeting_Notes_{dt.date.today().isoformat()}.md"
        with open(path, "w", encoding="utf-8") as w:
            w.write(summary_md)
        print(f"Summary written to {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())