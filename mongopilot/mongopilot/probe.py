"""MongoDB access through pymongo: topology discovery, metric sampling, slow-query log and index inventory.

Kept in its own module so tests and demo mode can replace it with a simulated implementation.
"""

from __future__ import annotations

import datetime as dt
import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any

# Connection-string options that must not be reused on direct connections to single members
_DROP_OPTS = {"replicaset", "directconnection", "readpreference", "readpreferencetags",
              "srvservicename", "srvmaxhosts", "loadbalanced", "maxstalenessseconds"}
SYSTEM_DBS = {"admin", "local", "config"}


@dataclass
class Node:
    host: str
    role: str                     # PRIMARY, SECONDARY, ARBITER, MONGOS, STANDALONE, ...
    shard: str | None = None
    rs_name: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class Topology:
    kind: str                     # standalone | replicaset | sharded
    nodes: list[Node]
    replica_sets: dict[str, dict[str, Any]]   # set name -> raw replSetGetStatus output
    info: dict[str, Any] = field(default_factory=dict)


class ConnectionSpec:
    """Credentials and TLS options extracted from a connection string."""

    def __init__(self, uri: str):
        from pymongo import uri_parser  # lazy import: tests do not need pymongo

        parsed = uri_parser.parse_uri(uri)
        self.uri = uri
        self.seeds = [f"{h}:{p}" for h, p in parsed["nodelist"]]
        self.username = parsed.get("username")
        self.password = parsed.get("password")
        opts = dict(parsed.get("options") or {})
        self.options = {k: v for k, v in opts.items() if k.lower() not in _DROP_OPTS}

    def direct_kwargs(self) -> dict[str, Any]:
        kw: dict[str, Any] = dict(self.options)
        if self.username:
            kw["username"] = self.username
            kw["password"] = self.password
        kw["directConnection"] = True
        return kw


def parse_slow_log_lines(lines: list[str], since: float) -> list[dict[str, Any]]:
    """Extract "Slow query" entries (structured logs, MongoDB 4.4+) newer than `since` (epoch seconds)."""
    out: list[dict[str, Any]] = []
    for line in lines:
        if '"Slow query"' not in line:
            continue
        try:
            doc = json.loads(line)
        except ValueError:
            continue
        if doc.get("msg") != "Slow query":
            continue
        raw_t = (doc.get("t") or {}).get("$date")
        try:
            ts = dt.datetime.fromisoformat(str(raw_t).replace("Z", "+00:00")).timestamp()
        except ValueError:
            continue
        if ts <= since:
            continue
        attr = doc.get("attr") or {}
        attr["_ts"] = ts
        out.append(attr)
    return out


def plain(value: Any) -> Any:
    """BSON values (dates, timestamps, ObjectIds, Int64...) converted to JSON-friendly types."""
    if isinstance(value, bool) or value is None or isinstance(value, (str, float)):
        return value
    if isinstance(value, int):
        return int(value)
    if isinstance(value, dict):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    return str(value)


def _attach_config(admin, status: dict[str, Any]) -> None:
    """Add the replica set configuration (hidden, priority, votes, delay, tags) to a status document."""
    try:
        status["_config"] = admin.command("replSetGetConfig").get("config", {})
    except Exception:
        status["_config"] = {}


def member_settings(status: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out = {}
    for m in (status.get("_config") or {}).get("members", []):
        out[m.get("host")] = {
            "hidden": bool(m.get("hidden")), "arbiter_only": bool(m.get("arbiterOnly")),
            "priority": m.get("priority", 1), "votes": m.get("votes", 1),
            "delay": int(m.get("secondaryDelaySecs", m.get("slaveDelay", 0)) or 0),
            "tags": dict(m.get("tags") or {}),
        }
    return out


def nodes_from_status(status: dict[str, Any], shard: str | None = None) -> list[Node]:
    settings = member_settings(status)
    return [Node(m["name"], m.get("stateStr", "UNKNOWN"), shard=shard, rs_name=status.get("set"),
                 extra=settings.get(m["name"], {})) for m in status.get("members", [])]


class PyMongoProbe:
    """Real implementation based on pymongo (thread-safe, with a client cache)."""

    def __init__(self, timeout_ms: int = 4000, storage_ttl: int = 300):
        self.timeout_ms = timeout_ms
        self.storage_ttl = storage_ttl
        self._clients: dict[tuple[str, str], Any] = {}
        self._lock = threading.Lock()
        self._host_info: dict[str, tuple[float, dict[str, Any]]] = {}
        self._storage: dict[str, tuple[float, dict[str, Any]]] = {}

    # -- clients -----------------------------------------------------------
    def _client(self, uri: str, host: str | None = None):
        from pymongo import MongoClient

        key = (uri, host or "*")
        with self._lock:
            client = self._clients.get(key)
            if client is None:
                common = dict(serverSelectionTimeoutMS=self.timeout_ms, connectTimeoutMS=self.timeout_ms,
                              socketTimeoutMS=self.timeout_ms * 5, appname="MongoPilot")
                if host is None:
                    client = MongoClient(uri, **common)
                else:
                    spec = ConnectionSpec(uri)
                    client = MongoClient(host, **spec.direct_kwargs(), **common)
                self._clients[key] = client
            return client

    def forget(self, uri: str) -> None:
        with self._lock:
            for key in [k for k in self._clients if k[0] == uri]:
                try:
                    self._clients.pop(key).close()
                except Exception:
                    pass

    def close(self) -> None:
        with self._lock:
            for c in self._clients.values():
                try:
                    c.close()
                except Exception:
                    pass
            self._clients.clear()

    # -- discovery ---------------------------------------------------------
    def discover(self, uri: str) -> Topology:
        client = self._client(uri)
        admin = client.admin
        hello = admin.command("hello")
        if hello.get("msg") == "isdbgrid":
            return self._discover_sharded(uri, client)
        if hello.get("setName"):
            rs = admin.command("replSetGetStatus")
            _attach_config(admin, rs)
            return Topology("replicaset", nodes_from_status(rs), {rs.get("set"): rs})
        host = hello.get("me") or ConnectionSpec(uri).seeds[0]
        return Topology("standalone", [Node(host, "STANDALONE")], {})

    def _discover_sharded(self, uri: str, client) -> Topology:
        admin = client.admin
        nodes: list[Node] = []
        rs_status: dict[str, dict[str, Any]] = {}
        shards = admin.command("listShards").get("shards", [])
        info: dict[str, Any] = {"shards": [s["_id"] for s in shards]}
        mongos_hosts: list[str] = []
        try:  # mongos seen in the last 10 minutes (needs read access to config.mongos)
            cutoff = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - dt.timedelta(minutes=10)
            mongos_hosts = [d["_id"] for d in client.config.mongos.find({"ping": {"$gte": cutoff}}, {"_id": 1})]
        except Exception:
            pass
        if not mongos_hosts:
            mongos_hosts = ConnectionSpec(uri).seeds
        nodes += [Node(h, "MONGOS") for h in mongos_hosts]
        try:
            bal = admin.command("balancerStatus")
            info["balancer_enabled"] = 0 if bal.get("mode") == "off" else 1
            info["balancer_running"] = 1 if bal.get("inBalancerRound") else 0
        except Exception:
            pass
        try:
            info["jumbo_chunks"] = client.config.chunks.count_documents({"jumbo": True})
        except Exception:
            pass
        groups: list[tuple[str, str]] = [(s["_id"], s["host"]) for s in shards]
        try:
            cfg = admin.command("serverStatus").get("sharding", {}).get("configsvrConnectionString")
            if cfg:
                groups.append(("config", cfg))
        except Exception:
            pass
        for shard_name, conn in groups:
            set_name, _, hosts = conn.partition("/")
            host_list = [h for h in (hosts or set_name).split(",") if h]
            status = None
            for h in host_list:
                try:
                    member_admin = self._client(uri, h).admin
                    status = member_admin.command("replSetGetStatus")
                    _attach_config(member_admin, status)
                    break
                except Exception:
                    continue
            if status is None:
                nodes += [Node(h, "UNKNOWN", shard=shard_name, rs_name=set_name) for h in host_list]
                continue
            rs_status[status.get("set", set_name)] = status
            nodes += nodes_from_status(status, shard_name)
        return Topology("sharded", nodes, rs_status, info)

    # -- sampling ----------------------------------------------------------
    def sample(self, uri: str, node: Node, slow_op_seconds: int) -> dict[str, Any]:
        """Collect raw data for one process. Raises if the process is unreachable."""
        client = self._client(uri, node.host)
        admin = client.admin
        out: dict[str, Any] = {"hello": admin.command("hello")}
        if node.role == "ARBITER":
            return out  # arbiters have no users: reachability is enough
        out["serverStatus"] = admin.command("serverStatus")
        out["hostInfo"] = self._cached_host_info(node.host, admin)
        try:
            out["dbStats"] = admin.command("dbStats")
        except Exception:
            out["dbStats"] = None
        try:
            ops = admin.command({"currentOp": 1, "active": True, "secs_running": {"$gte": slow_op_seconds},
                                 "op": {"$ne": "none"}}).get("inprog", [])
            ops = [o for o in ops if not str(o.get("desc", "")).startswith(("ReplBatcher", "rsSync", "monitoring"))
                   and o.get("ns") != "local.oplog.rs"]
            out["slow_ops"] = len(ops)
            out["longest_op_s"] = max((o.get("secs_running", 0) for o in ops), default=0)
            out["slow_ops_sample"] = [
                {"opid": str(o.get("opid")), "ns": o.get("ns"), "op": o.get("op"),
                 "secs": o.get("secs_running"), "client": o.get("client"), "desc": o.get("desc")}
                for o in sorted(ops, key=lambda o: -o.get("secs_running", 0))[:10]
            ]
        except Exception:
            pass
        if node.role in ("PRIMARY", "SECONDARY", "STANDALONE"):
            if node.role != "STANDALONE":
                self._oplog(client, out)
            if node.role in ("PRIMARY", "STANDALONE"):
                out["storage"] = self._cached_storage(node.host, client)
            try:
                fcv = admin.command({"getParameter": 1, "featureCompatibilityVersion": 1})
                out["fcv"] = fcv.get("featureCompatibilityVersion", {}).get("version")
            except Exception:
                pass
        return out

    def _cached_host_info(self, host: str, admin) -> dict[str, Any] | None:
        hit = self._host_info.get(host)
        if hit and time.time() - hit[0] < 3600:
            return hit[1]
        try:
            info = admin.command("hostInfo")
            data = {"cores": (info.get("system") or {}).get("numCores"),
                    "mem_mb": (info.get("system") or {}).get("memSizeMB"),
                    "os": (info.get("os") or {}).get("name")}
        except Exception:
            data = None
        self._host_info[host] = (time.time(), data)
        return data

    def _oplog(self, client, out: dict[str, Any]) -> None:
        try:
            oplog = client.local["oplog.rs"]
            first = oplog.find({}, {"ts": 1}).sort("$natural", 1).limit(1).next()["ts"]
            last = oplog.find({}, {"ts": 1}).sort("$natural", -1).limit(1).next()["ts"]
            out["oplog_window_h"] = round((last.time - first.time) / 3600, 2)
        except Exception:
            return
        try:
            st = client.local.command("collStats", "oplog.rs")
            out["oplog_used_mb"] = st.get("size", 0) / 1048576
            out["oplog_max_mb"] = st.get("maxSize", 0) / 1048576
        except Exception:
            pass

    def _cached_storage(self, host: str, client) -> dict[str, Any] | None:
        """Sum of dbStats across user databases, refreshed every `storage_ttl` seconds."""
        hit = self._storage.get(host)
        if hit and time.time() - hit[0] < self.storage_ttl:
            return hit[1]
        try:
            names = [n for n in client.list_database_names() if n not in SYSTEM_DBS][:100]
            tot = {"data_bytes": 0.0, "storage_bytes": 0.0, "index_bytes": 0.0, "objects": 0, "collections": 0,
                   "indexes": 0, "databases": len(names)}
            for name in names:
                s = client[name].command("dbStats")
                tot["data_bytes"] += s.get("dataSize", 0)
                tot["storage_bytes"] += s.get("storageSize", 0)
                tot["index_bytes"] += s.get("indexSize", 0)
                tot["objects"] += s.get("objects", 0)
                tot["collections"] += s.get("collections", 0)
                tot["indexes"] += s.get("indexes", 0)
        except Exception:
            tot = None
        self._storage[host] = (time.time(), tot)
        return tot

    # -- server parameters -------------------------------------------------
    def get_parameters(self, uri: str, host: str) -> dict[str, dict[str, Any]]:
        """All server parameters of one process, with runtime/startup settability when available (5.0+)."""
        admin = self._client(uri, host).admin
        try:
            res = admin.command({"getParameter": {"allParameters": True, "showDetails": True}})
            detailed = True
        except Exception:
            res = admin.command({"getParameter": "*"})
            detailed = False
        out: dict[str, dict[str, Any]] = {}
        for name, val in res.items():
            if name in ("ok", "$clusterTime", "operationTime"):
                continue
            if detailed and isinstance(val, dict) and "value" in val:
                out[name] = {"value": plain(val["value"]), "runtime": val.get("settableAtRuntime"),
                             "startup": val.get("settableAtStartup")}
            else:
                out[name] = {"value": plain(val), "runtime": None, "startup": None}
        return out

    # -- slow query log ----------------------------------------------------
    def slow_log(self, uri: str, node: Node, since: float) -> list[dict[str, Any]]:
        """Slow queries from the in-memory log (getLog global, last 1024 lines)."""
        res = self._client(uri, node.host).admin.command({"getLog": "global"})
        return parse_slow_log_lines(res.get("log", []), since)

    # -- index inventory ---------------------------------------------------
    def index_inventory(self, uri: str, topology: Topology, max_collections: int = 300) -> dict[str, Any]:
        """Collections, index definitions, sizes and $indexStats usage summed across data-bearing members."""
        client = self._client(uri)
        members: list[Any] = []
        if topology.kind == "replicaset":
            members = [self._client(uri, n.host) for n in topology.nodes if n.role in ("PRIMARY", "SECONDARY")]
        else:  # standalone, or sharded through mongos (which fans $indexStats out to the shards)
            members = [client]
        out: dict[str, Any] = {}
        warnings: list[str] = []
        for db_name in [n for n in client.list_database_names() if n not in SYSTEM_DBS]:
            db = client[db_name]
            try:
                colls = [c["name"] for c in db.list_collections(filter={"type": "collection"})
                         if not c["name"].startswith("system.")]
            except Exception as exc:
                warnings.append(f"Cannot list collections of {db_name}: {exc}")
                continue
            for coll_name in colls:
                if len(out) >= max_collections:
                    warnings.append(f"Stopped after {max_collections} collections")
                    return {"collections": out, "warnings": warnings}
                ns = f"{db_name}.{coll_name}"
                coll = db[coll_name]
                entry: dict[str, Any] = {"indexes": [], "usage": {}, "sizes": {}, "count": None, "size": None}
                try:
                    for spec in coll.list_indexes():
                        spec = dict(spec)
                        spec["key"] = list(dict(spec["key"]).items())
                        entry["indexes"].append(spec)
                except Exception as exc:
                    warnings.append(f"Cannot list indexes of {ns}: {exc}")
                    continue
                for member in members:
                    try:
                        for st in member[db_name][coll_name].aggregate([{"$indexStats": {}}]):
                            u = entry["usage"].setdefault(st["name"], {"ops": 0, "since": None, "hosts": []})
                            acc = st.get("accesses") or {}
                            u["ops"] += int(acc.get("ops", 0))
                            since = acc.get("since")
                            if since is not None:
                                ts = since.replace(tzinfo=dt.timezone.utc).timestamp() if since.tzinfo is None \
                                    else since.timestamp()
                                u["since"] = max(u["since"] or 0, ts)  # shortest observation window
                            u["hosts"].append(st.get("host") or st.get("shard"))
                    except Exception as exc:
                        warnings.append(f"$indexStats failed on {ns}: {exc}")
                        break
                try:  # one document per shard on sharded collections: sum them
                    for doc in coll.aggregate([{"$collStats": {"storageStats": {}}}]):
                        stats = doc["storageStats"]
                        for name, size in (stats.get("indexSizes") or {}).items():
                            entry["sizes"][name] = entry["sizes"].get(name, 0) + size
                        entry["count"] = (entry["count"] or 0) + stats.get("count", 0)
                        entry["size"] = (entry["size"] or 0) + stats.get("size", 0)
                except Exception:
                    pass
                out[ns] = entry
        return {"collections": out, "warnings": warnings}
