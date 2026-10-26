"""The answer path: find entry topics, walk the graph, compose cited steps, check, revise, drop, record gaps."""
from __future__ import annotations

import json
import time

from . import model as models
from .checks import check_step, norm, passed, tokens
from .graph import SET_NAMES, Graph, build
from .gaps import GapStore
from .search import Index

MAX_REVISIONS = 2


class Engine:
    def __init__(self, g: Graph | None = None, gaps: GapStore | None = None):
        self.g = g or build()
        self.ix = Index(self.g)
        self.gaps = gaps or GapStore()

    # ------------------------------------------------------------------ context
    def _ctx(self, path: dict) -> list[dict]:
        out = []
        for tid in path["topics"]:
            t = self.g.topics[tid]
            r = path["reached"][tid]
            out.append({"id": tid, "set": SET_NAMES.get(t.set, t.set), "title": t.title,
                        "reached_by": r["detail"] if r["type"] != "entry" else "entry topic",
                        "sections": [{"sid": s.sid, "title": s.title, "text": s.text} for s in t.sections if s.text]})
        return out

    def _cite(self, tid: str, sid: str | None, path: dict) -> dict:
        t = self.g.topics[tid]
        sec = next((s for s in t.sections if s.sid == sid), None)
        line = sec.line if sec else None
        r = path["reached"][tid]
        return {
            "topic": {"id": tid, "title": t.title, "set": t.set, "set_name": SET_NAMES.get(t.set, t.set),
                      "section": sid, "section_title": sec.title if sec else "", "url": t.url,
                      "source": self.g.source_link(t.file, line), "file": t.file, "line": line},
            "reached": {"type": r["type"], "detail": r["detail"], "from": r.get("from"), "via": r.get("via"),
                        "edges": [dict(e, link=self.g.source_link(e["file"], e["line"])) for e in r["edges"]]},
        }

    def _graph_view(self, path: dict) -> dict:
        nodes, edges, seen = [], [], set()

        def node(n):
            if n in seen:
                return
            seen.add(n)
            if n in self.g.topics:
                t = self.g.topics[n]
                nodes.append({"id": n, "label": t.title, "kind": "topic", "set": t.set})
            else:
                nodes.append({"id": n, "label": self.g.subjects.get(n, n), "kind": "subject", "set": ""})

        for tid in path["topics"]:
            node(tid)
            for e in path["reached"][tid]["edges"]:
                node(e["src"]), node(e["dst"])
                edges.append({"src": e["src"], "dst": e["dst"], "type": e["type"],
                              "where": f'{e["file"]}:{e["line"]}', "link": self.g.source_link(e["file"], e["line"])})
        return {"nodes": nodes, "edges": edges}

    # ------------------------------------------------------------------ answer
    def ask(self, question: str, model=None) -> dict:
        t0 = time.time()
        question = (question or "").strip()[:500]
        path = self.ix.path(question)
        base = {"question": question, "path": {"topics": path["topics"], "hits": path.get("hits", [])}}
        if not path["topics"]:
            return dict(base, summary="", steps=[], dropped=[], gaps=[], not_covered="No topic matches the question.",
                        model={"name": "none"}, graph={"nodes": [], "edges": []}, ms=int((time.time() - t0) * 1000))

        ctx = self._ctx(path)
        if model is None:
            model = models.GeminiModel() if models.has_key() else models.ExtractiveModel(question, ctx)
        if isinstance(model, models.ExtractiveModel) and not model.ctx:
            model.question, model.ctx = question, ctx
        prompt = json.dumps({"question": question, "topics": ctx}, ensure_ascii=False)
        model_error = None
        try:
            draft = model.compose(prompt)
        except Exception as e:
            model_error = f"{type(e).__name__}: {str(e)[:200]}"
            model = models.ExtractiveModel(question, ctx)
            draft = model.compose(prompt)

        steps = [dict(s, revisions=0) for s in draft.get("steps", []) if isinstance(s, dict)]
        results = [check_step(s, path["topics"], self.g.topics) for s in steps]
        for rnd in range(1, MAX_REVISIONS + 1):
            failed = [i for i, r in enumerate(results) if not passed(r)]
            if not failed:
                break
            req = {"question": question, "topics": ctx,
                   "failed": [{"index": i, "step": {k: steps[i].get(k) for k in ("text", "topic", "section", "quote")},
                               "failures": [r["detail"] for r in results[i] if not r["ok"]]} for i in failed]}
            try:
                rev = model.revise(json.dumps(req, ensure_ascii=False))
            except Exception as e:
                model_error = model_error or f"revise: {type(e).__name__}: {str(e)[:200]}"
                break
            for fix in rev.get("steps", []):
                i = fix.get("index")
                if isinstance(i, int) and i in failed:
                    steps[i] = dict({k: fix.get(k, "") for k in ("text", "topic", "section", "quote")},
                                    revisions=rnd)
                    results[i] = check_step(steps[i], path["topics"], self.g.topics)
                    if not steps[i]["text"]:
                        results[i].append({"name": "has text", "ok": False, "detail": "the model withdrew the step"})

        kept, dropped, gaps = [], [], []
        for s, r in zip(steps, results):
            if passed(r):
                kept.append(dict(text=s["text"], quote=s["quote"], revisions=s["revisions"], checks=r,
                                 **self._cite(s["topic"], s.get("section"), path)))
            else:
                reason = "; ".join(x["detail"] for x in r if not x["ok"])
                dropped.append({"text": s.get("text", ""), "topic": s.get("topic", ""), "reason": reason,
                                "revisions": s["revisions"]})
                gaps.append({"kind": "unchecked-step", "topic": s.get("topic", ""), "reason": reason,
                             "text": s.get("text", "")[:400],
                             "action": "Check whether the topic should state this, or whether the answer needs "
                                       "another topic on the path."})
        for i, s in enumerate(kept, start=1):
            s["n"] = i

        # A step that crosses documentation sets only through a shared subject has no link a reader could follow.
        cited = {s["topic"]["id"] for s in kept}
        for tid in path["topics"]:
            r = path["reached"][tid]
            src = r.get("from")
            if r["type"] == "about" and src and tid in cited and self.g.topics[src].set != self.g.topics[tid].set:
                a, b = self.g.topics[src], self.g.topics[tid]
                gaps.append({"kind": "missing-link", "from": src, "to": tid,
                             "subject": self.g.subjects.get(r.get("via"), r.get("via")),
                             "reason": f"{a.title} and {b.title} share a subject but neither links to the other",
                             "action": f"Add a related link from {a.file} to {b.file} (a reltable row or xref), "
                                       "so readers and the next build follow an authored link."})

        summary = draft.get("summary", "") or ""
        alltext = norm(" ".join(self.g.topics[t].text for t in path["topics"]))
        if any(norm(x) not in alltext for x in tokens(summary)) or not kept:
            summary = "" if not kept else "Here is what the documentation sets say, step by step."
        not_covered = draft.get("not_covered", "") or ""
        if any(norm(x) not in alltext for x in tokens(not_covered)):
            not_covered = ""

        if gaps:
            self.gaps.record(gaps, question)
        return dict(base,
                    summary=summary, steps=kept, dropped=dropped, gaps=gaps, not_covered=not_covered,
                    model={"name": model.name, "id": getattr(model, "id", ""), "tokens": getattr(model, "tokens", {}),
                           "error": model_error},
                    graph=self._graph_view(path), corpus_commit=self.g.commit, ms=int((time.time() - t0) * 1000))
