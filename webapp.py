import os
import re
import datetime as dt
from uuid import uuid4

from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS

from meeting_notes.config import load_settings
from meeting_notes.summarize import summarize_markdown
from meeting_notes.trello_client import (
    ensure_list_id,
    create_card,
    create_checklist,
    add_checkitem,
    add_attachment_file,
)


app = Flask(__name__)
# Allow cross-origin calls to the API for GitHub Pages frontend
CORS(app, resources={r"/process": {"origins": "*"}, r"/prompt": {"origins": "*"}})


def _strip_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```") and t.endswith("```"):
        t = t[3:]
        if "\n" in t:
            first_nl = t.find("\n")
            t = t[first_nl + 1 :]
        if t.endswith("```"):
            t = t[:-3]
    return t.strip()


def transform_summary_for_trello(summary_md: str):
    clean = _strip_fences(summary_md)
    lines = clean.splitlines()
    card_title = None
    for ln in lines:
        s = ln.strip()
        if s.startswith("# "):
            card_title = s[2:].strip()
            break
    section_starts = []
    for i, ln in enumerate(lines):
        s = ln.strip()
        if s.startswith("## "):
            name = s[3:].strip()
            section_starts.append((i, name))

    actions_idx = next(
        (i for i, n in section_starts if n.lower().startswith("actions")),
        None,
    )
    if actions_idx is not None:
        desc_lines = lines[:actions_idx]
    else:
        desc_lines = lines

    checklist_defs = []
    if actions_idx is not None:
        tail_sections = [(i, n) for i, n in section_starts if i >= actions_idx]
        for i, name in tail_sections:
            next_indices = [j for j, _ in section_starts if j > i]
            end = min(next_indices) if next_indices else len(lines)
            sec_lines = lines[i:end]
            items = []
            for ln in sec_lines[1:]:
                s = ln.strip()
                if s.startswith(("- ", "* ")):
                    items.append(s[2:].strip())
                elif re.match(r"^\d+\.\s+", s):
                    items.append(re.sub(r"^\d+\.\s+", "", s).strip())
            if items:
                checklist_defs.append((name, items))

    if desc_lines and desc_lines[0].strip().startswith("# "):
        desc_lines = desc_lines[1:]
    while desc_lines and desc_lines[0].strip() == "":
        desc_lines = desc_lines[1:]
    while desc_lines and desc_lines[-1].strip() == "":
        desc_lines = desc_lines[:-1]
    desc_text = "\n".join(desc_lines).strip()

    return card_title, desc_text, checklist_defs


@app.get("/")
def index():
    return send_from_directory("web", "index.html")

@app.get("/<path:filename>")
def web_static(filename: str):
    # Serve static assets in the web/ folder (e.g., config.js)
    return send_from_directory("web", filename)


@app.get("/prompt")
def get_prompt():
    # Return the active system prompt text for frontend display
    settings = load_settings()
    return jsonify({"prompt": settings.summary_system_prompt})


@app.post("/process")
def process():
    file = request.files.get("file")
    if not file or file.filename == "":
        return jsonify({"error": "No file provided"}), 400
    if not file.filename.lower().endswith(".docx"):
        return jsonify({"error": "Only .docx files supported"}), 400

    uploads_dir = os.path.join(os.path.dirname(__file__), "uploads")
    os.makedirs(uploads_dir, exist_ok=True)
    fname = f"{uuid4().hex}.docx"
    path = os.path.join(uploads_dir, fname)
    file.save(path)
    original_name = file.filename

    settings = load_settings()
    try:
        from meeting_notes.docx_utils import extract_text_from_docx

        transcript_text = extract_text_from_docx(path)
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 500

    # Optional one-time prompt override from the request
    one_time_prompt = request.form.get("system_prompt")
    summary_md = summarize_markdown(
        settings.openai_api_key,
        transcript_text,
        one_time_prompt or settings.summary_system_prompt,
    )

    # Save the generated summary to a local file to attach to the Trello card
    summary_fname = f"{uuid4().hex}.md"
    summary_path = os.path.join(os.path.dirname(__file__), "uploads", summary_fname)
    try:
        with open(summary_path, "w", encoding="utf-8") as f:
            f.write(summary_md or "")
    except Exception:
        # If writing the summary file fails, continue without blocking the process
        summary_path = None

    card_title, desc_text, checklist_defs = transform_summary_for_trello(summary_md)
    try:
        list_id = ensure_list_id(
            settings.trello_key, settings.trello_token, settings.trello_list_id_or_board_id
        )
        card_name = card_title or f"{settings.meeting_subject or 'Meeting'} – {dt.date.today().isoformat()}"
        card = create_card(settings.trello_key, settings.trello_token, list_id, card_name, desc_text)
        for cl_name, items in checklist_defs:
            cl = create_checklist(settings.trello_key, settings.trello_token, card.get("id"), cl_name)
            cl_id = cl.get("id")
            for it in items:
                add_checkitem(settings.trello_key, settings.trello_token, cl_id, it)
        # Attach the uploaded transcript file to the Trello card (best-effort)
        try:
            add_attachment_file(
                settings.trello_key,
                settings.trello_token,
                card.get("id"),
                path,
                original_name or fname,
            )
        except Exception:
            # Ignore attachment errors to avoid failing the whole process
            pass
        # Attach the generated summary markdown file to the Trello card (best-effort)
        try:
            if summary_path:
                summary_display_name = (card_name or "Meeting") + " – Summary.md"
                add_attachment_file(
                    settings.trello_key,
                    settings.trello_token,
                    card.get("id"),
                    summary_path,
                    summary_display_name,
                )
        except Exception:
            # Ignore attachment errors to avoid failing the whole process
            pass
        return jsonify({
            "cardUrl": card.get("shortUrl"),
            "cardId": card.get("id"),
            "title": card_name,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    # Bind to 0.0.0.0 and respect PORT env for cloud deployments
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_DEBUG", "0") == "1")