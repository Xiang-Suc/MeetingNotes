from typing import List, Optional

from openai import OpenAI


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
    return resp.choices[0].message.content