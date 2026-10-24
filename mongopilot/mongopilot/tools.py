"""Database tools run from MongoPilot against one node: mongosh sessions, mongodump, mongoexport and data access
for the Schema Analyzer.

The MongoDB command line tools run inside the MongoPilot container (the Docker image installs mongosh and the
MongoDB Database Tools). Credentials never reach the database: the connection string is built per request and
passed to the tools through a temporary configuration file (mongodump, mongoexport) or the command line (mongosh,
inside the container only).
"""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import fcntl
import gzip
import json
import logging
import os
import re
import shutil
import signal
import struct
import subprocess
import termios
import threading
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, quote_plus, urlencode

log = logging.getLogger("mongopilot.tools")

AUTH_MODES = ("admin", "monitor", "custom")
MAX_SHELLS = 12
SHELL_IDLE_SECONDS = 1800
SHELL_BUFFER_CHARS = 2_000_000
MAX_RUNNING_EXPORTS = 3


class ToolError(Exception):
    pass


# ---------------------------------------------------------------------------
# connection strings
# ---------------------------------------------------------------------------

def _split(uri: str) -> tuple[str, str, str, list[tuple[str, str]]]:
    scheme, rest = uri.split("://", 1)
    head, _, query = rest.partition("?")
    hostpart = head.split("/", 1)[0]
    creds, _, hosts = hostpart.rpartition("@") if "@" in hostpart else ("", "", hostpart)
    return scheme, creds, hosts, parse_qsl(query, keep_blank_values=True)


def node_uri(uri: str, host: str, credentials: tuple[str, str, str] | None = None) -> str:
    """Direct connection to one node, keeping the TLS and auth options of the deployment's connection string.

    credentials = (user, password, authSource) replaces the user of the connection string.
    """
    scheme, creds, _hosts, params = _split(uri)
    drop = {"replicaset", "directconnection", "readpreference", "readpreferencetags", "srvservicename",
            "srvmaxhosts", "loadbalanced", "maxstalenessseconds"}
    if credentials:
        drop |= {"authsource", "authmechanism"}
    out = [(k, v) for k, v in params if k.lower() not in drop]
    if scheme == "mongodb+srv" and not any(k.lower() in ("tls", "ssl") for k, _ in out):
        out.append(("tls", "true"))   # SRV connection strings enable TLS by default
    if credentials:
        user, password, source = credentials
        creds = f"{quote_plus(user)}:{quote_plus(password)}" if user else ""
        if user:
            out.append(("authSource", source or "admin"))
    out.append(("directConnection", "true"))
    return f"mongodb://{creds + '@' if creds else ''}{host}/?{urlencode(out, safe='/:,')}"


def redact(text: str) -> str:
    """Hide passwords in connection strings."""
    return re.sub(r"(mongodb(?:\+srv)?://[^:/@\s]+:)[^@\s]+@", r"\1****@", text)


def user_of(uri: str) -> str:
    _, creds, _, params = _split(uri)
    from urllib.parse import unquote_plus
    return unquote_plus(creds.split(":", 1)[0]) if creds else "(no authentication)"


# ---------------------------------------------------------------------------
# mongodump / mongoexport / mongorestore / mongoimport command lines
# ---------------------------------------------------------------------------

def _json_arg(name: str, raw: Any) -> str | None:
    if raw in (None, ""):
        return None
    text = raw if isinstance(raw, str) else json.dumps(raw)
    try:
        value = json.loads(text)
    except ValueError as exc:
        raise ToolError(f"{name} must be valid JSON (Extended JSON v2): {exc}") from None
    if not isinstance(value, dict):
        raise ToolError(f"{name} must be a JSON object")
    return json.dumps(value, separators=(",", ":"))


def _int_arg(name: str, raw: Any, minimum: int = 0) -> int | None:
    if raw in (None, ""):
        return None
    try:
        v = int(raw)
    except (TypeError, ValueError):
        raise ToolError(f"{name} must be a number") from None
    if v < minimum:
        raise ToolError(f"{name} must be at least {minimum}")
    return v


NAME_RE = re.compile(r"^[^/\\. \"$*<>:|?\x00]+$")
COLL_RE = re.compile(r"^[^$\x00]+$")


def dump_plan(p: dict[str, Any], role: str | None) -> tuple[list[str], str]:
    """Validate mongodump options and return (arguments, file suffix). Output: one gzipped archive."""
    db, coll = (p.get("db") or "").strip(), (p.get("collection") or "").strip()
    if db and not NAME_RE.match(db):
        raise ToolError("Invalid database name")
    if coll and (not db or not COLL_RE.match(coll)):
        raise ToolError("A collection needs a database" if not db else "Invalid collection name")
    args = ["--gzip"]
    if db:
        args.append(f"--db={db}")
    if coll:
        args.append(f"--collection={coll}")
    query = _json_arg("Query", p.get("query"))
    if query:
        if not coll:
            raise ToolError("A query can only be used when dumping one collection")
        args.append(f"--query={query}")
    for ex in [x.strip() for x in (p.get("exclude_collections") or "").split(",") if x.strip()]:
        if not db or coll:
            raise ToolError("Excluded collections need a database and no single collection")
        args.append(f"--excludeCollection={ex}")
    if p.get("oplog"):
        if db or coll:
            raise ToolError("--oplog only works for a full dump (no database or collection)")
        if role not in ("PRIMARY", "SECONDARY"):
            raise ToolError("--oplog needs a replica set member (not a mongos or standalone)")
        args.append("--oplog")
    if p.get("users_and_roles"):
        if not db or coll:
            raise ToolError("Users and roles can only be dumped together with one database")
        args.append("--dumpDbUsersAndRoles")
    par = _int_arg("Parallel collections", p.get("parallel"), 1)
    if par:
        args.append(f"--numParallelCollections={min(par, 16)}")
    if p.get("force_table_scan"):
        args.append("--forceTableScan")
    if p.get("read_preference") in ("primary", "primaryPreferred", "secondary", "secondaryPreferred", "nearest"):
        args.append(f"--readPreference={p['read_preference']}")
    return args, ".archive.gz"


def export_plan(p: dict[str, Any]) -> tuple[list[str], str]:
    """Validate mongoexport options and return (arguments, file suffix)."""
    db, coll = (p.get("db") or "").strip(), (p.get("collection") or "").strip()
    if not db or not coll:
        raise ToolError("mongoexport needs a database and a collection")
    if not NAME_RE.match(db) or not COLL_RE.match(coll):
        raise ToolError("Invalid database or collection name")
    fmt = p.get("format") or "json"
    if fmt not in ("json", "csv"):
        raise ToolError("Format must be json or csv")
    args = [f"--db={db}", f"--collection={coll}", f"--type={fmt}"]
    fields = ",".join(f.strip() for f in (p.get("fields") or "").split(",") if f.strip())
    if fmt == "csv" and not fields:
        raise ToolError("CSV exports need the list of fields")
    if fields:
        args.append(f"--fields={fields}")
    for name, flag in (("Query", "--query"), ("Sort", "--sort")):
        v = _json_arg(name, p.get(name.lower()))
        if v:
            args.append(f"{flag}={v}")
    for name, flag in (("Limit", "--limit"), ("Skip", "--skip")):
        v = _int_arg(name, p.get(name.lower()), 0)
        if v:
            args.append(f"{flag}={v}")
    if fmt == "json":
        if p.get("json_array"):
            args.append("--jsonArray")
        if p.get("pretty"):
            args.append("--pretty")
        if p.get("json_format") in ("relaxed", "canonical"):
            args.append(f"--jsonFormat={p['json_format']}")
    elif p.get("no_header"):
        args.append("--noHeaderLine")
    if p.get("read_preference") in ("primary", "primaryPreferred", "secondary", "secondaryPreferred", "nearest"):
        args.append(f"--readPreference={p['read_preference']}")
    suffix = f".{fmt}" + (".gz" if p.get("gzip") else "")
    return args, suffix


NS_PATTERN_RE = re.compile(r"^[\w*$\-]+(\.[\w*$\-.]+)?$")
RESTORE_EXT = (".archive", ".archive.gz", ".agz")
IMPORT_EXT = {".json": "json", ".csv": "csv", ".tsv": "tsv"}
UPLOAD_EXT = (".archive", ".archive.gz", ".agz", ".gz", ".json", ".json.gz", ".csv", ".csv.gz", ".tsv", ".tsv.gz")
WRITE_ROLES = ("PRIMARY", "MONGOS", "STANDALONE")


def _ns_list(name: str, raw: Any) -> list[str]:
    items = [x.strip() for x in str(raw or "").split(",") if x.strip()]
    for x in items:
        if not NS_PATTERN_RE.match(x):
            raise ToolError(f"{name}: '{x}' is not a valid namespace pattern (db.collection, * as wildcard)")
    return items


def restorable(file_name: str) -> bool:
    return file_name.endswith(RESTORE_EXT)


def import_type(file_name: str) -> str | None:
    base = file_name[:-3] if file_name.endswith(".gz") else file_name
    return next((t for ext, t in IMPORT_EXT.items() if base.endswith(ext)), None)


def restore_plan(p: dict[str, Any], source_name: str) -> list[str]:
    """Validate mongorestore options for an archive file (as written by mongodump --archive)."""
    if not restorable(source_name):
        raise ToolError("mongorestore restores archive files written by mongodump --archive (.archive or .archive.gz)")
    args = []
    if source_name.endswith((".gz", ".agz")):
        args.append("--gzip")
    for x in _ns_list("Include", p.get("ns_include")):
        args.append(f"--nsInclude={x}")
    for x in _ns_list("Exclude", p.get("ns_exclude")):
        args.append(f"--nsExclude={x}")
    ns_from, ns_to = _ns_list("Rename from", p.get("ns_from")), _ns_list("Rename to", p.get("ns_to"))
    if len(ns_from) != len(ns_to):
        raise ToolError("Rename needs as many target namespaces as source namespaces")
    for a, b in zip(ns_from, ns_to):
        args += [f"--nsFrom={a}", f"--nsTo={b}"]
    flags = {"drop": "--drop", "dry_run": "--dryRun", "oplog_replay": "--oplogReplay",
             "no_index_restore": "--noIndexRestore", "maintain_insertion_order": "--maintainInsertionOrder",
             "stop_on_error": "--stopOnError", "bypass_validation": "--bypassDocumentValidation",
             "preserve_uuid": "--preserveUUID", "users_and_roles": "--restoreDbUsersAndRoles"}
    for key, flag in flags.items():
        if p.get(key):
            args.append(flag)
    if p.get("preserve_uuid") and not p.get("drop"):
        raise ToolError("--preserveUUID requires --drop")
    if p.get("oplog_replay") and (ns_from or p.get("ns_include") or p.get("ns_exclude")):
        raise ToolError("--oplogReplay restores the whole dump: remove the namespace filters")
    par = _int_arg("Parallel collections", p.get("parallel"), 1)
    if par:
        args.append(f"--numParallelCollections={min(par, 16)}")
    workers = _int_arg("Insertion workers", p.get("workers"), 1)
    if workers:
        args.append(f"--numInsertionWorkersPerCollection={min(workers, 16)}")
    return args


def import_plan(p: dict[str, Any], source_name: str) -> list[str]:
    """Validate mongoimport options."""
    db, coll = (p.get("db") or "").strip(), (p.get("collection") or "").strip()
    if not db or not coll:
        raise ToolError("mongoimport needs a database and a collection")
    if not NAME_RE.match(db) or not COLL_RE.match(coll):
        raise ToolError("Invalid database or collection name")
    fmt = p.get("format") or import_type(source_name) or "json"
    if fmt not in ("json", "csv", "tsv"):
        raise ToolError("Format must be json, csv or tsv")
    args = [f"--db={db}", f"--collection={coll}", f"--type={fmt}"]
    fields = ",".join(f.strip() for f in (p.get("fields") or "").split(",") if f.strip())
    if fmt == "json":
        if p.get("json_array"):
            args.append("--jsonArray")
    else:
        if p.get("headerline"):
            args.append("--headerline")
        elif fields:
            args.append(f"--fields={fields}")
        else:
            raise ToolError("CSV and TSV imports need the header line or the list of fields")
        if p.get("columns_have_types"):
            args.append("--columnsHaveTypes")
        if p.get("ignore_blanks"):
            args.append("--ignoreBlanks")
    mode = p.get("mode") or "insert"
    if mode not in ("insert", "upsert", "merge", "delete"):
        raise ToolError("Mode must be insert, upsert, merge or delete")
    if mode != "insert":
        args.append(f"--mode={mode}")
    upsert_fields = ",".join(f.strip() for f in (p.get("upsert_fields") or "").split(",") if f.strip())
    if upsert_fields:
        if mode == "insert":
            raise ToolError("Upsert fields are used with the upsert, merge or delete modes")
        args.append(f"--upsertFields={upsert_fields}")
    for key, flag in (("drop", "--drop"), ("stop_on_error", "--stopOnError"),
                      ("maintain_insertion_order", "--maintainInsertionOrder"),
                      ("bypass_validation", "--bypassDocumentValidation")):
        if p.get(key):
            args.append(flag)
    if p.get("drop") and mode != "insert":
        raise ToolError("--drop can only be used with the insert mode")
    workers = _int_arg("Insertion workers", p.get("workers"), 1)
    if workers:
        args.append(f"--numInsertionWorkers={min(workers, 16)}")
    return args


def safe_name(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-")[:60] or "x"


# ---------------------------------------------------------------------------
# exports (mongodump / mongoexport runs)
# ---------------------------------------------------------------------------

class SubprocessToolRunner:
    """Runs the MongoDB Database Tools installed in the container."""

    def __init__(self, binaries: dict[str, str]):
        self.binaries = binaries

    def available(self, tool: str) -> bool:
        return shutil.which(self.binaries.get(tool, tool)) is not None

    async def run(self, tool: str, uri: str, args: list[str], workdir: Path, on_line, register) -> int:
        """args include the file arguments (--archive, --out, --file) with absolute paths."""
        binary = shutil.which(self.binaries.get(tool, tool))
        if not binary:
            raise ToolError(f"{tool} is not installed in the MongoPilot container (MongoDB Database Tools)")
        cfg = workdir / f".{uuid.uuid4().hex}.cfg.yaml"
        fd = os.open(cfg, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(f"uri: {json.dumps(uri)}\n")
        try:
            proc = await asyncio.create_subprocess_exec(
                binary, f"--config={cfg}", *args,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, start_new_session=True)
            register(proc)
            assert proc.stdout is not None
            async for raw in proc.stdout:
                on_line(raw.decode("utf-8", "replace").rstrip())
            return await proc.wait()
        finally:
            with contextlib.suppress(OSError):
                cfg.unlink()


class ExportManager:
    def __init__(self, db, settings, runner, data=None):
        self.db = db
        self.dir = Path(settings.data_dir) / "exports"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.runner = runner
        self.data = data
        self.procs: dict[int, Any] = {}
        self.tasks: dict[int, asyncio.Task] = {}
        db.execute("UPDATE exports SET status = 'failed', error = 'Interrupted by a restart' WHERE status = 'running'")

    def path(self, row: dict[str, Any]) -> Path | None:
        return self.dir / row["file_name"] if row.get("file_name") else None

    def start(self, kind: str, dep: dict[str, Any], host: str, role: str | None, uri: str, params: dict[str, Any],
              description: str, actor: str) -> int:
        if kind in ("restore", "import"):
            return self._start_write(kind, dep, host, role, uri, params, description, actor)
        tool = {"dump": "mongodump", "export": "mongoexport"}[kind]
        args, suffix = dump_plan(params, role) if kind == "dump" else export_plan(params)
        self._check_capacity()
        ts = time.strftime("%Y%m%d-%H%M%S")
        what = params.get("db") or "all"
        if params.get("collection"):
            what += "." + params["collection"]
        file_name = f"{safe_name(dep['name'])}-{safe_name(host.replace(':', '_'))}-{safe_name(what)}-{ts}{suffix}"
        gz_after = kind == "export" and bool(params.get("gzip"))
        raw_name = file_name[:-3] if gz_after else file_name
        out_flag = "--archive" if kind == "dump" else "--out"
        command = redact(f"{tool} --uri={uri.split('?')[0]} " + " ".join(_quote(a) for a in args)
                         + f" {out_flag}={raw_name}")
        row_id = self.db.insert("exports", {
            "deployment_id": dep["id"], "kind": kind, "host": host, "description": description.strip()[:500],
            "params": json.dumps(params), "command": command, "status": "running", "file_name": file_name,
            "created_by": actor, "created_at": int(time.time())})
        out = self.dir / raw_name
        io_args = [f"--archive={out}"] if kind == "dump" else [f"--out={out}"]
        self.tasks[row_id] = asyncio.get_running_loop().create_task(
            self._run(row_id, tool, uri, args + io_args, out, gz_after))
        return row_id

    def _check_capacity(self) -> None:
        running = self.db.one("SELECT COUNT(*) AS n FROM exports WHERE status = 'running'")["n"]
        if running >= MAX_RUNNING_EXPORTS:
            raise ToolError(f"{running} tool runs are already in progress: wait for one to finish")

    def source(self, source_id: Any, kind: str) -> dict[str, Any]:
        try:
            row = self.db.one("SELECT * FROM exports WHERE id = ?", (int(source_id),))
        except (TypeError, ValueError):
            row = None
        if not row or row["status"] != "success" or not row.get("file_name") or not (self.dir / row["file_name"]).exists():
            raise ToolError("Choose a file that is available in MongoPilot")
        if kind == "restore" and not restorable(row["file_name"]):
            raise ToolError("mongorestore needs an archive written by mongodump --archive (.archive or .archive.gz)")
        if kind == "import" and not import_type(row["file_name"]):
            raise ToolError("mongoimport needs a .json, .csv or .tsv file (optionally gzipped)")
        return row

    def _start_write(self, kind: str, dep: dict[str, Any], host: str, role: str | None, uri: str,
                     params: dict[str, Any], description: str, actor: str) -> int:
        tool = {"restore": "mongorestore", "import": "mongoimport"}[kind]
        if role not in WRITE_ROLES:
            raise ToolError(f"{tool} writes data: choose the primary or a mongos router (this node is "
                            f"{(role or 'unknown').lower()})")
        src = self.source(params.get("source_id"), kind)
        args = restore_plan(params, src["file_name"]) if kind == "restore" else import_plan(params, src["file_name"])
        self._check_capacity()
        path = self.dir / src["file_name"]
        gunzip = kind == "import" and src["file_name"].endswith(".gz")
        flag = "--archive" if kind == "restore" else "--file"
        params = {**params, "source_file": src["file_name"]}
        command = redact(f"{tool} --uri={uri.split('?')[0]} " + " ".join(_quote(a) for a in args)
                         + f" {flag}={src['file_name'][:-3] if gunzip else src['file_name']}")
        row_id = self.db.insert("exports", {
            "deployment_id": dep["id"], "kind": kind, "host": host, "description": description.strip()[:500],
            "params": json.dumps(params), "command": command, "status": "running", "file_name": None,
            "created_by": actor, "created_at": int(time.time())})
        self.tasks[row_id] = asyncio.get_running_loop().create_task(
            self._run_write(row_id, tool, uri, args, flag, path, gunzip))
        return row_id

    async def _run_write(self, row_id: int, tool: str, uri: str, args: list[str], flag: str, path: Path,
                         gunzip: bool) -> None:
        tmp = None
        try:
            if gunzip:   # mongoimport does not read compressed files
                tmp = self.dir / f".{uuid.uuid4().hex}-{path.name[:-3]}"
                await asyncio.to_thread(_gunzip_file, path, tmp)
            await self._run(row_id, tool, uri, args + [f"{flag}={tmp or path}"], None, False)
        finally:
            if tmp is not None:
                with contextlib.suppress(OSError):
                    tmp.unlink()

    async def save_upload(self, dep: dict[str, Any], name: str, chunks, description: str, actor: str,
                          max_bytes: int) -> int:
        """Store a file uploaded from the browser, to be restored or imported."""
        name = os.path.basename(name or "")
        if not name.lower().endswith(UPLOAD_EXT):
            raise ToolError("Upload a mongodump archive (.archive, .archive.gz) or a .json, .csv, .tsv file "
                            "(optionally .gz)")
        ts = time.strftime("%Y%m%d-%H%M%S")
        file_name = f"upload-{ts}-{safe_name(name)}"
        if name.lower().endswith(".gz") and not file_name.endswith(".gz"):
            file_name += ".gz"
        path = self.dir / file_name
        size = 0
        try:
            with path.open("wb") as f:
                async for chunk in chunks:
                    size += len(chunk)
                    if size > max_bytes:
                        raise ToolError(f"The file is larger than {max_bytes // 1048576} MB (MP_MAX_UPLOAD_MB)")
                    f.write(chunk)
        except BaseException:
            with contextlib.suppress(OSError):
                path.unlink()
            raise
        if not size:
            path.unlink()
            raise ToolError("The file is empty")
        return self.db.insert("exports", {
            "deployment_id": dep["id"], "kind": "upload", "host": "", "description": description.strip()[:500],
            "params": json.dumps({"original_name": name}), "command": "", "status": "success", "file_name": file_name,
            "size": size, "created_by": actor, "created_at": int(time.time()), "finished_at": int(time.time())})

    async def _run(self, row_id: int, tool: str, uri: str, args: list[str], out: Path | None, gz_after: bool) -> None:
        lines: list[str] = []
        last_flush = [0.0]

        def on_line(line: str) -> None:
            if not line:
                return
            lines.append(redact(line))
            if len(lines) > 400:
                del lines[:100]
            if time.time() - last_flush[0] > 1:
                last_flush[0] = time.time()
                self.db.update("exports", row_id, {"log": "\n".join(lines)})

        def register(proc) -> None:
            self.procs[row_id] = proc

        status, error = "success", None
        try:
            rc = await self.runner.run(tool, uri, args, self.dir, on_line, register)
            row = self.db.one("SELECT status FROM exports WHERE id = ?", (row_id,))
            if row and row["status"] == "cancelled":
                status, error = "cancelled", "Cancelled"
            elif rc != 0:
                status = "failed"
                error = next((ln for ln in reversed(lines) if "error" in ln.lower() or "failed" in ln.lower()),
                             f"{tool} exited with code {rc}")
                # tools prefix their messages with a timestamp and a tab
                error = re.sub(r"^\d{4}-\d\d-\d\dT[\d:.]+[+-]\d{4}\s+", "", error).removeprefix("Failed: ")
            elif gz_after and out is not None:
                await asyncio.to_thread(_gzip_file, out)
                out = out.with_name(out.name + ".gz")
        except ToolError as exc:
            status, error = "failed", str(exc)
        except Exception as exc:   # noqa: BLE001 - reported to the user
            log.exception("%s failed", tool)
            status, error = "failed", f"{type(exc).__name__}: {exc}"
        finally:
            self.procs.pop(row_id, None)
            self.tasks.pop(row_id, None)
        size = out.stat().st_size if out is not None and out.exists() else None
        if status != "success" and out is not None:
            for f in (out, out.with_name(out.name + ".gz")):
                with contextlib.suppress(OSError):
                    f.unlink()
            size = None
        self.db.update("exports", row_id, {"status": status, "error": error, "log": "\n".join(lines),
                                           "size": size, "finished_at": int(time.time())})

    def cancel(self, row_id: int) -> None:
        self.db.update("exports", row_id, {"status": "cancelled"})
        proc = self.procs.get(row_id)
        if proc is not None:
            with contextlib.suppress(ProcessLookupError, OSError):
                os.killpg(proc.pid, signal.SIGTERM)

    def delete(self, row: dict[str, Any]) -> None:
        if row["status"] == "running":
            self.cancel(row["id"])
        p = self.path(row)
        if p:
            with contextlib.suppress(OSError):
                p.unlink()
        self.db.execute("DELETE FROM exports WHERE id = ?", (row["id"],))


def _quote(a: str) -> str:
    return a if re.fullmatch(r"[\w@%+=:,./-]+", a) else "'" + a.replace("'", "'\\''") + "'"


def _gunzip_file(src: Path, dst: Path) -> None:
    with gzip.open(src, "rb") as f_in, dst.open("wb") as f_out:
        shutil.copyfileobj(f_in, f_out)


def _gzip_file(path: Path) -> None:
    with path.open("rb") as src, gzip.open(path.with_name(path.name + ".gz"), "wb") as dst:
        shutil.copyfileobj(src, dst)
    path.unlink()


# ---------------------------------------------------------------------------
# mongosh sessions (pseudo-terminal, streamed to the browser with long polling)
# ---------------------------------------------------------------------------

class ShellSession:
    def __init__(self, dep_id: int, host: str, actor: str, description: str):
        self.id = uuid.uuid4().hex
        self.dep_id, self.host, self.actor, self.description = dep_id, host, actor, description
        self.created = self.last_seen = time.time()
        self.buf: list[str] = []
        self.base = 0          # characters dropped from the start of the buffer
        self.size = 0
        self.closed = False
        self.exit_code: int | None = None
        self.lock = threading.Lock()

    def emit(self, text: str) -> None:
        if not text:
            return
        with self.lock:
            self.buf.append(text)
            self.size += len(text)
            while self.size - self.base > SHELL_BUFFER_CHARS and len(self.buf) > 1:
                self.base += len(self.buf.pop(0))

    def read(self, cursor: int) -> tuple[str, int]:
        with self.lock:
            end = self.size
            if cursor >= end:
                return "", end
            text = "".join(self.buf)
            start = max(cursor, self.base) - self.base
            return text[start:], end

    def write(self, data: str) -> None:
        raise NotImplementedError

    def resize(self, rows: int, cols: int) -> None:
        pass

    def close(self) -> None:
        self.closed = True


class PtyShell(ShellSession):
    """A real mongosh process attached to a pseudo-terminal."""

    def __init__(self, argv: list[str], env: dict[str, str], rows: int, cols: int, banner: str = "", **kw):
        super().__init__(**kw)
        self.emit(banner)
        master, slave = os.openpty()
        _set_size(slave, rows, cols)
        self.proc = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=slave, env=env, close_fds=True,
                                     start_new_session=True)
        os.close(slave)
        self.master = master
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")
        threading.Thread(target=self._reader, daemon=True, name=f"mongosh-{self.id[:6]}").start()

    def _reader(self) -> None:
        while True:
            try:
                data = os.read(self.master, 65536)
            except OSError:
                break
            if not data:
                break
            self.emit(self.decoder.decode(data))
        self.exit_code = self.proc.wait()
        self.emit(f"\r\n\x1b[90m[mongosh exited with code {self.exit_code}]\x1b[0m\r\n")
        self.closed = True
        with contextlib.suppress(OSError):
            os.close(self.master)

    def write(self, data: str) -> None:
        if self.closed:
            return
        with contextlib.suppress(OSError):
            os.write(self.master, data.encode())

    def resize(self, rows: int, cols: int) -> None:
        with contextlib.suppress(OSError):
            _set_size(self.master, rows, cols)
            os.killpg(self.proc.pid, signal.SIGWINCH)

    def close(self) -> None:
        if self.proc.poll() is None:
            with contextlib.suppress(ProcessLookupError, OSError):
                os.killpg(self.proc.pid, signal.SIGTERM)
        super().close()


def _set_size(fd: int, rows: int, cols: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", max(5, rows), max(20, cols), 0, 0))


class ShellManager:
    def __init__(self, settings, factory=None):
        self.settings = settings
        self.sessions: dict[str, ShellSession] = {}
        self.factory = factory      # demo: callable(uri, rows, cols, **session fields) -> ShellSession
        self.home = Path(settings.data_dir) / "mongosh-home"

    def available(self) -> bool:
        return self.factory is not None or shutil.which(self.settings.mongosh_binary) is not None

    def open(self, uri: str, host: str, rows: int, cols: int, **kw) -> ShellSession:
        self.reap()
        if len(self.sessions) >= MAX_SHELLS:
            raise ToolError(f"Too many open mongosh sessions ({MAX_SHELLS}): close some of them")
        kw["host"] = host
        if self.factory is not None:
            sess = self.factory(uri, rows, cols, **kw)
        else:
            binary = shutil.which(self.settings.mongosh_binary)
            if not binary:
                raise ToolError("mongosh is not installed in the MongoPilot container")
            self.home.mkdir(parents=True, exist_ok=True)
            env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(self.home), "TERM": "xterm-256color",
                   "LANG": "C.UTF-8", "MONGOSH_NO_UPDATE_CHECK": "1"}
            banner = f"\x1b[90mConnecting to {host} as {user_of(uri)} with mongosh…\x1b[0m\r\n"
            sess = PtyShell([binary, uri, "--quiet"], env, rows, cols, banner=banner, **kw)
        self.sessions[sess.id] = sess
        return sess

    def get(self, sid: str) -> ShellSession:
        sess = self.sessions.get(sid)
        if not sess:
            raise ToolError("This mongosh session has ended")
        sess.last_seen = time.time()
        return sess

    def reap(self) -> None:
        nowt = time.time()
        for sid, sess in list(self.sessions.items()):
            if nowt - sess.last_seen > SHELL_IDLE_SECONDS or (sess.closed and nowt - sess.last_seen > 120):
                sess.close()
                self.sessions.pop(sid, None)

    def close(self, sid: str) -> None:
        sess = self.sessions.pop(sid, None)
        if sess:
            sess.close()

    def close_all(self) -> None:
        for sid in list(self.sessions):
            self.close(sid)


# ---------------------------------------------------------------------------
# data access for the Schema Analyzer and the namespace pickers (pymongo)
# ---------------------------------------------------------------------------

class MongoData:
    timeout_ms = 8000

    def _client(self, uri: str):
        from pymongo import MongoClient
        return MongoClient(uri, serverSelectionTimeoutMS=self.timeout_ms, connectTimeoutMS=self.timeout_ms,
                           socketTimeoutMS=120000, appname="MongoPilot tools")

    def namespaces(self, uri: str) -> list[dict[str, Any]]:
        client = self._client(uri)
        try:
            out = []
            for name in sorted(client.list_database_names()):
                try:
                    colls = [{"name": c["name"], "type": c.get("type", "collection")}
                             for c in client[name].list_collections()
                             if not c["name"].startswith("system.")]
                except Exception as exc:   # noqa: BLE001
                    out.append({"db": name, "collections": [], "error": str(exc)[:200]})
                    continue
                out.append({"db": name, "collections": sorted(colls, key=lambda c: c["name"])})
            return out
        finally:
            client.close()

    def sample(self, uri: str, db: str, coll: str, flt: str | None, size: int) -> tuple[list[dict[str, Any]], int | None]:
        from bson import json_util
        query = json_util.loads(flt) if flt and flt.strip() else {}
        if not isinstance(query, dict):
            raise ToolError("The filter must be a JSON object")
        client = self._client(uri)
        try:
            c = client[db][coll]
            try:
                total = c.estimated_document_count(maxTimeMS=5000)
            except Exception:   # noqa: BLE001 - views do not support it
                total = None
            pipeline: list[dict[str, Any]] = [{"$match": query}] if query else []
            pipeline.append({"$sample": {"size": size}})
            docs = list(c.aggregate(pipeline, maxTimeMS=60000, allowDiskUse=False))
            return docs, total
        finally:
            client.close()
