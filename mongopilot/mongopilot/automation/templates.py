"""Versions and upgrade paths, remote scripts, the configuration file model and systemd units
for managed processes (mongod and mongos)."""

from __future__ import annotations

import copy
import re
import shlex
from typing import Any

import yaml

BASE_BIN = "/opt/mongopilot/mongodb"
BASE_ETC = "/etc/mongopilot"
BASE_LOG = "/var/log/mongopilot"
DEFAULT_DATA_ROOT = "/var/lib/mongopilot"

# Release series MongoPilot can install, oldest first. 8.2 and 8.3 are minor releases available to
# Community; from 9.0 on, minor releases are Atlas-only.
SUPPORTED_MAJORS = ["4.4", "5.0", "6.0", "7.0", "8.0", "8.2", "8.3", "9.0"]
# Allowed binary upgrades between series (patch upgrades inside a series are always allowed).
# 8.0 can go straight to 9.0; before 9.0 minor releases cannot be skipped (8.2 -> 8.3 -> 9.0).
UPGRADE_PATHS: dict[str, list[str]] = {
    "4.4": ["5.0"], "5.0": ["6.0"], "6.0": ["7.0"], "7.0": ["8.0"],
    "8.0": ["9.0", "8.2"], "8.2": ["8.3"], "8.3": ["9.0"],
}
# Platforms dropped by a series (os_id, major version) -> first series without support
PLATFORM_DROPPED = {("ubuntu", "20"): "9.0", ("ubuntu", "18"): "7.0", ("amzn", "2"): "8.0",
                    ("debian", "11"): "8.0", ("debian", "10"): "7.0", ("rhel", "7"): "8.0"}
RHEL_LIKE = ("rhel", "rocky", "almalinux", "ol", "centos")


def slug(name: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", name.strip().lower()).strip("-")
    return s or "deployment"


def major(version: str) -> str:
    return ".".join(version.split(".")[:2])


def _tuple(version: str) -> tuple[int, ...]:
    return tuple(int(x) for x in version.split("."))


def validate_version(version: str) -> None:
    if not re.fullmatch(r"\d+\.\d+\.\d+", version or ""):
        raise ValueError(f"Invalid version: {version!r} (expected e.g. 9.0.1)")
    if major(version) not in SUPPORTED_MAJORS:
        raise ValueError(f"Version {version} is not supported. Supported series: {', '.join(SUPPORTED_MAJORS)}")


def fcv_options(binary_series: str) -> list[str]:
    """FCV values valid with binaries of a series: the series itself and the series it can be upgraded from."""
    return [binary_series] + [s for s, nxt in UPGRADE_PATHS.items() if binary_series in nxt]


def check_upgrade_path(current: str, target: str, fcv: str | None) -> list[str]:
    """Check MongoDB upgrade rules. Returns warnings, raises when the upgrade is not allowed."""
    validate_version(target)
    cur_m, tgt_m = major(current), major(target)
    if _tuple(target) == _tuple(current):
        raise ValueError("The target version is the current version")
    if _tuple(target) < _tuple(current):
        raise ValueError("Downgrades are not supported by MongoPilot: they require a manual procedure")
    warnings: list[str] = []
    if cur_m != tgt_m:
        allowed = UPGRADE_PATHS.get(cur_m, [])
        if tgt_m not in allowed:
            raise ValueError(f"MongoDB {cur_m} can only be upgraded to: {', '.join(allowed) or 'nothing'}"
                             + (" (upgrade one release at a time)" if allowed else ""))
        if fcv and fcv != cur_m:
            raise ValueError(f"featureCompatibilityVersion is {fcv}: it must be {cur_m} before upgrading to {tgt_m}")
        if not fcv:
            warnings.append("FCV not detected: make sure it matches the current series before proceeding")
        warnings.append(f"After the upgrade, set FCV to {tgt_m} only once you are sure you will not need to roll back")
    return warnings


# -- operating system detection ---------------------------------------

FACTS_SCRIPT = r"""
set -u
. /etc/os-release 2>/dev/null || true
echo "os_id=${ID:-unknown}"
echo "os_version=${VERSION_ID:-unknown}"
echo "os_name=${PRETTY_NAME:-unknown}"
echo "arch=$(uname -m)"
echo "cpus=$(nproc 2>/dev/null || echo ?)"
echo "mem_mb=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)"
echo "systemd=$(command -v systemctl >/dev/null && echo yes || echo no)"
echo "curl=$(command -v curl >/dev/null && echo yes || echo no)"
echo "wget=$(command -v wget >/dev/null && echo yes || echo no)"
echo "thp=$(cat /sys/kernel/mm/transparent_hugepage/enabled 2>/dev/null | sed 's/.*\[\(.*\)\].*/\1/')"
echo "whoami=$(id -un)"
"""


def parse_facts(output: str) -> dict[str, str]:
    facts: dict[str, str] = {}
    for line in output.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            facts[k.strip()] = v.strip()
    return facts


def check_platform(facts: dict[str, str], version: str) -> None:
    """Raise when the OS is known not to be supported by this MongoDB series."""
    os_id = facts.get("os_id", "").lower()
    key_id = "rhel" if os_id in RHEL_LIKE else os_id
    os_major = facts.get("os_version", "").split(".")[0]
    dropped = PLATFORM_DROPPED.get((key_id, os_major))
    if dropped and _tuple(major(version) + ".0") >= _tuple(dropped + ".0"):
        raise ValueError(f"MongoDB {major(version)} does not support {facts.get('os_name') or os_id + ' ' + os_major}: "
                         f"use a newer operating system (e.g. Ubuntu 22.04/24.04, Debian 12, RHEL 8/9)")


def download_targets(facts: dict[str, str]) -> list[str]:
    """Platform names used by MongoDB tarballs, most likely first."""
    os_id = facts.get("os_id", "").lower()
    ver = facts.get("os_version", "")
    major_ver = ver.split(".")[0]
    if os_id == "ubuntu":
        return [f"ubuntu{ver.replace('.', '')}"]
    if os_id == "debian":
        return [f"debian{major_ver}"]
    if os_id in RHEL_LIKE or os_id == "fedora":
        if major_ver == "10":
            return ["rhel10", "rhel100"]
        if major_ver == "9":
            return ["rhel93", "rhel90", "rhel9"]
        if major_ver == "8":
            return ["rhel8", "rhel80", "rhel82"]
        return [f"rhel{major_ver}0", f"rhel{major_ver}"]
    if os_id == "amzn":
        return ["amazon2023"] if ver.startswith("2023") else ["amazon2"]
    if os_id in ("sles", "opensuse-leap"):
        return [f"suse{major_ver}"]
    return []


def tarball_arch(facts: dict[str, str]) -> str:
    arch = facts.get("arch", "x86_64")
    return {"x86_64": "x86_64", "amd64": "x86_64", "aarch64": "aarch64", "arm64": "aarch64"}.get(arch, arch)


def candidate_urls(facts: dict[str, str], version: str, base: str = "https://fastdl.mongodb.org/linux") -> list[str]:
    arch = tarball_arch(facts)
    return [f"{base.rstrip('/')}/mongodb-linux-{arch}-{t}-{version}.tgz" for t in download_targets(facts)]


def install_script(version: str, urls: list[str], sha256: str | None = None) -> str:
    """Install mongod and mongos into /opt/mongopilot/mongodb/<version> (idempotent)."""
    dest = f"{BASE_BIN}/{version}"
    url_list = " ".join(shlex.quote(u) for u in urls)
    sha = shlex.quote(sha256 or "")
    return f"""
set -euo pipefail
DEST={shlex.quote(dest)}
if [ -x "$DEST/bin/mongod" ] && "$DEST/bin/mongod" --version 2>/dev/null | grep -q "v{version}"; then
  echo "MongoDB {version} already installed in $DEST"; exit 0
fi
# mongod system dependencies (best effort)
if command -v apt-get >/dev/null; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq || true
  apt-get install -y -qq openssl liblzma5 curl >/dev/null || true
  apt-get install -y -qq libcurl4 >/dev/null 2>&1 || apt-get install -y -qq libcurl4t64 >/dev/null 2>&1 || true
elif command -v dnf >/dev/null; then
  dnf install -y -q libcurl openssl xz-libs >/dev/null || true
elif command -v yum >/dev/null; then
  yum install -y -q libcurl openssl xz-libs >/dev/null || true
elif command -v zypper >/dev/null; then
  zypper -q --non-interactive install libcurl4 libopenssl1_1 >/dev/null || true
fi
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
OK=""
for URL in {url_list}; do
  echo "Download: $URL"
  if command -v curl >/dev/null; then
    curl -fsSL --retry 3 -o "$TMP/mongodb.tgz" "$URL" && OK="$URL" && break
  else
    wget -q -O "$TMP/mongodb.tgz" "$URL" && OK="$URL" && break
  fi
done
[ -n "$OK" ] || {{ echo "No valid download URL for this platform" >&2; exit 2; }}
SHA={sha}
if [ -n "$SHA" ]; then echo "$SHA  $TMP/mongodb.tgz" | sha256sum -c - ; fi
mkdir -p "$DEST"
tar -xzf "$TMP/mongodb.tgz" -C "$DEST" --strip-components=1
"$DEST/bin/mongod" --version | head -1
"""


# -- paths and units ----------------------------------------------------

def unit_name(dep_slug: str, port: int) -> str:
    return f"mongopilot-{dep_slug}-{port}.service"


def member_paths(dep_slug: str, port: int, data_root: str = DEFAULT_DATA_ROOT, binary: str = "mongod") -> dict[str, Any]:
    return {
        "dbpath": f"{data_root.rstrip('/')}/{dep_slug}/{port}" if binary == "mongod" else None,
        "logdir": f"{BASE_LOG}/{dep_slug}",
        "logpath": f"{BASE_LOG}/{dep_slug}/{binary}-{port}.log",
        "etcdir": f"{BASE_ETC}/{dep_slug}",
        "conf": f"{BASE_ETC}/{dep_slug}/{binary}-{port}.conf",
        "keyfile": f"{BASE_ETC}/{dep_slug}/keyfile",
        "unit": unit_name(dep_slug, port),
    }


def tls_paths(dep_slug: str) -> dict[str, str]:
    d = f"{BASE_ETC}/{dep_slug}/tls"
    return {"dir": d, "ca": f"{d}/ca.pem", "pem": f"{d}/server.pem"}


def binary_of(member: dict[str, Any]) -> str:
    return "mongos" if member.get("role") == "mongos" else "mongod"


def systemd_unit(dep_name: str, port: int, version: str, paths: dict[str, Any], binary: str = "mongod") -> str:
    return f"""# Generated by MongoPilot
[Unit]
Description=MongoDB {binary} {version} - {dep_name} port {port} (MongoPilot)
After=network-online.target
Wants=network-online.target

[Service]
User=mongod
Group=mongod
ExecStart={BASE_BIN}/{version}/bin/{binary} --config {paths['conf']}
LimitFSIZE=infinity
LimitCPU=infinity
LimitAS=infinity
LimitNOFILE=64000
LimitNPROC=64000
LimitMEMLOCK=infinity
TasksMax=infinity
TasksAccounting=false
Restart=on-failure
RestartSec=5
TimeoutStopSec=300

[Install]
WantedBy=multi-user.target
"""


# -- configuration file model ---------------------------------------------

CONFIG_HEADER = ("# Generated by MongoPilot. Edit it from MongoPilot (Configuration tab):\n"
                 "# changes made directly on the server are overwritten.\n")
PROTECTED_KEYS = ["net.port", "storage.dbPath", "replication.replSetName", "sharding.clusterRole",
                  "sharding.configDB", "security.keyFile", "systemLog.path", "net.tls"]


def build_config(managed: dict[str, Any], member: dict[str, Any]) -> dict[str, Any]:
    """Default configuration of a managed process."""
    role = member.get("role", "replica")
    paths = member["paths"]
    cfg: dict[str, Any] = {"systemLog": {"destination": "file", "path": paths["logpath"], "logAppend": True,
                                         "logRotate": "reopen"}}
    if role != "mongos":
        storage: dict[str, Any] = {"dbPath": paths["dbpath"]}
        if managed.get("cache_gb"):
            storage["wiredTiger"] = {"engineConfig": {"cacheSizeGB": float(managed["cache_gb"])}}
        cfg["storage"] = storage
    cfg["net"] = {"port": int(member["port"]), "bindIp": managed.get("bind_ip") or "0.0.0.0"}
    if role != "mongos":
        cfg["replication"] = {"replSetName": member.get("rs_name") or managed["rs_name"]}
    if role in ("configsvr", "shardsvr"):
        cfg["sharding"] = {"clusterRole": role}
    if role == "mongos":
        cfg["sharding"] = {"configDB": config_db_string(managed)}
    cfg["security"] = {"keyFile": paths["keyfile"]}
    if role != "mongos":
        cfg["security"]["authorization"] = "enabled"
    cfg["processManagement"] = {"timeZoneInfo": "/usr/share/zoneinfo"}
    return cfg


def config_db_string(managed: dict[str, Any]) -> str:
    cfg_members = [m for m in managed["members"] if m.get("role") == "configsvr"]
    return f"{managed['config_rs']}/" + ",".join(m["host"] for m in cfg_members)


def member_config(managed: dict[str, Any], member: dict[str, Any]) -> dict[str, Any]:
    """Current configuration of a member: the stored (possibly edited) one, or the default."""
    return copy.deepcopy(member.get("config") or build_config(managed, member))


def render_config(cfg: dict[str, Any]) -> str:
    return CONFIG_HEADER + yaml.safe_dump(cfg, sort_keys=False, default_flow_style=False, allow_unicode=True)


def parse_config(text: str) -> dict[str, Any]:
    lines = [ln for ln in (text or "").splitlines() if not ln.lstrip().startswith("#")]
    try:
        data = yaml.safe_load("\n".join(lines)) or {}
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f" (line {mark.line + 1})" if mark else ""
        raise ValueError(f"Invalid YAML{where}: {getattr(exc, 'problem', exc)}") from None
    if not isinstance(data, dict):
        raise ValueError("The configuration must be a YAML mapping (key: value)")
    return data


def flatten(d: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in d.items():
        path = f"{prefix}.{k}" if prefix else str(k)
        if isinstance(v, dict) and v:
            out.update(flatten(v, path))
        else:
            out[path] = v
    return out


def config_changes(old: dict[str, Any], new: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    fo, fn = flatten(old), flatten(new)
    sets = {k: v for k, v in fn.items() if k not in fo or fo[k] != v}
    deletes = [k for k in fo if k not in fn]
    return sets, deletes


def check_protected(old: dict[str, Any], new: dict[str, Any]) -> None:
    sets, deletes = config_changes(old, new)
    touched = sorted({k for k in list(sets) + deletes if any(k == p or k.startswith(p + ".") for p in PROTECTED_KEYS)})
    if touched:
        raise ValueError(f"These settings are managed by MongoPilot and cannot be changed here: {', '.join(touched)}")


def apply_changes(cfg: dict[str, Any], sets: dict[str, Any], deletes: list[str]) -> dict[str, Any]:
    out = copy.deepcopy(cfg)
    for path in deletes:
        if any(path == p for p in PROTECTED_KEYS):
            continue
        parts = path.split(".")
        node = out
        for p in parts[:-1]:
            node = node.get(p) if isinstance(node, dict) else None
            if node is None:
                break
        if isinstance(node, dict):
            node.pop(parts[-1], None)
    for path, value in sets.items():
        if any(path == p for p in PROTECTED_KEYS):
            continue
        parts = path.split(".")
        node = out
        for p in parts[:-1]:
            if not isinstance(node.get(p), dict):
                node[p] = {}
            node = node[p]
        node[parts[-1]] = copy.deepcopy(value)
    return _prune(out)


def _prune(d: dict[str, Any]) -> dict[str, Any]:
    for k in list(d):
        if isinstance(d[k], dict):
            _prune(d[k])
            if not d[k]:
                del d[k]
    return d


def set_tls(cfg: dict[str, Any], mode: str, paths: dict[str, str] | None, allow_invalid_hostnames: bool) -> dict[str, Any]:
    out = copy.deepcopy(cfg)
    net = out.setdefault("net", {})
    if mode == "disabled" or not paths:
        net.pop("tls", None)
        return out
    tls = {"mode": mode, "certificateKeyFile": paths["pem"], "CAFile": paths["ca"],
           "allowConnectionsWithoutCertificates": True}
    if allow_invalid_hostnames:
        tls["allowInvalidHostnames"] = True
    net["tls"] = tls
    return out


# -- remote scripts -------------------------------------------------------

def prepare_member_script(paths: dict[str, Any]) -> str:
    q = {k: shlex.quote(v) for k, v in paths.items() if v}
    data = f"mkdir -p {q['dbpath']}\nchown -R mongod:mongod {q['dbpath']}\nchmod 750 {q['dbpath']}\n" if paths.get("dbpath") else ""
    return f"""
set -euo pipefail
if ! id -u mongod >/dev/null 2>&1; then
  useradd --system --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin mongod 2>/dev/null \\
    || useradd -r -M -s /sbin/nologin mongod
fi
mkdir -p {q['logdir']} {q['etcdir']}
chown -R mongod:mongod {q['logdir']}
{data}"""


def write_file_script(path: str, mode: str = "644", owner: str = "root:root") -> str:
    """Write stdin to path atomically."""
    p = shlex.quote(path)
    return f"set -euo pipefail\nmkdir -p \"$(dirname {p})\"\ncat > {p}.mp-tmp\nchmod {mode} {p}.mp-tmp\n" \
           f"chown {owner} {p}.mp-tmp\nmv -f {p}.mp-tmp {p}\n"


def install_config_script(conf: str, version: str, binary: str) -> str:
    """Validate the configuration read from stdin with `<binary> --outputConfig`, then install it.

    MongoDB parses every option without starting, so a typo is caught before any restart.
    """
    p = shlex.quote(conf)
    exe = shlex.quote(f"{BASE_BIN}/{version}/bin/{binary}")
    return f"""set -uo pipefail
mkdir -p "$(dirname {p})"
cat > {p}.mp-new
if [ -x {exe} ]; then
  if ! OUT=$({exe} --config {p}.mp-new --outputConfig 2>&1 >/dev/null); then
    rm -f {p}.mp-new
    echo "MongoDB rejected the configuration: $OUT" >&2
    exit 3
  fi
fi
chmod 644 {p}.mp-new && chown root:root {p}.mp-new && mv -f {p}.mp-new {p}
echo "configuration installed: {conf}"
"""


def read_file_script(path: str) -> str:
    return f"cat {shlex.quote(path)}"


def systemctl_script(action: str, unit: str) -> str:
    assert action in ("start", "stop", "restart", "enable-start")
    u = shlex.quote(unit)
    if action == "enable-start":
        return f"set -euo pipefail\nsystemctl daemon-reload\nsystemctl enable {u}\nsystemctl restart {u}\n"
    return f"set -euo pipefail\nsystemctl daemon-reload\nsystemctl {action} {u}\n"


def wait_port_script(port: int, unit: str, logpath: str, timeout: int = 90) -> str:
    return f"""
for i in $(seq 1 {timeout}); do
  if (echo > /dev/tcp/127.0.0.1/{port}) 2>/dev/null; then echo "listening on port {port}"; exit 0; fi
  if ! systemctl is-active --quiet {shlex.quote(unit)}; then
    if systemctl is-failed --quiet {shlex.quote(unit)}; then break; fi
  fi
  sleep 1
done
echo "the process is not answering on port {port}" >&2
journalctl -u {shlex.quote(unit)} -n 30 --no-pager >&2 || true
tail -n 30 {shlex.quote(logpath)} >&2 || true
exit 1
"""
