"""Tests for mongorestore and mongoimport (v0.5)."""
from __future__ import annotations

import asyncio
import gzip
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from mongopilot.db import Database
from mongopilot.demo import DemoWorld
from mongopilot.demo_tools import DemoData, DemoToolRunner
from mongopilot.tools import ExportManager, ToolError, import_plan, import_type, restorable, restore_plan

URI = "mongodb://alice:pw@h1:27017/?directConnection=true"


async def _chunks(*parts: bytes):
    for p in parts:
        yield p


class PlansTest(unittest.TestCase):
    def test_restore_plan(self):
        args = restore_plan({"ns_include": "crm.*, shop.orders", "ns_from": "crm.*", "ns_to": "crm_copy.*", "drop": True,
                             "parallel": 2, "workers": 3}, "x.archive.gz")
        self.assertEqual(args[0], "--gzip")
        self.assertIn("--nsInclude=crm.*", args)
        self.assertIn("--nsInclude=shop.orders", args)
        self.assertEqual(args[args.index("--nsFrom=crm.*") + 1], "--nsTo=crm_copy.*")
        self.assertIn("--drop", args)
        self.assertIn("--numInsertionWorkersPerCollection=3", args)
        self.assertNotIn("--gzip", restore_plan({}, "x.archive"))
        for p, msg in [({"ns_include": "a b"}, "valid namespace"), ({"ns_from": "a.*"}, "as many"),
                       ({"preserve_uuid": True}, "requires --drop"), ({"oplog_replay": True, "ns_include": "a.*"}, "whole dump")]:
            with self.assertRaisesRegex(ToolError, msg):
                restore_plan(p, "x.archive.gz")
        with self.assertRaisesRegex(ToolError, "archive"):
            restore_plan({}, "data.json")

    def test_import_plan(self):
        args = import_plan({"db": "shop", "collection": "prices", "headerline": True, "mode": "upsert",
                            "upsert_fields": "sku", "ignore_blanks": True}, "p.csv.gz")
        self.assertIn("--type=csv", args)
        self.assertIn("--headerline", args)
        self.assertIn("--mode=upsert", args)
        self.assertIn("--upsertFields=sku", args)
        self.assertIn("--jsonArray", import_plan({"db": "a", "collection": "b", "json_array": True}, "x.json"))
        for p, msg in [({"db": "a", "collection": "b", "format": "csv"}, "header line"),
                       ({"db": "a", "collection": "b", "upsert_fields": "x"}, "upsert"),
                       ({"db": "a", "collection": "b", "drop": True, "mode": "merge"}, "insert mode"),
                       ({"db": "a"}, "database and a collection")]:
            with self.assertRaisesRegex(ToolError, msg):
                import_plan(p, "x.json")
        self.assertEqual(import_type("a.tsv.gz"), "tsv")
        self.assertIsNone(import_type("a.archive"))
        self.assertTrue(restorable("upload-x.archive.gz"))


class WriteToolsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = Database(self.tmp / "t.sqlite3")
        world = DemoWorld(speed=0.01)
        world.add_replicaset("crm", "crm", ["h1:27017", "h2:27017"], "8.0.4")
        data = DemoData(world)
        self.dep = {"id": self.db.insert("deployments", {"name": "crm", "uri_enc": "x", "created_at": 0}), "name": "crm"}
        self.mgr = ExportManager(self.db, SimpleNamespace(data_dir=self.tmp), DemoToolRunner(data, delay=0.01), data)

    def run_kind(self, kind, params, role="PRIMARY", uri=URI):
        async def go():
            rid = self.mgr.start(kind, self.dep, "h1:27017", role, uri, params, "t", "admin")
            await asyncio.gather(*self.mgr.tasks.values())
            return rid
        return self.db.one("SELECT * FROM exports WHERE id = ?", (asyncio.run(go()),))

    def test_dump_then_restore(self):
        dump = self.run_kind("dump", {"db": "crm"}, role="SECONDARY")
        self.assertEqual(dump["status"], "success")
        row = self.run_kind("restore", {"source_id": dump["id"], "ns_from": "crm.*", "ns_to": "crm_copy.*", "drop": True})
        self.assertEqual(row["status"], "success", row)
        self.assertIn("finished restoring crm_copy.contacts (300 documents", row["log"])
        self.assertIn("600 document(s) restored successfully", row["log"])
        self.assertIn("--nsFrom=crm.*", row["command"])
        self.assertIsNone(row["file_name"])
        row = self.run_kind("restore", {"source_id": dump["id"], "ns_include": "crm.contacts", "dry_run": True})
        self.assertIn("0 document(s) restored", row["log"])
        with self.assertRaisesRegex(ToolError, "primary"):
            self.run_kind("restore", {"source_id": dump["id"]}, role="SECONDARY")
        row = self.run_kind("restore", {"source_id": dump["id"]}, uri="mongodb://monitor:x@h1:27017/")
        self.assertEqual(row["status"], "failed")
        self.assertIn("not authorized", row["error"])

    def test_upload_and_import(self):
        rid = asyncio.run(self.mgr.save_upload(self.dep, "prices.csv.gz", _chunks(gzip.compress(b"sku,price\na,1\nb,2\nc,3\n")),
                                               "", "admin", 10 * 1048576))
        up = self.db.one("SELECT * FROM exports WHERE id = ?", (rid,))
        self.assertEqual(up["kind"], "upload")
        self.assertTrue(up["file_name"].endswith(".csv.gz"))
        row = self.run_kind("import", {"source_id": rid, "db": "shop", "collection": "prices", "headerline": True})
        self.assertEqual(row["status"], "success", row)
        self.assertIn("3 document(s) imported successfully", row["log"])
        self.assertIn("--file=upload-", row["command"])
        self.assertNotIn(".gz", row["command"].split("--file=")[1])
        self.assertEqual([p.name for p in (self.tmp / "exports").iterdir() if p.name.startswith(".")], [])
        with self.assertRaisesRegex(ToolError, "archive"):
            self.run_kind("restore", {"source_id": rid})
        with self.assertRaisesRegex(ToolError, "Upload a mongodump"):
            asyncio.run(self.mgr.save_upload(self.dep, "evil.sh", _chunks(b"x"), "", "a", 100))
        with self.assertRaisesRegex(ToolError, "larger"):
            asyncio.run(self.mgr.save_upload(self.dep, "big.json", _chunks(b"x" * 80, b"y" * 80), "", "a", 100))
        self.assertEqual(self.db.one("SELECT COUNT(*) AS n FROM exports WHERE kind = 'upload'")["n"], 1)


if __name__ == "__main__":
    unittest.main()
