from typing import List, Optional
import re

from openai import OpenAI


def _apply_terminology_corrections(text: str) -> str:
    t = text or ""
    t = re.sub(r"\bZ\s*[-]?\s*cache\b", "Zcash", t, flags=re.IGNORECASE)
    t = re.sub(r"\bZcash\s+(?:D|Dee|Di|d|me|ME|Mee|Mi|May|M)\b", "Zcash Me", t, flags=re.IGNORECASE)
    t = re.sub(r"\bZcash\s+me\b", "Zcash Me", t, flags=re.IGNORECASE)
    return t


def summarize_markdown(api_key: str, transcript_text: str, system_prompt: Optional[str] = None) -> str:
    client = OpenAI(api_key=api_key)
    prompt = system_prompt or (
        "You are Meeting Markdown Assistant. Produce concise, well-structured Markdown including:"
        " 1) Overview; 2) Key decisions; 3) Action items (owner + due date if present);"
        " 4) Risks/Blockers; 5) Follow-ups. Keep it clear and scannable."
    )
    messages: List[dict] = [
        {
            "role": "system",
            "content": prompt,
        },
        {"role": "user", "content": transcript_text},
    ]

    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=messages,
        temperature=0.2,
    )
    return _apply_terminology_corrections(resp.choices[0].message.content)
