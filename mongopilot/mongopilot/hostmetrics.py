"""Host hardware metrics collected over SSH from /proc (no agent, no sudo).

Covers the hardware metrics MongoDB recommends watching: normalized system CPU, disk IOPS,
disk latency, free disk space and system memory.
"""

from __future__ import annotations

import re
from typing import Any

MARKER = "# mongopilot-host-metrics"

SCRIPT = MARKER + r"""
echo "@@stat"; head -1 /proc/stat
echo "@@cores"; nproc 2>/dev/null || grep -c ^processor /proc/cpuinfo
echo "@@load"; cat /proc/loadavg
echo "@@mem"; grep -E '^(MemTotal|MemAvailable|SwapTotal|SwapFree):' /proc/meminfo
echo "@@disk"; cat /proc/diskstats
echo "@@df"; df -P -k -x tmpfs -x devtmpfs -x squashfs -x overlay -x vboxsf -x 9p 2>/dev/null | tail -n +2
echo "@@end"
"""

# Whole devices only (partitions and virtual devices are skipped)
_DEVICE = re.compile(r"^(sd[a-z]+|vd[a-z]+|xvd[a-z]+|hd[a-z]+|nvme\d+n\d+)$")


def parse(output: str) -> dict[str, Any]:
    sections: dict[str, list[str]] = {}
    current = None
    for line in output.splitlines():
        if line.startswith("@@"):
            current = line[2:].strip()
            sections[current] = []
        elif current:
            sections[current].append(line)
    snap: dict[str, Any] = {}
    stat = (sections.get("stat") or [""])[0].split()
    if stat and stat[0] == "cpu":
        vals = [float(x) for x in stat[1:]]
        vals += [0.0] * (8 - len(vals))
        user, nice, system, idle, iowait, irq, softirq, steal = vals[:8]
        snap["cpu_total"] = sum(vals[:8])
        snap["cpu_idle"] = idle
        snap["cpu_iowait"] = iowait
    try:
        snap["cores"] = int((sections.get("cores") or ["1"])[0].strip())
    except ValueError:
        snap["cores"] = 1
    load = (sections.get("load") or [""])[0].split()
    if load:
        snap["load1"] = float(load[0])
    mem: dict[str, float] = {}
    for line in sections.get("mem", []):
        k, _, v = line.partition(":")
        mem[k.strip()] = float(v.split()[0])
    if mem.get("MemTotal"):
        snap["mem_used_pct"] = round(100 * (1 - mem.get("MemAvailable", 0) / mem["MemTotal"]), 2)
    if mem.get("SwapTotal"):
        snap["swap_used_pct"] = round(100 * (1 - mem.get("SwapFree", 0) / mem["SwapTotal"]), 2)
    disks: dict[str, dict[str, float]] = {}
    for line in sections.get("disk", []):
        f = line.split()
        if len(f) < 14 or not _DEVICE.match(f[2]):
            continue
        disks[f[2]] = {"reads": float(f[3]), "read_ms": float(f[6]), "writes": float(f[7]),
                       "write_ms": float(f[10]), "io_ms": float(f[12])}
    snap["disks"] = disks
    free = []
    for line in sections.get("df", []):
        f = line.split()
        if len(f) >= 6 and f[1].isdigit() and int(f[1]) > 0:
            free.append(100 * int(f[3]) / int(f[1]))
    if free:
        snap["disk_free_pct"] = round(min(free), 2)
    return snap


def compute(prev: tuple[float, dict[str, Any]] | None, cur: tuple[float, dict[str, Any]]) -> dict[str, float]:
    """Metric values from one snapshot (gauges) and the previous one (rates)."""
    t1, s1 = cur
    out: dict[str, float] = {"host_up": 1.0}
    for k in ("mem_used_pct", "swap_used_pct", "disk_free_pct"):
        if k in s1:
            out["sys_" + k if k == "mem_used_pct" else k] = s1[k]
    if "load1" in s1:
        out["load_per_core"] = round(s1["load1"] / max(1, s1.get("cores", 1)), 2)
    if prev is None:
        return out
    t0, s0 = prev
    dt = t1 - t0
    if dt <= 0:
        return out
    d_total = s1.get("cpu_total", 0) - s0.get("cpu_total", 0)
    if d_total > 0:
        d_idle = s1["cpu_idle"] - s0["cpu_idle"]
        d_iow = s1["cpu_iowait"] - s0["cpu_iowait"]
        out["sys_cpu_pct"] = round(100 * (d_total - d_idle - d_iow) / d_total, 2)
        out["sys_iowait_pct"] = round(100 * d_iow / d_total, 2)
    reads = writes = read_ms = write_ms = 0.0
    util = 0.0
    for name, d1 in s1.get("disks", {}).items():
        d0 = s0.get("disks", {}).get(name)
        if not d0 or d1["reads"] < d0["reads"] or d1["writes"] < d0["writes"]:
            continue
        reads += d1["reads"] - d0["reads"]
        writes += d1["writes"] - d0["writes"]
        read_ms += d1["read_ms"] - d0["read_ms"]
        write_ms += d1["write_ms"] - d0["write_ms"]
        util = max(util, min(100.0, 100 * (d1["io_ms"] - d0["io_ms"]) / (dt * 1000)))
    if s1.get("disks"):
        out["disk_read_iops"] = round(reads / dt, 2)
        out["disk_write_iops"] = round(writes / dt, 2)
        out["disk_read_latency_ms"] = round(read_ms / reads, 2) if reads else 0.0
        out["disk_write_latency_ms"] = round(write_ms / writes, 2) if writes else 0.0
        out["disk_util_pct"] = round(util, 2)
    return out
