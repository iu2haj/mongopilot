"""Demo mode: a small simulated world of servers and MongoDB clusters.

Implements the same interfaces as PyMongoProbe, SshRunner and MongoOps, so the UI and the automation
can be tried without real servers. Also used by the tests.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import itertools
import json
import math
import random
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from . import hostmetrics
from .automation import templates as T
from .automation.ssh import CommandResult, SshTarget
from .probe import Node, Topology

OPS = ("insert", "query", "update", "delete", "getmore", "command")


def hosts_from_uri(uri: str) -> list[str]:
    rest = uri.split("://", 1)[1]
    rest = rest.split("/", 1)[0].split("?", 1)[0]
    if "@" in rest:
        rest = rest.rsplit("@", 1)[1]
    return [h if ":" in h else f"{h}:27017" for h in rest.split(",") if h]


@dataclass
class SimMember:
    host: str
    rs: str | None
    version: str
    priority: float = 1.0
    arbiter: bool = False
    hidden: bool = False
    state: str = "SECONDARY"
    running: bool = True
    restart_until: float = 0.0
    started_at: float = field(default_factory=time.time)
    lag_s: float = 0.0
    load: float = 1.0
    disk_pct: float = 40.0
    seed: int = field(default_factory=lambda: random.randint(0, 10_000))
    counters: dict[str, float] = field(default_factory=dict)
    last_tick: float = field(default_factory=time.time)
    pending_version: str | None = None
    delay: int = 0
    votes: int = 1
    params: dict[str, Any] = field(default_factory=dict)

    def settings(self) -> dict[str, Any]:
        return {"hidden": self.hidden, "arbiter_only": self.arbiter, "priority": self.priority,
                "votes": self.votes, "delay": self.delay, "tags": {}}

    @property
    def reachable(self) -> bool:
        return self.running and time.time() >= self.restart_until


@dataclass
class SimCluster:
    name: str
    kind: str                                  # replicaset | sharded
    sets: dict[str, list[SimMember]]           # set name -> members
    shard_of: dict[str, str] = field(default_factory=dict)   # set name -> shard name
    mongos: list[SimMember] = field(default_factory=list)
    fcv: str = "7.0"
    users: dict[str, str] = field(default_factory=dict)
    roles: set[str] = field(default_factory=set)
    balancer: bool = True

    def all_members(self) -> list[SimMember]:
        return [m for ms in self.sets.values() for m in ms] + self.mongos


# ---------------------------------------------------------------------------
# Simulated workloads: slow query templates and index inventories per cluster
# ---------------------------------------------------------------------------

def _q(ns, command, plan, keys, docs, ret, ms, every_s, sort_stage=False, typ="command", app="shop-api"):
    return {"ns": ns, "command": command, "plan": plan, "keys": keys, "docs": docs, "ret": ret, "ms": ms,
            "every": every_s, "sort": sort_stage, "type": typ, "app": app}


DATE = {"$date": "2026-10-01T00:00:00Z"}
OID = {"$oid": "652f1c0e9b1e8a3d4c5b6a79"}

WORKLOADS: dict[str, list[dict[str, Any]]] = {
    "orders-prod": [
        _q("shop.orders", {"find": "orders", "filter": {"customerId": OID, "status": "open"},
                           "sort": {"createdAt": -1}, "limit": 20, "$db": "shop"},
           "IXSCAN { customerId: 1 }", 1800, 1800, 20, 240, 45, sort_stage=True),
        _q("shop.orders", {"aggregate": "orders", "pipeline": [
            {"$match": {"status": "shipped", "shippedAt": {"$gte": DATE}}},
            {"$group": {"_id": "$carrier", "n": {"$sum": 1}}}], "$db": "shop"},
           "COLLSCAN", 0, 410000, 6, 1300, 300, app="reporting"),
        _q("shop.orders", {"q": {"status": "pending", "createdAt": {"$lt": DATE}}, "u": {"$set": {"status": "x"}},
                           "multi": True},
           "COLLSCAN", 0, 395000, 80, 900, 600, typ="update", app="cleanup-job"),
        _q("shop.order_items", {"find": "order_items", "filter": {"orderId": OID}, "$db": "shop"},
           "IXSCAN { orderId: 1, sku: 1 }", 6, 6, 6, 130, 90),
        _q("shop.products", {"find": "products", "filter": {"category": "laptops", "price": {"$lte": 1500}},
                             "sort": {"rating": -1}, "limit": 40, "$db": "shop"},
           "IXSCAN { category: 1 }", 5200, 5200, 40, 310, 60, sort_stage=True),
        _q("shop.customers", {"find": "customers", "filter": {"address.city": "Milan",
                                                              "lastName": {"$regex": "^Ros"}}, "$db": "shop"},
           "COLLSCAN", 0, 82000, 14, 420, 180),
        _q("shop.customers", {"find": "customers", "filter": {"$or": [{"email": "a@b.it"}, {"phone": "+39 02"}]},
                              "$db": "shop"},
           "COLLSCAN", 0, 82000, 1, 380, 240),
        _q("shop.sessions", {"count": "sessions", "query": {"userId": OID}, "$db": "shop"},
           "COUNT_SCAN { userId: 1 }", 3, 0, 1, 110, 400),
    ],
    "crm": [
        _q("crm.contacts", {"find": "contacts", "filter": {"owner": "u-118", "stage": {"$in": ["lead", "won"]}},
                            "sort": {"updatedAt": -1}, "$db": "crm"},
           "COLLSCAN", 0, 64000, 50, 520, 120, app="crm-web"),
    ],
    "catalog": [
        _q("catalog.items", {"find": "items", "filter": {"brand": "acme", "stock": {"$gt": 0}}, "$db": "catalog"},
           "SHARD_MERGE COLLSCAN", 0, 210000, 300, 700, 150, app="catalog-api"),
    ],
}

MB = 1024 * 1024


def _ix(name, key, ops, size_mb, **opts):
    return {"name": name, "key": list(key.items()), "ops": ops, "size": int(size_mb * MB), **opts}


INVENTORIES: dict[str, dict[str, dict[str, Any]]] = {
    "orders-prod": {
        "shop.orders": {"count": 2_400_000, "size": 3_100 * MB, "indexes": [
            _ix("_id_", {"_id": 1}, 912_000, 96),
            _ix("customerId_1", {"customerId": 1}, 640_000, 61),
            _ix("customerId_1_createdAt_-1", {"customerId": 1, "createdAt": -1}, 52_000, 88),
            _ix("legacyRef_1", {"legacyRef": 1}, 0, 182),
            _ix("couponCode_1", {"couponCode": 1}, 0, 9, sparse=True),
        ]},
        "shop.order_items": {"count": 9_800_000, "size": 2_200 * MB, "indexes": [
            _ix("_id_", {"_id": 1}, 120_000, 240),
            _ix("orderId_1", {"orderId": 1}, 3_100, 205),
            _ix("orderId_1_sku_1", {"orderId": 1, "sku": 1}, 1_430_000, 310),
            _ix("sku_1", {"sku": 1}, 14, 190),
        ]},
        "shop.products": {"count": 62_000, "size": 140 * MB, "indexes": [
            _ix("_id_", {"_id": 1}, 300_000, 2),
            _ix("category_1", {"category": 1}, 410_000, 1.4),
            _ix("name_text", {"_fts": "text", "_ftsx": 1}, 9_200, 22),
        ]},
        "shop.customers": {"count": 82_000, "size": 95 * MB, "indexes": [
            _ix("_id_", {"_id": 1}, 98_000, 3),
            _ix("email_1", {"email": 1}, 44_000, 4, unique=True),
            _ix("taxCode_1", {"taxCode": 1}, 0, 4, unique=True),
            _ix("lastName_1", {"lastName": 1}, 1_200, 3),
        ]},
        "shop.sessions": {"count": 1_200_000, "size": 410 * MB, "indexes": [
            _ix("_id_", {"_id": 1}, 1_000, 38),
            _ix("userId_1", {"userId": 1}, 802_000, 31),
            _ix("createdAt_1", {"createdAt": 1}, 0, 30, expireAfterSeconds=86400),
        ]},
    },
    "crm": {
        "crm.contacts": {"count": 64_000, "size": 120 * MB, "indexes": [
            _ix("_id_", {"_id": 1}, 50_000, 2),
            _ix("owner_1", {"owner": 1}, 0, 1.6),
            _ix("owner_1_updatedAt_-1", {"owner": 1, "updatedAt": -1}, 0, 2.1),
        ]},
    },
    "catalog": {
        "catalog.items": {"count": 4_300_000, "size": 5_900 * MB, "indexes": [
            _ix("_id_", {"_id": 1}, 2_000_000, 140),
            _ix("sku_hashed", {"sku": "hashed"}, 1_800_000, 120, _shardKey=True),
        ]},
    },
}


class DemoWorld:
    def __init__(self, speed: float = 1.0):
        self.speed = speed
        self.clusters: list[SimCluster] = []
        self.tunnels: dict[int, str] = {}
        self.lock = threading.RLock()
        self._ports = itertools.count(40000)
        self.started: dict[str, str] = {}   # host -> version started before rs.initiate

    # -- lookup ------------------------------------------------------------
    def find(self, host: str) -> tuple[SimCluster, SimMember] | None:
        with self.lock:
            for c in self.clusters:
                for m in c.all_members():
                    if m.host == host:
                        return c, m
        return None

    def resolve(self, uri: str) -> list[str]:
        out = []
        for h in hosts_from_uri(uri):
            port = h.rsplit(":", 1)[1]
            if h.startswith("127.0.0.1:") and port.isdigit() and int(port) in self.tunnels:
                out.append(self.tunnels[int(port)])
            else:
                out.append(h)
        return out

    def cluster_for(self, uri: str) -> SimCluster:
        for h in self.resolve(uri):
            found = self.find(h)
            if found:
                return found[0]
        raise ConnectionError("No servers found yet, Timeout: 4.0s (simulated)")

    # -- dynamics ----------------------------------------------------------
    def elect(self, cluster: SimCluster) -> None:
        """Elect a primary in every set that has no reachable primary."""
        for members in cluster.sets.values():
            if any(m.state == "PRIMARY" and m.reachable for m in members):
                continue
            for m in members:
                if m.state == "PRIMARY":
                    m.state = "SECONDARY"
            voters = [m for m in members if m.reachable]
            if len(voters) * 2 <= len(members):
                continue  # no majority
            electable = sorted((m for m in voters if not m.arbiter and m.priority > 0),
                               key=lambda m: (-m.priority, m.host))
            if electable:
                electable[0].state = "PRIMARY"

    def tick(self, m: SimMember) -> None:
        now = time.time()
        dt_s = max(0.0, now - m.last_tick)
        m.last_tick = now
        c = m.counters
        rnd = random.Random(m.seed + int(now // 30))
        wave = 1 + 0.45 * math.sin(now / 900 + m.seed)
        primary = m.state in ("PRIMARY", "MONGOS")
        base = 120 * m.load * wave * (1.0 if primary else 0.35)

        def add(key: str, rate: float) -> None:
            c[key] = c.get(key, 0.0) + max(0.0, rate) * dt_s

        for op, share in (("insert", .12), ("query", .45), ("update", .18), ("delete", .03),
                          ("getmore", .07), ("command", .35)):
            add(op, base * share * rnd.uniform(.85, 1.15))
        reads, writes = base * .52, base * .33
        add("lat_reads_ops", reads)
        add("lat_reads", reads * rnd.uniform(2_000, 9_000) * m.load)     # microseconds
        add("lat_writes_ops", writes)
        add("lat_writes", writes * rnd.uniform(1_500, 6_000) * m.load)
        add("lat_commands_ops", base * .35)
        add("lat_commands", base * .35 * rnd.uniform(400, 1500))
        add("lat_transactions_ops", base * .01)
        add("lat_transactions", base * .01 * rnd.uniform(8_000, 20_000))
        returned = base * .45 * rnd.uniform(8, 14)
        targeting = 2.5 + (60 if m.seed % 5 == 0 and rnd.random() < .2 else 0)
        add("doc_returned", returned)
        add("scanned_objects", returned * targeting)
        add("scanned_keys", returned * 1.8)
        add("doc_inserted", base * .12)
        add("doc_updated", base * .18)
        add("doc_deleted", base * .03)
        add("scan_and_order", base * .002 * (8 if rnd.random() < .15 else 1))
        add("write_conflicts", base * .0005)
        add("cursor_timed_out", .002)
        add("conn_created", .4)
        add("bytesIn", base * 900)
        add("bytesOut", base * 4200)
        add("numRequests", base * 1.1)
        add("pf", rnd.uniform(0, 0.4))
        add("asserts_warning", .02 if rnd.random() < .1 else 0)
        add("asserts_user", base * .001)
        add("cache_read", base * 9_000 * m.load)
        add("cache_written", base * 4_000 * m.load)
        add("evicted_mod", base * .05)
        add("evicted_unmod", base * .2)
        add("app_evict", base * .001)
        add("txn_committed", base * .01)
        add("txn_aborted", base * .0003)
        add("ttl_deleted", 1.5 if primary else 0)
        add("cpu_user_us", 1e6 * 4 * min(.95, .18 * m.load * wave + rnd.uniform(0, .05)))
        add("cpu_sys_us", 1e6 * 4 * .04)

    # -- construction ------------------------------------------------------
    def add_replicaset(self, name: str, rs: str, hosts: list[str], version: str, **kw) -> SimCluster:
        members = [SimMember(h, rs, version) for h in hosts]
        for m in members:
            m.load = kw.get("load", 1.0)
            m.disk_pct = kw.get("disk_pct", 42.0) + random.uniform(-3, 3)
        c = SimCluster(name, "replicaset", {rs: members}, fcv=T.major(version))
        with self.lock:
            self.clusters.append(c)
            self.elect(c)
        return c

    def add_sharded(self, name: str, shards: dict[str, list[str]], config: list[str], mongos: list[str],
                    version: str) -> SimCluster:
        sets = {f"{s}-rs": [SimMember(h, f"{s}-rs", version) for h in hosts] for s, hosts in shards.items()}
        sets["csrs"] = [SimMember(h, "csrs", version) for h in config]
        c = SimCluster(name, "sharded", sets, {f"{s}-rs": s for s in shards} | {"csrs": "config"},
                       [SimMember(h, None, version, state="MONGOS") for h in mongos], fcv=T.major(version))
        for m in sets["csrs"]:
            m.load = 0.1
        with self.lock:
            self.clusters.append(c)
            self.elect(c)
        return c

    # -- MongoDB-style documents -------------------------------------------
    def rs_status_doc(self, cluster: SimCluster, rs: str) -> dict[str, Any]:
        now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
        members = []
        for i, m in enumerate(cluster.sets[rs]):
            if not m.running:
                state, health = "(not reachable/healthy)", 0
            elif not m.reachable:
                state, health = "STARTUP2", 1
            else:
                state, health = ("ARBITER" if m.arbiter else m.state), 1
            lag = m.lag_s if state == "SECONDARY" else 0
            members.append({"_id": i, "name": m.host, "stateStr": state, "health": health,
                            "optimeDate": now - dt.timedelta(seconds=lag),
                            "uptime": int(time.time() - m.started_at) if m.reachable else 0})
        return {"set": rs, "members": members, "ok": 1}

    def server_status(self, m: SimMember) -> dict[str, Any]:
        self.tick(m)
        rnd = random.Random(m.seed + int(time.time() // 30))
        c = m.counters
        g = lambda k: int(c.get(k, 0))  # noqa: E731
        conns = int(40 * m.load + rnd.uniform(0, 25))
        max_cache = 8 * 1024 ** 3
        return {
            "version": m.version, "process": "mongos" if m.state == "MONGOS" else "mongod",
            "pid": 1000 + m.seed, "uptime": time.time() - m.started_at,
            "opcounters": {op: g(op) for op in OPS},
            "opLatencies": {k: {"latency": g(f"lat_{k}"), "ops": g(f"lat_{k}_ops")}
                            for k in ("reads", "writes", "commands", "transactions")},
            "network": {"bytesIn": g("bytesIn"), "bytesOut": g("bytesOut"), "numRequests": g("numRequests")},
            "extra_info": {"page_faults": g("pf"), "user_time_us": g("cpu_user_us"),
                           "system_time_us": g("cpu_sys_us")},
            "asserts": {"regular": 0, "warning": g("asserts_warning"), "user": g("asserts_user")},
            "connections": {"current": conns, "available": 51200 - conns, "active": max(1, conns // 6),
                            "totalCreated": 500 + g("conn_created")},
            "metrics": {
                "document": {"returned": g("doc_returned"), "inserted": g("doc_inserted"),
                             "updated": g("doc_updated"), "deleted": g("doc_deleted")},
                "queryExecutor": {"scanned": g("scanned_keys"), "scannedObjects": g("scanned_objects")},
                "operation": {"scanAndOrder": g("scan_and_order"), "writeConflicts": g("write_conflicts")},
                "cursor": {"timedOut": g("cursor_timed_out"),
                           "open": {"total": rnd.randint(3, 30), "noTimeout": rnd.choice([0, 0, 1])}},
                "ttl": {"deletedDocuments": g("ttl_deleted")},
            },
            "mem": {"resident": int(2600 * m.load + rnd.uniform(0, 200)), "virtual": int(5200 * m.load + 800)},
            "storageEngine": {"name": "wiredTiger"},
            "wiredTiger": {
                "cache": {"maximum bytes configured": max_cache,
                          "bytes currently in the cache": max_cache * rnd.uniform(.55, .8),
                          "tracked dirty bytes in the cache": max_cache * rnd.uniform(.01, .06),
                          "bytes read into cache": g("cache_read"), "bytes written from cache": g("cache_written"),
                          "modified pages evicted": g("evicted_mod"), "unmodified pages evicted": g("evicted_unmod"),
                          "pages evicted by application threads": g("app_evict")},
                "transaction": {"transaction checkpoint most recent time (msecs)": rnd.randint(80, 900)},
            },
            "globalLock": {"currentQueue": {"readers": rnd.choice([0, 0, 0, 1]), "writers": rnd.choice([0, 0, 1]),
                                            "total": rnd.choice([0, 0, 1, 2])},
                           "activeClients": {"readers": rnd.randint(0, 4), "writers": rnd.randint(0, 3)}},
            "queues": {"execution": {"read": {"available": rnd.randint(110, 128)},
                                     "write": {"available": rnd.randint(118, 128)}}},
            "transactions": {"currentOpen": rnd.randint(0, 4), "currentActive": rnd.randint(0, 2),
                             "totalCommitted": g("txn_committed"), "totalAborted": g("txn_aborted")},
            "flowControl": {"isLagged": False},
        }


class DemoProbe:
    def __init__(self, world: DemoWorld):
        self.world = world

    def discover(self, uri: str) -> Topology:
        w = self.world
        with w.lock:
            c = w.cluster_for(uri)
            w.elect(c)
            for ms in c.sets.values():   # lag that fluctuates, with occasional spikes
                for m in ms:
                    if m.state == "SECONDARY":
                        r = random.Random(m.seed + int(time.time() // 60))
                        m.lag_s = r.choice([0, 0, 1, 1, 2, 3]) + (25 if m.seed % 7 == 0 and r.random() < .15 else 0)
                        if m.delay:
                            m.lag_s += m.delay
            status = {rs: w.rs_status_doc(c, rs) for rs in c.sets}
            if c.kind == "replicaset":
                rs = next(iter(c.sets))
                nodes = [Node(x["name"], x["stateStr"], rs_name=rs, extra=m.settings())
                         for x, m in zip(status[rs]["members"], c.sets[rs])]
                return Topology("replicaset", nodes, status)
            nodes = [Node(m.host, "MONGOS") for m in c.mongos]
            for rs, doc in status.items():
                nodes += [Node(x["name"], x["stateStr"], shard=c.shard_of[rs], rs_name=rs, extra=m.settings())
                          for x, m in zip(doc["members"], c.sets[rs])]
            return Topology("sharded", nodes, status,
                            {"shards": [s for s in c.shard_of.values() if s != "config"],
                             "balancer_enabled": 1 if c.balancer else 0, "balancer_running": 0,
                             "jumbo_chunks": 2 if c.name == "catalog" else 0})

    def sample(self, uri: str, node: Node, slow_op_seconds: int) -> dict[str, Any]:
        found = self.world.find(node.host)
        if not found or not found[1].reachable:
            raise ConnectionError(f"{node.host}: [Errno 111] Connection refused (simulated)")
        c, m = found
        with self.world.lock:
            ss = self.world.server_status(m)
        if m.arbiter:
            return {"hello": {"ok": 1}}
        out: dict[str, Any] = {"hello": {"ok": 1}, "serverStatus": ss,
                               "hostInfo": {"cores": 4, "mem_mb": 16_000, "os": "Ubuntu"}}
        r = random.Random(m.seed + int(time.time() // 120))
        slow = 1 if r.random() < .08 else 0
        out["slow_ops"] = slow
        out["longest_op_s"] = round(r.uniform(6, 420), 1) if slow else 0
        out["slow_ops_sample"] = [{"opid": str(r.randint(1, 99999)), "ns": "shop.orders", "op": "query",
                                   "secs": out["longest_op_s"], "client": "10.0.4.17:51234",
                                   "desc": "conn4821"}] if slow else []
        if m.state != "MONGOS":
            out["dbStats"] = {"fsUsedSize": m.disk_pct * 1e9, "fsTotalSize": 100e9}
            window = round(70 + 10 * math.sin(time.time() / 5000 + m.seed), 1)
            out["oplog_window_h"] = window
            out["oplog_used_mb"] = window * 0.9 * 1024 * m.load * .2
            out["oplog_max_mb"] = 51_200
            out["fcv"] = c.fcv
            if m.state == "PRIMARY":
                inv = INVENTORIES.get(c.name, {})
                out["storage"] = {"data_bytes": sum(x["size"] for x in inv.values()) or 2e9,
                                  "storage_bytes": (sum(x["size"] for x in inv.values()) or 2e9) * .45,
                                  "index_bytes": sum(i["size"] for x in inv.values() for i in x["indexes"]) or 3e8,
                                  "objects": sum(x["count"] for x in inv.values()), "databases": 1,
                                  "collections": len(inv), "indexes": sum(len(x["indexes"]) for x in inv.values())}
        return out

    def slow_log(self, uri: str, node: Node, since: float) -> list[dict[str, Any]]:
        found = self.world.find(node.host)
        if not found:
            raise ConnectionError("unreachable (simulated)")
        c, m = found
        if m.state != "PRIMARY" and not (m.state == "SECONDARY" and m.seed % 3 == 0):
            return []
        now = time.time()
        start = max(since, now - 6 * 3600)
        out: list[dict[str, Any]] = []
        for i, q in enumerate(WORKLOADS.get(c.name, [])):
            every = q["every"] * (1 if m.state == "PRIMARY" else 4)
            t = math.floor(start / every) * every + (i * 7) % every
            while t <= now:
                if t > start:
                    r = random.Random(f"{c.name}{m.host}{i}{int(t)}")
                    f = r.uniform(.6, 1.8)
                    out.append({"_ts": t, "type": q["type"], "ns": q["ns"], "command": q["command"],
                                "planSummary": q["plan"], "keysExamined": int(q["keys"] * r.uniform(.8, 1.2)),
                                "docsExamined": int(q["docs"] * r.uniform(.9, 1.1)),
                                "nreturned": max(0, int(q["ret"] * r.uniform(.7, 1.3))),
                                "hasSortStage": q["sort"], "durationMillis": int(q["ms"] * f),
                                "appName": q["app"]})
                t += every * random.Random(f"{i}{int(t)}").uniform(.7, 1.3)
        return out

    def index_inventory(self, uri: str, topology: Topology, max_collections: int = 300) -> dict[str, Any]:
        c = self.world.cluster_for(uri)
        now = time.time()
        colls: dict[str, Any] = {}
        for ns, spec in INVENTORIES.get(c.name, {}).items():
            idx, usage, sizes = [], {}, {}
            for ix in spec["indexes"]:
                d = {k: v for k, v in ix.items() if k not in ("ops", "size")}
                idx.append(d)
                usage[ix["name"]] = {"ops": ix["ops"], "since": now - 23 * 86400, "hosts": ["primary", "secondary"]}
                sizes[ix["name"]] = ix["size"]
            colls[ns] = {"indexes": idx, "usage": usage, "sizes": sizes, "count": spec["count"], "size": spec["size"]}
        return {"collections": colls, "warnings": []}

    def get_parameters(self, uri: str, host: str) -> dict[str, dict[str, Any]]:
        found = self.world.find(host)
        if not found or not found[1].reachable:
            raise ConnectionError(f"{host}: [Errno 111] Connection refused (simulated)")
        c, m = found
        out = {}
        for name, (value, runtime, startup) in DEMO_PARAMETERS.items():
            if m.state == "MONGOS" and name in MONGOD_ONLY_PARAMETERS:
                continue
            out[name] = {"value": m.params.get(name, value), "runtime": runtime, "startup": startup}
        out["featureCompatibilityVersion"] = {"value": {"version": c.fcv}, "runtime": False, "startup": False}
        return out

    def forget(self, uri: str) -> None:
        pass

    def close(self) -> None:
        pass


DEMO_PARAMETERS: dict[str, tuple[Any, bool, bool]] = {
    "allowDiskUseByDefault": (True, True, True),
    "authenticationMechanisms": (["SCRAM-SHA-1", "SCRAM-SHA-256"], False, True),
    "connPoolMaxConnsPerHost": (200, False, True),
    "cursorTimeoutMillis": (600000, True, True),
    "diagnosticDataCollectionEnabled": (True, True, True),
    "diagnosticDataCollectionPeriodMillis": (1000, True, True),
    "enableFlowControl": (True, True, True),
    "enableLocalhostAuthBypass": (True, False, True),
    "flowControlTargetLagSeconds": (10, True, True),
    "honorSystemUmask": (False, False, True),
    "initialSyncMethod": ("logical", False, True),
    "internalQueryFrameworkControl": ("trySbeRestricted", True, True),
    "internalQueryMaxBlockingSortMemoryUsageBytes": (104857600, True, True),
    "journalCommitInterval": (100, True, True),
    "logComponentVerbosity": ({"verbosity": 0, "command": {"verbosity": -1}}, True, True),
    "logLevel": (0, True, True),
    "maxIndexBuildMemoryUsageMegabytes": (200, True, True),
    "maxNumActiveUserIndexBuilds": (3, True, True),
    "maxTransactionLockRequestTimeoutMillis": (5, True, True),
    "notablescan": (False, True, True),
    "periodicNoopIntervalSecs": (10, False, True),
    "quiet": (False, True, True),
    "replWriterThreadCount": (16, False, True),
    "scramIterationCount": (10000, True, True),
    "scramSHA256IterationCount": (15000, True, True),
    "storageEngineConcurrencyAdjustmentAlgorithm": ("throughputProbing", False, True),
    "syncdelay": (60, True, True),
    "tlsMode": ("disabled", True, False),
    "transactionLifetimeLimitSeconds": (60, True, True),
    "ttlMonitorEnabled": (True, True, True),
    "ttlMonitorSleepSecs": (60, False, True),
    "watchdogPeriodSeconds": (-1, False, True),
    "wiredTigerConcurrentReadTransactions": (128, True, True),
    "wiredTigerConcurrentWriteTransactions": (128, True, True),
}
MONGOD_ONLY_PARAMETERS = {"enableFlowControl", "flowControlTargetLagSeconds", "initialSyncMethod", "journalCommitInterval",
                          "notablescan", "periodicNoopIntervalSecs", "replWriterThreadCount", "syncdelay",
                          "ttlMonitorEnabled", "ttlMonitorSleepSecs", "wiredTigerConcurrentReadTransactions",
                          "wiredTigerConcurrentWriteTransactions", "storageEngineConcurrencyAdjustmentAlgorithm"}


class DemoOps:
    """Simulates the administrative operations used by automation."""

    def __init__(self, world: DemoWorld):
        self.world = world

    def _set(self, uri: str) -> tuple[SimCluster, str, list[SimMember]]:
        """The replica set (or the mongos tier) a URI points to."""
        hosts = self.world.resolve(uri)
        for c in self.world.clusters:
            for rs, ms in c.sets.items():
                if any(m.host in hosts for m in ms):
                    return c, rs, ms
            if any(m.host in hosts for m in c.mongos):
                return c, "", c.mongos
        raise ConnectionError("No servers found yet, Timeout: 8.0s (simulated)")

    def hello(self, uri: str) -> dict[str, Any]:
        c, rs, ms = self._set(uri)
        with self.world.lock:
            self.world.elect(c)
            prim = next((m.host for m in ms if m.state == "PRIMARY"), None)
        return {"primary": prim, "isWritablePrimary": prim in self.world.resolve(uri)}

    def initiate(self, uri: str, config: dict[str, Any]) -> dict[str, Any]:
        host = self.world.resolve(uri)[0]
        with self.world.lock:
            if self.world.find(host):
                raise RuntimeError("already initialized")
            members = [SimMember(m["host"], config["_id"], self.world.started.get(m["host"], "7.0.14"),
                                 priority=m.get("priority", 1), arbiter=m.get("arbiterOnly", False),
                                 hidden=m.get("hidden", False), votes=m.get("votes", 1),
                                 delay=int(m.get("secondaryDelaySecs", m.get("slaveDelay", 0))),
                                 state="ARBITER" if m.get("arbiterOnly") else "SECONDARY")
                       for m in config["members"]]
            rs = config["_id"]
            if config.get("configsvr"):
                c = SimCluster(rs, "sharded", {rs: members}, {rs: "config"}, fcv=T.major(members[0].version))
            else:
                c = SimCluster(rs, "replicaset", {rs: members}, fcv=T.major(members[0].version))
            self.world.clusters.append(c)
            self.world.elect(c)
        return {"ok": 1}

    def create_user(self, uri: str, user: str, password: str, roles: list[Any]) -> None:
        self.world.cluster_for(uri).users[user] = password

    def create_role(self, uri: str, role: str, privileges: list[dict[str, Any]]) -> None:
        self.world.cluster_for(uri).roles.add(role)

    def rs_status(self, uri: str) -> dict[str, Any]:
        c, rs, _ = self._set(uri)
        with self.world.lock:
            self.world.elect(c)
            return self.world.rs_status_doc(c, rs)

    def step_down(self, uri: str, seconds: int = 60, catch_up: int = 30) -> None:
        c, rs, ms = self._set(uri)
        with self.world.lock:
            old = next((m for m in ms if m.state == "PRIMARY"), None)
            cand = sorted((m for m in ms if m is not old and m.reachable and not m.arbiter and m.priority > 0),
                          key=lambda m: -m.priority)
            if old and cand:
                old.state, cand[0].state = "SECONDARY", "PRIMARY"

    def get_fcv(self, uri: str) -> str:
        return self.world.cluster_for(uri).fcv

    def set_fcv(self, uri: str, version: str) -> None:
        self.world.cluster_for(uri).fcv = version

    def set_parameter(self, uri: str, name: str, value: Any) -> None:
        host = self.world.resolve(uri)[0]
        found = self.world.find(host)
        if not found or not found[1].reachable:
            raise ConnectionError(f"{host}: connection refused (simulated)")
        if name in DEMO_PARAMETERS and not DEMO_PARAMETERS[name][1]:
            raise RuntimeError(f"not allowed to change {name} at runtime")
        found[1].params[name] = value

    def add_shard(self, uri: str, conn: str, name: str) -> None:
        c, _, _ = self._set(uri)
        rs = conn.split("/", 1)[0]
        with self.world.lock:
            other = next(x for x in self.world.clusters if rs in x.sets and x is not c)
            c.sets[rs] = other.sets[rs]
            c.shard_of[rs] = name
            self.world.clusters.remove(other)

    def list_shards(self, uri: str) -> list[str]:
        c, _, _ = self._set(uri)
        return [s for s in c.shard_of.values() if s != "config"]

    def balancer(self, uri: str, enabled: bool) -> None:
        self._set(uri)[0].balancer = enabled

    def ping(self, uri: str) -> bool:
        host = self.world.resolve(uri)[0]
        found = self.world.find(host)
        if not found or not found[1].reachable:
            raise ConnectionError(f"{host}: connection refused (simulated)")
        return True


class DemoRunner:
    """Simulates SSH: interprets systemctl commands, files written by automation and host metric reads."""

    UNIT_RE = re.compile(r"mongopilot-[a-z0-9-]+-(\d+)\.service")

    def __init__(self, world: DemoWorld, os_id: str = "ubuntu", os_version: str = "22.04"):
        self.world = world
        self.os_id, self.os_version = os_id, os_version
        self.history: list[tuple[str, str, str | None]] = []
        self._host_state: dict[str, dict[str, float]] = {}
        self.files: dict[tuple[str, str], str] = {}
        self.mongos_cfg: dict[str, str] = {}   # mongos host -> config server replica set name

    def _proc_snapshot(self, host: str) -> str:
        now = time.time()
        st = self._host_state.setdefault(host, {"t": now - 30, "cpu": 0, "idle": 0, "iow": 0, "r": 0, "rms": 0,
                                                "w": 0, "wms": 0, "io": 0,
                                                "seed": int((re.sub(r"\D", "", host) or "0")[-1])})
        dt_s = max(0.0, now - st["t"])
        st["t"] = now
        r = random.Random(int(st["seed"]) + int(now // 30))
        busy = min(.95, .25 + .2 * math.sin(now / 1200 + st["seed"]) + r.uniform(0, .1))
        jiffies = 100 * 4 * dt_s
        st["cpu"] += jiffies
        st["iow"] += jiffies * .03
        st["idle"] += jiffies * (1 - busy - .03)
        reads, writes = r.uniform(20, 90) * dt_s, r.uniform(60, 220) * dt_s
        st["r"] += reads
        st["w"] += writes
        st["rms"] += reads * r.uniform(.4, 2.5)
        st["wms"] += writes * r.uniform(.8, 4 + (25 if r.random() < .05 else 0))
        st["io"] += dt_s * 1000 * min(.9, busy * .6)
        user = st["cpu"] - st["idle"] - st["iow"]
        return "\n".join([
            "@@stat", f"cpu  {int(user * .8)} 0 {int(user * .2)} {int(st['idle'])} {int(st['iow'])} 0 0 0 0 0",
            "@@cores", "4",
            "@@load", f"{busy * 4:.2f} {busy * 3.8:.2f} {busy * 3.5:.2f} 2/311 9131",
            "@@mem", "MemTotal:       16303428 kB", f"MemAvailable:   {int(16303428 * (.38 - busy * .1))} kB",
            "SwapTotal:       2097148 kB", "SwapFree:        2097148 kB",
            "@@disk",
            f"   8       0 sda {int(st['r'])} 0 0 {int(st['rms'])} {int(st['w'])} 0 0 {int(st['wms'])} 0 "
            f"{int(st['io'])} 0 0 0 0 0",
            "   8       1 sda1 10 0 0 1 10 0 0 1 0 1 0",
            "@@df", f"/dev/sda1 61255492 {int(61255492 * .46)} {int(61255492 * .54)} 46% /",
            "@@end", ""])

    async def run(self, target: SshTarget, script: str, stdin: str | None = None, timeout: int = 600,
                  sudo: bool = True) -> CommandResult:
        if script.startswith(hostmetrics.MARKER):
            return CommandResult(0, self._proc_snapshot(target.hostname), "")
        self.history.append((target.hostname, script, stdin))
        await asyncio.sleep(0.25 * self.world.speed)
        if script == T.FACTS_SCRIPT:
            return CommandResult(0, f"os_id={self.os_id}\nos_version={self.os_version}\n"
                                    f"os_name=Ubuntu {self.os_version} LTS\narch=x86_64\ncpus=4\nmem_mb=16000\n"
                                    "systemd=yes\ncurl=yes\nwget=no\nthp=madvise\nwhoami=vagrant\n", "")
        if script.strip() == "id -u":
            return CommandResult(0, "0\n", "")
        if stdin is not None:
            mfile = re.search(r"cat > '?([^'\s]+?)'?\.mp-(?:tmp|new)", script)
            if "--outputConfig" in script and "bogusOption" in stdin:
                return CommandResult(3, "", "MongoDB rejected the configuration: Unrecognized option: bogusOption")
            if mfile:
                self.files[(target.hostname, mfile.group(1))] = stdin
            cfgdb = re.search(r"configDB: (\S+?)/(\S+)", stdin)
            port = re.search(r"port: (\d+)", stdin)
            if cfgdb and port:
                self.mongos_cfg[f"{target.hostname}:{port.group(1)}"] = cfgdb.group(1)
        mread = re.fullmatch(r"cat '?([^'\s]+)'?", script.strip())
        if mread:
            content = self.files.get((target.hostname, mread.group(1)))
            return CommandResult(0, content, "") if content is not None else \
                CommandResult(1, "", f"cat: {mread.group(1)}: No such file or directory")
        if "Download:" in script:
            ver = re.search(r"MongoDB ([\d.]+) already installed", script)
            await asyncio.sleep(1.0 * self.world.speed)
            return CommandResult(0, f"Download: (simulated)\ndb version v{ver.group(1) if ver else '?'}\n", "")
        if stdin is not None and "ExecStart=" in stdin:
            unit = self.UNIT_RE.search(script)
            ver = re.search(r"mongodb/([\d.]+)/bin/mongo[ds]", stdin)
            if unit and ver:
                host = f"{target.hostname}:{unit.group(1)}"
                self.world.started[host] = ver.group(1)
                found = self.world.find(host)
                if found:
                    found[1].pending_version = ver.group(1)
            return CommandResult(0, "", "")
        m = re.search(r"systemctl (restart|stop|start) (\S+)", script)
        if m:
            action, unit = m.group(1), m.group(2).strip("'")
            port = self.UNIT_RE.search(unit)
            host = f"{target.hostname}:{port.group(1)}" if port else None
            if host and host in self.mongos_cfg and not self.world.find(host) and action != "stop":
                with self.world.lock:   # a new mongos joins the cluster whose config servers it points to
                    c = next((x for x in self.world.clusters if self.mongos_cfg[host] in x.sets), None)
                    if c:
                        c.mongos.append(SimMember(host, None, self.world.started.get(host, "7.0.14"), state="MONGOS"))
            found = self.world.find(host) if host else None
            if found:
                c, mem = found
                with self.world.lock:
                    if action == "stop":
                        mem.running = False
                    else:
                        if mem.state == "PRIMARY":
                            mem.state = "SECONDARY"
                        mem.running = True
                        mem.restart_until = time.time() + 2.5 * self.world.speed
                        mem.started_at = time.time()
                        if mem.pending_version:
                            mem.version, mem.pending_version = mem.pending_version, None
                    self.world.elect(c)
            return CommandResult(0, "", "")
        return CommandResult(0, "", "")

    def forget_host_key(self, hostname: str, port: int) -> None:
        pass

    @contextlib.asynccontextmanager
    async def tunnel(self, target: SshTarget, remote_port: int) -> AsyncIterator[int]:
        port = next(self.world._ports)
        self.world.tunnels[port] = f"{target.hostname}:{remote_port}"
        try:
            yield port
        finally:
            self.world.tunnels.pop(port, None)


class DemoNotifier:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    def send(self, rule, alert, kind: str) -> None:
        self.sent.append((kind, alert["message"]))

    def deliver(self, ch, rule, alert, kind: str) -> None:
        self.sent.append((kind, alert["message"]))


# ---------------------------------------------------------------------------
# Demo seed data
# ---------------------------------------------------------------------------

DEMO_KEY = ("-----BEGIN OPENSSH PRIVATE KEY-----\n"
            "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAAAAtzc2gtZWQyNTUx\n"
            "-----END OPENSSH PRIVATE KEY-----\n")
ORDERS_HOSTS = [f"192.168.56.1{i}:27017" for i in (1, 2, 3)]


def seed_demo(services) -> None:
    """Populate servers, deployments and history (only when the database is empty)."""
    from .automation.mongo_ops import admin_uri

    db, box, world = services.db, services.box, services.world
    first_run = not db.one("SELECT id FROM servers LIMIT 1")
    if first_run:
        for i in range(1, 7):
            db.insert("servers", {"name": f"mongo{i}", "hostname": f"192.168.56.1{i}", "ssh_port": 22,
                                  "ssh_user": "vagrant", "kind": "vagrant", "mongo_host": f"192.168.56.1{i}",
                                  "ssh_key_enc": box.encrypt(DEMO_KEY), "notes": "Simulated Vagrant VM",
                                  "created_at": int(time.time()),
                                  "facts": json.dumps({"os_name": "Ubuntu 22.04 LTS", "arch": "x86_64", "cpus": "4",
                                                       "mem_mb": "16000", "sudo": "yes", "systemd": "yes",
                                                       "os_id": "ubuntu", "os_version": "22.04"})})

    # Simulated clusters (re-created at every start: they live in memory only)
    orders = world.add_replicaset("orders-prod", "orders", ORDERS_HOSTS, "7.0.14", load=1.6)
    orders.sets["orders"][2].seed = 7  # this secondary gets lag spikes
    crm = world.add_replicaset("crm", "crm", ["crm-a.lab.local:27017", "crm-b.lab.local:27017",
                                             "crm-c.lab.local:27017"], "6.0.16", load=0.5, disk_pct=86.0)
    crm.sets["crm"] += [
        SimMember("crm-d.lab.local:27017", "crm", "6.0.16", priority=0, hidden=True, delay=3600, load=0.2),
        SimMember("crm-e.lab.local:27017", "crm", "6.0.16", priority=0, votes=0, load=0.3),
        SimMember("crm-arb.lab.local:27017", "crm", "6.0.16", priority=0, arbiter=True, state="ARBITER"),
    ]
    world.add_sharded("catalog", {"sh0": [f"cat-sh0-{i}.lab.local:27018" for i in "abc"],
                                  "sh1": [f"cat-sh1-{i}.lab.local:27018" for i in "abc"]},
                      [f"cat-cfg-{i}.lab.local:27019" for i in "abc"],
                      ["cat-mongos-1.lab.local:27017", "cat-mongos-2.lab.local:27017"], "8.0.4")

    if not first_run:
        # after a demo restart, re-create the clusters the user built
        for d in db.query("SELECT name, managed_enc FROM deployments WHERE managed = 1 AND name != 'orders-prod'"):
            m = box.decrypt_json(d["managed_enc"], {})
            if m.get("members"):
                world.add_replicaset(d["name"], m["rs_name"], [x["host"] for x in m["members"]], m["version"])
        return

    servers = {r["name"]: r["id"] for r in db.query("SELECT id, name FROM servers")}
    members = []
    for i, h in enumerate(ORDERS_HOSTS):
        members.append({"_id": i, "server_id": servers[f"mongo{i + 1}"], "port": 27017, "host": h, "priority": 1,
                        "votes": 1, "arbiter": False, "hidden": False, "version": "7.0.14",
                        "paths": T.member_paths("orders-prod", 27017)})
    managed = {"rs_name": "orders", "version": "7.0.14", "dep_slug": "orders-prod", "data_root": T.DEFAULT_DATA_ROOT,
               "bind_ip": "0.0.0.0", "cache_gb": None, "admin_user": "admin", "admin_password": "demo-password-123",
               "members": members}
    now = int(time.time())
    db.insert("deployments", {"name": "orders-prod", "managed": 1, "managed_enc": box.encrypt_json(managed),
                              "tags": json.dumps({"organization": "Acme Retail", "project": "E-commerce",
                                                  "cluster_type": "Production", "license_key": "ACME-PRD-7781-XK",
                                                  "license_type": "At node"}),
                              "uri_enc": box.encrypt(admin_uri("mongopilot-monitor", "x", ORDERS_HOSTS, "orders")),
                              "created_at": now})
    db.insert("deployments", {"name": "crm", "managed": 0, "created_at": now,
                              "tags": json.dumps({"organization": "Acme Retail", "project": "CRM",
                                                  "cluster_type": "Staging", "license_key": "ACME-STG-1203",
                                                  "license_type": "At memory"}),
                              "uri_enc": box.encrypt("mongodb://monitor:x@crm-a.lab.local:27017,"
                                                     "crm-b.lab.local:27017/?replicaSet=crm")})
    db.insert("deployments", {"name": "catalog", "managed": 0, "created_at": now,
                              "tags": json.dumps({"organization": "Globex", "project": "Product catalog",
                                                  "cluster_type": "Development", "license_key": "",
                                                  "license_type": "At node"}),
                              "uri_enc": box.encrypt("mongodb://monitor:x@cat-mongos-1.lab.local:27017/")})
    # First discovery creates the processes and imports simulated slow-query logs; then 6 hours of history
    for d in db.query("SELECT id FROM deployments"):
        services.collector.poll_deployment(d["id"])
    backfill(services, hours=6)


def backfill(services, hours: int = 6, step: int = 60) -> None:
    db = services.db
    now = int(time.time())
    rows = []
    procs = db.query("SELECT id, host, role, deployment_id FROM processes")
    for p in procs:
        found = services.world.find(p["host"])
        if not found:
            continue
        m = found[1]
        primary = p["role"] in ("PRIMARY", "MONGOS")
        for ts in range(now - hours * 3600, now, step):
            r = random.Random(m.seed * 100000 + ts)
            wave = 1 + 0.45 * math.sin(ts / 900 + m.seed)
            base = 120 * m.load * wave * (1.0 if primary else 0.35)
            v: dict[str, Any] = {"up": 1, "member_state_ok": 1}
            if not m.arbiter:
                for op, share in (("insert", .12), ("query", .45), ("update", .18), ("delete", .03),
                                  ("getmore", .07), ("command", .35)):
                    v[f"ops_{op}"] = round(base * share * r.uniform(.85, 1.15), 2)
                v["ops_total"] = round(sum(v[f"ops_{o}"] for o in OPS), 2)
                spike = m.seed % 5 == 0 and r.random() < .2
                v.update({
                    "docs_returned_s": round(base * .45 * r.uniform(8, 14), 1),
                    "docs_inserted_s": round(base * .12, 1), "docs_updated_s": round(base * .18, 1),
                    "docs_deleted_s": round(base * .03, 1), "net_requests_s": round(base * 1.1, 1),
                    "lat_reads_ms": round(r.uniform(2, 9) * m.load, 2), "lat_writes_ms": round(r.uniform(1.5, 6) * m.load, 2),
                    "lat_commands_ms": round(r.uniform(.4, 1.5), 2), "lat_txn_ms": round(r.uniform(8, 20), 2),
                    "query_targeting": round(2.5 + (60 if spike else 0) * r.uniform(.5, 1.5), 1),
                    "query_targeting_keys": round(r.uniform(1.4, 2.2), 2),
                    "scan_and_order_s": round(base * .002 * (8 if r.random() < .15 else 1), 3),
                    "write_conflicts_s": round(base * .0005, 3),
                    "connections_current": int(40 * m.load + r.uniform(0, 25)),
                    "connections_active": int(8 * m.load + r.uniform(0, 4)),
                    "connections_created_s": round(r.uniform(.2, .6), 2),
                    "cursors_open": r.randint(3, 30), "cursors_notimeout": r.choice([0, 0, 1]),
                    "cursors_timedout_s": 0.002,
                    "mem_resident_mb": int(2600 * m.load + r.uniform(0, 200)), "mem_virtual_mb": int(5200 * m.load + 800),
                    "cache_used_pct": round(r.uniform(55, 80), 2), "cache_dirty_pct": round(r.uniform(1, 6), 2),
                    "cache_read_bps": round(base * 9000 * m.load), "cache_written_bps": round(base * 4000 * m.load),
                    "cache_evicted_s": round(base * .25, 2), "cache_app_evictions_s": round(base * .001, 3),
                    "checkpoint_ms": r.randint(80, 900),
                    "cpu_process_pct": round(100 * min(.95, .18 * m.load * wave + r.uniform(0, .05)) + 4, 1),
                    "net_in_bps": round(base * 900, 1), "net_out_bps": round(base * 4200, 1),
                    "page_faults_s": round(r.uniform(0, .4), 3),
                    "queue_readers": r.choice([0, 0, 0, 1]), "queue_writers": r.choice([0, 0, 1]),
                    "queue_total": r.choice([0, 0, 1, 2]),
                    "active_readers": r.randint(0, 4), "active_writers": r.randint(0, 3),
                    "tickets_read_avail": r.randint(110, 128), "tickets_write_avail": r.randint(118, 128),
                    "txn_open": r.randint(0, 4), "txn_active": r.randint(0, 2),
                    "txn_committed_s": round(base * .01, 3), "txn_aborted_s": round(base * .0003, 4),
                    "ttl_deleted_s": 1.5 if primary else 0, "asserts_user_s": round(base * .001, 3),
                    "slow_ops": 0, "longest_op_s": 0,
                })
                v["mem_resident_pct"] = round(100 * v["mem_resident_mb"] / 16000, 2)
                v["cpu_user_pct"] = round(v["cpu_process_pct"] * 0.82, 2)
                v["cpu_sys_pct"] = round(v["cpu_process_pct"] - v["cpu_user_pct"], 2)
                v["cpu_cores_used"] = round(v["cpu_process_pct"] * 4 / 100, 3)
                if p["role"] != "MONGOS":
                    window = round(70 + 10 * math.sin(ts / 5000 + m.seed), 1)
                    v["disk_used_pct"] = round(m.disk_pct + (ts - now) / 86400 * 0.8, 2)
                    v["oplog_window_h"] = window
                    v["oplog_gb_per_hour"] = round(0.9 * m.load * .2, 3)
            if p["role"] == "SECONDARY":
                lag = r.choice([0, 0, 1, 1, 2, 3]) + (25 if m.seed % 7 == 0 and r.random() < .05 else 0)
                v["repl_lag_s"] = lag
                v["repl_headroom_h"] = round(70 + 10 * math.sin(ts / 5000) - lag / 3600, 2)
            rows.append((f"proc:{p['id']}", ts, v))
    for d in db.query("SELECT id FROM deployments"):
        for ts in range(now - hours * 3600, now, step):
            rows.append((f"dep:{d['id']}", ts, {"has_primary": 1, "members_unhealthy": 0, "nodes_down": 0,
                                                "primary_changes": 0}))
    for s in db.query("SELECT id FROM servers"):
        for ts in range(now - hours * 3600, now, step):
            r = random.Random(s["id"] * 7919 + ts)
            busy = min(.95, .25 + .2 * math.sin(ts / 1200 + s["id"]) + r.uniform(0, .1))
            rows.append((f"srv:{s['id']}", ts, {
                "host_up": 1, "sys_cpu_pct": round(100 * busy, 1), "sys_iowait_pct": 3.0,
                "load_per_core": round(busy, 2), "sys_mem_used_pct": round(100 * (.62 + busy * .1), 1),
                "swap_used_pct": 0.0, "disk_read_iops": round(r.uniform(20, 90), 1),
                "disk_write_iops": round(r.uniform(60, 220), 1), "disk_read_latency_ms": round(r.uniform(.4, 2.5), 2),
                "disk_write_latency_ms": round(r.uniform(.8, 4) + (25 if r.random() < .03 else 0), 2),
                "disk_util_pct": round(100 * min(.9, busy * .6), 1), "disk_free_pct": 54.0}))
    db.store_metrics(rows)
