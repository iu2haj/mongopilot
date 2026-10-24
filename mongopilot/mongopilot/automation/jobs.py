"""Automation jobs: replica set and sharded cluster creation, rolling restart/upgrade, FCV, process actions,
TLS configuration and configuration file changes."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import secrets
import time
from typing import Any, Awaitable, Callable

from ..config import Settings
from ..crypto import SecretBox
from ..db import Database, now
from . import releases
from . import templates as T
from .mongo_ops import MONITOR_ROLE, MONITOR_ROLE_PRIVILEGES, MongoOps, admin_uri, with_tls
from .ssh import CommandResult, SshRunner, SshTarget, target_for

log = logging.getLogger("mongopilot.jobs")

MONITOR_USER = "mongopilot-monitor"
TLS_MODES = ["disabled", "allowTLS", "preferTLS", "requireTLS"]
FORWARDED_HOSTS = ("host.docker.internal", "127.0.0.1", "localhost")


class JobError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Managed deployment model helpers
# ---------------------------------------------------------------------------

def normalize(managed: dict[str, Any]) -> dict[str, Any]:
    """Fill fields added after 0.2 so older managed deployments keep working."""
    managed.setdefault("kind", "replicaset")
    for m in managed.get("members", []):
        m.setdefault("role", "replica")
        if m["role"] == "replica":
            m.setdefault("rs_name", managed.get("rs_name"))
        m.setdefault("rs_name", None)
        m.setdefault("shard", None)
        m.setdefault("delay", 0)
    managed.setdefault("tls", {"mode": "disabled"})
    return managed


def rs_groups(managed: dict[str, Any]) -> list[dict[str, Any]]:
    """Replica sets of a deployment in upgrade order: config servers, then shards (or the single replica set)."""
    groups: dict[str, dict[str, Any]] = {}
    for m in managed["members"]:
        if m["role"] == "mongos":
            continue
        g = groups.setdefault(m["rs_name"], {"rs_name": m["rs_name"], "role": m["role"], "shard": m.get("shard"),
                                             "members": []})
        g["members"].append(m)
    order = {"configsvr": 0, "shardsvr": 1, "replica": 1}
    return sorted(groups.values(), key=lambda g: (order.get(g["role"], 2), g["rs_name"]))


def mongos_members(managed: dict[str, Any]) -> list[dict[str, Any]]:
    return [m for m in managed["members"] if m["role"] == "mongos"]


class JobContext:
    def __init__(self, mgr: "JobManager", job_id: int):
        self.mgr = mgr
        self.job_id = job_id
        self.db = mgr.db
        self.box = mgr.box

    def log(self, message: str, level: str = "info") -> None:
        self.db.insert("job_logs", {"job_id": self.job_id, "ts": now(), "level": level, "message": message})
        getattr(log, "warning" if level == "warn" else level if level in ("info", "error") else "info")(
            "[job %s] %s", self.job_id, message)

    def target(self, server: dict[str, Any]) -> SshTarget:
        return target_for(server, self.box)

    async def ssh(self, server: dict[str, Any], script: str, desc: str, stdin: str | None = None,
                  timeout: int = 600, check: bool = True, sudo: bool = True) -> CommandResult:
        self.log(f"[{server['name']}] {desc}")
        res = await self.mgr.runner.run(self.target(server), script, stdin=stdin, timeout=timeout, sudo=sudo)
        for line in res.stdout.strip().splitlines()[-15:]:
            self.log(f"[{server['name']}]   {line}", "debug")
        if check and not res.ok:
            tail = "\n".join((res.stderr or res.stdout).strip().splitlines()[-20:])
            raise JobError(f"{desc} failed on {server['name']} (exit {res.rc}): {tail}")
        return res

    async def mongo(self, fn: Callable[..., Any], *args: Any) -> Any:
        return await asyncio.to_thread(fn, *args)

    async def wait_until(self, desc: str, check: Callable[[], Awaitable[bool]], timeout: int, interval: float = 2):
        deadline = time.monotonic() + timeout
        last_exc: Exception | None = None
        while time.monotonic() < deadline:
            try:
                if await check():
                    return
            except Exception as exc:  # transient state (election, restart)
                last_exc = exc
            await asyncio.sleep(interval * self.mgr.speed)
        raise JobError(f"Timed out waiting for {desc}" + (f" (last error: {last_exc})" if last_exc else ""))


class JobManager:
    def __init__(self, db: Database, box: SecretBox, settings: Settings, runner: SshRunner | Any,
                 ops: MongoOps | Any, on_deployment_change: Callable[[int], Any] | None = None):
        self.db = db
        self.box = box
        self.settings = settings
        self.runner = runner
        self.ops = ops
        self.probe = None   # set by the app, used to drop cached connections when URIs change
        self.on_deployment_change = on_deployment_change
        self.speed = 1.0  # tests lower it to shorten waits
        self._locks: dict[int, asyncio.Lock] = {}
        self._tasks: set[asyncio.Task] = set()
        self.handlers: dict[str, Callable[[JobContext, dict[str, Any]], Awaitable[None]]] = {
            "server_check": server_check,
            "create_replicaset": create_replicaset,
            "create_sharded": create_sharded,
            "rolling_restart": rolling_restart,
            "rolling_upgrade": rolling_upgrade,
            "set_fcv": set_fcv,
            "process_action": process_action,
            "configure_tls": configure_tls,
            "apply_config": apply_config,
        }

    def recover(self) -> None:
        """Jobs left half-way when the app restarted are marked as interrupted."""
        for j in self.db.query("SELECT id FROM jobs WHERE status IN ('queued', 'running')"):
            self.db.update("jobs", j["id"], {"status": "failed", "finished_at": now(),
                                             "error": "Interrupted by a MongoPilot restart"})

    def busy(self, deployment_id: int) -> bool:
        return bool(self.db.one("SELECT id FROM jobs WHERE deployment_id = ? AND status IN ('queued','running')",
                                (deployment_id,)))

    def submit(self, job_type: str, title: str, params: dict[str, Any], deployment_id: int | None,
               actor: str | None) -> int:
        if job_type not in self.handlers:
            raise ValueError(f"Unknown job type: {job_type}")
        if deployment_id and self.busy(deployment_id):
            raise ValueError("Another job is already running on this deployment")
        job_id = self.db.insert("jobs", {"type": job_type, "deployment_id": deployment_id, "title": title,
                                         "params_enc": self.box.encrypt_json(params), "status": "queued",
                                         "created_by": actor, "created_at": now()})
        self.db.audit(actor, f"job:{job_type}", {"job_id": job_id, "deployment_id": deployment_id})
        task = asyncio.get_running_loop().create_task(self._run(job_id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return job_id

    async def _run(self, job_id: int) -> None:
        job = self.db.one("SELECT * FROM jobs WHERE id = ?", (job_id,))
        lock = self._locks.setdefault(job["deployment_id"] or -job_id, asyncio.Lock())
        async with lock:
            ctx = JobContext(self, job_id)
            self.db.update("jobs", job_id, {"status": "running", "started_at": now()})
            try:
                await self.handlers[job["type"]](ctx, self.box.decrypt_json(job["params_enc"], {}))
                self.db.update("jobs", job_id, {"status": "success", "finished_at": now()})
                ctx.log("Completed successfully")
            except Exception as exc:
                ctx.log(f"ERROR: {exc}", "error")
                if not isinstance(exc, (JobError, ValueError)):
                    log.exception("Job %s failed", job_id)
                self.db.update("jobs", job_id, {"status": "failed", "finished_at": now(), "error": str(exc)[:2000]})
            finally:
                if job["deployment_id"] and self.on_deployment_change:
                    self.on_deployment_change(job["deployment_id"])

    async def wait_all(self) -> None:
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    # -- shared helpers ---------------------------------------------------
    def server(self, server_id: int) -> dict[str, Any]:
        s = self.db.one("SELECT * FROM servers WHERE id = ?", (server_id,))
        if not s:
            raise JobError(f"Server {server_id} does not exist")
        return s

    def managed(self, dep_id: int) -> tuple[dict[str, Any], dict[str, Any]]:
        dep = self.db.one("SELECT * FROM deployments WHERE id = ?", (dep_id,))
        if not dep or not dep["managed"]:
            raise JobError("This deployment is not managed by MongoPilot")
        return dep, normalize(self.box.decrypt_json(dep["managed_enc"], {}))

    def save_managed(self, dep_id: int, managed: dict[str, Any]) -> None:
        self.db.update("deployments", dep_id, {"managed_enc": self.box.encrypt_json(managed)})

    # -- TLS as seen by MongoPilot's own connections -------------------------
    def ca_file(self, dep_id: int) -> str:
        return str(self.settings.data_dir / "tls" / f"deployment-{dep_id}-ca.pem")

    def client_tls(self, dep_id: int, managed: dict[str, Any]) -> dict[str, Any] | None:
        tls = managed.get("tls") or {}
        if not tls.get("client"):
            return None
        return {"enabled": True, "ca_file": self.ca_file(dep_id),
                "allow_invalid_hostnames": bool(tls.get("allow_invalid_hostnames"))}

    def set_client_tls(self, dep_id: int, managed: dict[str, Any], enabled: bool) -> None:
        """Switch MongoPilot's connections (monitoring and automation) to or from TLS."""
        tls = managed.setdefault("tls", {"mode": "disabled"})
        tls["client"] = enabled
        if enabled and tls.get("ca_pem"):
            path = self.settings.data_dir / "tls"
            path.mkdir(parents=True, exist_ok=True)
            f = path / f"deployment-{dep_id}-ca.pem"
            f.write_text(tls["ca_pem"])
        self.save_managed(dep_id, managed)
        dep = self.db.one("SELECT uri_enc FROM deployments WHERE id = ?", (dep_id,))
        old = self.box.decrypt(dep["uri_enc"])
        new = with_tls(old, self.client_tls(dep_id, managed))
        if new != old:
            self.db.update("deployments", dep_id, {"uri_enc": self.box.encrypt(new)})
            if self.probe is not None and hasattr(self.probe, "forget"):
                self.probe.forget(old)

    def rs_uri(self, dep_id: int, managed: dict[str, Any], group: dict[str, Any]) -> str:
        return admin_uri(managed["admin_user"], managed["admin_password"], [m["host"] for m in group["members"]],
                         group["rs_name"], tls=self.client_tls(dep_id, managed))

    def cluster_uri(self, dep_id: int, managed: dict[str, Any]) -> str:
        """Where cluster-wide commands go: the mongos routers, or the replica set."""
        if managed["kind"] == "sharded":
            return admin_uri(managed["admin_user"], managed["admin_password"],
                             [m["host"] for m in mongos_members(managed)], tls=self.client_tls(dep_id, managed))
        return self.rs_uri(dep_id, managed, rs_groups(managed)[0])

    def direct(self, dep_id: int, managed: dict[str, Any], host: str) -> str:
        return admin_uri(managed["admin_user"], managed["admin_password"], [host], extra="directConnection=true",
                         tls=self.client_tls(dep_id, managed))


# ---------------------------------------------------------------------------
# Reusable steps
# ---------------------------------------------------------------------------

async def gather_facts(ctx: JobContext, server: dict[str, Any]) -> dict[str, str]:
    res = await ctx.ssh(server, T.FACTS_SCRIPT, "Detecting operating system", sudo=False, timeout=60)
    facts = T.parse_facts(res.stdout)
    sudo = await ctx.ssh(server, "id -u", "Checking passwordless sudo", check=False, timeout=60)
    facts["sudo"] = "yes" if sudo.ok and sudo.stdout.strip() == "0" else "no"
    ctx.db.update("servers", server["id"], {"facts": json.dumps(facts)})
    ctx.log(f"[{server['name']}] {facts.get('os_name')} · {facts.get('arch')} · {facts.get('cpus')} CPU · "
            f"{facts.get('mem_mb')} MB RAM · sudo: {facts['sudo']}")
    if facts["sudo"] != "yes":
        raise JobError(f"{server['name']}: user {server['ssh_user']} must be able to run sudo without a password")
    if facts.get("systemd") != "yes":
        raise JobError(f"{server['name']}: systemd is not available")
    return facts


async def install_binaries(ctx: JobContext, server: dict[str, Any], facts: dict[str, str], version: str,
                           tarball_url: str | None = None) -> None:
    try:
        T.check_platform(facts, version)
    except ValueError as exc:
        raise JobError(f"{server['name']}: {exc}") from None
    s = ctx.mgr.settings
    urls, sha = await asyncio.to_thread(releases.resolve, facts, version, s.mongodb_release_index,
                                        s.mongodb_mirror, tarball_url)
    await ctx.ssh(server, T.install_script(version, urls, sha), f"Installing MongoDB {version}", timeout=1200)


async def write_remote(ctx: JobContext, server, path: str, content: str, desc: str, mode="644",
                       owner="root:root") -> None:
    await ctx.ssh(server, T.write_file_script(path, mode, owner), desc, stdin=content, timeout=60)


async def write_member_config(ctx: JobContext, managed: dict[str, Any], m: dict[str, Any],
                              version: str | None = None) -> None:
    srv = ctx.mgr.server(m["server_id"])
    text = T.render_config(T.member_config(managed, m))
    await ctx.ssh(srv, T.install_config_script(m["paths"]["conf"], version or m.get("version") or managed["version"],
                                               T.binary_of(m)),
                  f"Validating and writing {m['paths']['conf']}", stdin=text, timeout=120)


async def write_unit(ctx: JobContext, dep_name: str, m: dict[str, Any], version: str) -> None:
    srv = ctx.mgr.server(m["server_id"])
    await write_remote(ctx, srv, f"/etc/systemd/system/{m['paths']['unit']}",
                       T.systemd_unit(dep_name, m["port"], version, m["paths"], T.binary_of(m)),
                       f"Writing systemd unit {m['paths']['unit']} (MongoDB {version})")


def _member_state(status: dict[str, Any], host: str) -> tuple[str | None, int | None]:
    for m in status.get("members", []):
        if m.get("name") == host:
            return m.get("stateStr"), m.get("health")
    return None, None


def _primary(status: dict[str, Any]) -> str | None:
    return next((m["name"] for m in status.get("members", []) if m.get("stateStr") == "PRIMARY"), None)


async def wait_member_healthy(ctx: JobContext, uri: str, host: str, timeout: int = 600) -> None:
    async def ok() -> bool:
        st = await ctx.mongo(ctx.mgr.ops.rs_status, uri)
        state, health = _member_state(st, host)
        return health == 1 and state in ("PRIMARY", "SECONDARY", "ARBITER")
    await ctx.wait_until(f"{host} to be PRIMARY/SECONDARY again", ok, timeout, interval=3)
    ctx.log(f"{host} is healthy again")


async def wait_primary(ctx: JobContext, uri: str, not_host: str | None = None, timeout: int = 180) -> str:
    found: dict[str, str] = {}

    async def ok() -> bool:
        p = _primary(await ctx.mongo(ctx.mgr.ops.rs_status, uri))
        if p and p != not_host:
            found["p"] = p
            return True
        return False
    await ctx.wait_until("a primary to be elected", ok, timeout, interval=2)
    return found["p"]


async def start_process(ctx: JobContext, dep_name: str, managed: dict[str, Any], m: dict[str, Any],
                        keyfile: str) -> None:
    """Prepare directories, keyfile, configuration and unit, then start one mongod/mongos."""
    srv, paths = ctx.mgr.server(m["server_id"]), m["paths"]
    checks = f"""
if [ -n "{paths['dbpath'] or ''}" ] && [ -d "{paths['dbpath'] or '/nonexistent'}" ] && [ -n "$(ls -A {paths['dbpath'] or '/nonexistent'} 2>/dev/null)" ]; then
  echo "Data directory {paths['dbpath']} is not empty: refusing to overwrite it" >&2; exit 3; fi
if (echo > /dev/tcp/127.0.0.1/{m['port']}) 2>/dev/null; then
  echo "Port {m['port']} is already in use" >&2; exit 3; fi
"""
    await ctx.ssh(srv, checks, f"Checking that port {m['port']} is free", timeout=60)
    await ctx.ssh(srv, T.prepare_member_script(paths), "Creating the mongod user and directories", timeout=60)
    await write_remote(ctx, srv, paths["keyfile"], keyfile, "Writing the keyfile", "400", "mongod:mongod")
    await write_member_config(ctx, managed, m)
    await write_unit(ctx, dep_name, m, m["version"])
    await ctx.ssh(srv, T.systemctl_script("enable-start", paths["unit"]), f"Starting {T.binary_of(m)} on port {m['port']}",
                  timeout=120)
    await ctx.ssh(srv, T.wait_port_script(m["port"], paths["unit"], paths["logpath"]),
                  f"Waiting for {T.binary_of(m)} on port {m['port']}", timeout=150)


def rs_config_doc(rs_name: str, members: list[dict[str, Any]], version: str, configsvr: bool = False) -> dict[str, Any]:
    delay_field = "secondaryDelaySecs" if T._tuple(T.major(version) + ".0") >= (5, 0, 0) else "slaveDelay"
    out = []
    for m in members:
        doc: dict[str, Any] = {"_id": m["_id"], "host": m["host"], "priority": m["priority"], "votes": m["votes"]}
        if m.get("arbiter"):
            doc["arbiterOnly"] = True
        if m.get("hidden"):
            doc["hidden"] = True
        if m.get("delay"):
            doc[delay_field] = int(m["delay"])
        out.append(doc)
    cfg: dict[str, Any] = {"_id": rs_name, "members": out}
    if configsvr:
        cfg["configsvr"] = True
    return cfg


async def initiate_set(ctx: JobContext, rs_name: str, members: list[dict[str, Any]], version: str,
                       configsvr: bool = False) -> str:
    """replSetInitiate through an SSH tunnel to the first member (localhost exception). Returns the primary."""
    first = members[0]
    runner = ctx.mgr.runner
    async with runner.tunnel(ctx.target(ctx.mgr.server(first["server_id"])), first["port"]) as lport:
        local = f"mongodb://127.0.0.1:{lport}/?directConnection=true"
        await ctx.mongo(ctx.mgr.ops.initiate, local, rs_config_doc(rs_name, members, version, configsvr))
        ctx.log(f"replSetInitiate of {rs_name} run on {first['host']}")
        hello: dict[str, Any] = {}

        async def has_primary() -> bool:
            hello.update(await ctx.mongo(ctx.mgr.ops.hello, local))
            return bool(hello.get("primary"))
        await ctx.wait_until(f"a primary in {rs_name}", has_primary, 180, interval=2)
    ctx.log(f"{rs_name}: primary elected on {hello['primary']}")
    return hello["primary"]


async def create_users(ctx: JobContext, rs_name: str, members: list[dict[str, Any]], primary: str,
                       p: dict[str, Any], label: str) -> None:
    """First admin user through the localhost exception, then the monitoring role and user."""
    pm = next((m for m in members if m["host"] == primary), members[0])
    async with ctx.mgr.runner.tunnel(ctx.target(ctx.mgr.server(pm["server_id"])), pm["port"]) as lport:
        local = f"mongodb://127.0.0.1:{lport}/?directConnection=true"
        await ctx.mongo(ctx.mgr.ops.create_user, local, p["admin_user"], p["admin_password"],
                        [{"role": "root", "db": "admin"}])
    admin = admin_uri(p["admin_user"], p["admin_password"], [m["host"] for m in members], rs_name)
    await ctx.mongo(ctx.mgr.ops.create_role, admin, MONITOR_ROLE, MONITOR_ROLE_PRIVILEGES)
    await ctx.mongo(ctx.mgr.ops.create_user, admin, MONITOR_USER, p["monitor_password"],
                    [{"role": "clusterMonitor", "db": "admin"}, {"role": "read", "db": "local"},
                     {"role": MONITOR_ROLE, "db": "admin"}])
    ctx.log(f"{label}: users '{p['admin_user']}' and '{MONITOR_USER}' created")


def _servers_checks(ctx: JobContext, members: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    servers = {m["server_id"]: ctx.mgr.server(m["server_id"]) for m in members}
    forwarded = [s["name"] for s in servers.values() if not s["mongo_host"] and s["hostname"] in FORWARDED_HOSTS]
    if forwarded:
        raise JobError(f"{', '.join(forwarded)}: set 'MongoDB host name' to the VM's private network IP. "
                       "Cluster members cannot reach each other through a forwarded SSH port")
    return servers


def build_member(srv: dict[str, Any], spec: dict[str, Any], idx: int, role: str, rs_name: str | None,
                 shard: str | None, dep_slug: str, data_root: str, version: str) -> dict[str, Any]:
    port = int(spec["port"])
    hidden, arbiter = bool(spec.get("hidden")), bool(spec.get("arbiter"))
    delay = int(spec.get("delay") or 0)
    if delay:
        hidden = True   # a delayed member must not become primary or serve normal reads
    return {
        "_id": idx, "server_id": srv["id"], "port": port, "host": f"{srv['mongo_host'] or srv['hostname']}:{port}",
        "priority": 0 if (hidden or arbiter or delay) else float(spec.get("priority", 1)),
        "votes": int(spec.get("votes", 1)), "arbiter": arbiter, "hidden": hidden, "delay": delay,
        "role": role, "rs_name": rs_name, "shard": shard, "version": version,
        "paths": T.member_paths(dep_slug, port, data_root, "mongos" if role == "mongos" else "mongod"),
    }


# ---------------------------------------------------------------------------
# Server check
# ---------------------------------------------------------------------------

async def server_check(ctx: JobContext, params: dict[str, Any]) -> None:
    server = ctx.mgr.server(params["server_id"])
    facts = await gather_facts(ctx, server)
    if facts.get("thp") == "always":
        ctx.log(f"[{server['name']}] Transparent Huge Pages = always: for MongoDB earlier than 8.0 it is "
                "recommended to disable them", "warn")
    targets = T.download_targets(facts)
    if targets:
        ctx.log(f"[{server['name']}] MongoDB platform: {', '.join(targets)} ({T.tarball_arch(facts)})")
    else:
        ctx.log(f"[{server['name']}] Platform not recognized: an explicit tarball URL will be needed", "warn")
    for series in ("9.0", "8.0"):
        try:
            T.check_platform(facts, series + ".0")
            ctx.log(f"[{server['name']}] Supports MongoDB {series}")
            break
        except ValueError as exc:
            ctx.log(f"[{server['name']}] {exc}", "warn")
    mem = int(facts.get("mem_mb") or 0)
    if mem and mem < 1800:
        ctx.log(f"[{server['name']}] Only {mem} MB of RAM: give the VM at least 2 GB for MongoDB", "warn")


# ---------------------------------------------------------------------------
# Replica set creation
# ---------------------------------------------------------------------------

def _check_members(db: Database, specs: list[dict[str, Any]], seen: set, label: str) -> None:
    if not specs:
        raise ValueError(f"{label}: at least one member is required")
    for m in specs:
        if not db.one("SELECT id FROM servers WHERE id = ?", (m["server_id"],)):
            raise ValueError(f"Server {m['server_id']} does not exist")
        key = (int(m["server_id"]), int(m["port"]))
        if key in seen:
            raise ValueError("Two processes use the same server and port")
        seen.add(key)


def _voting_warnings(specs: list[dict[str, Any]], label: str) -> list[str]:
    warnings = []
    voting = sum(1 for m in specs if int(m.get("votes", 1)) > 0)
    if voting % 2 == 0:
        warnings.append(f"{label}: {voting} voting members, an odd number avoids election deadlocks")
    if len(specs) < 3:
        warnings.append(f"{label}: with fewer than 3 members there is no automatic failover")
    if len({int(m["server_id"]) for m in specs}) < len(specs):
        warnings.append(f"{label}: several members on the same server, you lose redundancy if that server fails")
    return warnings


def _check_common(params: dict[str, Any]) -> None:
    T.validate_version(params["version"])
    if not params.get("admin_user") or len(params.get("admin_password") or "") < 12:
        raise ValueError("Admin user is required and the password must be at least 12 characters long")


def validate_replicaset_params(db: Database, params: dict[str, Any]) -> list[str]:
    """Preliminary checks; returns warnings, raises ValueError when the parameters are not acceptable."""
    _check_common(params)
    members = params.get("members") or []
    if members and members[0].get("arbiter"):
        raise ValueError("The first member cannot be an arbiter")
    _check_members(db, members, set(), "Replica set")
    if all(m.get("arbiter") or m.get("delay") or m.get("hidden") for m in members):
        raise ValueError("At least one member must be able to become primary")
    return _voting_warnings(members, "Replica set")


async def create_replicaset(ctx: JobContext, p: dict[str, Any]) -> None:
    dep_id, version, rs_name = p["deployment_id"], p["version"], p["rs_name"]
    dep_slug = T.slug(p["name"])
    data_root = p.get("data_root") or T.DEFAULT_DATA_ROOT
    for w in validate_replicaset_params(ctx.db, p):
        ctx.log(w, "warn")
    servers = {m["server_id"]: ctx.mgr.server(m["server_id"]) for m in p["members"]}
    members = [build_member(servers[m["server_id"]], m, i, "replica", rs_name, None, dep_slug, data_root, version)
               for i, m in enumerate(p["members"])]
    _servers_checks(ctx, members)
    managed = normalize({"kind": "replicaset", "rs_name": rs_name, "version": version, "dep_slug": dep_slug,
                         "data_root": data_root, "bind_ip": p.get("bind_ip") or "0.0.0.0", "cache_gb": p.get("cache_gb"),
                         "admin_user": p["admin_user"], "admin_password": p["admin_password"], "members": members})

    ctx.log(f"Step 1/5 · Preparing {len(servers)} servers")
    for srv in servers.values():
        facts = await gather_facts(ctx, srv)
        await install_binaries(ctx, srv, facts, version, p.get("tarball_url"))

    ctx.log("Step 2/5 · Configuring and starting the mongod processes")
    keyfile = base64.b64encode(secrets.token_bytes(756)).decode()
    for m in members:
        await start_process(ctx, p["name"], managed, m, keyfile)

    ctx.log("Step 3/5 · Initiating the replica set")
    primary = await initiate_set(ctx, rs_name, members, version)

    ctx.log("Step 4/5 · Creating the admin and monitoring users")
    await create_users(ctx, rs_name, members, primary, p, rs_name)

    ctx.log("Step 5/5 · Enabling monitoring")
    ctx.mgr.save_managed(dep_id, managed)
    ctx.db.update("deployments", dep_id, {"paused": 0, "last_error": None})


# ---------------------------------------------------------------------------
# Sharded cluster creation
# ---------------------------------------------------------------------------

def validate_sharded_params(db: Database, p: dict[str, Any]) -> list[str]:
    _check_common(p)
    seen: set = set()
    cfg = (p.get("config") or {}).get("members") or []
    _check_members(db, cfg, seen, "Config servers")
    if any(m.get("arbiter") for m in cfg):
        raise ValueError("Config server replica sets cannot have arbiters")
    shards = p.get("shards") or []
    if not shards:
        raise ValueError("At least one shard is required")
    names = set()
    warnings = _voting_warnings(cfg, "Config servers")
    for i, sh in enumerate(shards):
        name = T.slug(sh.get("name") or f"shard{i}")
        if name in names:
            raise ValueError(f"Duplicate shard name: {name}")
        names.add(name)
        ms = sh.get("members") or []
        if ms and ms[0].get("arbiter"):
            raise ValueError(f"Shard {name}: the first member cannot be an arbiter")
        _check_members(db, ms, seen, f"Shard {name}")
        warnings += _voting_warnings(ms, f"Shard {name}")
    mongos = p.get("mongos") or []
    _check_members(db, mongos, seen, "mongos routers")
    if len(mongos) < 2:
        warnings.append("A single mongos router is a single point of failure for applications")
    return warnings


async def create_sharded(ctx: JobContext, p: dict[str, Any]) -> None:
    dep_id, version = p["deployment_id"], p["version"]
    dep_slug = T.slug(p["name"])
    data_root = p.get("data_root") or T.DEFAULT_DATA_ROOT
    for w in validate_sharded_params(ctx.db, p):
        ctx.log(w, "warn")
    cfg_rs = p["config"].get("rs_name") or f"{dep_slug}-config"
    all_specs = [m["server_id"] for m in p["config"]["members"]] + \
        [m["server_id"] for sh in p["shards"] for m in sh["members"]] + [m["server_id"] for m in p["mongos"]]
    servers = {sid: ctx.mgr.server(sid) for sid in all_specs}
    members: list[dict[str, Any]] = []
    for i, m in enumerate(p["config"]["members"]):
        members.append(build_member(servers[m["server_id"]], m, i, "configsvr", cfg_rs, "config", dep_slug,
                                    data_root, version))
    shard_sets: list[tuple[str, str, list[dict[str, Any]]]] = []
    for si, sh in enumerate(p["shards"]):
        name = T.slug(sh.get("name") or f"shard{si}")
        rs = f"{dep_slug}-{name}"
        ms = [build_member(servers[m["server_id"]], m, i, "shardsvr", rs, name, dep_slug, data_root, version)
              for i, m in enumerate(sh["members"])]
        shard_sets.append((name, rs, ms))
        members += ms
    routers = [build_member(servers[m["server_id"]], m, i, "mongos", None, None, dep_slug, data_root, version)
               for i, m in enumerate(p["mongos"])]
    members += routers
    _servers_checks(ctx, members)
    managed = normalize({"kind": "sharded", "config_rs": cfg_rs, "rs_name": None, "version": version,
                         "dep_slug": dep_slug, "data_root": data_root, "bind_ip": p.get("bind_ip") or "0.0.0.0",
                         "cache_gb": p.get("cache_gb"), "admin_user": p["admin_user"],
                         "admin_password": p["admin_password"], "members": members})

    ctx.log(f"Step 1/6 · Preparing {len(servers)} servers")
    for srv in servers.values():
        facts = await gather_facts(ctx, srv)
        await install_binaries(ctx, srv, facts, version, p.get("tarball_url"))

    ctx.log("Step 2/6 · Starting the config servers and the shard members")
    keyfile = base64.b64encode(secrets.token_bytes(756)).decode()
    for m in members:
        if m["role"] != "mongos":
            await start_process(ctx, p["name"], managed, m, keyfile)

    ctx.log(f"Step 3/6 · Initiating the config server replica set {cfg_rs}")
    cfg_members = [m for m in members if m["role"] == "configsvr"]
    primary = await initiate_set(ctx, cfg_rs, cfg_members, version, configsvr=True)
    await create_users(ctx, cfg_rs, cfg_members, primary, p, "Cluster (config servers)")

    ctx.log("Step 4/6 · Initiating the shards and their shard-local users")
    for name, rs, ms in shard_sets:
        primary = await initiate_set(ctx, rs, ms, version)
        # Shard-local users let MongoPilot connect to shard members directly (monitoring, rolling operations)
        await create_users(ctx, rs, ms, primary, p, f"Shard {name}")

    ctx.log("Step 5/6 · Starting the mongos routers")
    for m in routers:
        await start_process(ctx, p["name"], managed, m, keyfile)

    ctx.log("Step 6/6 · Adding the shards to the cluster")
    mongos_uri = admin_uri(p["admin_user"], p["admin_password"], [m["host"] for m in routers])
    for name, rs, ms in shard_sets:
        conn = f"{rs}/" + ",".join(m["host"] for m in ms if not m["arbiter"])
        await ctx.mongo(ctx.mgr.ops.add_shard, mongos_uri, conn, name)
        ctx.log(f"Shard {name} added ({conn})")

    async def all_added() -> bool:
        return set(await ctx.mongo(ctx.mgr.ops.list_shards, mongos_uri)) >= {n for n, _, _ in shard_sets}
    await ctx.wait_until("all shards to be listed", all_added, 120, interval=2)
    ctx.mgr.save_managed(dep_id, managed)
    ctx.db.update("deployments", dep_id, {"paused": 0, "last_error": None})


# ---------------------------------------------------------------------------
# Rolling operations
# ---------------------------------------------------------------------------

async def rolling(ctx: JobContext, dep: dict[str, Any], managed: dict[str, Any],
                  before_restart: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
                  only_hosts: set[str] | None = None, force: bool = False) -> None:
    """Restart processes one at a time: config servers, then each shard (secondaries first, primary after a
    step-down), then the mongos routers. The balancer is paused on sharded clusters."""
    dep_id = dep["id"]
    sharded = managed["kind"] == "sharded"
    wanted = (lambda m: m["host"] in only_hosts) if only_hosts else (lambda m: True)
    groups = [(g, [m for m in g["members"] if wanted(m)]) for g in rs_groups(managed)]
    groups = [(g, ms) for g, ms in groups if ms]
    routers = [m for m in mongos_members(managed) if wanted(m)]

    for g, _ in groups:   # health check before touching anything
        st = await ctx.mongo(ctx.mgr.ops.rs_status, ctx.mgr.rs_uri(dep_id, managed, g))
        bad = [m["name"] for m in st.get("members", [])
               if m.get("health") != 1 or m.get("stateStr") not in ("PRIMARY", "SECONDARY", "ARBITER")]
        if bad and not force:
            raise JobError(f"Unhealthy members in {g['rs_name']} before starting: {', '.join(bad)}. "
                           "Fix them and try again")

    balancer_stopped = False
    if sharded and groups:
        await ctx.mongo(ctx.mgr.ops.balancer, ctx.mgr.cluster_uri(dep_id, managed), False)
        balancer_stopped = True
        ctx.log("Balancer stopped")
    try:
        for g, ms in groups:
            uri = ctx.mgr.rs_uri(dep_id, managed, g)
            primary = _primary(await ctx.mongo(ctx.mgr.ops.rs_status, uri))
            order = [m for m in ms if m["host"] != primary] + [m for m in ms if m["host"] == primary]
            ctx.log(f"{g['rs_name']}: " + " → ".join(m["host"] + (" (primary)" if m["host"] == primary else "")
                                                    for m in order))
            for m in order:
                srv, paths = ctx.mgr.server(m["server_id"]), m["paths"]
                if _primary(await ctx.mongo(ctx.mgr.ops.rs_status, uri)) == m["host"]:
                    ctx.log(f"Stepping down primary {m['host']}")
                    await ctx.mongo(ctx.mgr.ops.step_down, uri)
                    ctx.log(f"New primary: {await wait_primary(ctx, uri, not_host=m['host'])}")
                if before_restart:
                    await before_restart(m)
                await ctx.ssh(srv, T.systemctl_script("restart", paths["unit"]), f"Restarting {m['host']}",
                              timeout=360)
                await ctx.ssh(srv, T.wait_port_script(m["port"], paths["unit"], paths["logpath"]),
                              f"Waiting for mongod on port {m['port']}", timeout=150)
                uri = ctx.mgr.rs_uri(dep_id, managed, g)   # TLS settings may have changed
                await wait_member_healthy(ctx, uri, m["host"])
        for m in routers:
            srv, paths = ctx.mgr.server(m["server_id"]), m["paths"]
            if before_restart:
                await before_restart(m)
            await ctx.ssh(srv, T.systemctl_script("restart", paths["unit"]), f"Restarting mongos {m['host']}",
                          timeout=180)
            await ctx.ssh(srv, T.wait_port_script(m["port"], paths["unit"], paths["logpath"]),
                          f"Waiting for mongos on port {m['port']}", timeout=150)

            async def up(h=m["host"]) -> bool:
                return await ctx.mongo(ctx.mgr.ops.ping, ctx.mgr.direct(dep_id, managed, h))
            await ctx.wait_until(f"mongos {m['host']} to answer", up, 120)
            ctx.log(f"mongos {m['host']} is answering again")
    finally:
        if balancer_stopped:
            try:
                await ctx.mongo(ctx.mgr.ops.balancer, ctx.mgr.cluster_uri(dep_id, managed), True)
                ctx.log("Balancer restarted")
            except Exception as exc:
                ctx.log(f"Could not restart the balancer, run sh.startBalancer() on a mongos: {exc}", "error")


async def rolling_restart(ctx: JobContext, p: dict[str, Any]) -> None:
    dep, managed = ctx.mgr.managed(p["deployment_id"])
    await rolling(ctx, dep, managed, force=bool(p.get("force")))


async def rolling_upgrade(ctx: JobContext, p: dict[str, Any]) -> None:
    dep, managed = ctx.mgr.managed(p["deployment_id"])
    target = p["version"]
    fcv = await ctx.mongo(ctx.mgr.ops.get_fcv, ctx.mgr.cluster_uri(dep["id"], managed))
    ctx.log(f"Current version {managed['version']}, FCV {fcv}, target {target}")
    for w in T.check_upgrade_path(managed["version"], target, fcv):
        ctx.log(w, "warn")
    servers = {m["server_id"]: ctx.mgr.server(m["server_id"]) for m in managed["members"]}
    ctx.log("Installing the new binaries on every server (no restart yet)")
    for srv in servers.values():
        facts = json.loads(srv["facts"] or "{}") or await gather_facts(ctx, srv)
        await install_binaries(ctx, srv, facts, target, p.get("tarball_url"))

    async def switch(m: dict[str, Any]) -> None:
        await write_unit(ctx, dep["name"], m, target)
        m["version"] = target
        ctx.mgr.save_managed(dep["id"], managed)

    await rolling(ctx, dep, managed, before_restart=switch, force=bool(p.get("force")))
    managed["version"] = target
    ctx.mgr.save_managed(dep["id"], managed)
    ctx.log(f"All processes run {target}. When you are ready, set FCV to {T.major(target)} from the deployment page.")


async def set_fcv(ctx: JobContext, p: dict[str, Any]) -> None:
    dep, managed = ctx.mgr.managed(p["deployment_id"])
    target, running = p["fcv"], T.major(managed["version"])
    if target not in T.fcv_options(running):
        raise JobError(f"FCV {target} is not valid for {running} binaries (valid: {', '.join(T.fcv_options(running))})")
    uri = ctx.mgr.cluster_uri(dep["id"], managed)
    ctx.log(f"Current FCV {await ctx.mongo(ctx.mgr.ops.get_fcv, uri)} → {target}")
    await ctx.mongo(ctx.mgr.ops.set_fcv, uri, target)
    ctx.log(f"featureCompatibilityVersion set to {target}")


async def process_action(ctx: JobContext, p: dict[str, Any]) -> None:
    _, managed = ctx.mgr.managed(p["deployment_id"])
    action = p["action"]
    if action not in ("start", "stop", "restart"):
        raise JobError(f"Invalid action: {action}")
    m = next((m for m in managed["members"] if m["host"] == p["host"]), None)
    if not m:
        raise JobError(f"Process {p['host']} is not part of this deployment")
    srv, paths = ctx.mgr.server(m["server_id"]), m["paths"]
    await ctx.ssh(srv, T.systemctl_script(action, paths["unit"]), f"systemctl {action} {paths['unit']}", timeout=360)
    if action != "stop":
        await ctx.ssh(srv, T.wait_port_script(m["port"], paths["unit"], paths["logpath"]),
                      f"Waiting for {T.binary_of(m)} on port {m['port']}", timeout=150)


# ---------------------------------------------------------------------------
# Configuration files
# ---------------------------------------------------------------------------

async def apply_config(ctx: JobContext, p: dict[str, Any]) -> None:
    """Write the stored configuration of some members and optionally restart them in a rolling fashion."""
    dep, managed = ctx.mgr.managed(p["deployment_id"])
    hosts = set(p.get("hosts") or [m["host"] for m in managed["members"]])
    targets = [m for m in managed["members"] if m["host"] in hosts]
    if p.get("reason"):
        ctx.log(p["reason"])
    for m in targets:   # validate and install every file first: a typo stops the job before any restart
        await write_member_config(ctx, managed, m)
    if p.get("restart"):
        await rolling(ctx, dep, managed, only_hosts=hosts)
    else:
        ctx.log("Files written: the changes take effect at the next restart of each process")


# ---------------------------------------------------------------------------
# TLS
# ---------------------------------------------------------------------------

async def configure_tls(ctx: JobContext, p: dict[str, Any]) -> None:
    """Move the cluster to the requested tlsMode with MongoDB's staged procedure:
    disabled → allowTLS (rolling restart with certificates) → preferTLS → requireTLS (runtime setParameter).
    Going back down uses setParameter, then a rolling restart without TLS when disabling."""
    dep, managed = ctx.mgr.managed(p["deployment_id"])
    dep_id = dep["id"]
    tls = managed.setdefault("tls", {"mode": "disabled"})
    target = p["mode"]
    if target not in TLS_MODES:
        raise JobError(f"Invalid TLS mode: {target}")
    current = tls.get("mode", "disabled")
    paths = T.tls_paths(managed["dep_slug"])
    material = p.get("material")   # {"ca_pem", "ca_key_pem"?, "pems": {server_id: pem}}
    if "allow_invalid_hostnames" in p:
        tls["allow_invalid_hostnames"] = bool(p["allow_invalid_hostnames"])
    ctx.log(f"TLS mode {current} → {target}")

    def configs_with(mode: str) -> None:
        for m in managed["members"]:
            m["config"] = T.set_tls(T.member_config(managed, m), mode, paths, bool(tls.get("allow_invalid_hostnames")))

    async def set_mode_runtime(mode: str) -> None:
        for m in managed["members"]:
            await ctx.mongo(ctx.mgr.ops.set_parameter, ctx.mgr.direct(dep_id, managed, m["host"]), "tlsMode", mode)
        ctx.log(f"tlsMode set to {mode} on every process")
        tls["mode"] = mode
        ctx.mgr.save_managed(dep_id, managed)

    if material and target != "disabled":
        tls.update({k: v for k, v in material.items() if k in ("ca_pem", "ca_key_pem", "generated")})
        tls["pems"] = material["pems"]
        ctx.log("Copying the CA and server certificates to the servers")
        done: set[int] = set()
        for m in managed["members"]:
            if m["server_id"] in done:
                continue
            done.add(m["server_id"])
            srv = ctx.mgr.server(m["server_id"])
            await write_remote(ctx, srv, paths["ca"], tls["ca_pem"], "Writing the CA certificate", "444", "mongod:mongod")
            await write_remote(ctx, srv, paths["pem"], tls["pems"][str(m["server_id"])],
                               "Writing the server certificate and key", "400", "mongod:mongod")
        ctx.mgr.save_managed(dep_id, managed)

    if target != "disabled" and current == "disabled":
        if not tls.get("ca_pem"):
            raise JobError("No certificates: generate or upload them")
        ctx.log("Stage 1 · Rolling restart with TLS in allowTLS mode (TLS and plain connections accepted)")
        configs_with("allowTLS")
        await apply_rolling_config(ctx, dep, managed)
        tls["mode"] = "allowTLS"
        ctx.mgr.set_client_tls(dep_id, managed, True)
        current = "allowTLS"
    elif material and current != "disabled":
        ctx.log("Rolling restart to load the new certificates")
        configs_with(current)
        await apply_rolling_config(ctx, dep, managed)

    if target == "disabled":
        while TLS_MODES.index(current) > TLS_MODES.index("allowTLS"):
            current = TLS_MODES[TLS_MODES.index(current) - 1]
            await set_mode_runtime(current)
        if current != "disabled":
            ctx.mgr.set_client_tls(dep_id, managed, False)
            ctx.log("Rolling restart without TLS")
            configs_with("disabled")
            await apply_rolling_config(ctx, dep, managed)
        tls.update({"mode": "disabled", "client": False})
        ctx.mgr.save_managed(dep_id, managed)
        return

    while TLS_MODES.index(current) < TLS_MODES.index(target):
        current = TLS_MODES[TLS_MODES.index(current) + 1]
        await set_mode_runtime(current)
    while TLS_MODES.index(current) > TLS_MODES.index(target):
        current = TLS_MODES[TLS_MODES.index(current) - 1]
        await set_mode_runtime(current)
    ctx.log("Saving the final TLS mode in the configuration files (no restart needed)")
    configs_with(target)
    for m in managed["members"]:
        await write_member_config(ctx, managed, m)
    tls["mode"] = target
    ctx.mgr.set_client_tls(dep_id, managed, True)


async def apply_rolling_config(ctx: JobContext, dep: dict[str, Any], managed: dict[str, Any]) -> None:
    ctx.mgr.save_managed(dep["id"], managed)
    for m in managed["members"]:
        await write_member_config(ctx, managed, m)
    await rolling(ctx, dep, managed, force=False)
