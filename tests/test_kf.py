import json
from pathlib import Path

import pytest

from kf.answer import Engine
from kf.gaps import GapStore
from kf.graph import AUTHORED, build
from kf.model import ExtractiveModel, ScriptedModel

SAMPLES = json.loads((Path(__file__).parent / "samples.json").read_text())


@pytest.fixture(scope="module")
def engine():
    return Engine(gaps=GapStore(use_firestore=False))


def test_graph_has_every_authored_edge_type_with_file_and_line(engine):
    g = engine.g
    assert {e.type for e in g.edges} == set(AUTHORED)
    assert all(e.file and e.line for e in g.edges)
    assert {"tpu", "nat"} <= {t.set for t in g.topics.values()}


def test_sets_join_only_through_a_shared_subject(engine):
    cross = [e for e in engine.g.edges if e.type != "about" and e.src.split(":")[0] != e.dst.split(":")[0]
             and e.dst in engine.g.topics]
    assert not cross, "the sample sets have no authored link between them; that is the problem KnowledgeFlow solves"


@pytest.mark.parametrize("s", SAMPLES, ids=[s["id"] for s in SAMPLES])
def test_sample_paths(engine, s):
    p = engine.ix.path(s["question"])
    assert p["entry"] == s["entry"]
    assert all(t in p["topics"] for t in s["path"]), p["topics"]
    sets = {engine.g.topics[t].set for t in p["topics"]}
    assert (len(sets) > 1) == s["cross_set"]


@pytest.mark.parametrize("s", SAMPLES, ids=[s["id"] for s in SAMPLES])
def test_sample_answers_cite_every_step(engine, s):
    r = engine.ask(s["question"], model=ExtractiveModel())
    assert r["steps"], r
    for st in r["steps"]:
        assert st["topic"]["id"] in r["path"]["topics"]
        assert st["reached"]["type"] and st["topic"]["source"].startswith("https://github.com/")
        assert all(c["ok"] for c in st["checks"])


def test_cross_set_answer_records_missing_link(engine):
    r = engine.ask(SAMPLES[0]["question"], model=ExtractiveModel())
    assert {st["topic"]["set"] for st in r["steps"]} == {"tpu", "nat"}
    gap = next(g for g in r["gaps"] if g["kind"] == "missing-link")
    assert gap["from"] == "tpu:tpu-networking" and gap["to"] == "nat:overview"
    assert any(g["id"] == gap["id"] for g in engine.gaps.list())


def test_failing_step_is_revised_then_dropped_and_recorded(engine):
    q = SAMPLES[0]["question"]
    good = {"text": "Create the VM with the `--internal-ips` flag so it has no external IP address.",
            "topic": "tpu:tpu-networking", "section": "ip-addresses",
            "quote": "To create one without an external IP address, use the --internal-ips flag"}
    invented = {"text": "Cloud NAT gives each VM `--min-ports-per-vm=4096` ports.", "topic": "nat:overview",
                "section": "types-of-cloud-nat", "quote": "The VMs share a set of external IP addresses"}
    off_path = {"text": "Each v5e chip has 16 GiB of HBM.", "topic": "tpu:tpu-v5e-specifications",
                "section": "body", "quote": "anything"}
    fixed = {"index": 2, "text": "Public NAT lets resources without external IPv4 addresses reach IPv4 destinations "
             "on the internet.", "topic": "nat:overview", "section": "types-of-cloud-nat",
             "quote": "Lets resources without external IPv4 addresses connect to IPv4 destinations on the internet"}
    m = ScriptedModel({"summary": "Use Cloud NAT.", "steps": [good, invented, off_path], "not_covered": ""},
                      [{"steps": [dict(invented, index=1), fixed]}, {"steps": [dict(invented, index=1)]}])
    r = engine.ask(q, model=m)
    assert [s["text"] for s in r["steps"]] == [good["text"], fixed["text"]]
    assert r["steps"][1]["revisions"] == 1
    assert len(r["dropped"]) == 1 and "--min-ports-per-vm=4096" in r["dropped"][0]["reason"]
    assert r["dropped"][0]["revisions"] == 2
    assert any(g["kind"] == "unchecked-step" for g in r["gaps"])


def test_api(engine):
    from fastapi.testclient import TestClient
    from api.main import app
    c = TestClient(app)
    assert c.get("/api/health").json()["ok"]
    r = c.post("/api/ask", json={"question": SAMPLES[0]["question"]}).json()
    assert r["steps"] and r["graph"]["edges"]
    assert c.get("/api/gaps").json()["gaps"]
