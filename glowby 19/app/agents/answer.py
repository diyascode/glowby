"""
Answer agent — for QUESTIONS typed into Glowby.

A statement gets a truth rating; a question gets an answer. Same
evidence machinery (fact-check database + live web hunt), different
output: a short, sourced answer instead of a score — a question is not
true or false, so rating it would be nonsense.

Rules mirror the judge fleet's: the evidence speaks, not the model's
memory; missing evidence is said out loud, never papered over.
"""

import json
import os

MODEL = os.environ.get("GLOWBY_CLAUDE_MODEL", "claude-sonnet-4-5")

PROMPT = """You are the answer agent for Glowby, a fact-checking service. \
A user typed a QUESTION. Evidence was gathered by the evidence agent. \
Answer the question in 1-4 plain sentences.

Rules:
- Base the answer ONLY on the evidence below — not on memory. Name the \
sources in the answer ("According to Reuters...").
- If sources disagree, say so and present both.
- If the evidence does not actually answer the question, say plainly what \
could not be determined — never guess to seem helpful.
- If the answer is time-sensitive (prices, office-holders, ongoing events), \
prefer the newest evidence and say when it's from.
- No hedging filler, no "great question", no speculation.

Question: "{question}"

Evidence:
{evidence}

Respond with ONLY the answer text (no JSON, no preamble)."""


def answer_question(question: str, evidence: dict):
    """Question + evidence bundle in, short sourced answer out (or None)."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None

    import anthropic

    ev_text = json.dumps({
        "professional_fact_checks": (evidence or {}).get("fact_checks", []),
        "web_sources": (evidence or {}).get("web_sources", []),
        "search_failed": bool((evidence or {}).get("search_failed")),
    }, indent=1)[:12000]
    try:
        client = anthropic.Anthropic(api_key=api_key)
        message = client.messages.create(
            model=MODEL,
            max_tokens=500,
            temperature=0,
            messages=[{"role": "user", "content": PROMPT.format(
                question=question[:500], evidence=ev_text)}],
        )
    except Exception:
        return None
    text = "".join(
        b.text for b in message.content if getattr(b, "type", "") == "text"
    ).strip()
    return text or None
