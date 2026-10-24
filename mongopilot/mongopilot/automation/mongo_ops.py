"""MongoDB administrative operations used by automation (synchronous, via pymongo)."""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qsl, quote_plus, urlencode

# Extra privileges for the monitoring user, needed by the Query Performance section:
# list collections and indexes, read $indexStats and collection statistics. No data access.
MONITOR_ROLE = "mongopilotIndexInspector"
MONITOR_ROLE_PRIVILEGES = [
    {"resource": {"db": "", "collection": ""},
     "actions": ["listCollections", "listIndexes", "indexStats", "collStats", "dbStats"]},
]


def admin_uri(user: str, password: str, hosts: list[str], rs_name: str | None = None,
              extra: str = "", tls: dict[str, Any] | None = None) -> str:
    q = "authSource=admin"
    if rs_name:
        q += f"&replicaSet={rs_name}"
    if extra:
        q += f"&{extra.lstrip('&')}"
    uri = f"mongodb://{quote_plus(user)}:{quote_plus(password)}@{','.join(hosts)}/?{q}"
    return with_tls(uri, tls)


def with_tls(uri: str, tls: dict[str, Any] | None) -> str:
    """Add or remove the TLS client options MongoPilot uses for a deployment.

    tls = {"enabled": bool, "ca_file": path, "allow_invalid_hostnames": bool}
    """
    base, _, query = uri.partition("?")
    params = [(k, v) for k, v in parse_qsl(query, keep_blank_values=True)
              if k.lower() not in ("tls", "ssl", "tlscafile", "tlsallowinvalidhostnames")]
    if tls and tls.get("enabled"):
        params.append(("tls", "true"))
        if tls.get("ca_file"):
            params.append(("tlsCAFile", tls["ca_file"]))
        if tls.get("allow_invalid_hostnames"):
            params.append(("tlsAllowInvalidHostnames", "true"))
    return f"{base}?{urlencode(params, safe='/:,')}" if params else base


def direct_uri(uri: str, host: str) -> str:
    """Same credentials and options, direct connection to one member."""
    scheme, rest = uri.split("://", 1)
    creds, _, tail = rest.rpartition("@") if "@" in rest.split("/", 1)[0] else ("", "", rest)
    _hosts, _, query = tail.partition("/")
    params = [(k, v) for k, v in parse_qsl(query.lstrip("?"), keep_blank_values=True)
              if k.lower() not in ("replicaset", "directconnection")]
    params.append(("directConnection", "true"))
    return f"{scheme}://{creds + '@' if creds else ''}{host}/?{urlencode(params, safe='/:,')}"


def coerce_parameter(current: Any, raw: Any) -> Any:
    """Convert a value from the UI to the type of the parameter's current value."""
    import json
    if isinstance(current, bool):
        if isinstance(raw, bool):
            return raw
        s = str(raw).strip().lower()
        if s in ("true", "1", "yes", "on"):
            return True
        if s in ("false", "0", "no", "off"):
            return False
        raise ValueError("Expected true or false")
    if isinstance(current, int):
        try:
            return int(str(raw).strip())
        except ValueError:
            raise ValueError("Expected an integer") from None
    if isinstance(current, float):
        try:
            return float(str(raw).strip())
        except ValueError:
            raise ValueError("Expected a number") from None
    if isinstance(current, (dict, list)):
        if isinstance(raw, (dict, list)):
            return raw
        try:
            return json.loads(raw)
        except ValueError:
            raise ValueError("Expected a JSON value") from None
    return raw if isinstance(raw, str) else str(raw)


class MongoOps:
    """Real implementation. Every method opens and closes its own connection."""

    def __init__(self, timeout_ms: int = 8000):
        self.timeout_ms = timeout_ms

    def _client(self, uri: str):
        from pymongo import MongoClient
        return MongoClient(uri, serverSelectionTimeoutMS=self.timeout_ms, connectTimeoutMS=self.timeout_ms,
                           appname="MongoPilot-automation")

    def _cmd(self, uri: str, cmd: Any, **kw) -> dict[str, Any]:
        client = self._client(uri)
        try:
            return dict(client.admin.command(cmd, **kw))
        finally:
            client.close()

    def hello(self, uri: str) -> dict[str, Any]:
        return self._cmd(uri, "hello")

    def initiate(self, uri: str, config: dict[str, Any]) -> dict[str, Any]:
        return self._cmd(uri, {"replSetInitiate": config})

    def create_user(self, uri: str, user: str, password: str, roles: list[Any]) -> None:
        self._cmd(uri, {"createUser": user, "pwd": password, "roles": roles})

    def create_role(self, uri: str, role: str, privileges: list[dict[str, Any]]) -> None:
        from pymongo.errors import OperationFailure
        try:
            self._cmd(uri, {"createRole": role, "privileges": privileges, "roles": []})
        except OperationFailure as exc:
            if exc.code != 51002:  # role already exists
                raise

    def rs_status(self, uri: str) -> dict[str, Any]:
        return self._cmd(uri, "replSetGetStatus")

    def step_down(self, uri: str, seconds: int = 60, catch_up: int = 30) -> None:
        from pymongo.errors import AutoReconnect, ConnectionFailure
        try:
            self._cmd(uri, {"replSetStepDown": seconds, "secondaryCatchUpPeriodSecs": catch_up})
        except (AutoReconnect, ConnectionFailure):
            pass  # older versions close connections during step down

    def get_fcv(self, uri: str) -> str | None:
        res = self._cmd(uri, {"getParameter": 1, "featureCompatibilityVersion": 1})
        return res.get("featureCompatibilityVersion", {}).get("version")

    def set_fcv(self, uri: str, version: str) -> None:
        cmd: dict[str, Any] = {"setFeatureCompatibilityVersion": version}
        if int(version.split(".")[0]) >= 7:
            cmd["confirm"] = True
        self._cmd(uri, cmd)

    def set_parameter(self, uri: str, name: str, value: Any) -> None:
        self._cmd(uri, {"setParameter": 1, name: value})

    def add_shard(self, uri: str, conn: str, name: str) -> None:
        from pymongo.errors import OperationFailure
        try:
            self._cmd(uri, {"addShard": conn, "name": name})
        except OperationFailure as exc:
            if "already" not in str(exc).lower():
                raise

    def list_shards(self, uri: str) -> list[str]:
        return [s["_id"] for s in self._cmd(uri, "listShards").get("shards", [])]

    def balancer(self, uri: str, enabled: bool) -> None:
        self._cmd(uri, "balancerStart" if enabled else "balancerStop")

    def ping(self, uri: str) -> bool:
        return bool(self._cmd(uri, "ping").get("ok"))
