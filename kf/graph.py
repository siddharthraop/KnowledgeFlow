"""Build the knowledge graph from authored structure only.

Two documentation sets from the corpus:
  tpu/  DITA 1.3 (map hierarchy, reltable, conrefs, keyrefs, xrefs, subject classification)
  nat/  MyST Markdown (toctree, links, substitutions, front-matter subjects)

Edge types (all authored; each edge records the file and line it was read from):
  contains  map hierarchy or toctree
  related   reltable row
  reuses    conref
  states    keyref or MyST substitution
  links-to  xref or MyST link
  about     subjectref in the classification map, or `subjects:` in MyST front matter
"""
from __future__ import annotations

import ast
import copy
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from lxml import etree

AUTHORED = ("contains", "related", "reuses", "states", "links-to", "about")
SET_NAMES = {"tpu": "Cloud TPU (DITA)", "nat": "Cloud NAT (MyST)"}
CORPUS_REPO = "https://github.com/siddharthraop/dita-corpus"


@dataclass
class Section:
    sid: str
    title: str
    text: str
    line: int


@dataclass
class Topic:
    id: str            # "<set>:<name>", for example "tpu:tpu-networking"
    set: str
    file: str          # path relative to the corpus root
    title: str
    shortdesc: str
    url: str           # the published page the topic was adapted from
    sections: list[Section] = field(default_factory=list)
    searchable: bool = True

    @property
    def text(self) -> str:
        return "\n".join([self.title, self.shortdesc] + [f"{s.title}\n{s.text}" for s in self.sections])


@dataclass
class Edge:
    src: str
    dst: str
    type: str
    file: str
    line: int
    detail: str = ""

    def as_dict(self):
        return {"src": self.src, "dst": self.dst, "type": self.type, "file": self.file, "line": self.line,
                "detail": self.detail}


@dataclass
class Graph:
    topics: dict[str, Topic] = field(default_factory=dict)
    edges: list[Edge] = field(default_factory=list)
    keys: dict[str, str] = field(default_factory=dict)       # "key:<set>:<name>" -> value
    subjects: dict[str, str] = field(default_factory=dict)   # "subject:<key>" -> label
    subject_parent: dict[str, str] = field(default_factory=dict)
    commit: str = "main"

    def add(self, *a, **kw):
        self.edges.append(Edge(*a, **kw))

    def source_link(self, file: str, line: int | None = None) -> str:
        return f"{CORPUS_REPO}/blob/{self.commit}/{file}" + (f"#L{line}" if line else "")

    def stats(self):
        by = {}
        for e in self.edges:
            by[e.type] = by.get(e.type, 0) + 1
        return {"topics": len(self.topics), "subjects": len(self.subjects), "keys": len(self.keys),
                "edges": len(self.edges), "by_type": by, "corpus_commit": self.commit}


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def _rel(path: Path, root: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


# ----------------------------------------------------------------------------- DITA

_parser = etree.XMLParser(load_dtd=False, no_network=True, resolve_entities=False, remove_comments=True)


def _parse(path: Path):
    return etree.parse(str(path), _parser)


_BLOCK = {"p", "li", "entry", "step", "cmd", "info", "note", "codeblock", "row", "title", "section", "prereq",
          "result", "dt", "dd", "stepresult", "context", "postreq", "shortdesc", "ul", "ol", "table", "dl"}


def _text(el) -> str:
    """Element text with spaces between block elements and none inside inline ones."""
    el = copy.deepcopy(el)
    for e in el.iter():
        if isinstance(e.tag, str) and e.tag in _BLOCK:
            e.tail = " " + (e.tail or "")
            e.text = (e.text or "")
    return _norm("".join(el.itertext()))


def _dita_topic_id(file: str) -> str:
    return "tpu:" + Path(file).stem


def _read_keys(root: Path, g: Graph):
    keymap = root / "tpu/keys/tpu-keys.ditamap"
    for kd in _parse(keymap).iter("keydef"):
        kw = kd.find("topicmeta/keywords/keyword")
        val = _norm("".join(kw.itertext())) if kw is not None else ""
        for k in kd.get("keys", "").split():
            g.keys[f"key:tpu:{k}"] = val


def _resolve(el, file_path: Path, g: Graph, cache: dict):
    """Inline keyref values and conref content so the topic text is what a reader sees."""
    for kw in el.iter():
        kr = kw.get("keyref")
        if kr and not "".join(kw.itertext()).strip():
            kw.text = g.keys.get(f"key:tpu:{kr}", kr)
    for c in list(el.iter()):
        ref = c.get("conref")
        if not ref:
            continue
        href, _, frag = ref.partition("#")
        target = (file_path.parent / href).resolve()
        if target not in cache:
            cache[target] = _parse(target)
        eid = frag.split("/")[-1]
        hit = cache[target].xpath(f"//*[@id='{eid}']")
        if hit:
            src = copy.deepcopy(hit[0])
            _resolve(src, target, g, cache)
            c.text = src.text
            for ch in list(c):
                c.remove(ch)
            for ch in src:
                c.append(ch)


def _dita_topic(path: Path, root: Path, g: Graph, cache: dict) -> Topic:
    file = _rel(path, root)
    tree = _parse(path)
    top = tree.getroot()
    work = copy.deepcopy(top)
    _resolve(work, path, g, cache)
    title = _norm("".join(work.find("title").itertext())) if work.find("title") is not None else path.stem
    sd = work.find("shortdesc")
    shortdesc = _norm("".join(sd.itertext())) if sd is not None else ""
    src = top.find("prolog/data[@name='source']")
    url = src.get("href") if src is not None else ""
    t = Topic(id=_dita_topic_id(file), set="tpu", file=file, title=title, shortdesc=shortdesc, url=url,
              searchable="/reuse/" not in file)
    # Sections: <section> elements, or the whole body when a topic has none (tasks, simple topics).
    orig_by_id = {e.get("id"): e for e in top.iter() if e.get("id")}
    body = next((work.find(b) for b in ("conbody", "refbody", "taskbody", "body") if work.find(b) is not None), None)
    if body is not None:
        secs = body.findall(".//section")
        if body.tag == "taskbody":
            for part in body:
                if not isinstance(part.tag, str):
                    continue
                if part.tag == "steps":
                    for n, st in enumerate(part.findall("step"), start=1):
                        sid = f"step-{n}"
                        oid = None
                        t.sections.append(Section(sid, f"Step {n}", _text(st), st.sourceline))
                else:
                    t.sections.append(Section(part.tag, part.tag.capitalize(), _text(part), part.sourceline))
        elif secs:
            for s in secs:
                st = s.find("title")
                stitle = _norm("".join(st.itertext())) if st is not None else ""
                if st is not None:
                    st.getparent().remove(st)
                orig = orig_by_id.get(s.get("id"))
                t.sections.append(Section(s.get("id") or f"s{len(t.sections)+1}", stitle, _text(s),
                                          orig.sourceline if orig is not None else s.sourceline))
            # body text outside sections
            rest = copy.deepcopy(body)
            for s in rest.findall(".//section"):
                s.getparent().remove(s)
            txt = _text(rest)
            if txt:
                t.sections.insert(0, Section("body", "", txt, body.sourceline))
        else:
            orig_body = next((top.find(b) for b in ("conbody", "refbody", "taskbody", "body") if top.find(b) is not None), None)
            t.sections.append(Section("body", "", _text(body),
                                      orig_body.sourceline if orig_body is not None else 1))

    # Edges read from the original (unresolved) file, with their own lines.
    for el in top.iter():
        if not isinstance(el.tag, str):
            continue
        kr = el.get("keyref")
        if kr:
            g.add(t.id, f"key:tpu:{kr}", "states", file, el.sourceline, kr)
        cr = el.get("conref")
        if cr:
            href = cr.partition("#")[0]
            g.add(t.id, _dita_topic_id(_rel(path.parent / href, root)), "reuses", file, el.sourceline, cr)
        if el.tag in ("xref", "link") and el.get("href") and el.get("scope") != "external":
            href = el.get("href").partition("#")[0]
            if href.endswith(".dita"):
                g.add(t.id, _dita_topic_id(_rel(path.parent / href, root)), "links-to", file, el.sourceline, href)
    return t


def _read_dita(root: Path, g: Graph):
    _read_keys(root, g)
    cache: dict = {}
    for p in sorted((root / "tpu").rglob("*.dita")):
        t = _dita_topic(p, root, g, cache)
        g.topics[t.id] = t

    mp = root / "tpu/tpu.ditamap"
    mfile = _rel(mp, root)
    mtree = _parse(mp)

    def tid(tr):
        return _dita_topic_id(_rel(mp.parent / tr.get("href"), root))

    # contains: map hierarchy (topicref inside topicref), outside the reltable
    for tr in mtree.getroot().iter("topicref"):
        if tr.get("href", "").endswith(".dita") and not any(a.tag == "reltable" for a in tr.iterancestors()):
            for child in tr.findall("topicref"):
                if child.get("href", "").endswith(".dita"):
                    g.add(tid(tr), tid(child), "contains", mfile, child.sourceline)
    # related: every pair of topics in different cells of a relrow
    for row in mtree.getroot().iter("relrow"):
        cells = [[tr for tr in c.iter("topicref") if tr.get("href")] for c in row.findall("relcell")]
        for i, a in enumerate(cells):
            for b in cells[i + 1:]:
                for x in a:
                    for y in b:
                        g.add(tid(x), tid(y), "related", mfile, row.sourceline, "reltable row")

    # subjects and about edges
    scheme = root / "tpu/subjects/tpu-scheme.ditamap"
    for sd in _parse(scheme).iter("subjectdef"):
        nt = sd.find("topicmeta/navtitle")
        par = sd.getparent()
        for k in sd.get("keys", "").split():
            g.subjects[f"subject:{k}"] = _norm(nt.text) if nt is not None else k
            if par is not None and par.tag == "subjectdef" and par.get("keys"):
                g.subject_parent[f"subject:{k}"] = f"subject:{par.get('keys').split()[0]}"
    cls = root / "tpu/subjects/tpu-classification.ditamap"
    cfile = _rel(cls, root)
    for tr in _parse(cls).getroot().iter("topicref"):
        for sr in tr.iter("subjectref"):
            g.add(_dita_topic_id(_rel(cls.parent / tr.get("href"), root)), f"subject:{sr.get('keyref')}", "about",
                  cfile, sr.sourceline, sr.get("keyref"))


# ----------------------------------------------------------------------------- MyST

_FM = re.compile(r"\A---\n(.*?)\n---\n", re.S)


def _read_myst(root: Path, g: Graph):
    nat = root / "nat"
    conf = (nat / "conf.py").read_text()
    subs = {}
    for node in ast.parse(conf).body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "myst_substitutions":
            subs = ast.literal_eval(node.value)
    for k, v in subs.items():
        g.keys[f"key:nat:{k}"] = str(v)

    def sub(s):
        return re.sub(r"\{\{\s*(\w+)\s*\}\}", lambda m: str(subs.get(m.group(1), m.group(0))), s)

    for p in sorted(nat.glob("*.md")):
        file = _rel(p, root)
        raw = p.read_text()
        lines = raw.splitlines()
        tidv = f"nat:{p.stem}"
        fm = {}
        m = _FM.match(raw)
        body_start = 0
        if m:
            body_start = m.group(0).count("\n")
            for i, ln in enumerate(m.group(1).splitlines(), start=2):
                k, _, v = ln.partition(":")
                fm[k.strip()] = (v.strip(), i)
        url = fm.get("source", ("", 0))[0]
        title, shortdesc, sections = p.stem, "", []
        cur = None
        in_fence = False
        para = []
        for i in range(body_start, len(lines)):
            ln = lines[i]
            n = i + 1
            if ln.startswith("```") or ln.startswith(":::"):
                in_fence = not in_fence if ln.strip() in ("```", ":::") or not in_fence else in_fence
            hm = re.match(r"^(#{1,3})\s+(.*)", ln) if not in_fence else None
            if hm and len(hm.group(1)) == 1:
                title = _norm(sub(hm.group(2)))
                continue
            if hm:
                cur = Section(re.sub(r"[^a-z0-9]+", "-", sub(hm.group(2)).lower()).strip("-"), _norm(sub(hm.group(2))), "", n)
                sections.append(cur)
                continue
            if cur is None:
                if ln.strip() and not shortdesc:
                    para.append(ln)
                elif para and not ln.strip():
                    shortdesc = _norm(sub(" ".join(para)))
                continue
            cur.text += " " + ln
        if para and not shortdesc:
            shortdesc = _norm(sub(" ".join(para)))
        for s in sections:
            s.text = _norm(sub(re.sub(r"^\(\S+\)=$", "", s.text)))
        t = Topic(id=tidv, set="nat", file=file, title=title, shortdesc=shortdesc, url=url, sections=sections,
                  searchable=p.stem != "index")
        g.topics[tidv] = t

        # edges
        if "subjects" in fm:
            v, ln_no = fm["subjects"]
            for s in re.findall(r"[\w-]+", v):
                g.add(tidv, f"subject:{s}", "about", file, ln_no, s)
        in_toc = False
        for i, ln in enumerate(lines, start=1):
            if ln.startswith("```{toctree}"):
                in_toc = True
                continue
            if in_toc:
                if ln.startswith("```"):
                    in_toc = False
                elif ln.strip() and not ln.startswith(":"):
                    g.add(tidv, f"nat:{ln.strip()}", "contains", file, i)
                continue
            for mm in re.finditer(r"\]\(([\w./-]+\.md)(#[\w-]+)?\)", ln):
                g.add(tidv, f"nat:{Path(mm.group(1)).stem}", "links-to", file, i, mm.group(1))
            for mm in re.finditer(r"\{\{\s*(\w+)\s*\}\}", ln):
                g.add(tidv, f"key:nat:{mm.group(1)}", "states", file, i, mm.group(1))


def corpus_root() -> Path:
    env = os.environ.get("KF_CORPUS")
    if env:
        return Path(env)
    return Path(__file__).resolve().parent.parent / "corpus"


def build(root: Path | None = None) -> Graph:
    root = Path(root or corpus_root())
    g = Graph()
    commit = os.environ.get("KF_CORPUS_COMMIT", "")
    if not commit:
        try:
            commit = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True,
                                    timeout=5).stdout.strip()
        except Exception:
            commit = ""
    g.commit = commit or "main"
    _read_dita(root, g)
    _read_myst(root, g)
    # keep only edges whose ends exist
    known = set(g.topics) | set(g.keys) | set(g.subjects)
    g.edges = [e for e in g.edges if e.src in known and e.dst in known]
    return g


if __name__ == "__main__":
    import json
    gr = build()
    print(json.dumps(gr.stats(), indent=2))
