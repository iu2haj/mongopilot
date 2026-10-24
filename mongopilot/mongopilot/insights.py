"""Query performance insights: query shapes from slow-query logs and index recommendations.

Everything here works from data MongoDB already exposes to a monitoring user:
- slow-query log entries (planSummary, keysExamined, docsExamined, nreturned, hasSortStage, the command);
- index definitions, $indexStats usage counters and index sizes.
Query values are never stored: shapes keep field names and operators only.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Any

# Extended-JSON wrappers that represent a literal value, not a query operator
VALUE_WRAPPERS = {"$date": "<date>", "$oid": "<objectId>", "$numberLong": "<number>", "$numberInt": "<number>",
                  "$numberDouble": "<number>", "$numberDecimal": "<number>", "$binary": "<binary>",
                  "$uuid": "<uuid>", "$timestamp": "<timestamp>", "$regularExpression": "<regex>",
                  "$regex": "<regex>", "$minKey": "<minKey>", "$maxKey": "<maxKey>", "$symbol": "<string>"}
EQUALITY_OPS = {"$eq", "$in", "$elemMatch", "$all", "$size"}
RANGE_OPS = {"$gt", "$gte", "$lt", "$lte", "$ne", "$nin", "$regex", "$exists", "$type", "$mod", "$not"}
UNINDEXABLE = {"$where", "$expr", "$text", "$jsonSchema", "$near", "$nearSphere", "$geoWithin",
               "$geoIntersects", "$function"}
SYSTEM_DBS = ("admin.", "local.", "config.")
WRITE_OPS = {"update", "remove", "delete", "findAndModify"}


# ---------------------------------------------------------------------------
# Redaction and shapes
# ---------------------------------------------------------------------------

def _is_wrapper(value: Any) -> bool:
    return isinstance(value, dict) and len(value) >= 1 and next(iter(value)) in VALUE_WRAPPERS \
        and all(k.startswith("$") for k in value)


def redact(value: Any) -> Any:
    """Replace literal values with type placeholders, keeping field names and operators."""
    if _is_wrapper(value):
        return VALUE_WRAPPERS[next(iter(value))]
    if isinstance(value, dict):
        return {k: redact(v) for k, v in value.items()}
    if isinstance(value, list):
        if not value:
            return []
        if all(not isinstance(v, (dict, list)) for v in value):
            return [redact(value[0])] if len(value) == 1 else ["<array>"]
        return [redact(v) for v in value[:5]]
    if isinstance(value, bool):
        return "<bool>"
    if isinstance(value, (int, float)):
        return "<number>"
    if value is None:
        return None
    return "<string>"


def extract_query(attr: dict[str, Any]) -> dict[str, Any] | None:
    """Turn a slow-query log entry into {ns, op, filter, sort, ...} (literal values still present)."""
    ns = attr.get("ns") or ""
    cmd = attr.get("command") or {}
    if not isinstance(cmd, dict):
        return None
    op_type = attr.get("type") or "command"
    if cmd.get("getMore") is not None and isinstance(attr.get("originatingCommand"), dict):
        cmd = attr["originatingCommand"]
    first = next(iter(cmd), "")
    db = cmd.get("$db") or ns.split(".", 1)[0]
    if ns.endswith(".$cmd") and isinstance(cmd.get(first), str):
        ns = f"{db}.{cmd[first]}"
    if not ns or ns.startswith(SYSTEM_DBS) or ".system." in ns:
        return None
    filt: Any = {}
    sort: Any = {}
    op = first
    if op_type in ("update", "remove"):
        op = op_type
        filt = cmd.get("q") or {}
    elif first == "find":
        filt, sort = cmd.get("filter") or {}, cmd.get("sort") or {}
    elif first in ("count", "distinct"):
        filt = cmd.get("query") or {}
    elif first == "findAndModify" or first == "findandmodify":
        op = "findAndModify"
        filt, sort = cmd.get("query") or {}, cmd.get("sort") or {}
    elif first == "aggregate":
        pipeline = cmd.get("pipeline") or []
        for stage in pipeline:
            if not isinstance(stage, dict) or not stage:
                break
            name = next(iter(stage))
            if name == "$match" and not filt:
                filt = stage["$match"]
            elif name == "$sort" and not sort:
                sort = stage["$sort"]
                break
            elif name in ("$match", "$sort"):
                continue
            else:
                break
    elif first in ("update", "delete"):
        stmts = cmd.get("updates") or cmd.get("deletes") or []
        if stmts and isinstance(stmts[0], dict):
            filt = stmts[0].get("q") or {}
    else:
        return None
    if not isinstance(filt, dict):
        filt = {}
    if not isinstance(sort, dict):
        sort = {}
    return {"ns": ns, "op": op, "filter": filt, "sort": sort}


def shape_of(query: dict[str, Any]) -> dict[str, Any]:
    shape = {"filter": redact(query["filter"]), "sort": {k: (v if v in (1, -1) else "<meta>")
                                                         for k, v in query["sort"].items()}}
    canon = json.dumps([query["ns"], query["op"], shape], sort_keys=True)
    return {"key": hashlib.sha1(canon.encode()).hexdigest()[:16], "shape": shape}


def slow_entry(attr: dict[str, Any]) -> dict[str, Any] | None:
    """Normalize a slow-query log entry for storage (no literal values)."""
    q = extract_query(attr)
    if q is None:
        return None
    sh = shape_of(q)
    return {
        "ts": attr.get("_ts") or time.time(),
        "ns": q["ns"], "op": q["op"],
        "duration_ms": float(attr.get("durationMillis") or 0),
        "plan": str(attr.get("planSummary") or ""),
        "keys_examined": int(attr.get("keysExamined") or 0),
        "docs_examined": int(attr.get("docsExamined") or 0),
        "nreturned": int(attr.get("nreturned") or attr.get("nMatched") or attr.get("ndeleted") or 0),
        "has_sort": 1 if attr.get("hasSortStage") else 0,
        "shape_key": sh["key"],
        "shape": json.dumps(sh["shape"]),
        "app": str(attr.get("appName") or "")[:100],
    }


# ---------------------------------------------------------------------------
# ESR (Equality, Sort, Range) index suggestion
# ---------------------------------------------------------------------------

def classify(filt: dict[str, Any]) -> dict[str, Any]:
    """Split a (redacted or raw) filter into equality fields, range fields, $or branches and blockers."""
    eq: list[str] = []
    rng: list[str] = []
    branches: list[dict[str, Any]] = []
    unsupported: list[str] = []

    def add(lst: list[str], f: str) -> None:
        if f not in eq and f not in rng:
            lst.append(f)

    def walk(doc: dict[str, Any]) -> None:
        for key, val in doc.items():
            if key == "$and" and isinstance(val, list):
                for sub in val:
                    if isinstance(sub, dict):
                        walk(sub)
            elif key in ("$or", "$nor") and isinstance(val, list):
                if key == "$or":
                    branches.extend(sub for sub in val if isinstance(sub, dict))
                else:
                    unsupported.append(key)
            elif key.startswith("$"):
                if key in UNINDEXABLE:
                    unsupported.append(key)
                continue
            elif val == "<regex>" or (_is_wrapper(val) and next(iter(val)) in ("$regex", "$regularExpression")):
                add(rng, key)   # a regex literal is matched as a range scan at best
            elif isinstance(val, dict) and not _is_wrapper(val) and val and all(k.startswith("$") for k in val):
                ops = set(val)
                if ops & UNINDEXABLE:
                    unsupported.extend(sorted(ops & UNINDEXABLE))
                elif ops <= EQUALITY_OPS:
                    add(eq, key)
                elif ops & {"$eq"} and not ops & RANGE_OPS:
                    add(eq, key)
                else:
                    add(rng, key)
            else:
                add(eq, key)
    walk(filt)
    return {"eq": eq, "range": rng, "or": branches, "unsupported": unsupported}


def esr_key(eq: list[str], sort: dict[str, Any], rng: list[str]) -> list[tuple[str, int]]:
    """Equality fields first, then sort fields (with direction), then range fields."""
    key: list[tuple[str, int]] = [(f, 1) for f in eq]
    used = set(eq)
    for f, d in sort.items():
        if f not in used and d in (1, -1):
            key.append((f, int(d)))
            used.add(f)
    for f in rng:
        if f not in used:
            key.append((f, 1))
            used.add(f)
    return key[:8]


def suggest_keys(filt: dict[str, Any], sort: dict[str, Any]) -> dict[str, Any]:
    """Suggested index key(s) for a query shape. $or queries need one index per branch."""
    c = classify(filt)
    if c["unsupported"]:
        return {"keys": [], "note": f"Uses {', '.join(sorted(set(c['unsupported'])))}, which a regular "
                                    "index cannot fully support"}
    keys: list[dict[str, Any]] = []
    if c["or"]:
        for branch in c["or"]:
            b = classify(branch)
            eq = c["eq"] + [f for f in b["eq"] if f not in c["eq"]]
            k = esr_key(eq, sort, c["range"] + [f for f in b["range"] if f not in c["range"]])
            if k and all(k != x["key"] for x in keys):
                keys.append({"key": k, "n_eq": len(eq)})
        note = "$or query: each branch needs its own index for the planner to use an index union" \
            if len(keys) > 1 else ""
        return {"keys": keys, "note": note}
    k = esr_key(c["eq"], sort, c["range"])
    if not k:
        return {"keys": [], "note": "No filter or sort: the operation reads the whole collection"}
    return {"keys": [{"key": k, "n_eq": len(c["eq"])}], "note": ""}


def _norm(key: Any) -> list[tuple[str, Any]]:
    if isinstance(key, dict):
        return list(key.items())
    return [(k, v) for k, v in key]


def index_supports(index_key: Any, wanted: list[tuple[str, int]], n_eq: int) -> bool:
    """True if an existing index serves the wanted ESR key (equality fields in any order, then the rest
    as a prefix, sort directions matching or all reversed)."""
    idx = _norm(index_key)
    if len(idx) < len(wanted) or any(not isinstance(d, (int, float)) for _, d in idx[:len(wanted)]):
        return False
    head = idx[:n_eq]
    if {f for f, _ in head} != {f for f, _ in wanted[:n_eq]}:
        return False
    rest_idx = idx[n_eq:len(wanted)]
    rest_want = wanted[n_eq:]
    if [f for f, _ in rest_idx] != [f for f, _ in rest_want]:
        return False
    same = all(int(a) == int(b) for (_, a), (_, b) in zip(rest_idx, rest_want))
    rev = all(int(a) == -int(b) for (_, a), (_, b) in zip(rest_idx, rest_want))
    return same or rev


_IDENT = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")


def key_to_js(key: Any) -> str:
    """Render an index key as mongosh syntax, e.g. { status: 1, "address.city": 1 }."""
    parts = []
    for f, d in _norm(key):
        name = f if _IDENT.match(f) else json.dumps(f)
        val = json.dumps(d) if isinstance(d, str) else str(int(d))
        parts.append(f"{name}: {val}")
    return "{ " + ", ".join(parts) + " }"


def coll_ref(ns: str) -> str:
    db, coll = ns.split(".", 1)
    return f'db.getSiblingDB("{db}").getCollection("{coll}")'


# ---------------------------------------------------------------------------
# Index advisor
# ---------------------------------------------------------------------------

def needs_index(group: dict[str, Any]) -> list[str]:
    """Reasons why a slow query group deserves a better index."""
    reasons = []
    if "COLLSCAN" in (group.get("plans") or ""):
        reasons.append("collection scan")
    if group.get("avg_targeting", 0) >= 10:
        reasons.append(f"examines {group['avg_targeting']:,.0f}× more documents than it returns")
    if group.get("sort_ratio", 0) >= 0.5:
        reasons.append("in-memory sort")
    return reasons


def impact(total_ms: float, grand_total_ms: float) -> tuple[float, str]:
    """Share of all slow-query time this suggestion could save, and a High/Medium/Low label."""
    share = 100 * total_ms / grand_total_ms if grand_total_ms else 0.0
    level = "High" if share >= 15 else "Medium" if share >= 4 else "Low"
    return round(share, 1), level


def analyze(groups: list[dict[str, Any]], inventory: dict[str, Any] | None, now: float | None = None,
            min_window_days: float = 7) -> dict[str, Any]:
    """Build the index advisor report from slow query groups and the index inventory."""
    now = now or time.time()
    collections = (inventory or {}).get("collections", {})
    grand_total = sum(g["avg_ms"] * g["count"] for g in groups)
    suggestions: dict[tuple[str, tuple], dict[str, Any]] = {}
    covered: list[dict[str, Any]] = []
    for g in groups:
        reasons = needs_index(g)
        if not reasons:
            continue
        shape = json.loads(g["shape"]) if isinstance(g["shape"], str) else g["shape"]
        sug = suggest_keys(shape.get("filter") or {}, shape.get("sort") or {})
        for item in sug["keys"]:
            key, n_eq = item["key"], item["n_eq"]
            existing = [ix for ix in collections.get(g["ns"], {}).get("indexes", [])
                        if index_supports(ix["key"], key, n_eq)]
            if existing:
                covered.append({"ns": g["ns"], "shape_key": g["shape_key"], "index": existing[0]["name"]})
                continue
            k = (g["ns"], tuple(key))
            s = suggestions.setdefault(k, {
                "ns": g["ns"], "key": key, "key_js": key_to_js(key), "reasons": set(), "shapes": [],
                "count": 0, "total_ms": 0.0, "targeting": [], "docs_examined": 0, "returned": 0,
                "note": sug["note"],
                "command": f"{coll_ref(g['ns'])}.createIndex({key_to_js(key)})",
            })
            s["reasons"].update(reasons)
            s["shapes"].append({"shape_key": g["shape_key"], "op": g["op"], "shape": shape, "count": g["count"]})
            s["count"] += g["count"]
            s["total_ms"] += g["avg_ms"] * g["count"]
            s["targeting"].append(g.get("avg_targeting", 0))
            s["docs_examined"] += g.get("avg_docs_examined", 0) * g["count"]
            s["returned"] += g.get("avg_returned", 0) * g["count"]
    out_sug = []
    for s in suggestions.values():
        avg_ms = s["total_ms"] / max(1, s["count"])
        tgt = max(s["targeting"]) if s["targeting"] else 0
        share, level = impact(s["total_ms"], grand_total)
        out_sug.append({**{k: v for k, v in s.items() if k not in ("reasons", "targeting", "total_ms")},
                        "key": [[f, d] for f, d in s["key"]],
                        "reasons": sorted(s["reasons"]), "avg_ms": round(avg_ms, 1), "avg_targeting": round(tgt, 1),
                        "avg_docs_examined": round(s["docs_examined"] / max(1, s["count"]), 1),
                        "avg_returned": round(s["returned"] / max(1, s["count"]), 1),
                        "time_share": share, "impact": level})
    out_sug.sort(key=lambda s: -s["time_share"])

    unused, redundant, colls_out = [], [], []
    total_indexes = 0
    for ns, c in sorted(collections.items()):
        idxs = c.get("indexes", [])
        total_indexes += len(idxs)
        rows = []
        for ix in idxs:
            name = ix["name"]
            u = c.get("usage", {}).get(name)
            size = c.get("sizes", {}).get(name)
            flags = _flags(ix)
            rows.append({"name": name, "key": [[f, d] for f, d in _norm(ix["key"])], "key_js": key_to_js(ix["key"]),
                         "ops": u["ops"] if u else None, "since": u["since"] if u else None,
                         "size": size, "flags": flags})
            if name == "_id_" or u is None:
                continue
            window_days = (now - u["since"]) / 86400 if u.get("since") else None
            if u["ops"] == 0:
                keep = [f for f in ("unique", "ttl", "shard key") if f in flags]
                unused.append({
                    "ns": ns, "name": name, "key_js": key_to_js(ix["key"]), "size": size,
                    "window_days": round(window_days, 1) if window_days is not None else None,
                    "low_confidence": window_days is not None and window_days < min_window_days,
                    "keep_reason": _keep_reason(keep),
                    "hide": f"{coll_ref(ns)}.hideIndex({json.dumps(name)})",
                    "drop": f"{coll_ref(ns)}.dropIndex({json.dumps(name)})",
                })
        for a in idxs:
            if a["name"] == "_id_" or {"unique", "ttl", "partial", "special", "sparse", "collation"} & set(_flags(a)):
                continue
            ka = _norm(a["key"])
            for b in idxs:
                if b is a or {"partial", "special", "sparse", "collation"} & set(_flags(b)):
                    continue
                kb = _norm(b["key"])
                if len(kb) <= len(ka):
                    continue
                prefix = kb[:len(ka)]
                if prefix == ka or all(fa == fb and int(da) == -int(db) for (fa, da), (fb, db) in zip(ka, prefix)):
                    redundant.append({
                        "ns": ns, "name": a["name"], "key_js": key_to_js(ka), "covered_by": b["name"],
                        "covered_by_key": key_to_js(kb), "size": c.get("sizes", {}).get(a["name"]),
                        "ops": (c.get("usage", {}).get(a["name"]) or {}).get("ops"),
                        "hide": f"{coll_ref(ns)}.hideIndex({json.dumps(a['name'])})",
                        "drop": f"{coll_ref(ns)}.dropIndex({json.dumps(a['name'])})",
                    })
                    break
        colls_out.append({"ns": ns, "count": c.get("count"), "size": c.get("size"),
                          "index_size": sum(v for v in c.get("sizes", {}).values() if v),
                          "indexes": rows})
    return {
        "summary": {"collections": len(collections), "indexes": total_indexes, "suggestions": len(out_sug),
                    "unused": len([u for u in unused if not u["keep_reason"]]), "redundant": len(redundant),
                    "slow_shapes": len(groups)},
        "suggestions": out_sug, "unused": unused, "redundant": redundant, "covered": covered,
        "collections": colls_out, "warnings": (inventory or {}).get("warnings", []),
        "inventory_available": inventory is not None,
    }


def _flags(ix: dict[str, Any]) -> list[str]:
    flags = []
    if ix.get("unique"):
        flags.append("unique")
    if ix.get("expireAfterSeconds") is not None:
        flags.append("ttl")
    if ix.get("partialFilterExpression"):
        flags.append("partial")
    if ix.get("sparse"):
        flags.append("sparse")
    if ix.get("hidden"):
        flags.append("hidden")
    if ix.get("collation"):
        flags.append("collation")
    if any(isinstance(d, str) for _, d in _norm(ix["key"])):
        flags.append("special")   # text, 2dsphere, hashed, wildcard
    if ix.get("_shardKey"):
        flags.append("shard key")
    return flags


def _keep_reason(flags: list[str]) -> str:
    if "unique" in flags:
        return "Enforces a unique constraint: keep it even if no query uses it"
    if "ttl" in flags:
        return "TTL index: used by the TTL monitor to expire documents (not counted as accesses)"
    if "shard key" in flags:
        return "Supports the shard key"
    return ""


def group_slow_ops(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Aggregate slow operation rows by query shape."""
    groups: dict[str, dict[str, Any]] = {}
    for r in rows:
        g = groups.setdefault(r["shape_key"], {
            "shape_key": r["shape_key"], "ns": r["ns"], "op": r["op"], "shape": r["shape"], "count": 0,
            "total_ms": 0.0, "max_ms": 0.0, "durations": [], "docs": 0, "keys": 0, "ret": 0, "sorts": 0,
            "plans": {}, "apps": set(), "hosts": set(), "last_seen": 0.0, "samples": []})
        g["count"] += 1
        g["total_ms"] += r["duration_ms"]
        g["max_ms"] = max(g["max_ms"], r["duration_ms"])
        g["durations"].append(r["duration_ms"])
        g["docs"] += r["docs_examined"]
        g["keys"] += r["keys_examined"]
        g["ret"] += r["nreturned"]
        g["sorts"] += r["has_sort"]
        g["plans"][r["plan"]] = g["plans"].get(r["plan"], 0) + 1
        if r.get("app"):
            g["apps"].add(r["app"])
        g["hosts"].add(r["host"])
        g["last_seen"] = max(g["last_seen"], r["ts"])
        g["samples"].append(r)
    out = []
    for g in groups.values():
        n = g["count"]
        d = sorted(g["durations"])
        samples = sorted(g["samples"], key=lambda r: -r["duration_ms"])[:5]
        out.append({
            "shape_key": g["shape_key"], "ns": g["ns"], "op": g["op"], "shape": g["shape"], "count": n,
            "avg_ms": round(g["total_ms"] / n, 1), "max_ms": round(g["max_ms"], 1),
            "p95_ms": round(d[min(len(d) - 1, int(0.95 * len(d)))], 1),
            "total_ms": round(g["total_ms"], 1),
            "avg_docs_examined": round(g["docs"] / n, 1), "avg_keys_examined": round(g["keys"] / n, 1),
            "avg_returned": round(g["ret"] / n, 1),
            "avg_targeting": round(g["docs"] / max(1, g["ret"]), 1) if g["docs"] else 0.0,
            "sort_ratio": round(g["sorts"] / n, 2),
            "plans": ", ".join(p or "?" for p, _ in sorted(g["plans"].items(), key=lambda kv: -kv[1])),
            "apps": sorted(g["apps"]), "hosts": sorted(g["hosts"]), "last_seen": g["last_seen"],
            "samples": [{k: s[k] for k in ("ts", "host", "duration_ms", "plan", "docs_examined", "keys_examined",
                                           "nreturned", "has_sort")} for s in samples],
        })
    out.sort(key=lambda g: -g["total_ms"])
    return out
