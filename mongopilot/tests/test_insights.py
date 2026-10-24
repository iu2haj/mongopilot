"""Tests for slow-query parsing, query shapes, ESR index suggestions and the index advisor."""

from __future__ import annotations

import json
import time
import unittest

from mongopilot import insights as I
from mongopilot.probe import parse_slow_log_lines


def log_line(attr, t="2026-10-23T10:00:00.123+00:00", msg="Slow query"):
    return json.dumps({"t": {"$date": t}, "s": "I", "c": "COMMAND", "id": 51803, "ctx": "conn1", "msg": msg,
                       "attr": attr})


class SlowLogTest(unittest.TestCase):
    def test_parse_filters_and_since(self):
        lines = [log_line({"ns": "a.b"}, "2026-10-23T10:00:00.000+00:00"),
                 log_line({"ns": "a.c"}, "2026-10-23T10:00:05.500Z"),
                 log_line({"ns": "a.d"}, msg="Connection accepted"),
                 "not json \"Slow query\""]
        since = time.mktime((2026, 10, 23, 10, 0, 1, 0, 0, 0)) - time.timezone
        out = parse_slow_log_lines(lines, since)
        self.assertEqual([e["ns"] for e in out], ["a.c"])
        self.assertAlmostEqual(out[0]["_ts"] % 60, 5.5, places=3)

    def test_extract_query_variants(self):
        f = I.extract_query({"ns": "shop.orders", "type": "command",
                             "command": {"find": "orders", "filter": {"a": 1}, "sort": {"b": -1}, "$db": "shop"}})
        self.assertEqual((f["op"], f["filter"], f["sort"]), ("find", {"a": 1}, {"b": -1}))
        a = I.extract_query({"ns": "shop.orders", "command": {"aggregate": "orders", "pipeline": [
            {"$match": {"s": "x"}}, {"$sort": {"t": 1}}, {"$group": {"_id": 1}}], "$db": "shop"}})
        self.assertEqual((a["filter"], a["sort"]), ({"s": "x"}, {"t": 1}))
        u = I.extract_query({"ns": "shop.orders", "type": "update", "command": {"q": {"s": 1}, "u": {}}})
        self.assertEqual((u["op"], u["filter"]), ("update", {"s": 1}))
        g = I.extract_query({"ns": "shop.orders", "command": {"getMore": 1, "collection": "orders"},
                             "originatingCommand": {"find": "orders", "filter": {"z": 1}}})
        self.assertEqual(g["filter"], {"z": 1})
        self.assertIsNone(I.extract_query({"ns": "local.oplog.rs", "command": {"find": "oplog.rs"}}))
        self.assertIsNone(I.extract_query({"ns": "admin.$cmd", "command": {"serverStatus": 1}}))

    def test_redaction_and_shape_key(self):
        q1 = {"ns": "s.o", "op": "find", "filter": {"c": {"$oid": "1"}, "d": {"$gte": {"$date": "x"}},
                                                    "e": [1, 2], "n": None, "b": True}, "sort": {}}
        q2 = {**q1, "filter": {"c": {"$oid": "2"}, "d": {"$gte": {"$date": "y"}}, "e": [7, 8], "n": None, "b": False}}
        r = I.redact(q1["filter"])
        self.assertEqual(r, {"c": "<objectId>", "d": {"$gte": "<date>"}, "e": ["<array>"], "n": None, "b": "<bool>"})
        self.assertEqual(I.shape_of(q1)["key"], I.shape_of(q2)["key"])
        self.assertNotIn("2026", json.dumps(I.slow_entry({"ns": "s.o", "command": {"find": "o", "filter": {
            "secret": "2026-private"}}})))


class EsrTest(unittest.TestCase):
    def test_esr_order(self):
        s = I.suggest_keys({"status": "<s>", "created": {"$gte": "<d>"}, "cust": "<o>"}, {"total": -1})
        self.assertEqual(s["keys"][0]["key"], [("status", 1), ("cust", 1), ("total", -1), ("created", 1)])
        self.assertEqual(s["keys"][0]["n_eq"], 2)

    def test_in_counts_as_equality_and_regex_as_range(self):
        c = I.classify({"a": {"$in": ["<array>"]}, "b": {"$regex": "<regex>"}, "c": {"$ne": 1}})
        self.assertEqual((c["eq"], c["range"]), (["a"], ["b", "c"]))

    def test_or_branches_and_unsupported(self):
        s = I.suggest_keys({"$or": [{"email": "<s>"}, {"phone": "<s>"}]}, {})
        self.assertEqual([k["key"] for k in s["keys"]], [[("email", 1)], [("phone", 1)]])
        self.assertIn("$or", s["note"])
        self.assertEqual(I.suggest_keys({"$text": {"$search": "<s>"}}, {})["keys"], [])
        self.assertEqual(I.suggest_keys({}, {})["keys"], [])

    def test_index_supports(self):
        want = [("status", 1), ("cust", 1), ("total", -1)]
        self.assertTrue(I.index_supports({"cust": 1, "status": 1, "total": -1}, want, 2))
        self.assertTrue(I.index_supports({"cust": 1, "status": 1, "total": 1, "x": 1}, want, 2))  # reversed sort ok
        self.assertFalse(I.index_supports({"cust": 1, "total": -1, "status": 1}, want, 2))
        self.assertFalse(I.index_supports({"cust": 1, "status": 1}, want, 2))
        self.assertFalse(I.index_supports({"cust": "hashed", "status": 1, "total": -1}, want, 2))

    def test_key_to_js(self):
        self.assertEqual(I.key_to_js([("a", 1), ("b.c", -1), ("t", "text")]), '{ a: 1, "b.c": -1, t: "text" }')


class AdvisorTest(unittest.TestCase):
    def rows(self):
        now = time.time()
        shape_scan = json.dumps({"filter": {"status": "<string>", "day": {"$gte": "<date>"}}, "sort": {}})
        shape_ok = json.dumps({"filter": {"orderId": "<objectId>"}, "sort": {}})
        out = []
        for i in range(20):
            out.append({"shape_key": "scan", "ns": "shop.orders", "op": "find", "shape": shape_scan, "duration_ms": 800.0,
                        "docs_examined": 100000, "keys_examined": 0, "nreturned": 10, "has_sort": 0, "plan": "COLLSCAN",
                        "app": "api", "host": "h1", "ts": now - i})
            out.append({"shape_key": "ok", "ns": "shop.items", "op": "find", "shape": shape_ok, "duration_ms": 120.0,
                        "docs_examined": 5, "keys_examined": 5, "nreturned": 5, "has_sort": 0,
                        "plan": "IXSCAN { orderId: 1 }", "app": "", "host": "h1", "ts": now - i})
        return out

    def inventory(self):
        now = time.time()

        def u(ops, days=30):
            return {"ops": ops, "since": now - days * 86400, "hosts": ["h1"]}
        return {"collections": {
            "shop.orders": {"indexes": [
                {"name": "_id_", "key": [("_id", 1)]},
                {"name": "status_1", "key": [("status", 1)]},
                {"name": "status_1_owner_1", "key": [("status", 1), ("owner", 1)]},
                {"name": "legacy_1", "key": [("legacy", 1)]},
                {"name": "tax_1", "key": [("tax", 1)], "unique": True},
                {"name": "exp_1", "key": [("exp", 1)], "expireAfterSeconds": 60},
                {"name": "fresh_1", "key": [("fresh", 1)]},
            ], "usage": {"_id_": u(10), "status_1": u(50), "status_1_owner_1": u(9), "legacy_1": u(0),
                         "tax_1": u(0), "exp_1": u(0), "fresh_1": u(0, days=2)},
                "sizes": {"legacy_1": 1000}, "count": 10, "size": 100},
            "shop.items": {"indexes": [{"name": "_id_", "key": [("_id", 1)]},
                                       {"name": "orderId_1", "key": [("orderId", 1)]}],
                           "usage": {"_id_": u(1), "orderId_1": u(500)}, "sizes": {}, "count": 1, "size": 1},
        }, "warnings": []}

    def test_group_and_analyze(self):
        groups = I.group_slow_ops(self.rows())
        self.assertEqual(groups[0]["shape_key"], "scan")           # sorted by total time
        self.assertEqual(groups[0]["avg_targeting"], 10000.0)
        self.assertEqual(groups[1]["avg_targeting"], 1.0)
        self.assertEqual(I.needs_index(groups[1]), [])
        rep = I.analyze(groups, self.inventory())
        self.assertEqual(len(rep["suggestions"]), 1)
        sug = rep["suggestions"][0]
        self.assertEqual(sug["key_js"], "{ status: 1, day: 1 }")
        self.assertIn("createIndex", sug["command"])
        self.assertEqual(sug["impact"], "High")
        unused = {u["name"]: u for u in rep["unused"]}
        self.assertEqual(set(unused), {"legacy_1", "tax_1", "exp_1", "fresh_1"})
        self.assertEqual(unused["legacy_1"]["keep_reason"], "")
        self.assertIn("unique", unused["tax_1"]["keep_reason"])
        self.assertIn("TTL", unused["exp_1"]["keep_reason"])
        self.assertTrue(unused["fresh_1"]["low_confidence"])
        self.assertIn("hideIndex", unused["legacy_1"]["hide"])
        self.assertEqual([(r["name"], r["covered_by"]) for r in rep["redundant"]], [("status_1", "status_1_owner_1")])
        self.assertEqual(rep["summary"]["unused"], 2)

    def test_existing_index_suppresses_suggestion(self):
        inv = self.inventory()
        inv["collections"]["shop.orders"]["indexes"].append({"name": "status_1_day_1", "key": [("status", 1), ("day", 1)]})
        rep = I.analyze(I.group_slow_ops(self.rows()), inv)
        self.assertEqual(rep["suggestions"], [])
        self.assertEqual(rep["covered"][0]["index"], "status_1_day_1")


if __name__ == "__main__":
    unittest.main()
