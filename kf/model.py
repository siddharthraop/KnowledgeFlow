"""Step composers: Gemini (live), extractive (no key), scripted (tests)."""
from __future__ import annotations

import json
import os
import re

from .search import terms

SYSTEM = """You answer an engineer's question using ONLY the documentation topics provided.
The topics come from different documentation sets and were joined by links and subjects that writers authored.

Return JSON: {"summary": str, "steps": [{"text": str, "topic": str, "section": str, "quote": str}], "not_covered": str}

Rules:
- Write 2 to 5 ordered steps that answer the question. Each step is one or two plain sentences, second person.
- "topic" is a topic id from the input and "section" is one of its section ids.
- "quote" is an exact, contiguous passage of at least 8 words copied character for character from that section
  that supports the step.
- Put every command, flag, field name and value in backticks, copied exactly from the cited section. Never invent
  a command, flag, number or value that the cited section doesn't contain. Don't combine flags into a new command.
- "summary" is one sentence with no commands, flags or numbers.
- "not_covered" says briefly what the question asks that these topics don't state (empty string if nothing).
"""

REVISE = """Some steps failed the checks. For each failed step, return a corrected step that passes every check,
using only the cited topic's text, or return it with "text": "" if the topics can't support it.
Return JSON: {"steps": [{"index": int, "text": str, "topic": str, "section": str, "quote": str}]}"""


def _clean_key():
    """Secret values pasted from a clipboard can carry extra lines. Keep the one that looks like a key."""
    for var in ("GOOGLE_API_KEY", "GEMINI_API_KEY"):
        raw = os.environ.get(var, "")
        lines = [l.strip() for l in raw.splitlines() if l.strip()]
        if lines and (len(lines) > 1 or lines[0] != raw):
            os.environ[var] = next((l for l in lines if re.fullmatch(r"(AIza[\w-]{30,}|AQ\.[\w.-]{20,})", l)),
                                   lines[0])


def _json(text: str):
    text = (text or "").strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    return json.loads(text)


class GeminiModel:
    name = "gemini"

    def __init__(self, model_id: str | None = None):
        _clean_key()
        from google import genai
        from google.genai import types
        self.id = model_id or os.environ.get("KF_MODEL", "gemini-3.8-flash")
        self.client = genai.Client(api_key=os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY"))
        self.types = types
        self.tokens = {"input": 0, "output": 0}

    def _ask(self, system: str, prompt: str):
        cfg = self.types.GenerateContentConfig(system_instruction=system, temperature=0.1,
                                               response_mime_type="application/json")
        r = self.client.models.generate_content(model=self.id, contents=prompt, config=cfg)
        um = getattr(r, "usage_metadata", None)
        if um:
            self.tokens["input"] += um.prompt_token_count or 0
            self.tokens["output"] += um.candidates_token_count or 0
        return _json(r.text)

    def compose(self, prompt: str):
        return self._ask(SYSTEM, prompt)

    def revise(self, prompt: str):
        return self._ask(SYSTEM + "\n" + REVISE, prompt)


class ExtractiveModel:
    """No model: for each topic on the path, quote the sentence that best matches the question."""
    name = "extractive"
    id = "none"
    tokens = {"input": 0, "output": 0}

    def __init__(self, question: str = "", ctx: list | None = None):
        self.question, self.ctx = question, ctx or []

    def compose(self, prompt: str):
        q = set(terms(self.question))
        steps = []
        for t in self.ctx:
            best = None
            for s in t["sections"]:
                for sent in re.split(r"(?<=[.:])\s+(?=[A-Z])", s["text"]):
                    sc = len(q & set(terms(sent)))
                    if len(sent.split()) >= 6 and (best is None or sc > best[0]):
                        best = (sc, s["sid"], sent.strip())
            if best and best[0] > 0:
                steps.append({"text": best[2], "topic": t["id"], "section": best[1], "quote": best[2]})
        return {"summary": "These passages from the documentation sets answer the question, in path order.",
                "steps": steps, "not_covered": ""}

    def revise(self, prompt: str):
        return {"steps": []}


class ScriptedModel:
    """Replays fixed responses; used by tests to prove the checks."""
    name = "scripted"
    id = "scripted"
    tokens = {"input": 0, "output": 0}

    def __init__(self, compose: dict, revisions: list[dict] | None = None):
        self._c, self._r = compose, list(revisions or [])

    def compose(self, prompt):
        return json.loads(json.dumps(self._c))

    def revise(self, prompt):
        return self._r.pop(0) if self._r else {"steps": []}


def has_key() -> bool:
    return bool(os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY"))
