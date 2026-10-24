"""SQLite storage: configuration, process state, metrics, slow queries, alerts and jobs."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS servers (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    hostname TEXT NOT NULL,
    ssh_port INTEGER NOT NULL DEFAULT 22,
    ssh_user TEXT NOT NULL,
    ssh_key_enc TEXT,
    mongo_host TEXT,             -- name/IP members use to reach each other (default: hostname)
    notes TEXT,
    facts TEXT,                  -- JSON: detected os, arch, cpu, ram
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS deployments (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    uri_enc TEXT NOT NULL,        -- connection string used for monitoring
    managed INTEGER NOT NULL DEFAULT 0,
    managed_enc TEXT,             -- encrypted JSON: members, version, admin credentials
    topology TEXT,                -- standalone | replicaset | sharded
    status TEXT,                  -- JSON: last detected status
    last_poll INTEGER,
    last_error TEXT,
    paused INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS processes (
    id INTEGER PRIMARY KEY,
    deployment_id INTEGER NOT NULL REFERENCES deployments(id) ON DELETE CASCADE,
    host TEXT NOT NULL,           -- host:port
    role TEXT,                    -- PRIMARY, SECONDARY, ARBITER, MONGOS, ...
    shard TEXT,                   -- shard name / "config" / null
    rs_name TEXT,
    version TEXT,
    up INTEGER NOT NULL DEFAULT 0,
    last_seen INTEGER,
    last_error TEXT,
    info TEXT,                    -- JSON: details (uptime, engine, fcv, ...)
    UNIQUE(deployment_id, host)
);

CREATE TABLE IF NOT EXISTS metrics (
    subject TEXT NOT NULL,        -- "proc:<id>", "dep:<id>" or "srv:<id>"
    ts INTEGER NOT NULL,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS metrics_subject_ts ON metrics(subject, ts);
CREATE INDEX IF NOT EXISTS metrics_ts ON metrics(ts);

CREATE TABLE IF NOT EXISTS channels (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    type TEXT NOT NULL,           -- email | slack | webhook
    config_enc TEXT NOT NULL,
    is_default INTEGER NOT NULL DEFAULT 1,  -- used by rules without explicit channels
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS alert_rules (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    metric TEXT NOT NULL,
    operator TEXT NOT NULL,       -- > >= < <= == !=
    threshold REAL NOT NULL,
    duration_s INTEGER NOT NULL DEFAULT 0,
    deployment_id INTEGER REFERENCES deployments(id) ON DELETE CASCADE,
    severity TEXT NOT NULL DEFAULT 'warning',
    channel_ids TEXT NOT NULL DEFAULT '[]',
    renotify_s INTEGER NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY,
    rule_id INTEGER NOT NULL REFERENCES alert_rules(id) ON DELETE CASCADE,
    subject TEXT NOT NULL,
    subject_label TEXT NOT NULL,
    deployment_id INTEGER,
    status TEXT NOT NULL,         -- open | acknowledged | resolved
    severity TEXT NOT NULL,
    message TEXT NOT NULL,
    value REAL,
    opened_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    resolved_at INTEGER,
    acknowledged_by TEXT,
    last_notified INTEGER
);
CREATE INDEX IF NOT EXISTS alerts_status ON alerts(status);

-- "Pending" state: condition true but not yet for duration_s
CREATE TABLE IF NOT EXISTS alert_pending (
    rule_id INTEGER NOT NULL,
    subject TEXT NOT NULL,
    since INTEGER NOT NULL,
    PRIMARY KEY(rule_id, subject)
);

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY,
    type TEXT NOT NULL,
    deployment_id INTEGER,
    title TEXT NOT NULL,
    params_enc TEXT,
    status TEXT NOT NULL,         -- queued | running | success | failed | cancelled
    created_by TEXT,
    created_at INTEGER NOT NULL,
    started_at INTEGER,
    finished_at INTEGER,
    error TEXT
);

CREATE TABLE IF NOT EXISTS job_logs (
    id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    ts INTEGER NOT NULL,
    level TEXT NOT NULL,
    message TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS job_logs_job ON job_logs(job_id, id);

CREATE TABLE IF NOT EXISTS slow_ops (
    id INTEGER PRIMARY KEY,
    deployment_id INTEGER NOT NULL,
    host TEXT NOT NULL,
    ts REAL NOT NULL,
    ns TEXT NOT NULL,
    op TEXT NOT NULL,
    duration_ms REAL NOT NULL,
    plan TEXT,
    keys_examined INTEGER,
    docs_examined INTEGER,
    nreturned INTEGER,
    has_sort INTEGER,
    shape_key TEXT NOT NULL,
    shape TEXT NOT NULL,          -- JSON, literal values redacted
    app TEXT
);
CREATE INDEX IF NOT EXISTS slow_ops_dep_ts ON slow_ops(deployment_id, ts);
CREATE INDEX IF NOT EXISTS slow_ops_dep_host_ts ON slow_ops(deployment_id, host, ts);

CREATE TABLE IF NOT EXISTS index_reports (
    id INTEGER PRIMARY KEY,
    deployment_id INTEGER NOT NULL REFERENCES deployments(id) ON DELETE CASCADE,
    status TEXT NOT NULL,         -- running | done | failed
    created_at INTEGER NOT NULL,
    finished_at INTEGER,
    range_s INTEGER,
    error TEXT,
    data TEXT
);

CREATE TABLE IF NOT EXISTS exports (
    id INTEGER PRIMARY KEY,
    deployment_id INTEGER NOT NULL REFERENCES deployments(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,                 -- dump | export
    host TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    params TEXT NOT NULL DEFAULT '{}',
    command TEXT NOT NULL DEFAULT '',   -- command line with the credentials removed
    status TEXT NOT NULL,               -- running | success | failed | cancelled
    file_name TEXT,
    size INTEGER,
    log TEXT NOT NULL DEFAULT '',
    error TEXT,
    created_by TEXT,
    created_at INTEGER NOT NULL,
    finished_at INTEGER
);
CREATE INDEX IF NOT EXISTS exports_dep ON exports(deployment_id, kind, created_at);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY,
    ts INTEGER NOT NULL,
    actor TEXT,
    action TEXT NOT NULL,
    detail TEXT
);
"""


def now() -> int:
    return int(time.time())


class Database:
    def __init__(self, path: Path | str):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._migrate()

    def _migrate(self) -> None:
        """Add columns introduced after the first release to existing databases."""
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(servers)").fetchall()}
        if "kind" not in cols:
            # vagrant = VM that is often re-created: its SSH host key is not pinned
            self._conn.execute("ALTER TABLE servers ADD COLUMN kind TEXT NOT NULL DEFAULT 'standard'")
        dcols = {r[1] for r in self._conn.execute("PRAGMA table_info(deployments)").fetchall()}
        if "tags" not in dcols:
            # organization, project, cluster_type, license_key, license_type
            self._conn.execute("ALTER TABLE deployments ADD COLUMN tags TEXT NOT NULL DEFAULT '{}'")

    # -- primitives -------------------------------------------------------
    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, tuple(params))

    def executemany(self, sql: str, rows: Iterable[Iterable[Any]]) -> None:
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                self._conn.executemany(sql, rows)
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, tuple(params)).fetchall()]

    def one(self, sql: str, params: Iterable[Any] = ()) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(sql, tuple(params)).fetchone()
            return dict(row) if row else None

    def insert(self, table: str, values: dict[str, Any]) -> int:
        cols = ", ".join(values)
        marks = ", ".join("?" for _ in values)
        cur = self.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", values.values())
        return int(cur.lastrowid)

    def update(self, table: str, row_id: int, values: dict[str, Any]) -> None:
        if not values:
            return
        sets = ", ".join(f"{k} = ?" for k in values)
        self.execute(f"UPDATE {table} SET {sets} WHERE id = ?", [*values.values(), row_id])

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- helpers ---------------------------------------------------------
    def audit(self, actor: str | None, action: str, detail: Any = None) -> None:
        self.insert(
            "audit",
            {"ts": now(), "actor": actor, "action": action,
             "detail": json.dumps(detail, default=str) if detail is not None else None},
        )

    def get_meta(self, key: str) -> str | None:
        row = self.one("SELECT value FROM meta WHERE key = ?", (key,))
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                     (key, value))

    def store_metrics(self, rows: list[tuple[str, int, dict[str, Any]]]) -> None:
        if rows:
            self.executemany(
                "INSERT INTO metrics (subject, ts, data) VALUES (?, ?, ?)",
                [(s, ts, json.dumps(d)) for s, ts, d in rows],
            )

    def metric_series(self, subject: str, since: int, names: list[str] | None = None,
                      max_points: int = 400) -> dict[str, Any]:
        rows = self.query(
            "SELECT ts, data FROM metrics WHERE subject = ? AND ts >= ? ORDER BY ts", (subject, since)
        )
        # Simple downsampling: average per bucket
        step = max(1, len(rows) // max_points + (1 if len(rows) % max_points else 0)) if rows else 1
        ts_out: list[int] = []
        series: dict[str, list[float | None]] = {}
        for i in range(0, len(rows), step):
            bucket = [json.loads(r["data"]) for r in rows[i:i + step]]
            ts_out.append(rows[min(i + step, len(rows)) - 1]["ts"])
            keys = names or sorted({k for b in bucket for k in b})
            for k in keys:
                vals = [b[k] for b in bucket if isinstance(b.get(k), (int, float))]
                series.setdefault(k, [None] * (len(ts_out) - 1))
                series[k].append(sum(vals) / len(vals) if vals else None)
            for k in series:
                if len(series[k]) < len(ts_out):
                    series[k].append(None)
        return {"ts": ts_out, "series": series}

    def purge_metrics(self, older_than: int) -> int:
        cur = self.execute("DELETE FROM metrics WHERE ts < ?", (older_than,))
        self.execute("DELETE FROM slow_ops WHERE ts < ?", (older_than,))
        self.execute("DELETE FROM index_reports WHERE created_at < ? AND id NOT IN "
                     "(SELECT MAX(id) FROM index_reports GROUP BY deployment_id)", (older_than,))
        return cur.rowcount
