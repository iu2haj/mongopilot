"""Monitoring loop: discovers the topology, samples every process, stores metrics and slow queries,
and collects host hardware metrics from registered servers over SSH."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from . import hostmetrics
from . import insights
from . import metrics as M
from .crypto import SecretBox
from .db import Database, now
from .probe import Node, Topology

log = logging.getLogger("mongopilot.monitor")


def is_data_bearing(node: Node) -> bool:
    return node.role in ("PRIMARY", "SECONDARY", "STANDALONE") and node.shard != "config"


class Collector:
    def __init__(self, db: Database, box: SecretBox, probe, slow_op_seconds: int = 5,
                 concurrency: int = 8, on_sample: Callable[[int], Any] | None = None,
                 capture_slow_queries: bool = True):
        self.db = db
        self.box = box
        self.probe = probe
        self.slow_op_seconds = slow_op_seconds
        self.concurrency = concurrency
        self.on_sample = on_sample
        self.capture_slow_queries = capture_slow_queries
        self._prev: dict[tuple[int, str], tuple[float, dict[str, float], float | None]] = {}
        self._log_since: dict[tuple[int, str], float] = {}
        self._last_primary: dict[tuple[int, str], str | None] = {}
        self._pool = ThreadPoolExecutor(max_workers=max(4, concurrency * 4), thread_name_prefix="probe")

    # -- loop ---------------------------------------------------------------
    async def poll_all(self) -> None:
        deps = self.db.query("SELECT id FROM deployments WHERE paused = 0")
        sem = asyncio.Semaphore(self.concurrency)

        async def one(dep_id: int):
            async with sem:
                await asyncio.to_thread(self.poll_deployment, dep_id)

        await asyncio.gather(*(one(d["id"]) for d in deps), return_exceptions=True)

    def poll_deployment(self, dep_id: int) -> dict[str, Any] | None:
        dep = self.db.one("SELECT * FROM deployments WHERE id = ?", (dep_id,))
        if not dep:
            return None
        uri = self.box.decrypt(dep["uri_enc"])
        ts = now()
        try:
            topo: Topology = self.probe.discover(uri)
        except Exception as exc:  # the whole deployment is unreachable
            msg = _short_error(exc)
            log.warning("Deployment %s unreachable: %s", dep["name"], msg)
            self.db.update("deployments", dep_id, {"last_poll": ts, "last_error": msg})
            self.db.execute("UPDATE processes SET up = 0, last_error = ? WHERE deployment_id = ?", (msg, dep_id))
            procs = self.db.query("SELECT id FROM processes WHERE deployment_id = ?", (dep_id,))
            rows = [(f"proc:{p['id']}", ts, {"up": 0}) for p in procs]
            rows.append((f"dep:{dep_id}", ts, {"has_primary": 0, "nodes_down": len(procs) or 1}))
            self.db.store_metrics(rows)
            if self.on_sample:
                self.on_sample(dep_id)
            return None

        rs_info = {name: M.replica_metrics(st) for name, st in topo.replica_sets.items()}
        futures = {n.host: self._pool.submit(self._sample_node, dep_id, uri, n) for n in topo.nodes}
        results: dict[str, tuple[dict[str, Any] | None, str | None, str | None]] = {
            h: f.result() for h, f in futures.items()}

        # Primary oplog window per replica set, for replication headroom on secondaries
        primary_window: dict[str, float] = {}
        for node in topo.nodes:
            raw = results[node.host][0]
            if node.role == "PRIMARY" and raw and raw.get("oplog_window_h") is not None:
                primary_window[node.rs_name or ""] = raw["oplog_window_h"]

        rows: list[tuple[str, int, dict[str, Any]]] = []
        seen_ids: list[int] = []
        nodes_down = 0
        versions: set[str] = set()
        fcv = None
        slow_log_errors: list[str] = []
        for node in topo.nodes:
            raw, err, log_err = results[node.host]
            if log_err:
                slow_log_errors.append(f"{node.host}: {log_err}")
            rs = rs_info.get(node.rs_name or "", {})
            member = rs.get("members", {}).get(node.host, {})
            proc_id = self._upsert_process(dep_id, node, raw, err, ts)
            seen_ids.append(proc_id)
            values: dict[str, Any] = {"up": 1 if raw is not None else 0}
            if raw is None:
                nodes_down += 1
            else:
                values.update(self._process_values(dep_id, node.host, raw))
                v = (raw.get("serverStatus") or {}).get("version")
                if v:
                    versions.add(v)
                fcv = fcv or raw.get("fcv")
            if member:
                values["member_state_ok"] = member["state_ok"]
                if member.get("lag") is not None:
                    values["repl_lag_s"] = round(member["lag"], 1)
                    pw = primary_window.get(node.rs_name or "")
                    if pw is not None:
                        values["repl_headroom_h"] = round(pw - member["lag"] / 3600, 2)
            elif node.role in ("MONGOS", "STANDALONE"):
                values["member_state_ok"] = values["up"]
            rows.append((f"proc:{proc_id}", ts, values))

        if seen_ids:
            marks = ",".join("?" * len(seen_ids))
            self.db.execute(f"DELETE FROM processes WHERE deployment_id = ? AND id NOT IN ({marks})",
                            (dep_id, *seen_ids))

        dep_values: dict[str, Any] = {"nodes_down": nodes_down}
        if rs_info:
            dep_values["has_primary"] = min(r["has_primary"] for r in rs_info.values())
            dep_values["members_unhealthy"] = sum(r["members_unhealthy"] for r in rs_info.values())
            changes = 0
            for name, r in rs_info.items():
                key = (dep_id, name)
                if key in self._last_primary and r["primary"] and r["primary"] != self._last_primary[key]:
                    changes += 1
                if r["primary"]:
                    self._last_primary[key] = r["primary"]
            dep_values["primary_changes"] = changes
        for k in ("balancer_enabled", "jumbo_chunks"):
            if topo.info.get(k) is not None:
                dep_values[k] = topo.info[k]
        rows.append((f"dep:{dep_id}", ts, dep_values))
        self.db.store_metrics(rows)

        status = {
            "kind": topo.kind,
            "versions": sorted(versions),
            "fcv": fcv,
            "nodes": len(topo.nodes),
            "nodes_down": nodes_down,
            "replica_sets": {k: {"primary": v["primary"], "has_primary": v["has_primary"],
                                 "members_unhealthy": v["members_unhealthy"]} for k, v in rs_info.items()},
            "slow_log_errors": slow_log_errors[:5],
            **topo.info,
        }
        self.db.update("deployments", dep_id, {"topology": topo.kind, "status": json.dumps(status),
                                               "last_poll": ts, "last_error": None})
        if self.on_sample:
            self.on_sample(dep_id)
        return status

    # -- single process ------------------------------------------------------
    def _sample_node(self, dep_id: int, uri: str,
                     node: Node) -> tuple[dict[str, Any] | None, str | None, str | None]:
        try:
            raw = self.probe.sample(uri, node, self.slow_op_seconds)
        except Exception as exc:
            return None, _short_error(exc), None
        log_err = None
        if self.capture_slow_queries and is_data_bearing(node):
            try:
                self._capture_slow_log(dep_id, uri, node)
            except Exception as exc:
                log_err = _short_error(exc)
        return raw, None, log_err

    def _capture_slow_log(self, dep_id: int, uri: str, node: Node) -> None:
        key = (dep_id, node.host)
        if key not in self._log_since:
            row = self.db.one("SELECT MAX(ts) t FROM slow_ops WHERE deployment_id = ? AND host = ?",
                              (dep_id, node.host))
            self._log_since[key] = (row or {}).get("t") or 0.0
        entries = self.probe.slow_log(uri, node, self._log_since[key])
        if not entries:
            return
        self._log_since[key] = max(e["_ts"] for e in entries)
        rows = []
        for attr in entries:
            e = insights.slow_entry(attr)
            if e:
                rows.append((dep_id, node.host, e["ts"], e["ns"], e["op"], e["duration_ms"], e["plan"],
                             e["keys_examined"], e["docs_examined"], e["nreturned"], e["has_sort"],
                             e["shape_key"], e["shape"], e["app"]))
        if rows:
            self.db.executemany(
                "INSERT INTO slow_ops (deployment_id, host, ts, ns, op, duration_ms, plan, keys_examined, "
                "docs_examined, nreturned, has_sort, shape_key, shape, app) VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)

    def _process_values(self, dep_id: int, host: str, raw: dict[str, Any]) -> dict[str, Any]:
        ss = raw.get("serverStatus")
        values: dict[str, Any] = {}
        if not ss:
            return values
        host_info = raw.get("hostInfo") or {}
        values.update(M.gauges(ss, host_info))
        cur_counters = M.counters(ss)
        key = (dep_id, host)
        prev = self._prev.get(key)
        mono = time.monotonic()
        uptime = values.get("uptime_s")
        if prev:
            values.update(M.rates((prev[0], prev[1]), (mono, cur_counters), prev[2], uptime,
                                  host_info.get("cores")))
        self._prev[key] = (mono, cur_counters, uptime)
        disk = M.disk_used_pct(raw.get("dbStats"))
        if disk is not None:
            values["disk_used_pct"] = disk
        values.update(M.oplog_values(raw))
        values.update(M.storage_values(raw.get("storage")))
        for k in ("slow_ops", "longest_op_s"):
            if raw.get(k) is not None:
                values[k] = raw[k]
        return values

    def _upsert_process(self, dep_id: int, node: Node, raw: dict[str, Any] | None,
                        err: str | None, ts: int) -> int:
        existing = self.db.one("SELECT id FROM processes WHERE deployment_id = ? AND host = ?",
                               (dep_id, node.host))
        info: dict[str, Any] = {}
        version = None
        if raw:
            ss = raw.get("serverStatus") or {}
            version = ss.get("version") or (raw.get("hello") or {}).get("version")
            info = {
                "uptime": ss.get("uptime"),
                "engine": (ss.get("storageEngine") or {}).get("name"),
                "process": ss.get("process"),
                "pid": ss.get("pid"),
                "fcv": raw.get("fcv"),
                "cores": (raw.get("hostInfo") or {}).get("cores"),
                "host_mem_mb": (raw.get("hostInfo") or {}).get("mem_mb"),
                "slow_ops_sample": raw.get("slow_ops_sample", []),
            }
        if node.extra:
            info["member"] = node.extra   # hidden, priority, votes, delay, tags from the replica set config
        values = {
            "role": node.role, "shard": node.shard, "rs_name": node.rs_name,
            "up": 1 if raw is not None else 0, "last_error": err, "info": json.dumps(info, default=str),
        }
        if raw is not None:
            values["last_seen"] = ts
        if version:
            values["version"] = version
        if existing:
            self.db.update("processes", existing["id"], values)
            return existing["id"]
        try:
            return self.db.insert("processes", {"deployment_id": dep_id, "host": node.host, **values})
        except sqlite3.IntegrityError:   # inserted meanwhile by a concurrent poll of the same deployment
            row = self.db.one("SELECT id FROM processes WHERE deployment_id = ? AND host = ?", (dep_id, node.host))
            self.db.update("processes", row["id"], values)
            return row["id"]

    def forget_deployment(self, dep_id: int) -> None:
        for d in (self._prev, self._log_since, self._last_primary):
            for key in [k for k in d if k[0] == dep_id]:
                d.pop(key, None)


class HostCollector:
    """Hardware metrics of registered servers, read from /proc over SSH (no sudo needed)."""

    def __init__(self, db: Database, box: SecretBox, runner, concurrency: int = 8):
        self.db = db
        self.box = box
        self.runner = runner
        self.concurrency = concurrency
        self._prev: dict[int, tuple[float, dict[str, Any]]] = {}

    async def poll_all(self) -> None:
        servers = self.db.query("SELECT * FROM servers WHERE ssh_key_enc IS NOT NULL")
        sem = asyncio.Semaphore(self.concurrency)

        async def one(srv):
            async with sem:
                await self.poll_server(srv)

        await asyncio.gather(*(one(s) for s in servers), return_exceptions=True)

    async def poll_server(self, srv: dict[str, Any]) -> dict[str, float]:
        from .automation.ssh import target_for

        ts = now()
        try:
            res = await self.runner.run(target_for(srv, self.box), hostmetrics.SCRIPT, timeout=30, sudo=False)
            if not res.ok:
                raise RuntimeError((res.stderr or res.stdout).strip()[-200:] or f"exit {res.rc}")
            snap = hostmetrics.parse(res.stdout)
        except Exception as exc:
            self._prev.pop(srv["id"], None)
            self.db.store_metrics([(f"srv:{srv['id']}", ts, {"host_up": 0})])
            log.info("Host metrics for %s failed: %s", srv["name"], _short_error(exc))
            return {"host_up": 0}
        mono = time.monotonic()
        values = hostmetrics.compute(self._prev.get(srv["id"]), (mono, snap))
        self._prev[srv["id"]] = (mono, snap)
        self.db.store_metrics([(f"srv:{srv['id']}", ts, values)])
        return values


def _short_error(exc: Exception) -> str:
    text = str(exc) or exc.__class__.__name__
    return text.split(", Timeout:")[0][:300]
