"""Find entry topics with BM25 (SQLite FTS5), then walk the graph to a cited path."""
from __future__ import annotations

import re
import sqlite3

import networkx as nx

from .graph import Graph

STOP = set("""a an and are as at be by can do does for from has have how i if in into is it its my no not of on or
our so that the their them then there these this to was what when where which who why will with without you your
me we us vs""".split())

# Traversal: topic-to-topic edges and topic-subject-topic joins. Keys and reuse warehouses are not walked;
# their text is already inlined into each topic.
WALK = {"contains", "related", "links-to", "about"}
# Prefer edges a writer drew between the two topics over the shared-subject join.
COST = {"related": 1.0, "links-to": 1.0, "contains": 1.2, "about": 1.5}


def terms(q: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9][a-z0-9_-]*", q.lower()) if w not in STOP and len(w) > 1]


class Index:
    def __init__(self, g: Graph):
        self.g = g
        self.db = sqlite3.connect(":memory:", check_same_thread=False)
        self.db.execute("CREATE VIRTUAL TABLE t USING fts5(id UNINDEXED, title, shortdesc, body, "
                        "tokenize='porter unicode61')")
        for t in g.topics.values():
            if t.searchable:
                self.db.execute("INSERT INTO t VALUES (?,?,?,?)",
                                (t.id, t.title, t.shortdesc, " ".join(f"{s.title} {s.text}" for s in t.sections)))
        self.nx = nx.Graph()
        for e in g.edges:
            if e.type not in WALK:
                continue
            a, b = e.src, e.dst
            if a == b:
                continue
            w = COST[e.type]
            if e.type == "about" and e.dst in set(g.subject_parent.values()):
                w += 0.5  # a broad subject joins less precisely than a specific one
            if self.nx.has_edge(a, b):
                d = self.nx[a][b]
                d["edges"].append(e)
                d["weight"] = min(d["weight"], w)
            else:
                self.nx.add_edge(a, b, weight=w, edges=[e])

    def bm25(self, q: str, k: int = 8) -> list[tuple[str, float]]:
        ts = terms(q)
        if not ts:
            return []
        match = " OR ".join(f'"{t}"' for t in ts)
        rows = self.db.execute("SELECT id, -bm25(t, 0, 3.0, 2.0, 1.0) AS s FROM t WHERE t MATCH ? ORDER BY s DESC "
                               "LIMIT ?", (match, k)).fetchall()
        return [(r[0], round(r[1], 3)) for r in rows]

    def path(self, q: str, max_topics: int = 4) -> dict:
        """Entry topic = best BM25 hit. Add other strong hits, and, when the hits stay inside one set, the best
        topic in another set that shares a subject with the entry. Each added topic is joined to the path by the
        cheapest authored route (at most 3 hops)."""
        hits = self.bm25(q)
        if not hits:
            return {"entry": None, "topics": [], "edges": [], "hits": []}
        # A product named in the question marks where the reader starts.
        qt = set(terms(q))
        boost = {"tpu": 1.15 if "tpu" in qt else 1.0, "nat": 1.15 if "nat" in qt else 1.0}
        hits = sorted(((t, round(s * boost.get(self.g.topics[t].set, 1.0), 3)) for t, s in hits),
                      key=lambda x: -x[1])
        score = dict(hits)
        entry = hits[0][0]
        top = hits[0][1]
        cand = [t for t, s in hits[1:] if s >= 0.5 * top]
        sets = {self.g.topics[entry].set} | {self.g.topics[t].set for t in cand}
        if len(sets) == 1:
            # cross-set via shared subjects of the entry topic
            other = []
            parents = set(self.g.subject_parent.values())
            leaf = [n for n in self.nx.neighbors(entry) if n.startswith("subject:") and n not in parents] \
                if entry in self.nx else []
            for subj in leaf:
                for n in self.nx.neighbors(subj):
                    if n in self.g.topics and self.g.topics[n].set not in sets and self.g.topics[n].searchable:
                        if score.get(n, 0.0) > 0:
                            other.append((score[n], n))
            if other:
                other.sort(reverse=True)
                cand.append(other[0][1])
        order = [entry]
        reached = {entry: {"type": "entry", "detail": f"BM25 rank 1 (score {top})", "from": None, "edges": []}}
        for c in cand:
            if len(order) >= max_topics or c in reached:
                continue
            best = None
            for src in order:
                try:
                    p = nx.shortest_path(self.nx, src, c, weight="weight")
                except (nx.NetworkXNoPath, nx.NodeNotFound):
                    continue
                if len(p) - 1 > 3:
                    continue
                cost = nx.path_weight(self.nx, p, "weight")
                if best is None or cost < best[0]:
                    best = (cost, p)
            if not best:
                continue
            p = best[1]
            # every topic on the route joins the path; subjects are recorded on the edge that crosses them
            prev = p[0]
            i = 1
            while i < len(p):
                node = p[i]
                hop = [self._edge(prev, node)]
                if self._join(node) and i + 1 < len(p):
                    nxt = p[i + 1]
                    hop.append(self._edge(node, nxt))
                    if node.startswith("subject:"):
                        typ, det = "about", f"shared subject: {self.g.subjects.get(node, node)}"
                    else:
                        typ, det = hop[1].type, f"{hop[0].type} {self.g.topics[node].title} / {hop[1].type}"
                    if nxt not in reached and len(order) < max_topics:
                        reached[nxt] = {"type": typ, "detail": det, "from": prev, "via": node,
                                        "edges": [e.as_dict() for e in hop]}
                        order.append(nxt)
                    prev = nxt
                    i += 2
                    continue
                if node not in reached and len(order) < max_topics:
                    e = hop[0]
                    reached[node] = {"type": e.type, "detail": e.detail or e.type, "from": prev,
                                     "edges": [e.as_dict()]}
                    order.append(node)
                prev = node
                i += 1
        for t in order:
            reached[t]["bm25"] = score.get(t)
        return {"entry": entry, "topics": order, "reached": reached, "hits": hits}

    def _join(self, n):
        return n.startswith("subject:") or (n in self.g.topics and not self.g.topics[n].searchable)

    def _edge(self, a, b):
        es = self.nx[a][b]["edges"]
        return min(es, key=lambda e: COST[e.type])
