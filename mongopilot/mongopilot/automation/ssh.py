"""Remote command execution through the system OpenSSH client, private key validation
and `vagrant ssh-config` import."""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import os
import re
import shlex
import socket
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator


@dataclass
class SshTarget:
    name: str
    hostname: str
    port: int
    user: str
    private_key: str | None   # private key content (plain text, kept in memory only)
    pin_host_key: bool = True  # False for Vagrant VMs, which get a new host key when re-created

    @property
    def needs_sudo(self) -> bool:
        return self.user != "root"


def target_for(server: dict[str, Any], box) -> SshTarget:
    return SshTarget(server["name"], server["hostname"], int(server["ssh_port"]), server["ssh_user"],
                     box.decrypt(server.get("ssh_key_enc")), (server.get("kind") or "standard") != "vagrant")


@dataclass
class CommandResult:
    rc: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.rc == 0


class SshError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Private keys
# ---------------------------------------------------------------------------

def validate_private_key(text: str) -> str:
    """Return the normalized key, or raise ValueError with a message the user can act on."""
    key = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not key:
        raise ValueError("The private key is empty")
    if "BEGIN CERTIFICATE" in key:
        raise ValueError("This is an SSL/TLS certificate, not an SSH private key. Upload the private key "
                         "file instead (for Vagrant: .vagrant/machines/<name>/<provider>/private_key)")
    if key.startswith("PuTTY-User-Key-File"):
        raise ValueError("This is a PuTTY .ppk key. Convert it with PuTTYgen: Conversions → Export OpenSSH key")
    if re.match(r"^(ssh-(rsa|ed25519|dss)|ecdsa-sha2-\S+|sk-\S+)\s", key):
        raise ValueError("This is a public key (.pub). Upload the matching private key instead")
    m = re.match(r"^-----BEGIN ([A-Z0-9 ]*PRIVATE KEY)-----\n(.*?)\n-----END \1-----$", key, re.S)
    if not m:
        raise ValueError("Not a valid private key: expected a file starting with "
                         "'-----BEGIN ... PRIVATE KEY-----'")
    kind, body = m.group(1), m.group(2)
    if kind == "ENCRYPTED PRIVATE KEY" or "Proc-Type: 4,ENCRYPTED" in body:
        raise ValueError("The key is protected by a passphrase. Remove it with: ssh-keygen -p -f <keyfile>")
    if kind == "OPENSSH PRIVATE KEY":
        try:
            raw = base64.b64decode("".join(body.split()), validate=False)
            if raw.startswith(b"openssh-key-v1\x00"):
                n = int.from_bytes(raw[15:19], "big")
                if raw[19:19 + n] != b"none":
                    raise ValueError("The key is protected by a passphrase. Remove it with: "
                                     "ssh-keygen -p -f <keyfile>")
        except binascii.Error:
            pass
    return key + "\n"


# ---------------------------------------------------------------------------
# vagrant ssh-config
# ---------------------------------------------------------------------------

def parse_ssh_config(text: str) -> list[dict[str, Any]]:
    """Parse the output of `vagrant ssh-config [machine...]` (one block per machine)."""
    hosts: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        if len(parts) < 2:
            continue
        k, v = parts[0].lower(), parts[1].strip().strip('"')
        if k == "host":
            cur = {"name": v, "hostname": v, "port": 22, "user": "vagrant", "identity_file": None}
            hosts.append(cur)
        elif cur is not None:
            if k == "hostname":
                cur["hostname"] = v
            elif k == "port":
                cur["port"] = int(v) if v.isdigit() else 22
            elif k == "user":
                cur["user"] = v
            elif k == "identityfile" and not cur["identity_file"]:
                cur["identity_file"] = v
    for h in hosts:
        h["forwarded"] = h["hostname"] in ("127.0.0.1", "localhost")
        if h["forwarded"]:
            # From inside the Docker container, the computer running Vagrant is host.docker.internal
            h["hostname"] = "host.docker.internal"
    return hosts


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

class SshRunner:
    def __init__(self, data_dir: Path, ssh_binary: str = "ssh"):
        self.ssh_binary = ssh_binary
        self.known_hosts = data_dir / "known_hosts"
        self.known_hosts.parent.mkdir(parents=True, exist_ok=True)
        self.known_hosts.touch(exist_ok=True)

    @contextlib.contextmanager
    def _keyfile(self, target: SshTarget):
        if not target.private_key:
            yield None
            return
        fd, path = tempfile.mkstemp(prefix="mp-key-")
        try:
            with os.fdopen(fd, "w") as fh:
                key = target.private_key.replace("\r\n", "\n")
                fh.write(key if key.endswith("\n") else key + "\n")
            os.chmod(path, 0o600)
            yield path
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(path)

    def _base_args(self, target: SshTarget, keyfile: str | None) -> list[str]:
        args = [self.ssh_binary, "-p", str(target.port),
                "-o", "BatchMode=yes",
                "-o", "ConnectTimeout=15",
                "-o", "ServerAliveInterval=15",
                "-o", "ServerAliveCountMax=4",
                "-o", "LogLevel=ERROR"]
        if target.pin_host_key:
            args += ["-o", "StrictHostKeyChecking=accept-new", "-o", f"UserKnownHostsFile={self.known_hosts}"]
        else:
            args += ["-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null"]
        if keyfile:
            args += ["-i", keyfile, "-o", "IdentitiesOnly=yes"]
        return args

    def forget_host_key(self, hostname: str, port: int) -> None:
        """Remove a pinned host key (after a server was legitimately re-installed)."""
        name = hostname if port == 22 else f"[{hostname}]:{port}"
        subprocess.run(["ssh-keygen", "-R", name, "-f", str(self.known_hosts)], capture_output=True, check=False)

    async def run(self, target: SshTarget, script: str, stdin: str | None = None,
                  timeout: int = 600, sudo: bool = True) -> CommandResult:
        """Run a bash script on the host. With sudo=True and a non-root user, uses `sudo -n`."""
        remote = f"bash -c {shlex.quote(script)}"
        if sudo and target.needs_sudo:
            remote = f"sudo -n {remote}"
        with self._keyfile(target) as keyfile:
            args = self._base_args(target, keyfile) + [f"{target.user}@{target.hostname}", remote]
            proc = await asyncio.create_subprocess_exec(
                *args, stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            try:
                out, err = await asyncio.wait_for(
                    proc.communicate(stdin.encode() if stdin is not None else None), timeout)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                raise SshError(f"Timed out after {timeout}s on {target.name}") from None
        stderr = err.decode(errors="replace")
        if "REMOTE HOST IDENTIFICATION HAS CHANGED" in stderr:
            stderr += ("\nThe server's SSH host key changed (for example, the VM was re-created). "
                       "Mark the server as a Vagrant VM or use 'Forget host key' on the Servers page.")
        return CommandResult(proc.returncode or 0, out.decode(errors="replace"), stderr)

    @contextlib.asynccontextmanager
    async def tunnel(self, target: SshTarget, remote_port: int) -> AsyncIterator[int]:
        """Open a local port forward to 127.0.0.1:remote_port on the host.

        Used to rely on MongoDB's localhost exception during bootstrap
        (rs.initiate and first user creation with authentication already enabled).
        """
        local_port = _free_port()
        with self._keyfile(target) as keyfile:
            args = self._base_args(target, keyfile) + [
                "-N", "-o", "ExitOnForwardFailure=yes",
                "-L", f"127.0.0.1:{local_port}:127.0.0.1:{remote_port}",
                f"{target.user}@{target.hostname}"]
            proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.DEVNULL,
                                                        stderr=asyncio.subprocess.PIPE)
            try:
                for _ in range(60):
                    if proc.returncode is not None:
                        err = (await proc.stderr.read()).decode(errors="replace") if proc.stderr else ""
                        raise SshError(f"SSH tunnel to {target.name} failed: {err.strip()}")
                    if _port_open(local_port):
                        break
                    await asyncio.sleep(0.25)
                else:
                    raise SshError(f"SSH tunnel to {target.name} did not become ready")
                yield local_port
            finally:
                if proc.returncode is None:
                    proc.terminate()
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(proc.wait(), 5)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _port_open(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.2)
        return s.connect_ex(("127.0.0.1", port)) == 0
