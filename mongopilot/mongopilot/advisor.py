"""Index advisor runs: combine stored slow queries with a live index inventory and save a report."""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from . import insights
from .crypto import SecretBox
from .db import Database, now

log = logging.getLogger("mongopilot.advisor")


def slow_rows(db: Database, dep_id: int, since: float, ns: str | None = None,
              limit: int = 50_000) -> list[dict[str, Any]]:
    sql = "SELECT * FROM slow_ops WHERE deployment_id = ? AND ts >= ?"
    params: list[Any] = [dep_id, since]
    if ns:
        sql += " AND ns = ?"
        params.append(ns)
    sql += " ORDER BY ts DESC LIMIT ?"
    params.append(limit)
    return db.query(sql, params)


def slow_groups(db: Database, dep_id: int, range_s: int, ns: str | None = None) -> list[dict[str, Any]]:
    return insights.group_slow_ops(slow_rows(db, dep_id, time.time() - range_s, ns))


class IndexAdvisor:
    def __init__(self, db: Database, box: SecretBox, probe):
        self.db = db
        self.box = box
        self.probe = probe

    def start(self, dep_id: int, range_s: int) -> int:
        running = self.db.one("SELECT id FROM index_reports WHERE deployment_id = ? AND status = 'running'",
                              (dep_id,))
        if running:
            raise ValueError("An analysis is already running for this deployment")
        return self.db.insert("index_reports", {"deployment_id": dep_id, "status": "running",
                                                "created_at": now(), "range_s": range_s})

    def run(self, report_id: int) -> dict[str, Any] | None:
        rep = self.db.one("SELECT * FROM index_reports WHERE id = ?", (report_id,))
        dep = self.db.one("SELECT * FROM deployments WHERE id = ?", (rep["deployment_id"],))
        try:
            uri = self.box.decrypt(dep["uri_enc"])
            topo = self.probe.discover(uri)
            inventory = self.probe.index_inventory(uri, topo)
            groups = slow_groups(self.db, dep["id"], rep["range_s"])
            data = insights.analyze(groups, inventory)
            data["topology"] = topo.kind
            self.db.update("index_reports", report_id, {"status": "done", "finished_at": now(),
                                                        "data": json.dumps(data, default=str)})
            return data
        except Exception as exc:
            log.warning("Index analysis of %s failed: %s", dep["name"], exc)
            msg = str(exc)
            if "not authorized" in msg or "Unauthorized" in msg:
                msg += (" — the monitoring user needs the listCollections, listIndexes, indexStats and collStats "
                        "privileges (see README: 'Query Performance privileges')")
            self.db.update("index_reports", report_id, {"status": "failed", "finished_at": now(),
                                                        "error": msg[:1500]})
            return None

    def latest(self, dep_id: int) -> dict[str, Any] | None:
        rep = self.db.one("SELECT * FROM index_reports WHERE deployment_id = ? ORDER BY id DESC LIMIT 1",
                          (dep_id,))
        if not rep:
            return None
        rep["data"] = json.loads(rep["data"]) if rep["data"] else None
        return rep
