"""Tests for the v0.4 features: tools (mongosh, mongodump, mongoexport, schema analyzer) and CPU/memory from
serverStatus."""
from __future__ import annotations

import asyncio
import datetime as dt
import gzip
import json
import re
import stat
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from mongopilot import metrics as M
from mongopilot.db import Database
from mongopilot.demo import DemoWorld
from mongopilot.demo_tools import DemoData, DemoShell, DemoToolRunner, ObjectId, js_to_json
from mongopilot.schema import analyze, bson_type
from mongopilot.tools import (ExportManager, ShellManager, ToolError, dump_plan, export_plan, node_uri, redact,
                              user_of)

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


class SchemaTest(unittest.TestCase):
    def test_analyze(self):
        import random
        r = random.Random(1)
        docs = []
        for i in range(200):
            d = {"_id": ObjectId(1700000000 + i, r), "name": f"n{i % 5}", "age": i if i % 4 else float(i) + .5,
                 "at": dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc) + dt.timedelta(hours=i),
                 "items": [{"sku": "a", "qty": 1}, {"sku": "b"}][: i % 3], "flag": i % 2 == 0}
            if i % 10:
                d["addr"] = {"city": "Rome", "zip": "00184" if i % 7 else 184}
            if i == 3:
                d["name"] = None
            docs.append(d)
        r = analyze(docs)
        self.assertEqual(r["sampled"], 200)
        f = {x["path"]: x for x in r["fields"]}
        self.assertEqual(r["fields"][0]["path"], "_id")
        self.assertEqual(f["_id"]["types"][0]["name"], "ObjectId")
        self.assertTrue(f["_id"]["types"][0]["unique"])
        names = {t["name"]: t for t in f["name"]["types"]}
        self.assertEqual(names["String"]["count"], 199)
        self.assertEqual(names["Null"]["count"], 1)
        self.assertEqual(names["String"]["distinct"], 5)
        ages = {t["name"]: t for t in f["age"]["types"]}
        self.assertEqual(set(ages), {"Int32", "Double"})
        self.assertEqual(ages["Int32"]["count"], 150)
        self.assertEqual(sum(ages["Int32"]["histogram"]["bins"]), 150)
        self.assertEqual(f["addr"]["probability"], 0.9)
        self.assertEqual(f["addr"]["missing"], 20)
        addr = f["addr"]["types"][0]
        zips = {t["name"] for t in next(x for x in addr["fields"] if x["name"] == "zip")["types"]}
        self.assertEqual(zips, {"String", "Int32"})
        items = f["items"]["types"][0]
        self.assertEqual(items["name"], "Array")
        self.assertEqual((items["min_length"], items["max_length"]), (0, 2))
        el = items["element"]["types"][0]
        self.assertEqual(el["name"], "Document")
        sku = next(x for x in el["fields"] if x["name"] == "sku")
        qty = next(x for x in el["fields"] if x["name"] == "qty")
        self.assertEqual(sku["path"], "items.sku")
        self.assertLess(qty["probability"], sku["probability"])
        flag = f["flag"]["types"][0]
        self.assertEqual((flag["true"], flag["false"]), (100, 100))
        at = f["at"]["types"][0]
        self.assertEqual(at["min"], "2026-01-01T00:00:00.000Z")
        self.assertEqual(sum(at["hours"]), 200)
        json.dumps(r)   # JSON-safe

    def test_bson_type(self):
        self.assertEqual(bson_type(2**40), "Int64")
        self.assertEqual(bson_type(True), "Boolean")
        self.assertEqual(bson_type(b"x"), "Binary")
        self.assertEqual(analyze([])["fields"], [])


class ToolsUnitTest(unittest.TestCase):
    def test_node_uri(self):
        u = node_uri("mongodb://mon:p%40ss@a:1,b:2/?replicaSet=rs0&authSource=admin&tls=true&tlsCAFile=/x/ca.pem", "b:2")
        self.assertTrue(u.startswith("mongodb://mon:p%40ss@b:2/?"))
        self.assertNotIn("replicaSet", u)
        self.assertIn("directConnection=true", u)
        self.assertIn("tlsCAFile=/x/ca.pem", u)
        u = node_uri("mongodb+srv://mon:x@cluster.example.com/?authSource=admin", "shard-00-01.example.com:27017",
                     ("alice", "s3cr:t", "users"))
        self.assertTrue(u.startswith("mongodb://alice:s3cr%3At@shard-00-01.example.com:27017/?"))
        self.assertIn("tls=true", u)
        self.assertIn("authSource=users", u)
        self.assertNotIn("authSource=admin", u)
        self.assertEqual(user_of(u), "alice")
        self.assertEqual(redact("x mongodb://alice:secret@h:1/ y"), "x mongodb://alice:****@h:1/ y")

    def test_dump_plan(self):
        args, suffix = dump_plan({"db": "shop", "collection": "orders", "query": '{"a": 1}', "parallel": 2}, "SECONDARY")
        self.assertEqual(suffix, ".archive.gz")
        self.assertIn("--gzip", args)
        self.assertIn('--query={"a":1}', args)
        self.assertIn("--numParallelCollections=2", args)
        self.assertIn("--oplog", dump_plan({"oplog": True}, "PRIMARY")[0])
        for bad, role, msg in [({"oplog": True, "db": "x"}, "PRIMARY", "full dump"), ({"oplog": True}, "MONGOS", "replica set"),
                               ({"query": "{}", "db": "x"}, "PRIMARY", "one collection"), ({"collection": "c"}, "PRIMARY", "needs a database"),
                               ({"db": "x", "collection": "c", "query": "[1]"}, "PRIMARY", "object"),
                               ({"db": "x", "collection": "c", "query": "{bad"}, "PRIMARY", "valid JSON"),
                               ({"users_and_roles": True}, "PRIMARY", "one database"), ({"db": "a b"}, "PRIMARY", "Invalid")]:
            with self.assertRaisesRegex(ToolError, msg):
                dump_plan(bad, role)

    def test_export_plan(self):
        args, suffix = export_plan({"db": "shop", "collection": "orders", "format": "csv", "fields": "a, b.c",
                                    "limit": "10", "gzip": True, "sort": '{"a": -1}'})
        self.assertEqual(suffix, ".csv.gz")
        self.assertIn("--fields=a,b.c", args)
        self.assertIn("--limit=10", args)
        self.assertIn('--sort={"a":-1}', args)
        args, suffix = export_plan({"db": "d", "collection": "c", "json_array": True, "pretty": True, "json_format": "canonical"})
        self.assertEqual(suffix, ".json")
        self.assertIn("--jsonArray", args)
        self.assertIn("--jsonFormat=canonical", args)
        with self.assertRaisesRegex(ToolError, "fields"):
            export_plan({"db": "d", "collection": "c", "format": "csv"})
        with self.assertRaisesRegex(ToolError, "database and a collection"):
            export_plan({"db": "d"})
        with self.assertRaisesRegex(ToolError, "at least 0"):
            export_plan({"db": "d", "collection": "c", "limit": -1})

    def test_js_to_json(self):
        self.assertEqual(json.loads(js_to_json("{status: 'paid', 'a.b': {$gt: 3}}")), {"status": "paid", "a.b": {"$gt": 3}})

    def test_cpu_from_extra_info(self):
        c0 = {"cpu_user_us": 0, "cpu_sys_us": 0}
        c1 = {"cpu_user_us": 3_000_000, "cpu_sys_us": 1_000_000}
        out = M.rates((0.0, c0), (10.0, c1), 100, 110, 4)
        self.assertEqual(out["cpu_user_pct"], 7.5)
        self.assertEqual(out["cpu_sys_pct"], 2.5)
        self.assertEqual(out["cpu_process_pct"], 10.0)
        self.assertEqual(out["cpu_cores_used"], 0.4)
        out = M.rates((0.0, c0), (10.0, c1), 100, 110, None)
        self.assertNotIn("cpu_user_pct", out)
        self.assertEqual(out["cpu_cores_used"], 0.4)
        g = M.gauges({"mem": {"resident": 4000, "virtual": 9000}}, {"mem_mb": 16000})
        self.assertEqual(g["mem_resident_pct"], 25.0)


class ExportManagerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = Database(self.tmp / "t.sqlite3")
        self.world = DemoWorld(speed=0.01)
        self.world.add_replicaset("orders-prod", "orders", ["h1:27017", "h2:27017"], "8.0.4")
        self.data = DemoData(self.world)
        self.dep = {"id": self.db.insert("deployments", {"name": "orders-prod", "uri_enc": "x", "created_at": 0}),
                    "name": "orders-prod"}
        self.mgr = ExportManager(self.db, SimpleNamespace(data_dir=self.tmp), DemoToolRunner(self.data, delay=0.01), self.data)

    def run_export(self, kind, params, uri="mongodb://alice:pw@h2:27017/?directConnection=true", role="SECONDARY"):
        async def go():
            rid = self.mgr.start(kind, self.dep, "h2:27017", role, uri, params, "test", "admin")
            await asyncio.gather(*self.mgr.tasks.values())
            return rid
        rid = asyncio.run(go())
        return self.db.one("SELECT * FROM exports WHERE id = ?", (rid,))

    def test_export_and_dump(self):
        row = self.run_export("export", {"db": "shop", "collection": "orders", "format": "csv", "fields": "_id,total,status",
                                         "query": '{"status": "paid"}', "limit": 20, "gzip": True})
        self.assertEqual(row["status"], "success", row)
        self.assertNotIn("pw", row["command"])
        self.assertIn("alice:****", row["command"])
        path = self.mgr.path(row)
        self.assertTrue(row["file_name"].endswith(".csv.gz"))
        lines = gzip.open(path, "rt").read().splitlines()
        self.assertEqual(lines[0], "_id,total,status")
        self.assertEqual(len(lines), 21)
        self.assertTrue(all(ln.endswith(",paid") for ln in lines[1:]))
        self.assertEqual(row["size"], path.stat().st_size)

        row = self.run_export("dump", {"oplog": True})
        self.assertEqual(row["status"], "success", row)
        self.assertIn("--oplog", row["command"])
        self.assertIn("oplog", row["log"])
        self.assertTrue(gzip.open(self.mgr.path(row), "rt").readline().startswith("# MongoPilot demo"))
        self.mgr.delete(row)
        self.assertFalse(self.mgr.path(row).exists())
        self.assertIsNone(self.db.one("SELECT id FROM exports WHERE id = ?", (row["id"],)))

    def test_monitor_user_cannot_read(self):
        row = self.run_export("export", {"db": "shop", "collection": "orders"},
                              uri="mongodb://mongopilot-monitor:x@h2:27017/?directConnection=true")
        self.assertEqual(row["status"], "failed")
        self.assertIn("not authorized", row["error"])
        self.assertFalse((self.tmp / "exports" / row["file_name"]).exists())

    def test_validation_and_restart_recovery(self):
        with self.assertRaises(ToolError):
            asyncio.run(self._start_bad())
        self.db.insert("exports", {"deployment_id": self.dep["id"], "kind": "dump", "host": "h", "status": "running",
                                   "created_at": 0})
        ExportManager(self.db, SimpleNamespace(data_dir=self.tmp), None)
        self.assertEqual(self.db.one("SELECT status FROM exports WHERE host = 'h'")["status"], "failed")

    async def _start_bad(self):
        self.mgr.start("dump", self.dep, "h2:27017", "MONGOS", "mongodb://h2/", {"oplog": True}, "", "a")


class ShellTest(unittest.TestCase):
    def test_demo_shell(self):
        world = DemoWorld(speed=0.01)
        world.add_replicaset("crm", "crm", ["c1:27017", "c2:27017"], "7.0.14")
        sh = DemoShell(world, DemoData(world), "mongodb://alice:pw@c1:27017/", dep_id=1, host="c1:27017", actor="a", description="")
        for cmd in ["show dbs\r", "use crm\r", "db.contacts.findOne({stage: 'lead'})\r", "db.contacts.estimatedDocumentCount()\r",
                    "rs.status()\r", "nope\r", "abc\x7f\x7f\x7fdb\r"]:
            sh.write(cmd)
        text = ANSI.sub("", sh.read(0)[0])
        self.assertIn("switched to db crm", text)
        self.assertIn('stage: "lead"', text)
        self.assertIn("121800", text)
        self.assertIn('set: "crm"', text)
        self.assertIn("not simulated", text)
        self.assertIn("crm [direct: ", text)
        sh.write("exit\r")
        self.assertTrue(sh.closed)
        mon = DemoShell(world, DemoData(world), "mongodb://monitor:x@c1:27017/", dep_id=1, host="c1:27017", actor="a", description="")
        mon.write("use crm\rdb.contacts.find()\r")
        self.assertIn("not authorized", ANSI.sub("", mon.read(0)[0]))

    def test_pty_shell(self):
        tmp = Path(tempfile.mkdtemp())
        fake = tmp / "fake-mongosh"
        fake.write_text('#!/bin/sh\necho "args: $1 $2"\nexec python3 -q -i\n')
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        mgr = ShellManager(SimpleNamespace(mongosh_binary=str(fake), data_dir=tmp))
        self.assertTrue(mgr.available())
        sess = mgr.open("mongodb://u:p@h:1/?directConnection=true", "h:1", 24, 80, dep_id=1, actor="a", description="")
        sess.write("print(6*7)\r")
        sess.resize(40, 120)
        sess.write("import shutil; print(shutil.get_terminal_size().columns)\r")
        deadline = time.time() + 10
        text = ""
        while time.time() < deadline and ("42" not in text or "120" not in text):
            time.sleep(0.1)
            text = ANSI.sub("", sess.read(0)[0])
        self.assertIn("Connecting to h:1 as u", text)
        self.assertIn("args: mongodb://u:p@h:1/?directConnection=true --quiet", text)
        self.assertIn("42", text)
        self.assertIn("120", text)
        self.assertIs(mgr.get(sess.id), sess)
        mgr.close(sess.id)
        with self.assertRaises(ToolError):
            mgr.get(sess.id)
        missing = ShellManager(SimpleNamespace(mongosh_binary="/nonexistent/mongosh", data_dir=tmp))
        self.assertFalse(missing.available())
        with self.assertRaisesRegex(ToolError, "not installed"):
            missing.open("mongodb://h/", "h", 24, 80, dep_id=1, actor="a", description="")


if __name__ == "__main__":
    unittest.main()
