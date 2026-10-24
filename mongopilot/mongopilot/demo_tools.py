"""Demo implementations of the database tools: generated documents, a simulated mongosh and simulated
mongodump/mongoexport runs. Nothing here connects to a real database."""

from __future__ import annotations

import asyncio
import csv
import datetime as dt
import fnmatch
import gzip
import io
import json
import random
import re
import zlib
from pathlib import Path
from typing import Any

from .schema import bson_type, display
from .tools import ShellSession, ToolError, user_of


class ObjectId:
    """Stand-in for bson.ObjectId (the schema analyzer recognises it by class name)."""

    def __init__(self, seconds: int, rnd: random.Random):
        self.hex = f"{seconds:08x}" + "".join(rnd.choice("0123456789abcdef") for _ in range(16))
        self.generation_time = dt.datetime.fromtimestamp(seconds, dt.timezone.utc)

    def __str__(self) -> str:
        return self.hex

    def __repr__(self) -> str:
        return f"ObjectId('{self.hex}')"


class Decimal128:
    def __init__(self, value: str):
        self.value = value

    def __str__(self) -> str:
        return self.value


T0 = int(dt.datetime(2023, 1, 1, tzinfo=dt.timezone.utc).timestamp())
T1 = int(dt.datetime(2026, 10, 20, tzinfo=dt.timezone.utc).timestamp())
CITIES = [("Milano", "20121", "IT"), ("Roma", "00184", "IT"), ("Torino", "10121", "IT"), ("Napoli", "80133", "IT"),
          ("Madrid", "28013", "ES"), ("Paris", "75002", "FR"), ("Berlin", "10115", "DE"), ("Lyon", "69002", "FR")]
FIRST = ["Giulia", "Marco", "Sara", "Luca", "Elena", "Paolo", "Chiara", "Andrea", "Marta", "Davide", "Anna", "Pierre"]
LAST = ["Rossi", "Bianchi", "Romano", "Colombo", "Ricci", "Marino", "Greco", "Bruno", "Gallo", "Conti", "Dubois"]
CATEGORIES = ["shoes", "jackets", "bags", "accessories", "shirts", "trousers", "kids"]


def _date(r: random.Random, lo: int = T0, hi: int = T1) -> dt.datetime:
    return dt.datetime.fromtimestamp(r.randint(lo, hi), dt.timezone.utc).replace(microsecond=r.randint(0, 999) * 1000)


def _oid(r: random.Random, lo: int = T0) -> ObjectId:
    return ObjectId(r.randint(lo, T1), r)


def _person(r: random.Random) -> tuple[str, str, str]:
    f, l_ = r.choice(FIRST), r.choice(LAST)
    return f, l_, f"{f}.{l_}{r.randint(1, 999)}@example.com".lower()


def gen_order(r: random.Random) -> dict[str, Any]:
    city, zip_, country = r.choice(CITIES)
    items = [{"sku": f"SKU-{r.randint(1000, 4200)}", "qty": r.choice([1, 1, 1, 2, 3]),
              "price": round(r.uniform(9, 240), 2)} for _ in range(r.choice([1, 1, 2, 2, 3, 4, 6]))]
    created = _date(r)
    d: dict[str, Any] = {
        "_id": ObjectId(int(created.timestamp()), r), "customerId": _oid(r),
        "status": r.choices(["paid", "shipped", "delivered", "cancelled", "pending"], [30, 25, 35, 4, 6])[0],
        "items": items, "total": round(sum(i["qty"] * i["price"] for i in items), 2), "currency": "EUR",
        "createdAt": created,
        "shipping": {"city": city, "zip": int(zip_) if r.random() < 0.08 else zip_, "country": country,
                     "method": r.choice(["standard", "express", "pickup"])},
        "paid": r.random() < 0.94,
    }
    if r.random() < 0.3:
        d["coupon"] = r.choice(["WELCOME10", "SPRING25", "VIP", "BLACKFRIDAY"])
    if r.random() < 0.05:
        d["notes"] = None if r.random() < 0.5 else "Leave at the reception"
    if r.random() < 0.6:
        d["deliveredAt"] = created + dt.timedelta(hours=r.randint(20, 160))
    return d


def gen_customer(r: random.Random) -> dict[str, Any]:
    f, l_, email = _person(r)
    city, zip_, country = r.choice(CITIES)
    d: dict[str, Any] = {"_id": _oid(r), "email": email, "name": {"first": f, "last": l_},
                         "tier": r.choices(["standard", "silver", "gold", "platinum"], [60, 25, 12, 3])[0],
                         "signupAt": _date(r), "address": {"city": city, "zip": zip_, "country": country},
                         "tags": r.sample(["newsletter", "b2b", "returns", "vip", "app", "outlet"], r.randint(0, 3)),
                         "marketingOptIn": r.random() < 0.55, "orders": r.randint(0, 80)}
    if r.random() < 0.7:
        d["phone"] = f"+39 3{r.randint(10, 99)} {r.randint(100000, 9999999)}"
    if r.random() < 0.15:
        d["lifetimeValue"] = Decimal128(f"{r.uniform(10, 9000):.2f}")
    return d


def gen_product(r: random.Random) -> dict[str, Any]:
    cat = r.choice(CATEGORIES)
    attrs: dict[str, Any] = {"color": r.choice(["black", "white", "red", "blue", "green", "beige"])}
    if cat in ("shoes", "kids"):
        attrs["sizes"] = sorted(r.sample(range(28, 47), r.randint(3, 8)))
    if cat in ("jackets", "shirts", "trousers"):
        attrs["sizes"] = r.sample(["XS", "S", "M", "L", "XL", "XXL"], r.randint(2, 6))
        attrs["material"] = r.choice(["cotton", "wool", "linen", "polyester"])
    return {"_id": _oid(r), "sku": f"SKU-{r.randint(1000, 4200)}", "name": f"{cat.title()} {r.randint(1, 900)}",
            "category": cat, "price": round(r.uniform(9, 400), 2), "stock": r.randint(0, 600),
            "attributes": attrs, "active": r.random() < 0.9, "updatedAt": _date(r)}


def gen_contact(r: random.Random) -> dict[str, Any]:
    f, l_, email = _person(r)
    return {"_id": _oid(r), "firstName": f, "lastName": l_, "email": email,
            "company": r.choice(["Acme", "Globex", "Initech", "Umbrella", "Stark", None]),
            "stage": r.choice(["lead", "qualified", "customer", "churned"]), "score": r.randint(0, 100),
            "owner": r.choice(["anna", "marco", "luca", "sara"]), "createdAt": _date(r),
            "lastContactAt": _date(r) if r.random() < 0.8 else None}


def gen_activity(r: random.Random) -> dict[str, Any]:
    kind = r.choice(["call", "email", "meeting", "note"])
    d: dict[str, Any] = {"_id": _oid(r), "contactId": _oid(r), "type": kind, "at": _date(r),
                         "by": r.choice(["anna", "marco", "luca", "sara"])}
    if kind == "call":
        d["durationSec"] = r.randint(20, 3600)
    if kind == "meeting":
        d["attendees"] = r.sample(["anna", "marco", "luca", "sara", "client"], r.randint(2, 4))
    return d


def gen_item(r: random.Random) -> dict[str, Any]:
    d = gen_product(r)
    d["reviews"] = {"count": r.randint(0, 900), "avg": round(r.uniform(1, 5), 1)}
    return d


def gen_review(r: random.Random) -> dict[str, Any]:
    return {"_id": _oid(r), "itemId": _oid(r), "rating": r.choice([1, 2, 3, 4, 4, 5, 5, 5]),
            "text": r.choice(["Great", "Too small", "Perfect fit", "Arrived late", "Love it", "Not as described"]),
            "verified": r.random() < 0.7, "at": _date(r)}


def gen_event(r: random.Random) -> dict[str, Any]:
    return {"_id": _oid(r), "type": r.choice(["login", "view", "click", "purchase"]), "at": _date(r),
            "userId": r.randint(1, 50000), "meta": {"ua": r.choice(["ios", "android", "web"])}}


COLLECTIONS = {
    "shop": {"orders": (gen_order, 251_334), "customers": (gen_customer, 48_210), "products": (gen_product, 3_204)},
    "crm": {"contacts": (gen_contact, 121_800), "activities": (gen_activity, 903_442)},
    "catalog": {"items": (gen_item, 1_402_118), "reviews": (gen_review, 2_118_007)},
    "app": {"events": (gen_event, 50_000)},
}


def dbs_for(cluster_name: str) -> list[str]:
    n = cluster_name.lower()
    if "crm" in n:
        return ["crm"]
    if "catalog" in n:
        return ["catalog"]
    if "order" in n or "shop" in n:
        return ["shop"]
    return ["app"]


def _match(doc: dict[str, Any], query: dict[str, Any]) -> bool:
    for key, cond in query.items():
        val: Any = doc
        for part in key.split("."):
            val = val.get(part) if isinstance(val, dict) else None
        v = display(val) if val is not None and bson_type(val) not in ("Int32", "Int64", "Double") else val
        if isinstance(cond, dict) and any(k.startswith("$") for k in cond):
            for op, arg in cond.items():
                arg = _from_ext(arg)
                if op == "$exists" and bool(arg) != (val is not None):
                    return False
                try:
                    if (op == "$gt" and not v > arg) or (op == "$gte" and not v >= arg) or \
                       (op == "$lt" and not v < arg) or (op == "$lte" and not v <= arg):
                        return False
                except TypeError:
                    return False
                if op == "$ne" and v == arg:
                    return False
                if op == "$in" and v not in [_from_ext(a) for a in arg]:
                    return False
        elif v != _from_ext(cond) and not (isinstance(val, list) and cond in val):
            return False
    return True


def _from_ext(v: Any) -> Any:
    if isinstance(v, dict) and "$date" in v:
        return str(v["$date"])
    return v


MONITOR_USERS = ("monitor", "mongopilot-monitor")


class DemoData:
    def __init__(self, world):
        self.world = world

    @staticmethod
    def check_read(uri: str, db: str, command: str = "find") -> None:
        """Like a real server: the monitoring user has no read access to application data."""
        if user_of(uri) in MONITOR_USERS:
            raise ToolError(f"not authorized on {db} to execute command {{ {command}: ... }} "
                            "(the monitoring user cannot read documents: connect as another user)")

    def _dbs(self, uri: str) -> list[str]:
        host = uri.split("@")[-1].split("/")[0]
        found = self.world.find(host)
        return dbs_for(found[0].name if found else "app")

    def namespaces(self, uri: str) -> list[dict[str, Any]]:
        out = [{"db": db, "collections": [{"name": c, "type": "collection"} for c in sorted(COLLECTIONS[db])]}
               for db in self._dbs(uri)]
        out += [{"db": "admin", "collections": []},
                {"db": "local", "collections": [{"name": "oplog.rs", "type": "collection"},
                                                {"name": "startup_log", "type": "collection"}]}]
        return sorted(out, key=lambda x: x["db"])

    def _spec(self, uri: str, db: str, coll: str):
        if db not in self._dbs(uri) or coll not in COLLECTIONS.get(db, {}):
            raise ToolError(f"Namespace {db}.{coll} not found (demo data: {', '.join(self._dbs(uri))})")
        return COLLECTIONS[db][coll]

    def documents(self, db: str, coll: str, gen, start: int, n: int):
        seed = zlib.crc32(f"{db}.{coll}".encode())
        for i in range(start, start + n):
            yield gen(random.Random(seed * 1_000_003 + i))

    def sample(self, uri: str, db: str, coll: str, flt: str | None, size: int):
        gen, total = self._spec(uri, db, coll)
        self.check_read(uri, db, "aggregate")
        query = json.loads(flt) if flt and flt.strip() else {}
        if not isinstance(query, dict):
            raise ToolError("The filter must be a JSON object")
        rnd = random.Random(size)
        docs: list[dict[str, Any]] = []
        tried = 0
        while len(docs) < size and tried < size * 20:
            i = rnd.randrange(total)
            tried += 1
            doc = next(self.documents(db, coll, gen, i, 1))
            if _match(doc, query):
                docs.append(doc)
        return docs, total

    def write_export(self, uri: str, p: dict[str, Any], out: Path, on_line) -> int:
        gen, total = self._spec(uri, p["db"], p["collection"])
        self.check_read(uri, p["db"])
        query = json.loads(p["query"]) if p.get("query") else {}
        limit = int(p.get("limit") or 0) or 2000
        skip = int(p.get("skip") or 0)
        fields = [f.strip() for f in (p.get("fields") or "").split(",") if f.strip()]
        rows = []
        for doc in self.documents(p["db"], p["collection"], gen, skip, 20000):
            if _match(doc, query):
                rows.append(doc)
            if len(rows) >= min(limit, 2000):
                break
        if fields:
            rows = [{f: _get(d, f) for f in fields} for d in rows]
        if p.get("format") == "csv":
            buf = io.StringIO()
            w = csv.writer(buf)
            if not p.get("no_header"):
                w.writerow(fields)
            for d in rows:
                w.writerow([_plain(d[f]) if d[f] is not None else "" for f in fields])
            out.write_text(buf.getvalue())
        else:
            def dumps(d: dict[str, Any]) -> str:
                return json.dumps(_ext(d), indent=2 if p.get("pretty") else None)
            if p.get("json_array"):
                out.write_text("[" + ",\n".join(dumps(d) for d in rows) + "]\n")
            else:
                out.write_text("\n".join(dumps(d) for d in rows) + "\n")
        on_line(f"connected to: mongodb://{uri.split('@')[-1].split('/')[0]}/")
        on_line(f"exported {len(rows)} records")
        return len(rows)


def _get(doc: dict[str, Any], path: str) -> Any:
    v: Any = doc
    for part in path.split("."):
        v = v.get(part) if isinstance(v, dict) else None
    return v


def _plain(v: Any) -> Any:
    if isinstance(v, (dict, list)):
        return json.dumps(_ext(v))
    return display(v)


def _ext(v: Any) -> Any:
    """Relaxed Extended JSON."""
    t = bson_type(v)
    if t == "ObjectId":
        return {"$oid": str(v)}
    if t == "Date":
        return {"$date": display(v)}
    if t == "Decimal128":
        return {"$numberDecimal": str(v)}
    if isinstance(v, dict):
        return {k: _ext(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_ext(x) for x in v]
    return v


class DemoToolRunner:
    """Simulates the MongoDB Database Tools. mongodump and mongoexport write real files built from the demo
    documents; mongorestore and mongoimport read the file and report what they would write (demo data never changes)."""

    def __init__(self, data: DemoData, delay: float = 0.4):
        self.data = data
        self.delay = delay

    def available(self, tool: str) -> bool:
        return True

    async def run(self, tool: str, uri: str, args: list[str], workdir: Path, on_line, register) -> int:
        multi: dict[str, list[Any]] = {}
        for a in args:
            k, _, v = a.partition("=")
            multi.setdefault(k.lstrip("-"), []).append(v if "=" in a else True)
        opts = {k: v[-1] for k, v in multi.items()}
        try:
            if tool == "mongoexport":
                return await self._export(uri, opts, on_line)
            if tool == "mongodump":
                return await self._dump(uri, opts, on_line)
            if tool == "mongorestore":
                return await self._restore(uri, opts, multi, on_line)
            if tool == "mongoimport":
                return await self._import(uri, opts, on_line)
        except ToolError as exc:
            await asyncio.sleep(self.delay)
            on_line(f"{_now()}\tFailed: {exc}")
            return 1
        raise ToolError(f"{tool} is not simulated")

    async def _export(self, uri, opts, on_line) -> int:
        await asyncio.sleep(self.delay)
        p = {"db": opts["db"], "collection": opts["collection"], "format": opts.get("type", "json"),
             "fields": opts.get("fields", ""), "query": opts.get("query"), "limit": opts.get("limit"),
             "skip": opts.get("skip"), "json_array": "jsonArray" in opts, "pretty": "pretty" in opts,
             "no_header": "noHeaderLine" in opts}
        await asyncio.to_thread(self.data.write_export, uri, p, Path(opts["out"]), on_line)
        return 0

    async def _dump(self, uri, opts, on_line) -> int:
        out = Path(opts["archive"])
        host = uri.split("@")[-1].split("/")[0]
        dbs = [opts["db"]] if "db" in opts else self.data._dbs(uri)
        for db in dbs:
            self.data.check_read(uri, db)
        on_line(f"{_now()}\twriting admin.system.version to archive '{out.name}'")
        content = io.StringIO()
        content.write("# MongoPilot demo: simulated mongodump archive (Extended JSON, not BSON)\n")
        for db in dbs:
            colls = [opts["collection"]] if "collection" in opts else sorted(COLLECTIONS.get(db, {}))
            for coll in colls:
                if coll in str(opts.get("excludeCollection", "")):
                    continue
                gen, total = self.data._spec(uri, db, coll)
                await asyncio.sleep(self.delay)
                on_line(f"{_now()}\twriting {db}.{coll} to archive '{out.name}'")
                n = min(total, 300)
                for doc in self.data.documents(db, coll, gen, 0, n):
                    content.write(json.dumps({"ns": f"{db}.{coll}", "doc": _ext(doc)}) + "\n")
                on_line(f"{_now()}\t[########################]  {db}.{coll}  {n}/{n}  (100.0%)")
                on_line(f"{_now()}\tdone dumping {db}.{coll} ({n} documents)")
        if "oplog" in opts:
            on_line(f"{_now()}\twriting captured oplog to archive")
            on_line(f"{_now()}\t\tdumped 112 oplog entries")
        with gzip.open(out, "wt") as f:
            f.write(content.getvalue())
        on_line(f"{_now()}\tdemo: dump of {host} written")
        return 0

    @staticmethod
    def _check_write(uri: str, ns: str) -> None:
        if user_of(uri) in MONITOR_USERS:
            raise ToolError(f"not authorized on {ns.split('.')[0]} to execute command {{ insert: ... }} "
                            "(the monitoring user cannot write: connect as another user)")

    async def _restore(self, uri, opts, multi, on_line) -> int:
        path = Path(opts["archive"])
        opener = gzip.open if "gzip" in opts else open
        counts: dict[str, int] = {}
        try:
            with opener(path, "rt") as f:
                first = f.readline()
                if not first.startswith("# MongoPilot demo"):
                    raise ValueError
                for line in f:
                    ns = json.loads(line)["ns"]
                    counts[ns] = counts.get(ns, 0) + 1
        except (OSError, ValueError, UnicodeDecodeError, KeyError):
            raise ToolError("demo: this is not an archive written by the demo mongodump, so it cannot be "
                            "simulated (the real mongorestore would read it)") from None
        include = [x for x in multi.get("nsInclude", []) if x is not True]
        exclude = [x for x in multi.get("nsExclude", []) if x is not True]
        renames = list(zip(multi.get("nsFrom", []), multi.get("nsTo", [])))
        on_line(f"{_now()}\tpreparing collections to restore from")
        total = 0
        for ns, n in sorted(counts.items()):
            if include and not any(fnmatch.fnmatchcase(ns, x) for x in include):
                continue
            if any(fnmatch.fnmatchcase(ns, x) for x in exclude):
                continue
            target = ns
            for a, b in renames:
                if fnmatch.fnmatchcase(ns, a):
                    target = b if "*" not in a else b.replace("*", ns.split(".", 1)[1])
            self._check_write(uri, target)
            await asyncio.sleep(self.delay)
            on_line(f"{_now()}\treading metadata for {target} from archive '{path.name}'")
            if "drop" in opts:
                on_line(f"{_now()}\tdropping collection {target} before restoring")
            on_line(f"{_now()}\trestoring {target} from archive '{path.name}'")
            on_line(f"{_now()}\tfinished restoring {target} ({n} documents, 0 failures)")
            total += n
        if "oplogReplay" in opts:
            on_line(f"{_now()}\treplaying oplog")
            on_line(f"{_now()}\tapplied 112 oplog entries")
        if "dryRun" in opts:
            on_line(f"{_now()}\tdry run completed")
            total = 0
        on_line(f"{_now()}\t{total} document(s) restored successfully. 0 document(s) failed to restore.")
        on_line(f"{_now()}\tdemo: nothing was written, the demo data does not change")
        return 0

    async def _import(self, uri, opts, on_line) -> int:
        path = Path(opts["file"])
        ns = f"{opts['db']}.{opts['collection']}"
        self._check_write(uri, ns)
        await asyncio.sleep(self.delay)
        text = path.read_text("utf-8", "replace")
        typ = opts.get("type", "json")
        if typ == "json":
            if "jsonArray" in opts:
                try:
                    n = len(json.loads(text))
                except ValueError:
                    raise ToolError("error reading JSON array: invalid JSON") from None
            else:
                n = sum(1 for ln in text.splitlines() if ln.strip())
        else:
            n = max(0, sum(1 for ln in text.splitlines() if ln.strip()) - (1 if "headerline" in opts else 0))
        on_line(f"{_now()}\tconnected to: mongodb://{uri.split('@')[-1].split('/')[0]}/")
        if "drop" in opts:
            on_line(f"{_now()}\tdropping: {ns}")
        on_line(f"{_now()}\t{n} document(s) imported successfully. 0 document(s) failed to import.")
        on_line(f"{_now()}\tdemo: nothing was written, the demo data does not change")
        return 0


def _now() -> str:
    return dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "+0000"


# ---------------------------------------------------------------------------
# simulated mongosh
# ---------------------------------------------------------------------------

GREEN, CYAN, GREY, RED, RESET = "\x1b[32m", "\x1b[36m", "\x1b[90m", "\x1b[31m", "\x1b[0m"


def js_to_json(text: str) -> str:
    """Accept the relaxed object syntax of the shell: unquoted keys, single quotes."""
    text = re.sub(r"'([^'\\]*)'", lambda m: json.dumps(m.group(1)), text.strip())
    text = re.sub(r"([{,]\s*)([A-Za-z_$][\w$.]*)\s*:", r'\1"\2":', text)
    text = re.sub(r"ISODate\(\s*(\"[^\"]*\")\s*\)", r'{"$date": \1}', text)
    return text or "{}"


def shell_format(v: Any, indent: int = 0) -> str:
    pad = "  " * indent
    t = bson_type(v)
    if t == "ObjectId":
        return f"ObjectId({GREEN}'{v}'{RESET})"
    if t == "Date":
        return f"ISODate({GREEN}'{display(v)}'{RESET})"
    if t == "Decimal128":
        return f"Decimal128({GREEN}'{v}'{RESET})"
    if t == "String":
        return f"{GREEN}{json.dumps(v)}{RESET}"
    if t in ("Int32", "Int64", "Double"):
        return f"{CYAN}{v}{RESET}"
    if t == "Boolean":
        return f"{CYAN}{str(v).lower()}{RESET}"
    if t == "Null":
        return f"{GREY}null{RESET}"
    if isinstance(v, list):
        if not v:
            return "[]"
        if all(bson_type(x) in ("String", "Int32", "Double") for x in v) and len(v) <= 8:
            return "[ " + ", ".join(shell_format(x) for x in v) + " ]"
        return "[\n" + ",\n".join(pad + "  " + shell_format(x, indent + 1) for x in v) + f"\n{pad}]"
    if isinstance(v, dict):
        if not v:
            return "{}"
        inner = ",\n".join(f"{pad}  {k if re.fullmatch(r'[A-Za-z_$][\w$]*', k) else json.dumps(k)}: "
                           f"{shell_format(x, indent + 1)}" for k, x in v.items())
        return "{\n" + inner + f"\n{pad}}}"
    return str(v)


HELP = """
  Shell help (MongoPilot demo, a subset of mongosh is simulated):

    show dbs                                   list databases
    use <db>                                   switch database
    show collections                           list collections of the current database
    db                                         current database
    db.version()                               server version
    db.hello()                                 replica set role of this node
    rs.status()                                replica set members (abridged)
    db.serverStatus().mem                      memory (MB)
    db.stats()                                 database statistics
    db.<coll>.find({filter}).limit(n)          query documents
    db.<coll>.findOne({filter})                one document
    db.<coll>.countDocuments({filter})         count (on a sample in the demo)
    db.<coll>.estimatedDocumentCount()         collection size
    db.<coll>.getIndexes()                     indexes
    cls                                        clear the screen
    exit                                       close the session
"""


class DemoShell(ShellSession):
    def __init__(self, world, data: DemoData, uri: str, **kw):
        super().__init__(**kw)
        host = kw["host"]
        self.world, self.data, self.uri, self.node = world, data, uri, host
        self.line = ""
        self.history: list[str] = []
        self.hpos = 0
        self.esc = ""
        dbs = data._dbs(uri)
        self.cur = dbs[0] if dbs else "test"
        found = world.find(host)
        version = found[1].version if found else "8.0.4"
        self.emit(f"{GREY}Connecting to {host} as {user_of(uri)} with mongosh (MongoPilot demo: simulated shell)…{RESET}\r\n"
                  f"Using MongoDB:\t\t{version}\r\nUsing Mongosh:\t\t2.5.0 (simulated)\r\n\r\n"
                  f"Type {CYAN}help{RESET} for the commands this demo understands.\r\n\r\n")
        self.emit(self.prompt())

    def prompt(self) -> str:
        found = self.world.find(self.node)
        if not found:
            return f"{self.cur}> "
        c, m = found
        if m.state == "MONGOS":
            return f"[direct: mongos] {self.cur}> "
        return f"{m.rs or c.name} [direct: {m.state.lower()}] {self.cur}> "

    def write(self, data: str) -> None:
        if self.closed:
            return
        for ch in data:
            if self.esc:
                self.esc += ch
                if len(self.esc) >= 3 or (len(self.esc) == 2 and ch != "["):
                    seq, self.esc = self.esc, ""
                    if seq == "\x1b[A" and self.history:
                        self.hpos = max(0, self.hpos - 1)
                        self._replace(self.history[self.hpos])
                    elif seq == "\x1b[B" and self.history:
                        self.hpos = min(len(self.history), self.hpos + 1)
                        self._replace(self.history[self.hpos] if self.hpos < len(self.history) else "")
                continue
            if ch == "\x1b":
                self.esc = ch
            elif ch in "\r\n":
                cmd, self.line = self.line, ""
                self.emit("\r\n")
                if cmd.strip():
                    self.history.append(cmd)
                self.hpos = len(self.history)
                self.run(cmd.strip())
                if not self.closed:
                    self.emit(self.prompt())
            elif ch in ("\x7f", "\b"):
                if self.line:
                    self.line = self.line[:-1]
                    self.emit("\b \b")
            elif ch == "\x03":
                self.line = ""
                self.emit("^C\r\n" + self.prompt())
            elif ch == "\x04":
                self.run("exit")
            elif ch == "\t":
                continue
            elif ch >= " ":
                self.line += ch
                self.emit(ch)

    def _replace(self, text: str) -> None:
        self.emit("\b \b" * len(self.line) + text)
        self.line = text

    def out(self, text: str) -> None:
        self.emit(text.replace("\n", "\r\n") + "\r\n")

    def run(self, cmd: str) -> None:
        try:
            self._run(cmd)
        except ToolError as exc:
            self.out(f"{RED}MongoServerError: {exc}{RESET}")
        except (ValueError, KeyError) as exc:
            self.out(f"{RED}SyntaxError: {exc}{RESET}")

    def _run(self, cmd: str) -> None:  # noqa: C901 - a small command dispatcher
        cmd = cmd.rstrip(";").strip()
        if not cmd:
            return
        if cmd in ("exit", "quit", "exit()", "quit()", ".exit"):
            self.out(f"{GREY}Bye{RESET}")
            self.closed = True
            return
        if cmd == "help":
            return self.out(HELP)
        if cmd == "cls":
            return self.emit("\x1b[2J\x1b[H")
        if cmd in ("show dbs", "show databases"):
            rows = [(ns["db"], sum(COLLECTIONS.get(ns["db"], {}).get(c["name"], (None, 50))[1] for c in ns["collections"]))
                    for ns in self.data.namespaces(self.uri)]
            return self.out("\n".join(f"{db:<12}{max(n * 0.0007, 0.04):>10.2f} MiB" for db, n in rows))
        m = re.fullmatch(r"use\s+(\w+)", cmd)
        if m:
            self.cur = m.group(1)
            return self.out(f"switched to db {self.cur}")
        if cmd in ("show collections", "show tables", "db.getCollectionNames()"):
            return self.out("\n".join(sorted(COLLECTIONS.get(self.cur, {}))) or "")
        if cmd == "db":
            return self.out(self.cur)
        found = self.world.find(self.node)
        c, mem = found if found else (None, None)
        if cmd == "db.version()":
            return self.out(mem.version if mem else "8.0.4")
        if cmd in ("db.hello()", "db.isMaster()", "rs.isMaster()", "rs.hello()"):
            doc = {"isWritablePrimary": bool(mem and mem.state == "PRIMARY"),
                   "secondary": bool(mem and mem.state == "SECONDARY"), "setName": mem.rs if mem else None,
                   "me": self.node, "maxWireVersion": 25, "ok": 1}
            return self.out(shell_format(doc))
        if cmd == "rs.status()":
            if not c or not mem or not mem.rs:
                raise ToolError("not running with --replSet")
            members = [{"_id": i, "name": x.host, "stateStr": x.state if x.running else "(not reachable/healthy)",
                        "health": 1 if x.running else 0} for i, x in enumerate(c.sets.get(mem.rs, []))]
            return self.out(shell_format({"set": mem.rs, "members": members, "ok": 1}))
        if cmd.startswith("db.serverStatus()"):
            ss = self.world.server_status(mem) if mem else {}
            path = cmd[len("db.serverStatus()"):].lstrip(".")
            v: Any = ss
            for part in [p for p in path.split(".") if p]:
                v = v.get(part) if isinstance(v, dict) else None
            if not path:
                v = {k: ss.get(k) for k in ("host", "version", "process", "uptime", "mem", "connections")}
            return self.out(shell_format(v))
        if cmd == "db.stats()":
            colls = COLLECTIONS.get(self.cur, {})
            objs = sum(t for _, t in colls.values())
            return self.out(shell_format({"db": self.cur, "collections": len(colls), "objects": objs,
                                          "avgObjSize": 412.5, "dataSize": objs * 412.5, "ok": 1}))
        m = re.fullmatch(r"db\.(?:getCollection\(['\"]([^'\"]+)['\"]\)|([A-Za-z_][\w.]*?))\.(\w+)\((.*?)\)(.*)", cmd)
        if m:
            coll = m.group(1) or m.group(2)
            method, arg, rest = m.group(3), m.group(4), m.group(5)
            gen, total = self.data._spec(self.uri, self.cur, coll)
            flt = js_to_json(arg.split("},", 1)[0] + ("}" if "}," in arg else "")) if arg.strip() else "{}"
            limit = 20
            lm = re.search(r"\.limit\((\d+)\)", rest)
            if lm:
                limit = min(int(lm.group(1)), 50)
            if method == "estimatedDocumentCount":
                return self.out(shell_format(total))
            if method == "countDocuments":
                if flt.strip() in ("{}", ""):
                    return self.out(shell_format(total))
                query = json.loads(flt)
                rnd = random.Random(7)
                probe = [next(self.data.documents(self.cur, coll, gen, rnd.randrange(total), 1)) for _ in range(400)]
                hits = sum(1 for d in probe if _match(d, query))
                return self.out(shell_format(round(total * hits / 400)) + f"   {GREY}// estimated from 400 documents in the demo{RESET}")
            if method == "getIndexes":
                return self.out(shell_format([{"v": 2, "key": {"_id": 1}, "name": "_id_"}]))
            if method in ("find", "findOne"):
                docs, _ = self.data.sample(self.uri, self.cur, coll, flt, 1 if method == "findOne" else limit)
                if method == "findOne":
                    return self.out(shell_format(docs[0]) if docs else f"{GREY}null{RESET}")
                text = shell_format(docs)
                if limit >= 20 and len(docs) == limit:
                    text += f'\nType "{CYAN}it{RESET}" for more (not simulated in the demo)'
                return self.out(text)
            raise ToolError(f"db.{coll}.{method}() is not simulated in the demo: type help")
        raise ToolError(f"'{cmd}' is not simulated in the MongoPilot demo shell: type help to see what is")
