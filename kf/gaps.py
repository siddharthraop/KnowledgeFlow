"""Record gaps: steps the checks dropped, and cross-set joins that no writer has linked.

Gaps go to Firestore (collection `knowledgeflow_gaps`) when the service can reach it, and to memory otherwise.
A gap's id is a hash of what it is about, so the same gap asked twice is one record with a count.
"""
from __future__ import annotations

import hashlib
import logging
import os
import threading
import time

log = logging.getLogger("kf.gaps")
COLLECTION = os.environ.get("KF_GAPS_COLLECTION", "knowledgeflow_gaps")


class GapStore:
    def __init__(self, use_firestore: bool | None = None):
        self._mem: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._db = None
        if use_firestore is None:
            use_firestore = os.environ.get("KF_FIRESTORE", "0") == "1"
        if use_firestore:
            try:
                from google.cloud import firestore
                self._db = firestore.Client(project=os.environ.get("GOOGLE_CLOUD_PROJECT") or None)
            except Exception as e:  # pragma: no cover - depends on the runtime
                log.warning("Firestore unavailable, gaps kept in memory: %s", e)

    @property
    def backend(self):
        return "firestore" if self._db else "memory"

    @staticmethod
    def gap_id(g: dict) -> str:
        key = "|".join(str(g.get(k, "")) for k in ("kind", "from", "to", "topic", "reason"))
        return hashlib.sha1(key.encode()).hexdigest()[:16]

    def record(self, gaps: list[dict], question: str):
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        for g in gaps:
            gid = g["id"] = self.gap_id(g)
            with self._lock:
                cur = self._mem.get(gid)
                if cur:
                    cur["count"] += 1
                    cur["last_seen"] = now
                    cur["questions"] = (cur["questions"] + [question])[-5:]
                else:
                    self._mem[gid] = dict(g, count=1, first_seen=now, last_seen=now, questions=[question])
            if self._db:
                try:
                    from google.cloud import firestore
                    ref = self._db.collection(COLLECTION).document(gid)
                    ref.set(dict(g, last_seen=now, count=firestore.Increment(1),
                                 questions=firestore.ArrayUnion([question[:300]])), merge=True)
                except Exception as e:  # pragma: no cover
                    log.warning("Firestore write failed: %s", e)
                    self._db = None

    def list(self, limit: int = 100) -> list[dict]:
        if self._db:
            try:
                docs = self._db.collection(COLLECTION).order_by("last_seen", direction="DESCENDING").limit(limit)
                return [dict(d.to_dict(), id=d.id) for d in docs.stream()]
            except Exception as e:  # pragma: no cover
                log.warning("Firestore read failed: %s", e)
        with self._lock:
            return sorted(self._mem.values(), key=lambda g: g["last_seen"], reverse=True)[:limit]
