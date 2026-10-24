"""Resolve the MongoDB Community download URL for a platform."""

from __future__ import annotations

import json
import logging
import time
import urllib.request
from typing import Any

from . import templates as T

log = logging.getLogger("mongopilot.releases")

_cache: dict[str, tuple[float, Any]] = {}


def _load_index(url: str, ttl: int = 6 * 3600) -> dict[str, Any] | None:
    hit = _cache.get(url)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    try:
        with urllib.request.urlopen(url, timeout=20) as resp:
            data = json.load(resp)
        _cache[url] = (time.time(), data)
        return data
    except Exception as exc:
        log.info("MongoDB release index not reachable (%s): using standard URLs", exc)
        return None


def available_versions(index_url: str) -> list[str]:
    data = _load_index(index_url)
    if not data:
        return []
    out = []
    for v in data.get("versions", []):
        ver = v.get("version", "")
        if v.get("production_release") and T.major(ver) in T.SUPPORTED_MAJORS and "-" not in ver:
            out.append(ver)
    return sorted(out, key=lambda s: tuple(int(x) for x in s.split(".")), reverse=True)


def resolve(facts: dict[str, str], version: str, index_url: str, mirror: str | None = None,
            override: str | None = None) -> tuple[list[str], str | None]:
    """Return (URLs to try in order, sha256 when known)."""
    if override:
        return [override], None
    if mirror:
        return T.candidate_urls(facts, version, mirror), None
    targets = T.download_targets(facts)
    arch = T.tarball_arch(facts)
    data = _load_index(index_url)
    if data:
        for v in data.get("versions", []):
            if v.get("version") != version:
                continue
            for target in targets:
                for d in v.get("downloads", []):
                    if (d.get("target") == target and d.get("arch") == arch
                            and d.get("edition") in ("base", "targeted") and d.get("archive", {}).get("url")):
                        return [d["archive"]["url"]], d["archive"].get("sha256")
    urls = T.candidate_urls(facts, version)
    if not urls:
        raise ValueError(f"Platform not recognized ({facts.get('os_name')} {facts.get('arch')}): "
                         "provide an explicit tarball URL")
    return urls, None
