"""Plain-Python checks. The model composes steps; these decide whether a step reaches the reader."""
from __future__ import annotations

import re

from .graph import Topic

_CODE = re.compile(r"`([^`]+)`")
_FLAG = re.compile(r"(?<![\w-])--[a-z][a-z0-9-]*(?:=[^\s`,;)]+)?", re.I)
_NUM = re.compile(r"(?<![\w.-])\d[\d,]*(?:\.\d+)?(?![\w-])")
_IDENT = re.compile(r"\b[a-z][a-z0-9]*(?:[_][a-z0-9]+)+\b|\b[a-z]+\d[\w.-]*\b", re.I)


def norm(s: str) -> str:
    s = (s or "").replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    s = re.sub(r"\s*\\\s*", " ", s)          # shell line continuations
    s = re.sub(r"\s*=\s*", "=", s)
    return re.sub(r"\s+", " ", s).strip().lower()


def tokens(text: str) -> list[str]:
    """Every command, flag, field name and number the step states."""
    found = []
    for m in _CODE.findall(text):
        found.append(m)
    plain = _CODE.sub(" ", text)
    found += _FLAG.findall(plain)
    found += _IDENT.findall(plain)
    found += _NUM.findall(plain)
    return [t for t in dict.fromkeys(x.strip() for x in found) if t]


def _in(tok: str, src: str) -> bool:
    t = norm(tok)
    if t in src:
        return True
    if re.fullmatch(r"[\d,]+(\.\d+)?", t):
        return t.replace(",", "") in src.replace(",", "")
    return False


def check_step(step: dict, path: list[str], topics: dict[str, Topic]) -> list[dict]:
    res = []
    tid = step.get("topic", "")
    on = tid in path and tid in topics
    res.append({"name": "topic on path", "ok": on, "detail": tid if on else f"{tid or '(none)'} is not on the path"})
    if not on:
        return res
    t = topics[tid]
    sec = next((s for s in t.sections if s.sid == step.get("section")), None)
    src_topic = norm(t.text)
    quote = norm(step.get("quote", ""))
    q_ok = len(quote.split()) >= 5 and quote in src_topic
    res.append({"name": "quote in source", "ok": q_ok,
                "detail": "found" if q_ok else "the quote is not in the cited topic word for word"})
    if q_ok and (sec is None or quote not in norm(sec.text)):
        # cite the section that really holds the quote
        sec = next((s for s in t.sections if quote in norm(s.text)), sec)
        step["section"] = sec.sid if sec else step.get("section")
    missing = [x for x in tokens(step.get("text", "")) if not _in(x, src_topic)]
    res.append({"name": "values in source", "ok": not missing,
                "detail": "every command, flag and value appears in the topic" if not missing
                else "not in the topic: " + ", ".join(missing)})
    if not step.get("text", "").strip():
        res.append({"name": "has text", "ok": False, "detail": "empty step"})
    return res


def passed(results: list[dict]) -> bool:
    return all(r["ok"] for r in results)
