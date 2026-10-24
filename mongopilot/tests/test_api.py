"""HTTP API tests with the app in demo mode (a real uvicorn server in a thread)."""

from __future__ import annotations

import base64
import json
import socket
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

import uvicorn
from cryptography.hazmat.primitives import serialization as S
from cryptography.hazmat.primitives.asymmetric import ed25519

from mongopilot.app import create_app
from mongopilot.config import Settings

KEY = ed25519.Ed25519PrivateKey.generate().private_bytes(S.Encoding.PEM, S.PrivateFormat.OpenSSH,
                                                         S.NoEncryption()).decode()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        settings = Settings(data_dir=Path(tempfile.mkdtemp()), admin_password="pw", secret_key="k", demo=True,
                            disable_background=True, mongodb_release_index="http://127.0.0.1:9/none.json")
        cls.app = create_app(settings)
        cls.app.state.services.jobs.speed = 0.01
        cls.app.state.services.world.speed = 0.01
        cls.port = free_port()
        cls.server = uvicorn.Server(uvicorn.Config(cls.app, port=cls.port, log_level="warning"))
        cls.thread = threading.Thread(target=cls.server.run, daemon=True)
        cls.thread.start()
        while not cls.server.started:
            time.sleep(0.05)

    @classmethod
    def tearDownClass(cls):
        cls.server.should_exit = True
        cls.thread.join(5)

    def call(self, method, path, body=None, auth=("admin", "pw")):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", method=method,
                                     data=json.dumps(body).encode() if body is not None else None)
        req.add_header("Content-Type", "application/json")
        if auth:
            req.add_header("Authorization", "Basic " + base64.b64encode(":".join(auth).encode()).decode())
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw)
            except Exception:
                return e.code, raw.decode()

    def wait(self, path, done):
        for _ in range(200):
            _, data = self.call("GET", path)
            if done(data):
                return data
            time.sleep(0.1)
        self.fail(f"{path} did not finish")

    def dep_id(self, name):
        _, deps = self.call("GET", "/api/deployments")
        return next(d["id"] for d in deps if d["name"] == name)

    def test_auth(self):
        self.assertEqual(self.call("GET", "/api/overview", auth=None)[0], 401)
        self.assertEqual(self.call("GET", "/api/overview", auth=("admin", "no"))[0], 401)
        self.assertEqual(self.call("GET", "/healthz", auth=None)[0], 200)

    def test_overview_and_detail(self):
        code, o = self.call("GET", "/api/overview")
        self.assertEqual(code, 200)
        self.assertGreaterEqual(o["deployments"], 3)
        self.assertGreater(o["slow_queries_24h"], 0)
        _, d = self.call("GET", f"/api/deployments/{self.dep_id('catalog')}")
        self.assertEqual(d["topology"], "sharded")
        self.assertEqual(len(d["processes"]), 11)
        self.assertEqual(d["metrics"]["jumbo_chunks"], 2)
        code, series = self.call("GET", f"/api/metrics?subject=proc:{d['processes'][0]['id']}&range=3600")
        self.assertEqual(code, 200)
        self.assertGreater(len(series["ts"]), 10)
        _, orders = self.call("GET", f"/api/deployments/{self.dep_id('orders-prod')}")
        self.assertTrue(all(p["server_id"] for p in orders["processes"]), "processes map to registered servers")

    def test_catalog_groups(self):
        _, cat = self.call("GET", "/api/catalog")
        groups = {m["group"] for m in cat["metrics"]}
        self.assertTrue({"Latency", "Query efficiency", "Host", "Replication"} <= groups)
        self.assertGreaterEqual(len(cat["metrics"]), 80)

    def test_servers_keys_and_vagrant_import(self):
        code, err = self.call("POST", "/api/servers", {"name": "x1", "hostname": "h", "ssh_user": "vagrant",
                                                       "ssh_key": "-----BEGIN CERTIFICATE-----\nA\n-----END CERTIFICATE-----"})
        self.assertEqual(code, 400)
        self.assertIn("certificate", err["error"])
        code, err = self.call("POST", "/api/servers", {"name": "x2", "hostname": "h", "ssh_user": "vagrant"})
        self.assertEqual(code, 400)
        self.assertIn("private key", err["error"])
        code, srv = self.call("POST", "/api/servers", {"name": "x3", "hostname": "192.168.56.30", "ssh_user": "vagrant",
                                                       "ssh_key": KEY})
        self.assertEqual(code, 201)
        _, got = self.call("GET", f"/api/servers/{srv['id']}")
        self.assertEqual(got["kind"], "vagrant")
        self.assertNotIn("ssh_key_enc", got)
        code, parsed = self.call("POST", "/api/servers/parse-ssh-config",
                                 {"text": "Host a\n  HostName 127.0.0.1\n  Port 2200\n  User vagrant\n"})
        self.assertEqual(parsed["hosts"][0]["hostname"], "host.docker.internal")
        self.assertEqual(self.call("POST", "/api/servers/parse-ssh-config", {"text": "nothing"})[0], 400)

    def test_replicaset_flow(self):
        code, srv = self.call("POST", "/api/servers", {"name": "rs-host", "hostname": "192.168.56.40",
                                                       "ssh_user": "vagrant", "ssh_key": KEY})
        self.assertEqual(code, 201)
        code, err = self.call("POST", "/api/deployments/replicaset", {
            "name": "x", "version": "7.0.14", "admin_user": "a", "admin_password": "short",
            "members": [{"server_id": srv["id"], "port": 27017}]})
        self.assertEqual(code, 400)
        self.assertIn("12 characters", err["error"])
        code, err = self.call("POST", "/api/deployments/replicaset", {
            "name": "y", "version": "7.0.14", "admin_user": "a", "admin_password": "x" * 12,
            "members": [{"server_id": 1, "port": 27017}]})
        self.assertEqual(code, 400)
        self.assertIn("already used", err["error"])
        code, r = self.call("POST", "/api/deployments/replicaset", {
            "name": "new-rs", "version": "7.0.14", "admin_user": "a", "admin_password": "x" * 12,
            "members": [{"server_id": srv["id"], "port": p} for p in (27017, 27018, 27019)]})
        self.assertEqual(code, 202, r)
        self.assertTrue(any("same server" in w for w in r["warnings"]))
        job = self.wait(f"/api/jobs/{r['job_id']}", lambda j: j["status"] not in ("queued", "running"))
        self.assertEqual(job["status"], "success", job.get("error"))
        _, d = self.call("GET", f"/api/deployments/{r['id']}")
        self.assertEqual(d["paused"], 0)
        self.assertEqual(len(d["managed_info"]["members"]), 3)

    def test_query_performance(self):
        dep = self.dep_id("orders-prod")
        code, slow = self.call("GET", f"/api/insights/{dep}/slow?range=86400")
        self.assertEqual(code, 200)
        self.assertGreater(slow["totals"]["operations"], 100)
        self.assertGreater(slow["totals"]["needing_index"], 0)
        top = slow["groups"][0]
        self.assertTrue(top["reasons"])
        self.assertIn("createIndex", top["suggestions"][0]["command"])
        self.assertFalse(slow["totals"]["checked_against_indexes"])
        code, _ = self.call("POST", f"/api/insights/{dep}/analyze", {"range": 86400})
        self.assertEqual(code, 202)
        rep = self.wait(f"/api/insights/{dep}/report", lambda r: r["report"]["status"] != "running")["report"]
        self.assertEqual(rep["status"], "done", rep.get("error"))
        data = rep["data"]
        self.assertGreater(data["summary"]["suggestions"], 0)
        self.assertIn("legacyRef_1", {u["name"] for u in data["unused"]})
        self.assertIn("customerId_1", {r["name"] for r in data["redundant"]})
        _, slow = self.call("GET", f"/api/insights/{dep}/slow?range=86400&ns=shop.customers")
        self.assertTrue(slow["totals"]["checked_against_indexes"])
        self.assertEqual({g["ns"] for g in slow["groups"]}, {"shop.customers"})
        or_group = next(g for g in slow["groups"] if "$or" in g["shape"]["filter"])
        existing = {s["key_js"]: s["existing"] for s in or_group["suggestions"]}
        self.assertEqual(existing["{ email: 1 }"], "email_1")
        self.assertIsNone(existing["{ phone: 1 }"])

    def test_rules_and_channels(self):
        code, r = self.call("POST", "/api/rules", {"name": "lag", "metric": "repl_lag_s", "operator": ">",
                                                   "threshold": 5, "duration_s": 0})
        self.assertEqual(code, 201)
        code, err = self.call("POST", "/api/rules", {"name": "x", "metric": "nope", "operator": ">", "threshold": 1})
        self.assertEqual(code, 400)
        self.assertIn("Unknown metric", err["error"])
        code, c = self.call("POST", "/api/channels", {"name": "hook", "type": "webhook",
                                                      "config": {"url": "https://example.invalid/a-long-hook-path"}})
        self.assertEqual(code, 201)
        _, chans = self.call("GET", "/api/channels")
        self.assertTrue(chans[0]["config"]["url"].endswith("…"))
        self.assertEqual(self.call("DELETE", f"/api/rules/{r['id']}")[0], 200)

    def test_unmanaged_actions_rejected(self):
        code, err = self.call("POST", f"/api/deployments/{self.dep_id('crm')}/actions", {"type": "rolling_restart"})
        self.assertEqual(code, 400)
        self.assertIn("only available for deployments created", err["error"])

    def test_tags_parameters_and_config(self):
        dep = self.dep_id("crm")
        code, err = self.call("PATCH", f"/api/deployments/{dep}", {"tags": {"cluster_type": "QA"}})
        self.assertEqual(code, 400)
        self.assertIn("Cluster type", err["error"])
        tags = {"organization": "Acme", "project": "CRM", "cluster_type": "Staging", "license_key": "K-1",
                "license_type": "At memory"}
        self.assertEqual(self.call("PATCH", f"/api/deployments/{dep}", {"tags": tags})[0], 200)
        _, d = self.call("GET", f"/api/deployments/{dep}")
        self.assertEqual(d["tags"], tags)
        code, params = self.call("GET", f"/api/deployments/{dep}/parameters")
        self.assertEqual(code, 200)
        self.assertFalse(params["managed"])
        self.assertTrue(any(p["name"] == "featureCompatibilityVersion" and p["read_only"] for p in params["parameters"]))
        code, err = self.call("POST", f"/api/deployments/{dep}/parameters", {"name": "logLevel", "value": 1, "hosts": []})
        self.assertEqual(code, 400)
        orders = self.dep_id("orders-prod")
        code, cfg = self.call("GET", f"/api/deployments/{orders}/config")
        self.assertEqual(code, 200)
        m = cfg["members"][0]
        code, err = self.call("PUT", f"/api/deployments/{orders}/config",
                              {"host": m["host"], "yaml": m["yaml"].replace("port: 27017", "port: 27999")})
        self.assertEqual(code, 400)
        self.assertIn("net.port", err["error"])
        code, err = self.call("PUT", f"/api/deployments/{orders}/config", {"host": m["host"], "yaml": "net: [unclosed"})
        self.assertEqual(code, 400)
        code, tls = self.call("GET", f"/api/deployments/{orders}/tls")
        self.assertEqual(tls["mode"], "disabled")
        code, err = self.call("POST", f"/api/deployments/{orders}/tls", {"mode": "requireTLS", "source": "keep"})
        self.assertEqual(code, 400)
        self.assertIn("Generate or upload", err["error"])

    def test_tools(self):
        dep = self.dep_id("crm")
        code, info = self.call("GET", f"/api/deployments/{dep}/tools")
        self.assertEqual(code, 200)
        self.assertEqual(info["default_auth"], "monitor")
        host = next(p["host"] for p in info["processes"] if p["role"] == "SECONDARY")
        arb = next(p["host"] for p in info["processes"] if p["role"] == "ARBITER")
        code, err = self.call("POST", f"/api/deployments/{dep}/namespaces", {"host": arb})
        self.assertEqual(code, 400)
        self.assertIn("Arbiters", err["error"])
        code, err = self.call("POST", f"/api/deployments/{dep}/namespaces", {"host": host, "auth": {"mode": "admin"}})
        self.assertEqual(code, 400)   # monitored deployment: no admin user
        code, ns = self.call("POST", f"/api/deployments/{dep}/namespaces", {"host": host})
        self.assertEqual(code, 200)
        self.assertIn("crm", [d["db"] for d in ns["databases"]])
        code, err = self.call("POST", f"/api/deployments/{dep}/schema", {"host": host, "db": "crm", "collection": "contacts"})
        self.assertEqual(code, 400)
        self.assertIn("not authorized", err["error"])
        auth = {"mode": "custom", "username": "alice", "password": "pw", "auth_source": "admin"}
        code, sc = self.call("POST", f"/api/deployments/{dep}/schema",
                             {"host": host, "auth": auth, "db": "crm", "collection": "contacts", "size": 300,
                              "filter": '{"stage": "lead"}'})
        self.assertEqual(code, 200, sc)
        self.assertEqual(sc["sampled"], 300)
        stage = next(f for f in sc["fields"] if f["name"] == "stage")
        self.assertEqual(stage["types"][0]["top_values"], [{"value": "lead", "count": 300}])

        code, r = self.call("POST", f"/api/deployments/{dep}/exports",
                            {"kind": "export", "host": host, "auth": auth, "description": "leads",
                             "params": {"db": "crm", "collection": "contacts", "format": "json", "limit": 5}})
        self.assertEqual(code, 202, r)
        row = self.wait(f"/api/exports/{r['id']}", lambda x: x["status"] != "running")
        self.assertEqual(row["status"], "success", row)
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/exports/{r['id']}/download")
        req.add_header("Authorization", "Basic " + base64.b64encode(b"admin:pw").decode())
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode()
            self.assertIn("attachment", resp.headers["content-disposition"])
        self.assertEqual(len(body.strip().splitlines()), 5)
        _, lst = self.call("GET", f"/api/deployments/{dep}/exports?kind=export")
        self.assertIn(r["id"], [e["id"] for e in lst["exports"]])
        self.assertEqual(self.call("DELETE", f"/api/exports/{r['id']}")[0], 200)
        self.assertEqual(self.call("GET", f"/api/exports/{r['id']}/download")[0], 404)
        code, err = self.call("POST", f"/api/deployments/{dep}/exports",
                              {"kind": "dump", "host": host, "auth": auth, "params": {"db": "crm", "query": "{}"}})
        self.assertEqual(code, 400)

        code, sh = self.call("POST", f"/api/deployments/{dep}/shell", {"host": host, "auth": auth, "rows": 30, "cols": 100})
        self.assertEqual(code, 200, sh)
        self.call("POST", f"/api/shell/{sh['id']}/input", {"data": "use crm\r"})
        _, out = self.call("GET", f"/api/shell/{sh['id']}/output?cursor=0&wait=2")
        self.assertIn("switched to db crm", out["data"])
        _, out2 = self.call("GET", f"/api/shell/{sh['id']}/output?cursor={out['cursor']}&wait=0.2")
        self.assertEqual(out2["data"], "")
        self.assertEqual(self.call("DELETE", f"/api/shell/{sh['id']}")[0], 200)
        self.assertEqual(self.call("GET", f"/api/shell/{sh['id']}/output")[0], 400)

    def test_restore_and_import(self):
        dep = self.dep_id("crm")
        _, info = self.call("GET", f"/api/deployments/{dep}/tools")
        self.assertTrue(info["available"]["mongorestore"] and info["available"]["mongoimport"])
        primary = next(p["host"] for p in info["processes"] if p["role"] == "PRIMARY")
        secondary = next(p["host"] for p in info["processes"] if p["role"] == "SECONDARY")
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/deployments/{dep}/uploads?name=items.json",
                                     data=b'{"a": 1}\n{"a": 2}\n', method="POST")
        req.add_header("Authorization", "Basic " + base64.b64encode(b"admin:pw").decode())
        with urllib.request.urlopen(req, timeout=10) as resp:
            up = json.loads(resp.read())
        _, src = self.call("GET", "/api/tool-sources?kind=import")
        self.assertIn(up["id"], [f["id"] for f in src["files"]])
        _, src = self.call("GET", "/api/tool-sources?kind=restore")
        self.assertNotIn(up["id"], [f["id"] for f in src["files"]])
        auth = {"mode": "custom", "username": "alice", "password": "pw"}
        params = {"source_id": up["id"], "db": "crm", "collection": "items"}
        code, err = self.call("POST", f"/api/deployments/{dep}/exports",
                              {"kind": "import", "host": secondary, "auth": auth, "params": params})
        self.assertEqual(code, 400)
        self.assertIn("primary", err["error"])
        code, r = self.call("POST", f"/api/deployments/{dep}/exports",
                            {"kind": "import", "host": primary, "auth": auth, "params": params})
        self.assertEqual(code, 202, r)
        row = self.wait(f"/api/exports/{r['id']}", lambda x: x["status"] != "running")
        self.assertEqual(row["status"], "success", row)
        self.assertIn("2 document(s) imported", row["log"])
        _, hist = self.call("GET", f"/api/deployments/{dep}/exports?kind=import")
        self.assertEqual(hist["exports"][0]["params"]["source_file"], row["params"]["source_file"])


if __name__ == "__main__":
    unittest.main()
