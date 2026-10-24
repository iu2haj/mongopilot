"""Web application: REST API, user interface and background loops."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import secrets
import time
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from . import __version__, insights, schema
from .advisor import IndexAdvisor, slow_groups
from .alerts import OPERATORS, AlertEngine, Notifier, seed_default_rules
from .automation import releases
from .automation import templates as T
from .automation import tlsgen
from .automation.jobs import (MONITOR_USER, TLS_MODES, JobManager, normalize, validate_replicaset_params,
                              validate_sharded_params)
from .automation.mongo_ops import MongoOps, admin_uri, coerce_parameter
from .automation.ssh import SshRunner, parse_ssh_config, validate_private_key
from .config import Settings
from .crypto import SecretBox
from .db import Database, now
from .metrics import CATALOG
from .monitor import Collector, HostCollector
from .tools import (AUTH_MODES, ExportManager, MongoData, ShellManager, SubprocessToolRunner, ToolError, node_uri,
                    redact)

log = logging.getLogger("mongopilot")
STATIC = Path(__file__).parent / "static"


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class Services:
    """Dependency container; every dependency can be replaced in tests."""

    def __init__(self, settings: Settings, probe=None, runner=None, ops=None, notifier=None):
        self.settings = settings
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        self.world = None
        if settings.demo:
            from .demo import DemoNotifier, DemoOps, DemoProbe, DemoRunner, DemoWorld
            self.world = DemoWorld()
            probe = probe or DemoProbe(self.world)
            runner = runner or DemoRunner(self.world)
            ops = ops or DemoOps(self.world)
            notifier = notifier or DemoNotifier()
        self.db = Database(settings.db_path)
        self.box = SecretBox.from_settings(settings.secret_key, settings.data_dir)
        if probe is None:
            from .probe import PyMongoProbe
            probe = PyMongoProbe()
        self.probe = probe
        self.runner = runner or SshRunner(settings.data_dir, settings.ssh_binary)
        self.collector = Collector(self.db, self.box, probe, settings.slow_op_seconds, settings.poll_concurrency)
        self.hosts = HostCollector(self.db, self.box, self.runner, settings.poll_concurrency)
        self.advisor = IndexAdvisor(self.db, self.box, probe)
        self.alerts = AlertEngine(self.db, self.box, settings,
                                  notifier or Notifier(self.db, self.box, settings))
        self.jobs = JobManager(self.db, self.box, settings, self.runner,
                               ops or MongoOps(), on_deployment_change=self._refresh_soon)
        self.jobs.probe = probe
        if settings.demo:
            from .demo_tools import DemoData, DemoShell, DemoToolRunner
            self.data = DemoData(self.world)
            tool_runner = DemoToolRunner(self.data, delay=0.4)
            world, data = self.world, self.data
            self.shells = ShellManager(settings, factory=lambda uri, rows, cols, **kw:
                                       DemoShell(world, data, uri, **kw))
        else:
            self.data = MongoData()
            tool_runner = SubprocessToolRunner({"mongodump": settings.mongodump_binary,
                                                "mongoexport": settings.mongoexport_binary,
                                                "mongorestore": settings.mongorestore_binary,
                                                "mongoimport": settings.mongoimport_binary})
            self.shells = ShellManager(settings)
        self.exports = ExportManager(self.db, settings, tool_runner, self.data)
        self.admin_password = settings.admin_password or self._generated_password()
        seed_default_rules(self.db)
        self.jobs.recover()
        self.db.execute("UPDATE index_reports SET status = 'failed', error = 'Interrupted by a restart' "
                        "WHERE status = 'running'")
        if settings.demo:
            from .demo import seed_demo
            seed_demo(self)

    def _generated_password(self) -> str:
        f = self.settings.data_dir / "admin-password.txt"
        if not f.exists():
            f.write_text(secrets.token_urlsafe(18))
            f.chmod(0o600)
        pw = f.read_text().strip()
        log.warning("MP_ADMIN_PASSWORD is not set: generated password stored in %s", f)
        return pw

    def _refresh_soon(self, dep_id: int) -> None:
        with contextlib.suppress(RuntimeError):
            asyncio.get_running_loop().create_task(asyncio.to_thread(self.collector.poll_deployment, dep_id))

    async def background(self) -> None:
        last_purge = 0
        while True:
            started = asyncio.get_running_loop().time()
            try:
                await asyncio.gather(self.collector.poll_all(), self.hosts.poll_all())
                await asyncio.to_thread(self.alerts.evaluate)
                if now() - last_purge > 3600:
                    deleted = await asyncio.to_thread(
                        self.db.purge_metrics, now() - self.settings.retention_days * 86400)
                    if deleted:
                        log.info("Deleted %s samples older than the retention period", deleted)
                    last_purge = now()
            except Exception:
                log.exception("Error in the monitoring loop")
            elapsed = asyncio.get_running_loop().time() - started
            await asyncio.sleep(max(1.0, self.settings.poll_interval - elapsed))


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def svc(request: Request) -> Services:
    return request.app.state.services


async def body(request: Request) -> dict[str, Any]:
    try:
        data = await request.json()
    except Exception:
        raise ApiError("Invalid JSON") from None
    if not isinstance(data, dict):
        raise ApiError("Expected a JSON object")
    return data


def need(data: dict[str, Any], *keys: str) -> None:
    missing = [k for k in keys if data.get(k) in (None, "")]
    if missing:
        raise ApiError(f"Missing required fields: {', '.join(missing)}")


def actor(request: Request) -> str:
    return getattr(request.state, "user", "admin")


def ok(data: Any = None, status: int = 200) -> JSONResponse:
    return JSONResponse({"ok": True} if data is None else data, status_code=status)


def _jsonf(value: str | None, default: Any = None) -> Any:
    return json.loads(value) if value else default


def _dep_or_404(s: Services, dep_id: int) -> dict[str, Any]:
    d = s.db.one("SELECT * FROM deployments WHERE id = ?", (dep_id,))
    if not d:
        raise ApiError("Deployment not found", 404)
    return d


# ---------------------------------------------------------------------------
# overview
# ---------------------------------------------------------------------------

async def overview(request: Request):
    s = svc(request)
    deps = s.db.query("SELECT id, name, topology, status, last_poll, last_error, paused, managed FROM deployments")
    procs = s.db.one("SELECT COUNT(*) n, SUM(up) up FROM processes") or {}
    alerts = s.db.query("SELECT severity, COUNT(*) n FROM alerts WHERE status = 'open' GROUP BY severity")
    jobs = s.db.one("SELECT COUNT(*) n FROM jobs WHERE status IN ('queued','running')")
    slow = s.db.one("SELECT COUNT(*) n FROM slow_ops WHERE ts >= ?", (time.time() - 86400,))
    return ok({
        "version": __version__,
        "demo": s.settings.demo,
        "poll_interval": s.settings.poll_interval,
        "deployments": len(deps),
        "deployments_error": sum(1 for d in deps if d["last_error"] and not d["paused"]),
        "processes": procs.get("n") or 0,
        "processes_up": procs.get("up") or 0,
        "alerts_open": {a["severity"]: a["n"] for a in alerts},
        "jobs_running": jobs["n"] if jobs else 0,
        "servers": (s.db.one("SELECT COUNT(*) n FROM servers") or {}).get("n", 0),
        "slow_queries_24h": slow["n"] if slow else 0,
    })


async def catalog(request: Request):
    return ok({"metrics": [{"name": k, "label": v[0], "unit": v[1], "scope": v[2], "group": v[3]}
                           for k, v in CATALOG.items()],
               "operators": list(OPERATORS), "versions_supported": T.SUPPORTED_MAJORS,
               "upgrade_paths": T.UPGRADE_PATHS, "cluster_types": CLUSTER_TYPES, "license_types": LICENSE_TYPES,
               "tls_modes": TLS_MODES})


async def mongodb_versions(request: Request):
    s = svc(request)
    versions = await asyncio.to_thread(releases.available_versions, s.settings.mongodb_release_index)
    return ok({"versions": versions[:60]})


# ---------------------------------------------------------------------------
# servers
# ---------------------------------------------------------------------------

def _server_out(s: Services, row: dict[str, Any], latest: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    out = {k: v for k, v in row.items() if k != "ssh_key_enc"}
    out["has_key"] = bool(row.get("ssh_key_enc"))
    out["facts"] = _jsonf(row.get("facts"), {})
    if latest is not None:
        out["metrics"] = latest.get(f"srv:{row['id']}", {})
    return out


def _server_values(s: Services, data: dict[str, Any], creating: bool) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for k in ("name", "hostname", "ssh_user"):
        if k in data and str(data[k] or "").strip():
            values[k] = str(data[k]).strip()
    if "notes" in data:
        values["notes"] = data.get("notes") or None
    if "ssh_port" in data or creating:
        try:
            values["ssh_port"] = int(data.get("ssh_port") or 22)
        except ValueError:
            raise ApiError("SSH port must be a number") from None
    if "mongo_host" in data or creating:
        values["mongo_host"] = (data.get("mongo_host") or "").strip() or None
    if "kind" in data or creating:
        kind = data.get("kind") or ("vagrant" if values.get("ssh_user") == "vagrant" else "standard")
        if kind not in ("vagrant", "standard"):
            raise ApiError("Server type must be 'vagrant' or 'standard'")
        values["kind"] = kind
    if data.get("ssh_key"):
        try:
            values["ssh_key_enc"] = s.box.encrypt(validate_private_key(data["ssh_key"]))
        except ValueError as exc:
            raise ApiError(str(exc)) from None
    elif creating:
        raise ApiError("Upload the SSH private key of the server")
    return values


async def servers_list(request: Request):
    s = svc(request)
    rows = s.db.query("SELECT * FROM servers ORDER BY name")
    latest = s.alerts.latest_values(max(300, s.settings.poll_interval * 5))
    used: dict[int, list[dict[str, Any]]] = {}
    for d in s.db.query("SELECT id, name, managed_enc FROM deployments WHERE managed = 1"):
        for m in s.box.decrypt_json(d["managed_enc"], {}).get("members", []):
            used.setdefault(m["server_id"], []).append({"deployment": d["name"], "deployment_id": d["id"],
                                                        "port": m["port"]})
    return ok([{**_server_out(s, r, latest), "processes": used.get(r["id"], [])} for r in rows])


async def servers_get(request: Request):
    s = svc(request)
    row = s.db.one("SELECT * FROM servers WHERE id = ?", (int(request.path_params["id"]),))
    if not row:
        raise ApiError("Server not found", 404)
    latest = s.alerts.latest_values(max(300, s.settings.poll_interval * 5))
    return ok(_server_out(s, row, latest))


async def servers_create(request: Request):
    s = svc(request)
    data = await body(request)
    need(data, "name", "hostname", "ssh_user")
    if s.db.one("SELECT id FROM servers WHERE name = ?", (data["name"].strip(),)):
        raise ApiError(f"A server named {data['name']} already exists")
    values = _server_values(s, data, creating=True)
    sid = s.db.insert("servers", {**values, "created_at": now()})
    s.db.audit(actor(request), "server:create", {"id": sid, "name": values["name"], "kind": values["kind"]})
    return ok({"id": sid}, 201)


async def servers_update(request: Request):
    s = svc(request)
    sid = int(request.path_params["id"])
    if not s.db.one("SELECT id FROM servers WHERE id = ?", (sid,)):
        raise ApiError("Server not found", 404)
    values = _server_values(s, await body(request), creating=False)
    s.db.update("servers", sid, values)
    s.db.audit(actor(request), "server:update", {"id": sid, "fields": [k for k in values if k != "ssh_key_enc"]})
    return ok()


async def servers_delete(request: Request):
    s = svc(request)
    sid = int(request.path_params["id"])
    for d in s.db.query("SELECT name, managed_enc FROM deployments WHERE managed = 1"):
        if any(m["server_id"] == sid for m in s.box.decrypt_json(d["managed_enc"], {}).get("members", [])):
            raise ApiError(f"The server hosts processes of deployment {d['name']}")
    s.db.execute("DELETE FROM servers WHERE id = ?", (sid,))
    s.db.execute("DELETE FROM metrics WHERE subject = ?", (f"srv:{sid}",))
    s.db.audit(actor(request), "server:delete", {"id": sid})
    return ok()


async def servers_check(request: Request):
    s = svc(request)
    sid = int(request.path_params["id"])
    srv = s.db.one("SELECT name FROM servers WHERE id = ?", (sid,))
    if not srv:
        raise ApiError("Server not found", 404)
    job_id = s.jobs.submit("server_check", f"Check server {srv['name']}", {"server_id": sid}, None, actor(request))
    return ok({"job_id": job_id}, 202)


async def servers_forget_key(request: Request):
    s = svc(request)
    srv = s.db.one("SELECT * FROM servers WHERE id = ?", (int(request.path_params["id"]),))
    if not srv:
        raise ApiError("Server not found", 404)
    if hasattr(s.runner, "forget_host_key"):
        await asyncio.to_thread(s.runner.forget_host_key, srv["hostname"], srv["ssh_port"])
    s.db.audit(actor(request), "server:forget_host_key", {"id": srv["id"]})
    return ok()


async def servers_parse_ssh_config(request: Request):
    data = await body(request)
    hosts = parse_ssh_config(data.get("text") or "")
    if not hosts:
        raise ApiError("No 'Host' entries found. Paste the full output of: vagrant ssh-config")
    return ok({"hosts": hosts})


# ---------------------------------------------------------------------------
# deployments
# ---------------------------------------------------------------------------

def _server_index(s: Services) -> dict[str, int]:
    idx: dict[str, int] = {}
    for r in s.db.query("SELECT id, hostname, mongo_host FROM servers"):
        for h in (r["mongo_host"], r["hostname"]):
            if h:
                idx[h.lower()] = r["id"]
    return idx


TAG_FIELDS = ("organization", "project", "cluster_type", "license_key", "license_type")
CLUSTER_TYPES = ["Production", "Staging", "Development"]
LICENSE_TYPES = ["At node", "At memory"]


def _tags_in(data: Any) -> dict[str, str]:
    data = data or {}
    if not isinstance(data, dict):
        raise ApiError("Tags must be an object")
    tags = {k: str(data.get(k) or "").strip()[:200] for k in TAG_FIELDS}
    if tags["cluster_type"] and tags["cluster_type"] not in CLUSTER_TYPES:
        raise ApiError(f"Cluster type must be one of: {', '.join(CLUSTER_TYPES)}")
    if tags["license_type"] and tags["license_type"] not in LICENSE_TYPES:
        raise ApiError(f"License type must be one of: {', '.join(LICENSE_TYPES)}")
    return tags


def _managed(s: Services, d: dict[str, Any]) -> dict[str, Any]:
    if not d["managed"]:
        raise ApiError("This is only available for deployments created by MongoPilot")
    return normalize(s.box.decrypt_json(d["managed_enc"], {}))


def _not_busy(s: Services, dep_id: int) -> None:
    if s.jobs.busy(dep_id):
        raise ApiError("A job is running on this deployment: wait for it to finish")


def _deployment_out(s: Services, d: dict[str, Any], detail: bool = False) -> dict[str, Any]:
    out = {k: d[k] for k in ("id", "name", "topology", "last_poll", "last_error", "paused", "created_at")}
    out["managed"] = bool(d["managed"])
    out["status"] = _jsonf(d["status"], {})
    out["tags"] = {k: "" for k in TAG_FIELDS} | (_jsonf(d.get("tags"), {}) or {})
    out["alerts_open"] = (s.db.one("SELECT COUNT(*) n FROM alerts WHERE deployment_id = ? AND status = 'open'",
                                   (d["id"],)) or {}).get("n", 0)
    procs = s.db.query("SELECT * FROM processes WHERE deployment_id = ? ORDER BY shard, host", (d["id"],))
    out["processes_total"] = len(procs)
    out["processes_up"] = sum(p["up"] for p in procs)
    if d["managed"] and d["managed_enc"]:
        m = normalize(s.box.decrypt_json(d["managed_enc"], {}))
        out["tls_mode"] = (m.get("tls") or {}).get("mode", "disabled")
    if detail:
        latest = s.alerts.latest_values(max(300, s.settings.poll_interval * 5))
        servers = _server_index(s)
        out["processes"] = [{**{k: v for k, v in p.items() if k != "info"}, "info": _jsonf(p["info"], {}),
                             "metrics": latest.get(f"proc:{p['id']}", {}),
                             "server_id": servers.get(p["host"].rsplit(":", 1)[0].lower())} for p in procs]
        out["metrics"] = latest.get(f"dep:{d['id']}", {})
        if d["managed"] and d["managed_enc"]:
            m = normalize(s.box.decrypt_json(d["managed_enc"], {}))
            names = {r["id"]: r["name"] for r in s.db.query("SELECT id, name FROM servers")}
            out["managed_info"] = {
                "kind": m.get("kind"), "rs_name": m.get("rs_name"), "config_rs": m.get("config_rs"),
                "version": m.get("version"), "admin_user": m.get("admin_user"),
                "bind_ip": m.get("bind_ip"), "data_root": m.get("data_root"),
                "fcv_options": T.fcv_options(T.major(m["version"])) if m.get("version") else [],
                "upgrade_to": T.UPGRADE_PATHS.get(T.major(m["version"]), []) if m.get("version") else [],
                "members": [{"host": x["host"], "server": names.get(x["server_id"], "?"), "server_id": x["server_id"],
                             "port": x["port"], "role": x["role"], "rs_name": x.get("rs_name"), "shard": x.get("shard"),
                             "priority": x.get("priority"), "votes": x.get("votes"), "arbiter": x.get("arbiter"),
                             "hidden": x.get("hidden"), "delay": x.get("delay", 0), "version": x.get("version"),
                             "unit": x["paths"]["unit"], "dbpath": x["paths"].get("dbpath"),
                             "customized": bool(x.get("config"))} for x in m.get("members", [])],
            }
        out["jobs"] = s.db.query("SELECT id, type, title, status, created_at, finished_at, error FROM jobs "
                                 "WHERE deployment_id = ? ORDER BY id DESC LIMIT 10", (d["id"],))
    return out


async def deployments_list(request: Request):
    s = svc(request)
    return ok([_deployment_out(s, d) for d in s.db.query("SELECT * FROM deployments ORDER BY name")])


async def deployments_get(request: Request):
    s = svc(request)
    return ok(_deployment_out(s, _dep_or_404(s, int(request.path_params["id"])), detail=True))


def _check_uri(uri: str) -> None:
    if not uri.startswith(("mongodb://", "mongodb+srv://")):
        raise ApiError("The connection string must start with mongodb:// or mongodb+srv://")


async def deployments_create(request: Request):
    """Register an existing deployment (monitoring only)."""
    s = svc(request)
    data = await body(request)
    need(data, "name", "uri")
    _check_uri(data["uri"])
    if s.db.one("SELECT id FROM deployments WHERE name = ?", (data["name"],)):
        raise ApiError("A deployment with this name already exists")
    tags = _tags_in(data.get("tags"))
    dep_id = s.db.insert("deployments", {"name": data["name"].strip(), "uri_enc": s.box.encrypt(data["uri"].strip()),
                                         "managed": 0, "tags": json.dumps(tags), "created_at": now()})
    s.db.audit(actor(request), "deployment:register", {"id": dep_id, "name": data["name"]})
    status = None
    try:
        status = await asyncio.to_thread(s.collector.poll_deployment, dep_id)
    except Exception as exc:
        log.warning("First sample failed: %s", exc)
    d = s.db.one("SELECT last_error FROM deployments WHERE id = ?", (dep_id,))
    return ok({"id": dep_id, "status": status, "error": d["last_error"] if d else None}, 201)


async def deployments_update(request: Request):
    s = svc(request)
    dep_id = int(request.path_params["id"])
    d = _dep_or_404(s, dep_id)
    data = await body(request)
    values: dict[str, Any] = {}
    if data.get("name"):
        name = data["name"].strip()
        if name != d["name"] and s.db.one("SELECT id FROM deployments WHERE name = ?", (name,)):
            raise ApiError("A deployment with this name already exists")
        values["name"] = name
    if "paused" in data:
        values["paused"] = 1 if data["paused"] else 0
    if "tags" in data:
        values["tags"] = json.dumps(_tags_in(data["tags"]))
    if data.get("uri"):
        _check_uri(data["uri"])
        if hasattr(s.probe, "forget"):
            s.probe.forget(s.box.decrypt(d["uri_enc"]))
        values["uri_enc"] = s.box.encrypt(data["uri"].strip())
    s.db.update("deployments", dep_id, values)
    s.db.audit(actor(request), "deployment:update", {"id": dep_id, "fields": [k for k in values if k != "uri_enc"]})
    return ok()


async def deployments_delete(request: Request):
    s = svc(request)
    dep_id = int(request.path_params["id"])
    d = _dep_or_404(s, dep_id)
    _not_busy(s, dep_id)
    procs = [p["id"] for p in s.db.query("SELECT id FROM processes WHERE deployment_id = ?", (dep_id,))]
    s.db.execute("DELETE FROM deployments WHERE id = ?", (dep_id,))
    subjects = [f"dep:{dep_id}"] + [f"proc:{p}" for p in procs]
    s.db.execute(f"DELETE FROM metrics WHERE subject IN ({','.join('?' * len(subjects))})", subjects)
    s.db.execute("DELETE FROM alerts WHERE deployment_id = ?", (dep_id,))
    s.db.execute("DELETE FROM slow_ops WHERE deployment_id = ?", (dep_id,))
    if hasattr(s.probe, "forget"):
        s.probe.forget(s.box.decrypt(d["uri_enc"]))
    s.collector.forget_deployment(dep_id)
    s.db.audit(actor(request), "deployment:delete", {"id": dep_id})
    return ok()


async def deployments_poll(request: Request):
    s = svc(request)
    dep_id = int(request.path_params["id"])
    status = await asyncio.to_thread(s.collector.poll_deployment, dep_id)
    await asyncio.to_thread(s.alerts.evaluate)
    return ok({"status": status})


def _port_conflicts(s: Services, specs: list[dict[str, Any]]) -> None:
    for d in s.db.query("SELECT name, managed_enc FROM deployments WHERE managed = 1"):
        for m in s.box.decrypt_json(d["managed_enc"], {}).get("members", []):
            for new in specs:
                if m["server_id"] == int(new["server_id"]) and m["port"] == int(new["port"]):
                    raise ApiError(f"Port {m['port']} is already used by {d['name']} on the same server")


def _host_of(servers: dict[int, dict[str, Any]], spec: dict[str, Any]) -> str:
    srv = servers[int(spec["server_id"])]
    return f"{srv['mongo_host'] or srv['hostname']}:{int(spec['port'])}"


async def _create_managed(request: Request, s: Services, data: dict[str, Any], job: str, topology: str,
                          monitor_hosts: list[str], rs_name: str | None, title: str, warnings: list[str]):
    data["cache_gb"] = None if data.get("cache_gb") in ("", None) else float(data["cache_gb"])
    data["monitor_password"] = secrets.token_urlsafe(24)
    uri = admin_uri(MONITOR_USER, data["monitor_password"], monitor_hosts, rs_name)
    dep_id = s.db.insert("deployments", {"name": data["name"].strip(), "uri_enc": s.box.encrypt(uri), "managed": 1,
                                         "managed_enc": s.box.encrypt_json({}), "topology": topology,
                                         "tags": json.dumps(_tags_in(data.get("tags"))),
                                         "paused": 1, "last_error": "Being created", "created_at": now()})
    data["deployment_id"] = dep_id
    try:
        job_id = s.jobs.submit(job, title, data, dep_id, actor(request))
    except ValueError as exc:
        s.db.execute("DELETE FROM deployments WHERE id = ?", (dep_id,))
        raise ApiError(str(exc)) from None
    return ok({"id": dep_id, "job_id": job_id, "warnings": warnings}, 202)


async def deployments_create_replicaset(request: Request):
    """Create a new managed replica set on the given servers."""
    s = svc(request)
    data = await body(request)
    need(data, "name", "version", "members", "admin_user", "admin_password")
    if s.db.one("SELECT id FROM deployments WHERE name = ?", (data["name"],)):
        raise ApiError("A deployment with this name already exists")
    data["rs_name"] = (data.get("rs_name") or T.slug(data["name"])).strip()
    _tags_in(data.get("tags"))
    try:
        warnings = validate_replicaset_params(s.db, data)
    except ValueError as exc:
        raise ApiError(str(exc)) from None
    _port_conflicts(s, data["members"])
    servers = {r["id"]: r for r in s.db.query("SELECT * FROM servers")}
    for m in data["members"]:
        m["server_id"] = int(m["server_id"])
    hosts = [_host_of(servers, m) for m in data["members"] if not m.get("arbiter")]
    return await _create_managed(request, s, data, "create_replicaset", "replicaset", hosts, data["rs_name"],
                                 f"Create replica set {data['name']}", warnings)


async def deployments_create_sharded(request: Request):
    """Create a new managed sharded cluster: config server replica set, shards and mongos routers."""
    s = svc(request)
    data = await body(request)
    need(data, "name", "version", "config", "shards", "mongos", "admin_user", "admin_password")
    if s.db.one("SELECT id FROM deployments WHERE name = ?", (data["name"],)):
        raise ApiError("A deployment with this name already exists")
    _tags_in(data.get("tags"))
    try:
        warnings = validate_sharded_params(s.db, data)
    except (ValueError, KeyError, TypeError) as exc:
        raise ApiError(str(exc)) from None
    specs = data["config"]["members"] + [m for sh in data["shards"] for m in sh["members"]] + data["mongos"]
    for m in specs:
        m["server_id"] = int(m["server_id"])
    _port_conflicts(s, specs)
    servers = {r["id"]: r for r in s.db.query("SELECT * FROM servers")}
    hosts = [_host_of(servers, m) for m in data["mongos"]]
    return await _create_managed(request, s, data, "create_sharded", "sharded", hosts, None,
                                 f"Create sharded cluster {data['name']}", warnings)


ACTION_TITLES = {
    "rolling_restart": "Rolling restart",
    "rolling_upgrade": "Rolling upgrade to {version}",
    "set_fcv": "Set FCV to {fcv}",
    "process_action": "{action} {host}",
}


async def deployments_action(request: Request):
    s = svc(request)
    dep_id = int(request.path_params["id"])
    d = _dep_or_404(s, dep_id)
    _managed(s, d)
    data = await body(request)
    kind = data.get("type")
    if kind not in ACTION_TITLES:
        raise ApiError("Unknown action")
    if kind == "rolling_upgrade":
        need(data, "version")
        try:
            T.validate_version(data["version"])
        except ValueError as exc:
            raise ApiError(str(exc)) from None
    if kind == "set_fcv":
        need(data, "fcv")
    if kind == "process_action":
        need(data, "host", "action")
    params = {**data, "deployment_id": dep_id}
    title = ACTION_TITLES[kind].format(**{k: data.get(k, "") for k in ("version", "fcv", "action", "host")})
    title = f"{title[0].upper()}{title[1:]} · {d['name']}"
    try:
        job_id = s.jobs.submit(kind, title, params, dep_id, actor(request))
    except ValueError as exc:
        raise ApiError(str(exc)) from None
    return ok({"job_id": job_id}, 202)


# ---------------------------------------------------------------------------
# server parameters
# ---------------------------------------------------------------------------

READ_ONLY_PARAMETERS = {"featureCompatibilityVersion", "tlsMode", "authSchemaVersion"}


def _hosts_of(s: Services, dep_id: int) -> list[dict[str, Any]]:
    return s.db.query("SELECT host, role, shard, up FROM processes WHERE deployment_id = ? ORDER BY shard, host",
                      (dep_id,))


async def parameters_get(request: Request):
    s = svc(request)
    dep_id = int(request.path_params["id"])
    d = _dep_or_404(s, dep_id)
    procs = _hosts_of(s, dep_id)
    host = request.query_params.get("host") or next((p["host"] for p in procs if p["up"] and p["role"] != "ARBITER"),
                                                    None)
    if not host:
        raise ApiError("No reachable process to read the parameters from")
    if not hasattr(s.probe, "get_parameters"):
        raise ApiError("Parameters are not available")
    try:
        params = await asyncio.to_thread(s.probe.get_parameters, s.box.decrypt(d["uri_enc"]), host)
    except Exception as exc:
        raise ApiError(f"Cannot read the parameters of {host}: {exc}") from None
    persisted: dict[str, Any] = {}
    if d["managed"]:
        m = _managed(s, d)
        member = next((x for x in m["members"] if x["host"] == host), None)
        if member:
            persisted = T.member_config(m, member).get("setParameter") or {}
    rows = [{"name": k, **v, "persisted": k in persisted, "persisted_value": persisted.get(k),
             "read_only": k in READ_ONLY_PARAMETERS} for k, v in sorted(params.items(), key=lambda kv: kv[0].lower())]
    return ok({"host": host, "processes": procs, "parameters": rows, "managed": bool(d["managed"])})


async def parameters_set(request: Request):
    """Set a parameter at runtime on some processes and optionally persist it in their configuration files."""
    s = svc(request)
    dep_id = int(request.path_params["id"])
    d = _dep_or_404(s, dep_id)
    managed = _managed(s, d)
    _not_busy(s, dep_id)
    data = await body(request)
    need(data, "name", "hosts")
    name = data["name"]
    if name in READ_ONLY_PARAMETERS:
        raise ApiError(f"{name} is managed elsewhere in MongoPilot (FCV action or TLS settings)")
    members = [m for m in managed["members"] if m["host"] in set(data["hosts"])]
    if not members:
        raise ApiError("Select at least one process of this deployment")
    try:
        current = await asyncio.to_thread(s.probe.get_parameters, s.box.decrypt(d["uri_enc"]), members[0]["host"])
    except Exception as exc:
        raise ApiError(f"Cannot read the current value: {exc}") from None
    if name not in current:
        raise ApiError(f"Unknown parameter on {members[0]['host']}: {name}")
    try:
        value = coerce_parameter(current[name]["value"], data.get("value"))
    except ValueError as exc:
        raise ApiError(f"{name}: {exc}") from None
    runtime = current[name].get("runtime")
    results = []
    if runtime is not False:
        for m in members:
            try:
                await asyncio.to_thread(s.jobs.ops.set_parameter, s.jobs.direct(dep_id, managed, m["host"]), name, value)
                results.append({"host": m["host"], "ok": True})
            except Exception as exc:
                results.append({"host": m["host"], "ok": False, "error": str(exc)[:300]})
    job_id = None
    if data.get("persist") or runtime is False:
        for m in members:
            cfg = T.member_config(managed, m)
            cfg.setdefault("setParameter", {})[name] = value
            m["config"] = cfg
        s.jobs.save_managed(dep_id, managed)
        restart = bool(data.get("restart"))
        job_id = s.jobs.submit("apply_config", f"Persist parameter {name} · {d['name']}",
                               {"deployment_id": dep_id, "hosts": [m["host"] for m in members], "restart": restart,
                                "reason": f"setParameter {name}: {json.dumps(value)}"}, dep_id, actor(request))
    s.db.audit(actor(request), "parameter:set", {"deployment_id": dep_id, "name": name, "value": value,
                                                 "hosts": [m["host"] for m in members]})
    return ok({"value": value, "runtime": runtime, "results": results, "job_id": job_id})


# ---------------------------------------------------------------------------
# configuration files
# ---------------------------------------------------------------------------

async def config_get(request: Request):
    s = svc(request)
    dep_id = int(request.path_params["id"])
    managed = _managed(s, _dep_or_404(s, dep_id))
    out = []
    for m in managed["members"]:
        cfg = T.member_config(managed, m)
        out.append({"host": m["host"], "role": m["role"], "rs_name": m.get("rs_name"), "shard": m.get("shard"),
                    "binary": T.binary_of(m), "path": m["paths"]["conf"], "yaml": T.render_config(cfg),
                    "customized": bool(m.get("config")) and cfg != T.build_config(managed, m)})
    return ok({"members": out, "protected": T.PROTECTED_KEYS})


async def config_remote(request: Request):
    """Read the file currently on the server, to spot manual changes."""
    s = svc(request)
    dep_id = int(request.path_params["id"])
    managed = _managed(s, _dep_or_404(s, dep_id))
    host = request.query_params.get("host")
    m = next((x for x in managed["members"] if x["host"] == host), None)
    if not m:
        raise ApiError("Unknown process")
    srv = s.db.one("SELECT * FROM servers WHERE id = ?", (m["server_id"],))
    from .automation.ssh import target_for
    res = await s.runner.run(target_for(srv, s.box), T.read_file_script(m["paths"]["conf"]), timeout=30)
    if not res.ok:
        raise ApiError(f"Cannot read {m['paths']['conf']} on {srv['name']}: {(res.stderr or res.stdout).strip()[:300]}")
    expected = T.render_config(T.member_config(managed, m))
    return ok({"host": host, "content": res.stdout, "matches": res.stdout.strip() == expected.strip()})


async def config_update(request: Request):
    s = svc(request)
    dep_id = int(request.path_params["id"])
    d = _dep_or_404(s, dep_id)
    managed = _managed(s, d)
    _not_busy(s, dep_id)
    data = await body(request)
    need(data, "host", "yaml")
    m = next((x for x in managed["members"] if x["host"] == data["host"]), None)
    if not m:
        raise ApiError("Unknown process")
    old = T.member_config(managed, m)
    try:
        new = T.parse_config(data["yaml"])
        T.check_protected(old, new)
    except ValueError as exc:
        raise ApiError(str(exc)) from None
    sets, deletes = T.config_changes(old, new)
    if not sets and not deletes:
        raise ApiError("No changes to apply")
    targets = [m]
    if data.get("scope") == "type":
        targets = [x for x in managed["members"] if T.binary_of(x) == T.binary_of(m)]
    for x in targets:
        x["config"] = new if x is m else T.apply_changes(T.member_config(managed, x), sets, deletes)
    s.jobs.save_managed(dep_id, managed)
    changed = ", ".join(list(sets)[:6] + [f"-{k}" for k in deletes[:3]])
    job_id = s.jobs.submit("apply_config", f"Apply configuration ({len(targets)} process"
                           f"{'es' if len(targets) > 1 else ''}) · {d['name']}",
                           {"deployment_id": dep_id, "hosts": [x["host"] for x in targets],
                            "restart": bool(data.get("restart")), "reason": f"Changed: {changed}"}, dep_id, actor(request))
    s.db.audit(actor(request), "config:update", {"deployment_id": dep_id, "hosts": [x["host"] for x in targets],
                                                 "set": list(sets), "deleted": deletes})
    return ok({"job_id": job_id, "changes": {"set": sets, "deleted": deletes}, "hosts": [x["host"] for x in targets]})


# ---------------------------------------------------------------------------
# TLS
# ---------------------------------------------------------------------------

async def tls_get(request: Request):
    s = svc(request)
    dep_id = int(request.path_params["id"])
    managed = _managed(s, _dep_or_404(s, dep_id))
    tls = managed.get("tls") or {}
    servers = {r["id"]: r for r in s.db.query("SELECT * FROM servers")}
    certs = []
    for sid in sorted({m["server_id"] for m in managed["members"]}):
        srv = servers.get(sid) or {}
        info: dict[str, Any] = {"server_id": sid, "server": srv.get("name"), "host": srv.get("mongo_host") or srv.get("hostname")}
        pem = (tls.get("pems") or {}).get(str(sid))
        if pem:
            try:
                info.update(tlsgen.inspect_server_pem(pem, tls.get("ca_pem", ""), [info["host"]]))
            except ValueError as exc:
                info["error"] = str(exc)
        certs.append(info)
    ca = None
    if tls.get("ca_pem"):
        try:
            ca = tlsgen.inspect_ca(tls["ca_pem"])
        except ValueError:
            ca = None
    return ok({"mode": tls.get("mode", "disabled"), "client": bool(tls.get("client")), "generated": bool(tls.get("generated")),
               "allow_invalid_hostnames": bool(tls.get("allow_invalid_hostnames")), "ca": ca, "certificates": certs,
               "ca_pem": tls.get("ca_pem"), "modes": TLS_MODES})


async def tls_set(request: Request):
    s = svc(request)
    dep_id = int(request.path_params["id"])
    d = _dep_or_404(s, dep_id)
    managed = _managed(s, d)
    _not_busy(s, dep_id)
    data = await body(request)
    mode = data.get("mode")
    if mode not in TLS_MODES:
        raise ApiError(f"TLS mode must be one of: {', '.join(TLS_MODES)}")
    source = data.get("source") or "keep"
    allow_invalid = bool(data.get("allow_invalid_hostnames"))
    servers = {r["id"]: r for r in s.db.query("SELECT * FROM servers")}
    server_ids = sorted({m["server_id"] for m in managed["members"]})
    material = None
    if mode != "disabled" and source == "generate":
        ca_pem, ca_key = tlsgen.generate_ca(d["name"])
        pems = {}
        for sid in server_ids:
            srv = servers[sid]
            names = [srv["mongo_host"] or srv["hostname"], srv["hostname"], srv["name"]]
            pems[str(sid)] = tlsgen.issue_server_cert(ca_pem, ca_key, names)
        material = {"ca_pem": ca_pem, "ca_key_pem": ca_key, "generated": True, "pems": pems}
    elif mode != "disabled" and source == "upload":
        ca_pem = data.get("ca_pem") or ""
        try:
            tlsgen.inspect_ca(ca_pem)
        except ValueError as exc:
            raise ApiError(str(exc)) from None
        pems = {}
        for sid in server_ids:
            srv = servers[sid]
            pem = (data.get("pems") or {}).get(str(sid)) or ""
            try:
                info = tlsgen.inspect_server_pem(pem, ca_pem, [srv["mongo_host"] or srv["hostname"]])
            except ValueError as exc:
                raise ApiError(f"{srv['name']}: {exc}") from None
            if info["missing_names"] and not allow_invalid:
                raise ApiError(f"{srv['name']}: the certificate does not cover {', '.join(info['missing_names'])}. "
                               "Add it to the Subject Alternative Names or allow invalid host names")
            pems[str(sid)] = pem
        material = {"ca_pem": ca_pem, "generated": False, "pems": pems}
    elif mode != "disabled" and not (managed.get("tls") or {}).get("ca_pem"):
        raise ApiError("Generate or upload certificates to enable TLS")
    params = {"deployment_id": dep_id, "mode": mode, "allow_invalid_hostnames": allow_invalid}
    if material:
        params["material"] = material
    try:
        job_id = s.jobs.submit("configure_tls", f"TLS {mode} · {d['name']}", params, dep_id, actor(request))
    except ValueError as exc:
        raise ApiError(str(exc)) from None
    return ok({"job_id": job_id}, 202)


# ---------------------------------------------------------------------------
# tools: mongosh, mongodump, mongoexport, schema analyzer
# ---------------------------------------------------------------------------

def _tool_target(s: Services, dep_id: int, data: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], str]:
    """Deployment, process and connection string (direct connection to the chosen node) for a tool."""
    d = _dep_or_404(s, dep_id)
    host = data.get("host")
    proc = s.db.one("SELECT * FROM processes WHERE deployment_id = ? AND host = ?", (dep_id, host))
    if not proc:
        raise ApiError("Choose a node of this deployment")
    if proc["role"] == "ARBITER":
        raise ApiError("Arbiters hold no data: choose another node")
    auth = data.get("auth") or {}
    mode = auth.get("mode") or ("admin" if d["managed"] else "monitor")
    if mode not in AUTH_MODES:
        raise ApiError(f"Authentication must be one of: {', '.join(AUTH_MODES)}")
    if mode == "admin":
        managed = _managed(s, d)
        uri = s.jobs.direct(dep_id, managed, host)
    elif mode == "custom":
        user = (auth.get("username") or "").strip()
        if not user:
            raise ApiError("Enter the user name")
        uri = node_uri(s.box.decrypt(d["uri_enc"]), host, (user, auth.get("password") or "",
                                                            (auth.get("auth_source") or "admin").strip()))
    else:
        uri = node_uri(s.box.decrypt(d["uri_enc"]), host)
    return d, proc, uri


async def tools_info(request: Request):
    s = svc(request)
    dep_id = int(request.path_params["id"])
    d = _dep_or_404(s, dep_id)
    procs = s.db.query("SELECT id, host, role, shard, rs_name, up FROM processes WHERE deployment_id = ? "
                       "ORDER BY shard, host", (dep_id,))
    runner = s.exports.runner
    return ok({"processes": procs, "managed": bool(d["managed"]), "auth_modes": list(AUTH_MODES),
               "default_auth": "admin" if d["managed"] else "monitor", "demo": s.settings.demo,
               "max_sample": s.settings.schema_max_sample,
               "max_upload_mb": s.settings.max_upload_mb,
               "available": {"mongosh": s.shells.available(),
                             **{t: runner.available(t) for t in ("mongodump", "mongoexport", "mongorestore",
                                                                 "mongoimport")}}})


def _tool_error(exc: Exception) -> ApiError:
    text = redact(str(exc))
    if "Authentication failed" in text or "auth failed" in text.lower():
        text = "Authentication failed: check the user name, password and authentication database"
    elif "not authorized" in text or "Unauthorized" in text:
        text = f"The user is not authorized: {text[:300]}"
    return ApiError(text[:500])


async def tools_namespaces(request: Request):
    s = svc(request)
    data = await body(request)
    _d, _p, uri = _tool_target(s, int(request.path_params["id"]), data)
    try:
        return ok({"databases": await asyncio.to_thread(s.data.namespaces, uri)})
    except (ToolError, ApiError):
        raise
    except Exception as exc:   # noqa: BLE001
        raise _tool_error(exc) from None


async def tools_schema(request: Request):
    s = svc(request)
    data = await body(request)
    need(data, "db", "collection")
    d, _p, uri = _tool_target(s, int(request.path_params["id"]), data)
    try:
        size = int(data.get("size") or 1000)
    except (TypeError, ValueError):
        raise ApiError("The sample size must be a number") from None
    size = max(1, min(size, s.settings.schema_max_sample))
    started = time.time()
    try:
        docs, total = await asyncio.to_thread(s.data.sample, uri, data["db"], data["collection"],
                                              data.get("filter"), size)
        result = await asyncio.to_thread(schema.analyze, docs)
    except (ToolError, ApiError):
        raise
    except ValueError as exc:
        raise ApiError(f"Invalid filter: {exc}") from None
    except Exception as exc:   # noqa: BLE001
        raise _tool_error(exc) from None
    result.update({"total": total, "requested": size, "namespace": f"{data['db']}.{data['collection']}",
                   "host": data["host"], "took_ms": int((time.time() - started) * 1000)})
    s.db.audit(actor(request), "tools:schema", {"deployment_id": d["id"], "host": data["host"],
                                                "namespace": result["namespace"], "sampled": result["sampled"]})
    return ok(result)


async def shell_open(request: Request):
    s = svc(request)
    data = await body(request)
    d, _p, uri = _tool_target(s, int(request.path_params["id"]), data)
    rows, cols = int(data.get("rows") or 30), int(data.get("cols") or 120)
    sess = await asyncio.to_thread(s.shells.open, uri, data["host"], rows, cols, dep_id=d["id"],
                                   actor=actor(request), description=(data.get("auth") or {}).get("mode", ""))
    s.db.audit(actor(request), "tools:mongosh", {"deployment_id": d["id"], "host": data["host"]})
    return ok({"id": sess.id, "host": sess.host})


async def shell_input(request: Request):
    s = svc(request)
    data = await body(request)
    s.shells.get(request.path_params["sid"]).write(str(data.get("data") or ""))
    return ok()


async def shell_output(request: Request):
    s = svc(request)
    sess = s.shells.get(request.path_params["sid"])
    try:
        cursor = int(request.query_params.get("cursor", 0))
    except ValueError:
        cursor = 0
    deadline = time.time() + min(float(request.query_params.get("wait", 25)), 30)
    while True:
        text, end = sess.read(cursor)
        if text or sess.closed or time.time() >= deadline:
            return ok({"data": text, "cursor": end, "closed": sess.closed})
        await asyncio.sleep(0.04)


async def shell_resize(request: Request):
    s = svc(request)
    data = await body(request)
    s.shells.get(request.path_params["sid"]).resize(int(data.get("rows") or 30), int(data.get("cols") or 120))
    return ok()


async def shell_close(request: Request):
    svc(request).shells.close(request.path_params["sid"])
    return ok()


TOOL_OF = {"dump": "mongodump", "export": "mongoexport", "restore": "mongorestore", "import": "mongoimport"}


async def uploads_create(request: Request):
    """Upload a file (raw request body) to restore or import it later."""
    s = svc(request)
    d = _dep_or_404(s, int(request.path_params["id"]))
    name = request.query_params.get("name") or request.headers.get("x-filename") or ""
    row_id = await s.exports.save_upload(d, name, request.stream(), request.query_params.get("description") or "",
                                         actor(request), s.settings.max_upload_mb * 1048576)
    s.db.audit(actor(request), "tools:upload", {"deployment_id": d["id"], "export_id": row_id, "name": name})
    return ok({"id": row_id}, 201)


async def tool_sources(request: Request):
    """Files that can be restored (mongodump archives) or imported (JSON, CSV, TSV), from every deployment."""
    s = svc(request)
    kind = request.query_params.get("kind")
    if kind not in ("restore", "import"):
        raise ApiError("kind must be restore or import")
    rows = s.db.query("SELECT e.*, d.name AS deployment FROM exports e JOIN deployments d ON d.id = e.deployment_id "
                      "WHERE e.status = 'success' AND e.file_name IS NOT NULL AND e.kind IN (?, 'upload') "
                      "ORDER BY e.id DESC", ("dump" if kind == "restore" else "export",))
    from .tools import import_type, restorable
    fit = restorable if kind == "restore" else import_type
    out = [{**_export_out(r), "format": import_type(r["file_name"])} for r in rows if fit(r["file_name"])
           and s.exports.path(r).exists()]
    return ok({"files": out})


def _export_out(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    out["params"] = _jsonf(row["params"], {})
    return out


async def exports_create(request: Request):
    s = svc(request)
    data = await body(request)
    kind = data.get("kind")
    if kind not in TOOL_OF:
        raise ApiError("kind must be dump, export, restore or import")
    d, proc, uri = _tool_target(s, int(request.path_params["id"]), data)
    params = data.get("params") or {}
    if not isinstance(params, dict):
        raise ApiError("params must be an object")
    try:
        row_id = s.exports.start(kind, d, data["host"], proc["role"], uri, params, data.get("description") or "",
                                 actor(request))
    except ToolError as exc:
        raise ApiError(str(exc)) from None
    s.db.audit(actor(request), f"tools:{TOOL_OF[kind]}",
               {"deployment_id": d["id"], "host": data["host"], "export_id": row_id, "params": params})
    return ok({"id": row_id}, 202)


async def exports_list(request: Request):
    s = svc(request)
    dep_id = int(request.path_params["id"])
    _dep_or_404(s, dep_id)
    kind = request.query_params.get("kind")
    sql = "SELECT * FROM exports WHERE deployment_id = ?" + (" AND kind = ?" if kind else "") + " ORDER BY id DESC"
    rows = s.db.query(sql, (dep_id, kind) if kind else (dep_id,))
    usage = sum(r["size"] or 0 for r in s.db.query("SELECT size FROM exports"))
    return ok({"exports": [_export_out(r) for r in rows], "storage_bytes": usage, "folder": str(s.exports.dir)})


def _export_or_404(s: Services, eid: int) -> dict[str, Any]:
    row = s.db.one("SELECT * FROM exports WHERE id = ?", (eid,))
    if not row:
        raise ApiError("Not found", 404)
    return row


async def export_get(request: Request):
    s = svc(request)
    return ok(_export_out(_export_or_404(s, int(request.path_params["id"]))))


async def export_download(request: Request):
    s = svc(request)
    row = _export_or_404(s, int(request.path_params["id"]))
    path = s.exports.path(row)
    if row["status"] != "success" or not path or not path.exists():
        raise ApiError("The file is not available", 404)
    s.db.audit(actor(request), "tools:download", {"export_id": row["id"], "file": row["file_name"]})
    return FileResponse(path, filename=row["file_name"], media_type="application/octet-stream")


async def export_delete(request: Request):
    s = svc(request)
    row = _export_or_404(s, int(request.path_params["id"]))
    s.exports.delete(row)
    s.db.audit(actor(request), "tools:delete", {"export_id": row["id"], "file": row["file_name"]})
    return ok()


# ---------------------------------------------------------------------------
# metrics
# ---------------------------------------------------------------------------

async def metrics_series(request: Request):
    s = svc(request)
    subject = request.query_params.get("subject", "")
    if not subject.startswith(("proc:", "dep:", "srv:")):
        raise ApiError("Invalid subject parameter")
    rng = min(int(request.query_params.get("range", 3600)), s.settings.retention_days * 86400)
    names = [n for n in request.query_params.get("names", "").split(",") if n] or None
    data = await asyncio.to_thread(s.db.metric_series, subject, now() - rng, names)
    return ok(data)


# ---------------------------------------------------------------------------
# query performance
# ---------------------------------------------------------------------------

def _report_inventory(report: dict[str, Any] | None) -> dict[str, Any] | None:
    """Rebuild a minimal index inventory from the last advisor report."""
    if not report or not report.get("data"):
        return None
    return {"collections": {c["ns"]: {"indexes": [{"name": ix["name"], "key": ix["key"]} for ix in c["indexes"]]}
                            for c in report["data"].get("collections", [])}}


async def insights_slow(request: Request):
    s = svc(request)
    dep_id = int(request.path_params["id"])
    _dep_or_404(s, dep_id)
    rng = min(int(request.query_params.get("range", 86400)), s.settings.retention_days * 86400)
    ns = request.query_params.get("ns") or None
    groups = await asyncio.to_thread(slow_groups, s.db, dep_id, rng, ns)
    report = s.advisor.latest(dep_id)
    inventory = _report_inventory(report)
    collections = (inventory or {}).get("collections", {})
    for g in groups:
        g["reasons"] = insights.needs_index(g)
        shape = json.loads(g["shape"])
        g["shape"] = shape
        g["suggestions"] = []
        if g["reasons"]:
            sug = insights.suggest_keys(shape.get("filter") or {}, shape.get("sort") or {})
            g["note"] = sug["note"]
            for item in sug["keys"]:
                existing = next((ix["name"] for ix in collections.get(g["ns"], {}).get("indexes", [])
                                 if insights.index_supports(ix["key"], item["key"], item["n_eq"])), None)
                g["suggestions"].append({
                    "key_js": insights.key_to_js(item["key"]), "existing": existing,
                    "command": f"{insights.coll_ref(g['ns'])}.createIndex({insights.key_to_js(item['key'])})"})
    totals = {
        "operations": sum(g["count"] for g in groups),
        "shapes": len(groups),
        "collscans": sum(g["count"] for g in groups if "COLLSCAN" in g["plans"]),
        "needing_index": sum(1 for g in groups if g["reasons"]),
        "namespaces": sorted({g["ns"] for g in groups}),
        "checked_against_indexes": inventory is not None,
    }
    status = _jsonf(_dep_or_404(s, dep_id)["status"], {})
    return ok({"groups": groups[:200], "totals": totals, "log_errors": status.get("slow_log_errors", [])})


async def insights_analyze(request: Request):
    s = svc(request)
    dep_id = int(request.path_params["id"])
    d = _dep_or_404(s, dep_id)
    if d["paused"]:
        raise ApiError("Monitoring of this deployment is paused")
    data = await body(request)
    rng = min(int(data.get("range") or 7 * 86400), s.settings.retention_days * 86400)
    try:
        report_id = s.advisor.start(dep_id, rng)
    except ValueError as exc:
        raise ApiError(str(exc)) from None
    asyncio.get_running_loop().create_task(asyncio.to_thread(s.advisor.run, report_id))
    s.db.audit(actor(request), "insights:analyze", {"deployment_id": dep_id})
    return ok({"report_id": report_id}, 202)


async def insights_report(request: Request):
    s = svc(request)
    dep_id = int(request.path_params["id"])
    _dep_or_404(s, dep_id)
    return ok({"report": s.advisor.latest(dep_id)})


# ---------------------------------------------------------------------------
# alerts
# ---------------------------------------------------------------------------

def _rule_in(data: dict[str, Any]) -> dict[str, Any]:
    need(data, "name", "metric", "operator")
    if data["metric"] not in CATALOG:
        raise ApiError("Unknown metric")
    if data["operator"] not in OPERATORS:
        raise ApiError("Invalid operator")
    if data.get("severity", "warning") not in ("critical", "warning", "info"):
        raise ApiError("Invalid severity")
    try:
        threshold = float(data.get("threshold"))
    except (TypeError, ValueError):
        raise ApiError("Invalid threshold") from None
    return {"name": data["name"], "metric": data["metric"], "operator": data["operator"], "threshold": threshold,
            "duration_s": int(data.get("duration_s") or 0), "deployment_id": data.get("deployment_id") or None,
            "severity": data.get("severity", "warning"),
            "channel_ids": json.dumps([int(c) for c in data.get("channel_ids") or []]),
            "renotify_s": int(data.get("renotify_s") or 0), "enabled": 1 if data.get("enabled", True) else 0}


async def rules_list(request: Request):
    rows = svc(request).db.query("SELECT * FROM alert_rules ORDER BY CASE severity WHEN 'critical' THEN 0 "
                                 "WHEN 'warning' THEN 1 ELSE 2 END, name")
    return ok([{**r, "channel_ids": json.loads(r["channel_ids"])} for r in rows])


async def rules_create(request: Request):
    s = svc(request)
    rid = s.db.insert("alert_rules", {**_rule_in(await body(request)), "created_at": now()})
    s.db.audit(actor(request), "rule:create", {"id": rid})
    return ok({"id": rid}, 201)


async def rules_update(request: Request):
    s = svc(request)
    rid = int(request.path_params["id"])
    if not s.db.one("SELECT id FROM alert_rules WHERE id = ?", (rid,)):
        raise ApiError("Rule not found", 404)
    s.db.update("alert_rules", rid, _rule_in(await body(request)))
    s.db.execute("DELETE FROM alert_pending WHERE rule_id = ?", (rid,))
    s.db.audit(actor(request), "rule:update", {"id": rid})
    return ok()


async def rules_delete(request: Request):
    s = svc(request)
    rid = int(request.path_params["id"])
    s.db.execute("DELETE FROM alert_rules WHERE id = ?", (rid,))
    s.db.execute("DELETE FROM alert_pending WHERE rule_id = ?", (rid,))
    s.db.audit(actor(request), "rule:delete", {"id": rid})
    return ok()


async def alerts_list(request: Request):
    s = svc(request)
    status = request.query_params.get("status", "active")
    if status == "active":
        rows = s.db.query("SELECT * FROM alerts WHERE status != 'resolved' ORDER BY opened_at DESC")
    else:
        rows = s.db.query("SELECT * FROM alerts ORDER BY opened_at DESC LIMIT 300")
    return ok(rows)


async def alerts_ack(request: Request):
    s = svc(request)
    aid = int(request.path_params["id"])
    s.db.execute("UPDATE alerts SET status = 'acknowledged', acknowledged_by = ?, updated_at = ? "
                 "WHERE id = ? AND status = 'open'", (actor(request), now(), aid))
    s.db.audit(actor(request), "alert:ack", {"id": aid})
    return ok()


# ---------------------------------------------------------------------------
# notification channels
# ---------------------------------------------------------------------------

CHANNEL_FIELDS = {"email": ["to"], "slack": ["webhook_url"], "webhook": ["url"]}


def _channel_cfg(data: dict[str, Any]) -> dict[str, Any]:
    ctype = data.get("type")
    if ctype not in CHANNEL_FIELDS:
        raise ApiError("Invalid channel type (email, slack, webhook)")
    cfg = data.get("config") or {}
    need(cfg, *CHANNEL_FIELDS[ctype])
    if ctype == "email" and isinstance(cfg["to"], str):
        cfg["to"] = [x.strip() for x in cfg["to"].replace(";", ",").split(",") if x.strip()]
    return cfg


def _mask(cfg: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for k, v in cfg.items():
        if k in ("webhook_url", "url") and isinstance(v, str) and len(v) > 24:
            out[k] = v[:24] + "…"
        elif k == "headers":
            out[k] = {h: "•••" for h in (v or {})}
        else:
            out[k] = v
    return out


async def channels_list(request: Request):
    s = svc(request)
    rows = s.db.query("SELECT * FROM channels ORDER BY name")
    return ok([{"id": r["id"], "name": r["name"], "type": r["type"], "is_default": bool(r["is_default"]),
                "config": _mask(s.box.decrypt_json(r["config_enc"], {}))} for r in rows])


async def channels_create(request: Request):
    s = svc(request)
    data = await body(request)
    need(data, "name", "type")
    cid = s.db.insert("channels", {"name": data["name"], "type": data["type"],
                                   "config_enc": s.box.encrypt_json(_channel_cfg(data)),
                                   "is_default": 1 if data.get("is_default", True) else 0, "created_at": now()})
    s.db.audit(actor(request), "channel:create", {"id": cid})
    return ok({"id": cid}, 201)


async def channels_delete(request: Request):
    s = svc(request)
    cid = int(request.path_params["id"])
    s.db.execute("DELETE FROM channels WHERE id = ?", (cid,))
    s.db.audit(actor(request), "channel:delete", {"id": cid})
    return ok()


async def channels_test(request: Request):
    s = svc(request)
    ch = s.db.one("SELECT * FROM channels WHERE id = ?", (int(request.path_params["id"]),))
    if not ch:
        raise ApiError("Channel not found", 404)
    rule = {"id": 0, "name": "Test notification", "metric": "up"}
    alert = {"id": 0, "severity": "info", "message": "This is a test notification from MongoPilot",
             "status": "open", "value": None, "subject_label": "-", "deployment_id": None, "opened_at": now()}
    try:
        await asyncio.to_thread(s.alerts.notifier.deliver, ch, rule, alert, "test")
    except Exception as exc:
        raise ApiError(f"Sending failed: {exc}") from None
    return ok()


# ---------------------------------------------------------------------------
# jobs and activity log
# ---------------------------------------------------------------------------

async def jobs_list(request: Request):
    rows = svc(request).db.query(
        "SELECT j.id, j.type, j.title, j.status, j.created_by, j.created_at, j.started_at, j.finished_at, "
        "j.error, j.deployment_id, d.name AS deployment FROM jobs j LEFT JOIN deployments d "
        "ON d.id = j.deployment_id ORDER BY j.id DESC LIMIT 200")
    return ok(rows)


async def jobs_get(request: Request):
    s = svc(request)
    jid = int(request.path_params["id"])
    job = s.db.one("SELECT id, type, title, status, created_by, created_at, started_at, finished_at, error, "
                   "deployment_id FROM jobs WHERE id = ?", (jid,))
    if not job:
        raise ApiError("Job not found", 404)
    after = int(request.query_params.get("after", 0))
    job["logs"] = s.db.query("SELECT id, ts, level, message FROM job_logs WHERE job_id = ? AND id > ? ORDER BY id",
                             (jid, after))
    return ok(job)


async def audit_list(request: Request):
    return ok(svc(request).db.query("SELECT * FROM audit ORDER BY id DESC LIMIT 300"))


# ---------------------------------------------------------------------------
# app
# ---------------------------------------------------------------------------

class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path == "/healthz":
            return await call_next(request)
        s: Services = request.app.state.services
        header = request.headers.get("authorization", "")
        if header.lower().startswith("basic "):
            try:
                user, _, pw = base64.b64decode(header[6:]).decode().partition(":")
            except Exception:
                user, pw = "", ""
            if (secrets.compare_digest(user, s.settings.admin_user)
                    and secrets.compare_digest(pw, s.admin_password)):
                request.state.user = user
                return await call_next(request)
        return Response("Authentication required", status_code=401,
                        headers={"WWW-Authenticate": 'Basic realm="MongoPilot", charset="UTF-8"'})


async def api_error(request: Request, exc: Exception):
    if isinstance(exc, ApiError):
        return JSONResponse({"error": str(exc)}, status_code=exc.status)
    if isinstance(exc, ToolError):
        return JSONResponse({"error": str(exc)}, status_code=400)
    if isinstance(exc, (ValueError, KeyError)):
        return JSONResponse({"error": f"Invalid request: {exc}"}, status_code=400)
    log.exception("API error", exc_info=exc)
    return JSONResponse({"error": f"Internal error: {exc}"}, status_code=500)


async def index(request: Request):
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


async def healthz(request: Request):
    return ok({"status": "ok", "version": __version__})


def create_app(settings: Settings | None = None, **deps: Any) -> Starlette:
    settings = settings or Settings()
    services = Services(settings, **deps)

    @contextlib.asynccontextmanager
    async def lifespan(app):
        task = None
        if not settings.disable_background:
            task = asyncio.create_task(services.background())
        log.info("MongoPilot %s started (polling every %ss)", __version__, settings.poll_interval)
        try:
            yield
        finally:
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            services.shells.close_all()
            if hasattr(services.probe, "close"):
                services.probe.close()

    P = "/api"
    ID = "{id:int}"
    routes = [
        Route("/", index),
        Route("/healthz", healthz),
        Route(f"{P}/overview", overview),
        Route(f"{P}/catalog", catalog),
        Route(f"{P}/mongodb-versions", mongodb_versions),
        Route(f"{P}/servers", servers_list),
        Route(f"{P}/servers", servers_create, methods=["POST"]),
        Route(f"{P}/servers/parse-ssh-config", servers_parse_ssh_config, methods=["POST"]),
        Route(f"{P}/servers/{ID}", servers_get),
        Route(f"{P}/servers/{ID}", servers_update, methods=["PUT"]),
        Route(f"{P}/servers/{ID}", servers_delete, methods=["DELETE"]),
        Route(f"{P}/servers/{ID}/check", servers_check, methods=["POST"]),
        Route(f"{P}/servers/{ID}/forget-host-key", servers_forget_key, methods=["POST"]),
        Route(f"{P}/deployments", deployments_list),
        Route(f"{P}/deployments", deployments_create, methods=["POST"]),
        Route(f"{P}/deployments/replicaset", deployments_create_replicaset, methods=["POST"]),
        Route(f"{P}/deployments/sharded", deployments_create_sharded, methods=["POST"]),
        Route(f"{P}/deployments/{ID}", deployments_get),
        Route(f"{P}/deployments/{ID}", deployments_update, methods=["PATCH"]),
        Route(f"{P}/deployments/{ID}", deployments_delete, methods=["DELETE"]),
        Route(f"{P}/deployments/{ID}/poll", deployments_poll, methods=["POST"]),
        Route(f"{P}/deployments/{ID}/actions", deployments_action, methods=["POST"]),
        Route(f"{P}/deployments/{ID}/parameters", parameters_get),
        Route(f"{P}/deployments/{ID}/parameters", parameters_set, methods=["POST"]),
        Route(f"{P}/deployments/{ID}/config", config_get),
        Route(f"{P}/deployments/{ID}/config", config_update, methods=["PUT"]),
        Route(f"{P}/deployments/{ID}/config/remote", config_remote),
        Route(f"{P}/deployments/{ID}/tls", tls_get),
        Route(f"{P}/deployments/{ID}/tls", tls_set, methods=["POST"]),
        Route(f"{P}/insights/{ID}/slow", insights_slow),
        Route(f"{P}/insights/{ID}/analyze", insights_analyze, methods=["POST"]),
        Route(f"{P}/insights/{ID}/report", insights_report),
        Route(f"{P}/deployments/{ID}/tools", tools_info),
        Route(f"{P}/deployments/{ID}/namespaces", tools_namespaces, methods=["POST"]),
        Route(f"{P}/deployments/{ID}/schema", tools_schema, methods=["POST"]),
        Route(f"{P}/deployments/{ID}/shell", shell_open, methods=["POST"]),
        Route(f"{P}/shell/{{sid}}/input", shell_input, methods=["POST"]),
        Route(f"{P}/shell/{{sid}}/output", shell_output),
        Route(f"{P}/shell/{{sid}}/resize", shell_resize, methods=["POST"]),
        Route(f"{P}/shell/{{sid}}", shell_close, methods=["DELETE"]),
        Route(f"{P}/deployments/{ID}/exports", exports_list),
        Route(f"{P}/deployments/{ID}/exports", exports_create, methods=["POST"]),
        Route(f"{P}/deployments/{ID}/uploads", uploads_create, methods=["POST"]),
        Route(f"{P}/tool-sources", tool_sources),
        Route(f"{P}/exports/{ID}", export_get),
        Route(f"{P}/exports/{ID}/download", export_download),
        Route(f"{P}/exports/{ID}", export_delete, methods=["DELETE"]),
        Route(f"{P}/metrics", metrics_series),
        Route(f"{P}/rules", rules_list),
        Route(f"{P}/rules", rules_create, methods=["POST"]),
        Route(f"{P}/rules/{ID}", rules_update, methods=["PUT"]),
        Route(f"{P}/rules/{ID}", rules_delete, methods=["DELETE"]),
        Route(f"{P}/alerts", alerts_list),
        Route(f"{P}/alerts/{ID}/ack", alerts_ack, methods=["POST"]),
        Route(f"{P}/channels", channels_list),
        Route(f"{P}/channels", channels_create, methods=["POST"]),
        Route(f"{P}/channels/{ID}", channels_delete, methods=["DELETE"]),
        Route(f"{P}/channels/{ID}/test", channels_test, methods=["POST"]),
        Route(f"{P}/jobs", jobs_list),
        Route(f"{P}/jobs/{ID}", jobs_get),
        Route(f"{P}/audit", audit_list),
        Mount("/static", StaticFiles(directory=STATIC), name="static"),
    ]
    app = Starlette(routes=routes, lifespan=lifespan, middleware=[Middleware(AuthMiddleware)],
                    exception_handlers={ApiError: api_error, ToolError: api_error, ValueError: api_error, KeyError: api_error,
                                        Exception: api_error})
    app.state.services = services
    return app
