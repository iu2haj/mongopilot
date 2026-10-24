"""Metric catalog and computation of metric values from raw samples.

The metric set follows MongoDB's monitoring guidance: throughput (opcounters, documents), operation
execution time (opLatencies), query targeting and scan-and-order, connections and queues, WiredTiger cache,
process CPU and memory, storage, replication lag / oplog window / oplog GB per hour / headroom, plus host
hardware metrics (normalized system CPU, disk IOPS and latency, free disk, system memory) collected over SSH.
"""

from __future__ import annotations

from typing import Any

# name: (label, unit, scope, group)    scope: proc = per process, dep = per deployment, srv = per server
CATALOG: dict[str, tuple[str, str, str, str]] = {
    # Throughput
    "ops_insert": ("Inserts", "op/s", "proc", "Throughput"),
    "ops_query": ("Queries", "op/s", "proc", "Throughput"),
    "ops_update": ("Updates", "op/s", "proc", "Throughput"),
    "ops_delete": ("Deletes", "op/s", "proc", "Throughput"),
    "ops_getmore": ("Getmores", "op/s", "proc", "Throughput"),
    "ops_command": ("Commands", "op/s", "proc", "Throughput"),
    "ops_total": ("Total operations", "op/s", "proc", "Throughput"),
    "docs_returned_s": ("Documents returned", "doc/s", "proc", "Throughput"),
    "docs_inserted_s": ("Documents inserted", "doc/s", "proc", "Throughput"),
    "docs_updated_s": ("Documents updated", "doc/s", "proc", "Throughput"),
    "docs_deleted_s": ("Documents deleted", "doc/s", "proc", "Throughput"),
    "net_requests_s": ("Network requests", "req/s", "proc", "Throughput"),
    # Latency (operation execution time)
    "lat_reads_ms": ("Read latency (avg)", "ms", "proc", "Latency"),
    "lat_writes_ms": ("Write latency (avg)", "ms", "proc", "Latency"),
    "lat_commands_ms": ("Command latency (avg)", "ms", "proc", "Latency"),
    "lat_txn_ms": ("Transaction latency (avg)", "ms", "proc", "Latency"),
    # Query efficiency
    "query_targeting": ("Query targeting (docs scanned / returned)", "", "proc", "Query efficiency"),
    "query_targeting_keys": ("Query targeting (keys scanned / returned)", "", "proc", "Query efficiency"),
    "scan_and_order_s": ("Scan and order (in-memory sorts)", "/s", "proc", "Query efficiency"),
    "write_conflicts_s": ("Write conflicts", "/s", "proc", "Query efficiency"),
    "slow_ops": ("Slow operations in progress", "", "proc", "Query efficiency"),
    "longest_op_s": ("Longest running operation", "s", "proc", "Query efficiency"),
    # Connections & cursors
    "connections_current": ("Current connections", "", "proc", "Connections & cursors"),
    "connections_active": ("Active connections", "", "proc", "Connections & cursors"),
    "connections_used_pct": ("Connections used", "%", "proc", "Connections & cursors"),
    "connections_created_s": ("New connections", "/s", "proc", "Connections & cursors"),
    "cursors_open": ("Open cursors", "", "proc", "Connections & cursors"),
    "cursors_notimeout": ("Open cursors without timeout", "", "proc", "Connections & cursors"),
    "cursors_timedout_s": ("Cursors timed out", "/s", "proc", "Connections & cursors"),
    # Concurrency
    "queue_readers": ("Queued readers", "", "proc", "Concurrency"),
    "queue_writers": ("Queued writers", "", "proc", "Concurrency"),
    "queue_total": ("Queued operations", "", "proc", "Concurrency"),
    "active_readers": ("Active readers", "", "proc", "Concurrency"),
    "active_writers": ("Active writers", "", "proc", "Concurrency"),
    "tickets_read_avail": ("Read tickets available", "", "proc", "Concurrency"),
    "tickets_write_avail": ("Write tickets available", "", "proc", "Concurrency"),
    # WiredTiger cache
    "cache_used_pct": ("Cache used", "%", "proc", "WiredTiger cache"),
    "cache_dirty_pct": ("Cache dirty", "%", "proc", "WiredTiger cache"),
    "cache_read_bps": ("Read into cache", "B/s", "proc", "WiredTiger cache"),
    "cache_written_bps": ("Written from cache", "B/s", "proc", "WiredTiger cache"),
    "cache_evicted_s": ("Pages evicted", "/s", "proc", "WiredTiger cache"),
    "cache_app_evictions_s": ("Pages evicted by application threads", "/s", "proc", "WiredTiger cache"),
    "checkpoint_ms": ("Last checkpoint duration", "ms", "proc", "WiredTiger cache"),
    # Process resources
    "cpu_process_pct": ("Normalized process CPU", "%", "proc", "Resources"),
    "cpu_user_pct": ("CPU user time (serverStatus.extra_info)", "%", "proc", "Resources"),
    "cpu_sys_pct": ("CPU system time (serverStatus.extra_info)", "%", "proc", "Resources"),
    "cpu_cores_used": ("CPU cores used by the process", "cores", "proc", "Resources"),
    "mem_resident_mb": ("Resident memory", "MB", "proc", "Resources"),
    "mem_virtual_mb": ("Virtual memory", "MB", "proc", "Resources"),
    "mem_resident_pct": ("Resident memory of host RAM", "%", "proc", "Resources"),
    "page_faults_s": ("Page faults", "/s", "proc", "Resources"),
    "net_in_bps": ("Network in", "B/s", "proc", "Resources"),
    "net_out_bps": ("Network out", "B/s", "proc", "Resources"),
    "disk_used_pct": ("Data volume used", "%", "proc", "Resources"),
    "uptime_s": ("Uptime", "s", "proc", "Resources"),
    # Storage (dbStats summed over user databases, on primaries/standalones)
    "data_size_gb": ("Data size", "GB", "proc", "Storage"),
    "storage_size_gb": ("Storage size", "GB", "proc", "Storage"),
    "index_size_gb": ("Index size", "GB", "proc", "Storage"),
    "collections": ("Collections", "", "proc", "Storage"),
    "indexes": ("Indexes", "", "proc", "Storage"),
    # Replication
    "repl_lag_s": ("Replication lag", "s", "proc", "Replication"),
    "oplog_window_h": ("Oplog window", "h", "proc", "Replication"),
    "oplog_gb_per_hour": ("Oplog GB per hour", "GB/h", "proc", "Replication"),
    "repl_headroom_h": ("Replication headroom", "h", "proc", "Replication"),
    "member_state_ok": ("Member healthy (1/0)", "", "proc", "Replication"),
    "flow_control_lagged": ("Flow control engaged (1/0)", "", "proc", "Replication"),
    "up": ("Process reachable (1/0)", "", "proc", "Replication"),
    # Transactions & other
    "txn_open": ("Open transactions", "", "proc", "Transactions & other"),
    "txn_active": ("Active transactions", "", "proc", "Transactions & other"),
    "txn_committed_s": ("Transactions committed", "/s", "proc", "Transactions & other"),
    "txn_aborted_s": ("Transactions aborted", "/s", "proc", "Transactions & other"),
    "ttl_deleted_s": ("TTL deletions", "doc/s", "proc", "Transactions & other"),
    "asserts_regular_s": ("Regular asserts", "/s", "proc", "Transactions & other"),
    "asserts_warning_s": ("Warning asserts", "/s", "proc", "Transactions & other"),
    "asserts_user_s": ("User asserts", "/s", "proc", "Transactions & other"),
    # Host hardware (SSH, registered servers)
    "sys_cpu_pct": ("Normalized system CPU", "%", "srv", "Host"),
    "sys_iowait_pct": ("CPU I/O wait", "%", "srv", "Host"),
    "load_per_core": ("Load average per core", "", "srv", "Host"),
    "sys_mem_used_pct": ("System memory used", "%", "srv", "Host"),
    "swap_used_pct": ("Swap used", "%", "srv", "Host"),
    "disk_read_iops": ("Disk read IOPS", "IOPS", "srv", "Host"),
    "disk_write_iops": ("Disk write IOPS", "IOPS", "srv", "Host"),
    "disk_read_latency_ms": ("Disk read latency", "ms", "srv", "Host"),
    "disk_write_latency_ms": ("Disk write latency", "ms", "srv", "Host"),
    "disk_util_pct": ("Disk utilization", "%", "srv", "Host"),
    "disk_free_pct": ("Disk space free (lowest volume)", "%", "srv", "Host"),
    "host_up": ("Server reachable over SSH (1/0)", "", "srv", "Host"),
    # Deployment
    "has_primary": ("Primary present (1/0)", "", "dep", "Deployment"),
    "members_unhealthy": ("Unhealthy members", "", "dep", "Deployment"),
    "nodes_down": ("Unreachable processes", "", "dep", "Deployment"),
    "primary_changes": ("Primary changes (elections)", "", "dep", "Deployment"),
    "balancer_enabled": ("Balancer enabled (1/0)", "", "dep", "Deployment"),
    "jumbo_chunks": ("Jumbo chunks", "", "dep", "Deployment"),
}

OPS = ("insert", "query", "update", "delete", "getmore", "command")
HEALTHY_STATES = {"PRIMARY", "SECONDARY", "ARBITER", "MONGOS", "STANDALONE"}

# cumulative counter -> per-second output metric
SIMPLE_RATES = {
    **{f"op_{op}": f"ops_{op}" for op in OPS},
    "net_in": "net_in_bps", "net_out": "net_out_bps", "net_requests": "net_requests_s",
    "page_faults": "page_faults_s",
    "asserts_regular": "asserts_regular_s", "asserts_warning": "asserts_warning_s", "asserts_user": "asserts_user_s",
    "doc_returned": "docs_returned_s", "doc_inserted": "docs_inserted_s", "doc_updated": "docs_updated_s",
    "doc_deleted": "docs_deleted_s",
    "scan_and_order": "scan_and_order_s", "write_conflicts": "write_conflicts_s",
    "cursor_timed_out": "cursors_timedout_s", "conn_created": "connections_created_s",
    "cache_read_bytes": "cache_read_bps", "cache_written_bytes": "cache_written_bps",
    "cache_app_evict": "cache_app_evictions_s",
    "txn_committed": "txn_committed_s", "txn_aborted": "txn_aborted_s", "ttl_deleted": "ttl_deleted_s",
}
LATENCY_KINDS = {"reads": "lat_reads_ms", "writes": "lat_writes_ms", "commands": "lat_commands_ms",
                 "transactions": "lat_txn_ms"}


def _get(doc: Any, *path: str, default: Any = None) -> Any:
    for key in path:
        if not isinstance(doc, dict) or key not in doc:
            return default
        doc = doc[key]
    return doc


def _num(value: Any) -> float | None:
    if isinstance(value, bool):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def counters(ss: dict[str, Any]) -> dict[str, float]:
    """Cumulative counters from serverStatus, used to compute per-interval rates."""
    raw: dict[str, Any] = {
        **{f"op_{op}": _get(ss, "opcounters", op) for op in OPS},
        "net_in": _get(ss, "network", "bytesIn"),
        "net_out": _get(ss, "network", "bytesOut"),
        "net_requests": _get(ss, "network", "numRequests"),
        "page_faults": _get(ss, "extra_info", "page_faults"),
        "asserts_regular": _get(ss, "asserts", "regular"),
        "asserts_warning": _get(ss, "asserts", "warning"),
        "asserts_user": _get(ss, "asserts", "user"),
        "doc_returned": _get(ss, "metrics", "document", "returned"),
        "doc_inserted": _get(ss, "metrics", "document", "inserted"),
        "doc_updated": _get(ss, "metrics", "document", "updated"),
        "doc_deleted": _get(ss, "metrics", "document", "deleted"),
        "qe_scanned": _get(ss, "metrics", "queryExecutor", "scanned"),
        "qe_scanned_objects": _get(ss, "metrics", "queryExecutor", "scannedObjects"),
        "scan_and_order": _get(ss, "metrics", "operation", "scanAndOrder"),
        "write_conflicts": _get(ss, "metrics", "operation", "writeConflicts"),
        "cursor_timed_out": _get(ss, "metrics", "cursor", "timedOut"),
        "conn_created": _get(ss, "connections", "totalCreated"),
        "cache_read_bytes": _get(ss, "wiredTiger", "cache", "bytes read into cache"),
        "cache_written_bytes": _get(ss, "wiredTiger", "cache", "bytes written from cache"),
        "cache_evicted_modified": _get(ss, "wiredTiger", "cache", "modified pages evicted"),
        "cache_evicted_unmodified": _get(ss, "wiredTiger", "cache", "unmodified pages evicted"),
        "cache_app_evict": _get(ss, "wiredTiger", "cache", "pages evicted by application threads"),
        "txn_committed": _get(ss, "transactions", "totalCommitted"),
        "txn_aborted": _get(ss, "transactions", "totalAborted"),
        "ttl_deleted": _get(ss, "metrics", "ttl", "deletedDocuments"),
        "cpu_user_us": _get(ss, "extra_info", "user_time_us"),
        "cpu_sys_us": _get(ss, "extra_info", "system_time_us"),
    }
    for kind in LATENCY_KINDS:
        raw[f"lat_{kind}"] = _get(ss, "opLatencies", kind, "latency")
        raw[f"lat_{kind}_ops"] = _get(ss, "opLatencies", kind, "ops")
    out: dict[str, float] = {}
    for k, v in raw.items():
        n = _num(v)
        if n is not None:
            out[k] = n
    return out


def rates(prev: tuple[float, dict[str, float]] | None, cur: tuple[float, dict[str, float]],
          prev_uptime: float | None = None, cur_uptime: float | None = None,
          cores: float | None = None) -> dict[str, float]:
    """Per-second rates and ratios between two samples. Skipped after a restart (counters reset)."""
    if prev is None:
        return {}
    (t0, c0), (t1, c1) = prev, cur
    dt = t1 - t0
    if dt <= 0:
        return {}
    if prev_uptime is not None and cur_uptime is not None and cur_uptime < prev_uptime:
        return {}

    def delta(key: str) -> float | None:
        if key not in c0 or key not in c1 or c1[key] < c0[key]:
            return None
        return c1[key] - c0[key]

    out: dict[str, float] = {}
    for src, dst in SIMPLE_RATES.items():
        d = delta(src)
        if d is not None:
            out[dst] = round(d / dt, 3)
    ops = [out[f"ops_{op}"] for op in OPS if f"ops_{op}" in out]
    if ops:
        out["ops_total"] = round(sum(ops), 3)
    ev = [delta("cache_evicted_modified"), delta("cache_evicted_unmodified")]
    if any(v is not None for v in ev):
        out["cache_evicted_s"] = round(sum(v or 0 for v in ev) / dt, 3)
    for kind, dst in LATENCY_KINDS.items():
        d_lat, d_ops = delta(f"lat_{kind}"), delta(f"lat_{kind}_ops")
        if d_lat is not None and d_ops:
            out[dst] = round(d_lat / d_ops / 1000, 3)   # microseconds -> ms per operation
    d_ret = delta("doc_returned")
    for src, dst in (("qe_scanned_objects", "query_targeting"), ("qe_scanned", "query_targeting_keys")):
        d_scan = delta(src)
        if d_scan is not None and d_ret is not None:
            out[dst] = round(d_scan / max(d_ret, 1.0), 2) if (d_scan or d_ret) else 0.0
    d_user, d_sys = delta("cpu_user_us"), delta("cpu_sys_us")
    if d_user is not None and d_sys is not None and d_user >= 0 and d_sys >= 0:
        # extra_info.user_time_us / system_time_us: CPU time consumed by the process (Linux)
        out["cpu_cores_used"] = round((d_user + d_sys) / 1e6 / dt, 3)
        if cores:
            out["cpu_process_pct"] = round(100 * (d_user + d_sys) / 1e6 / dt / cores, 2)
            out["cpu_user_pct"] = round(100 * d_user / 1e6 / dt / cores, 2)
            out["cpu_sys_pct"] = round(100 * d_sys / 1e6 / dt / cores, 2)
    return out


def gauges(ss: dict[str, Any], host_info: dict[str, Any] | None = None) -> dict[str, float]:
    """Instantaneous values from serverStatus."""
    out: dict[str, float] = {}

    def put(name: str, value: Any, digits: int | None = None) -> None:
        n = _num(value)
        if n is not None:
            out[name] = round(n, digits) if digits is not None else n

    cur = _num(_get(ss, "connections", "current"))
    avail = _num(_get(ss, "connections", "available"))
    put("connections_current", cur)
    put("connections_active", _get(ss, "connections", "active"))
    if cur is not None and avail is not None and cur + avail > 0:
        out["connections_used_pct"] = round(100 * cur / (cur + avail), 2)
    put("uptime_s", ss.get("uptime"))
    put("mem_resident_mb", _get(ss, "mem", "resident"))
    put("mem_virtual_mb", _get(ss, "mem", "virtual"))
    host_mem = _num((host_info or {}).get("mem_mb"))
    if host_mem and "mem_resident_mb" in out:
        out["mem_resident_pct"] = round(100 * out["mem_resident_mb"] / host_mem, 2)
    cache = _get(ss, "wiredTiger", "cache", default={}) or {}
    max_b = _num(cache.get("maximum bytes configured"))
    used_b = _num(cache.get("bytes currently in the cache"))
    dirty_b = _num(cache.get("tracked dirty bytes in the cache"))
    if max_b:
        if used_b is not None:
            out["cache_used_pct"] = round(100 * used_b / max_b, 2)
        if dirty_b is not None:
            out["cache_dirty_pct"] = round(100 * dirty_b / max_b, 2)
    put("checkpoint_ms", _get(ss, "wiredTiger", "transaction", "transaction checkpoint most recent time (msecs)"))
    q = _get(ss, "globalLock", "currentQueue", default={}) or {}
    put("queue_readers", q.get("readers"))
    put("queue_writers", q.get("writers"))
    put("queue_total", q.get("total"))
    a = _get(ss, "globalLock", "activeClients", default={}) or {}
    put("active_readers", a.get("readers"))
    put("active_writers", a.get("writers"))
    # Tickets: MongoDB 7+ exposes queues.execution, older versions wiredTiger.concurrentTransactions
    for section in (_get(ss, "queues", "execution"), _get(ss, "wiredTiger", "concurrentTransactions")):
        if isinstance(section, dict) and "read" in section:
            put("tickets_read_avail", _get(section, "read", "available"))
            put("tickets_write_avail", _get(section, "write", "available"))
            break
    put("cursors_open", _get(ss, "metrics", "cursor", "open", "total"))
    put("cursors_notimeout", _get(ss, "metrics", "cursor", "open", "noTimeout"))
    put("txn_open", _get(ss, "transactions", "currentOpen"))
    put("txn_active", _get(ss, "transactions", "currentActive"))
    lagged = _get(ss, "flowControl", "isLagged")
    if lagged is not None:
        out["flow_control_lagged"] = 1.0 if lagged else 0.0
    return out


def storage_values(storage: dict[str, Any] | None) -> dict[str, float]:
    if not storage:
        return {}
    gb = 1024 ** 3
    return {"data_size_gb": round(storage["data_bytes"] / gb, 3),
            "storage_size_gb": round(storage["storage_bytes"] / gb, 3),
            "index_size_gb": round(storage["index_bytes"] / gb, 3),
            "collections": float(storage["collections"]), "indexes": float(storage["indexes"])}


def oplog_values(raw: dict[str, Any]) -> dict[str, float]:
    out: dict[str, float] = {}
    window = _num(raw.get("oplog_window_h"))
    if window is None:
        return out
    out["oplog_window_h"] = window
    used = _num(raw.get("oplog_used_mb"))
    if used is not None and window > 0:
        out["oplog_gb_per_hour"] = round(used / 1024 / window, 4)
    return out


def disk_used_pct(db_stats: dict[str, Any] | None) -> float | None:
    if not db_stats:
        return None
    used, total = _num(db_stats.get("fsUsedSize")), _num(db_stats.get("fsTotalSize"))
    if used is None or not total:
        return None
    return round(100 * used / total, 2)


def replica_metrics(rs_status: dict[str, Any]) -> dict[str, Any]:
    """From replSetGetStatus: per-member lag, primary presence, unhealthy members."""
    members = rs_status.get("members", []) or []
    primary = next((m for m in members if m.get("stateStr") == "PRIMARY"), None)
    primary_optime = _optime_seconds(primary) if primary else None
    per_member: dict[str, dict[str, Any]] = {}
    unhealthy = 0
    for m in members:
        state = m.get("stateStr", "UNKNOWN")
        healthy = m.get("health", 0) == 1 and state in HEALTHY_STATES
        if not healthy:
            unhealthy += 1
        lag = None
        if state == "SECONDARY" and primary_optime is not None:
            mo = _optime_seconds(m)
            if mo is not None:
                lag = max(0.0, primary_optime - mo)
        per_member[m.get("name")] = {
            "state": state,
            "health": m.get("health"),
            "lag": lag,
            "state_ok": 1 if healthy else 0,
            "uptime": m.get("uptime"),
            "syncSource": m.get("syncSourceHost") or m.get("syncingTo"),
        }
    return {
        "set": rs_status.get("set"),
        "has_primary": 1 if primary else 0,
        "primary": primary.get("name") if primary else None,
        "members_unhealthy": unhealthy,
        "members": per_member,
    }


def _optime_seconds(member: dict[str, Any] | None) -> float | None:
    if not member:
        return None
    od = member.get("optimeDate")
    if od is not None and hasattr(od, "timestamp"):
        return od.timestamp()
    ts = _get(member, "optime", "ts")
    if ts is not None and hasattr(ts, "time"):  # bson.Timestamp
        return float(ts.time)
    return None
