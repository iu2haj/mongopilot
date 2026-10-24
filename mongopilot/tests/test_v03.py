"""Tests for the v0.3 features: sharded clusters, MongoDB 9.0, TLS, configuration files, parameters and tags."""
from __future__ import annotations

import asyncio
import datetime as dt
import unittest

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from mongopilot.automation import templates as T
from mongopilot.automation import tlsgen
from mongopilot.automation.jobs import validate_sharded_params
from mongopilot.automation.mongo_ops import coerce_parameter, with_tls

from test_core import make_services


class TemplatesV03Test(unittest.TestCase):
    def test_platform_9_0(self):
        with self.assertRaisesRegex(ValueError, "Ubuntu 20"):
            T.check_platform({"os_id": "ubuntu", "os_version": "20.04", "os_name": "Ubuntu 20.04"}, "9.0.1")
        T.check_platform({"os_id": "ubuntu", "os_version": "20.04", "os_name": "Ubuntu 20.04"}, "8.0.4")
        T.check_platform({"os_id": "ubuntu", "os_version": "24.04", "os_name": "Ubuntu 24.04"}, "9.0.1")

    def test_config_editing_rules(self):
        managed = {"kind": "sharded", "config_rs": "c", "dep_slug": "x", "bind_ip": "0.0.0.0", "members": [
            {"host": "a:27019", "port": 27019, "role": "configsvr", "rs_name": "c"}]}
        mongos = {"host": "a:27017", "port": 27017, "role": "mongos", "paths": T.member_paths("x", 27017, binary="mongos")}
        cfg = T.build_config(managed, mongos)
        self.assertEqual(cfg["sharding"]["configDB"], "c/a:27019")
        self.assertNotIn("storage", cfg)
        self.assertNotIn("authorization", cfg["security"])
        new = T.parse_config(T.render_config(cfg) + "operationProfiling:\n  slowOpThresholdMs: 50\n")
        sets, deletes = T.config_changes(cfg, new)
        self.assertEqual(sets, {"operationProfiling.slowOpThresholdMs": 50})
        self.assertEqual(deletes, [])
        bad = T.parse_config(T.render_config(cfg).replace("27017", "27999"))
        with self.assertRaisesRegex(ValueError, "net.port"):
            T.check_protected(cfg, bad)
        merged = T.apply_changes(cfg, sets, deletes)
        self.assertEqual(merged["operationProfiling"]["slowOpThresholdMs"], 50)

    def test_coerce_and_tls_uri(self):
        self.assertIs(coerce_parameter(True, "false"), False)
        self.assertEqual(coerce_parameter(10, "25"), 25)
        self.assertEqual(coerce_parameter(1.5, "2.5"), 2.5)
        self.assertEqual(coerce_parameter({"a": 1}, '{"a": 2}'), {"a": 2})
        with self.assertRaises(ValueError):
            coerce_parameter(10, "ten")
        opts = {"enabled": True, "ca_file": "/x/ca.pem", "allow_invalid_hostnames": True}
        uri = with_tls("mongodb://u:p@h:1/?replicaSet=r", opts)
        self.assertIn("tls=true", uri)
        self.assertIn("tlsCAFile=", uri)
        self.assertNotIn("tls", with_tls(uri, None).split("?")[1])


class TlsGenTest(unittest.TestCase):
    def test_generate_and_inspect(self):
        ca, key = tlsgen.generate_ca("Orders")
        pem = tlsgen.issue_server_cert(ca, key, ["192.168.56.11", "mongo1"])
        info = tlsgen.inspect_server_pem(pem, ca, ["192.168.56.11"])
        self.assertEqual(info["missing_names"], [])
        self.assertIn("mongo1", info["names"])
        self.assertEqual(tlsgen.inspect_server_pem(pem, ca, ["10.0.0.9"])["missing_names"], ["10.0.0.9"])
        other_ca, _ = tlsgen.generate_ca("Other")
        with self.assertRaisesRegex(ValueError, "CA"):
            tlsgen.inspect_server_pem(pem, other_ca, [])
        with self.assertRaisesRegex(ValueError, "key"):
            tlsgen.inspect_server_pem(pem[:pem.index("-----BEGIN", 10)], ca, [])

    def test_expired_certificate_is_rejected(self):
        ca, key = tlsgen.generate_ca("Lab")
        ca_cert = x509.load_pem_x509_certificate(ca.encode())
        ca_key = serialization.load_pem_private_key(key.encode(), None)
        k = ec.generate_private_key(ec.SECP256R1())
        past = dt.datetime(2020, 1, 1, tzinfo=dt.timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "old")]))
                .issuer_name(ca_cert.subject).public_key(k.public_key()).serial_number(1)
                .not_valid_before(past).not_valid_after(past + dt.timedelta(days=1)).sign(ca_key, hashes.SHA256()))
        pem = cert.public_bytes(serialization.Encoding.PEM).decode() + k.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
        with self.assertRaisesRegex(ValueError, "expired"):
            tlsgen.inspect_server_pem(pem, ca, [])


class ShardedAutomationTest(unittest.TestCase):
    def setUp(self):
        self.s, self.world, _, self.runner = make_services()
        self.servers = [self.s.db.insert("servers", {"name": f"vm{i}", "hostname": f"192.168.56.2{i}", "ssh_port": 22,
                                                     "ssh_user": "vagrant", "kind": "vagrant",
                                                     "ssh_key_enc": self.s.box.encrypt("KEY"), "created_at": 0})
                        for i in range(3)]

    def run_job(self, kind, params, dep_id):
        async def go():
            jid = self.s.jobs.submit(kind, "t", params, dep_id, "test")
            await self.s.jobs.wait_all()
            return jid
        jid = asyncio.run(go())
        job = self.s.db.one("SELECT * FROM jobs WHERE id = ?", (jid,))
        logs = "\n".join(r["message"] for r in self.s.db.query("SELECT message FROM job_logs WHERE job_id = ?", (jid,)))
        return job, logs

    def params(self):
        rs = lambda port: [{"server_id": sid, "port": port} for sid in self.servers]  # noqa: E731
        return {"name": "Shop", "version": "8.0.4", "admin_user": "admin", "admin_password": "a-long-password",
                "monitor_password": "mon", "config": {"members": rs(27019)},
                "shards": [{"name": "sh0", "members": rs(27018)}, {"name": "sh1", "members": rs(27020)}],
                "mongos": [{"server_id": self.servers[0], "port": 27017}, {"server_id": self.servers[1], "port": 27017}]}

    def create(self):
        p = self.params()
        hosts = "192.168.56.20:27017,192.168.56.21:27017"
        dep = self.s.db.insert("deployments", {"name": "Shop", "managed": 1, "paused": 1, "created_at": 0, "topology": "sharded",
                                               "uri_enc": self.s.box.encrypt(f"mongodb://mongopilot-monitor:mon@{hosts}/")})
        p["deployment_id"] = dep
        job, logs = self.run_job("create_sharded", p, dep)
        self.assertEqual(job["status"], "success", logs)
        return dep, logs

    def managed(self, dep):
        return self.s.box.decrypt_json(self.s.db.one("SELECT managed_enc FROM deployments WHERE id=?", (dep,))["managed_enc"])

    def test_validation(self):
        p = self.params()
        p["config"]["members"][1]["arbiter"] = True
        with self.assertRaisesRegex(ValueError, "arbiter"):
            validate_sharded_params(self.s.db, p)
        p = self.params()
        p["shards"][1]["name"] = "sh0"
        with self.assertRaisesRegex(ValueError, "Duplicate shard"):
            validate_sharded_params(self.s.db, p)
        p = self.params()
        p["mongos"] = p["mongos"][:1]
        self.assertTrue(any("single point of failure" in w for w in validate_sharded_params(self.s.db, p)))

    def test_create_upgrade_tls_and_config(self):
        dep, logs = self.create()
        m = self.managed(dep)
        self.assertEqual(m["kind"], "sharded")
        self.assertEqual(len(m["members"]), 11)
        self.assertEqual({x["role"] for x in m["members"]}, {"configsvr", "shardsvr", "mongos"})
        st = self.s.collector.poll_deployment(dep)
        self.assertIsNotNone(st)
        procs = self.s.db.query("SELECT * FROM processes WHERE deployment_id = ?", (dep,))
        self.assertEqual(len(procs), 11)

        # rolling upgrade straight to 9.0: balancer paused, config servers first, mongos last
        self.runner.history.clear()
        job, logs = self.run_job("rolling_upgrade", {"deployment_id": dep, "version": "9.0.1"}, dep)
        self.assertEqual(job["status"], "success", logs)
        self.assertIn("balancer", logs.lower())
        restarted = [h[1] for h in self.runner.history if "systemctl restart" in h[1]]
        self.assertIn("27019", restarted[0])
        self.assertIn("27017", restarted[-1])
        self.assertEqual(self.managed(dep)["version"], "9.0.1")
        job, logs = self.run_job("set_fcv", {"deployment_id": dep, "fcv": "9.0"}, dep)
        self.assertEqual(job["status"], "success", logs)

        # configuration file: a rejected option never reaches the server
        m = self.managed(dep)
        target = next(x for x in m["members"] if x["role"] == "shardsvr")
        cfg = T.member_config(m, target)
        cfg["bogusOption"] = 1
        target["config"] = cfg
        self.s.jobs.save_managed(dep, m)
        job, logs = self.run_job("apply_config", {"deployment_id": dep, "hosts": [target["host"]], "restart": True}, dep)
        self.assertEqual(job["status"], "failed", logs)

        # TLS: generated CA, staged to requireTLS, MongoPilot switches its connection string
        ca, key = tlsgen.generate_ca("Shop")
        pems = {}
        for sid in self.servers:
            srv = self.s.db.one("SELECT * FROM servers WHERE id = ?", (sid,))
            pems[str(sid)] = tlsgen.issue_server_cert(ca, key, [srv["hostname"]])
        cfg.pop("bogusOption")
        target["config"] = cfg
        self.s.jobs.save_managed(dep, m)
        job, logs = self.run_job("configure_tls", {"deployment_id": dep, "mode": "requireTLS", "allow_invalid_hostnames": False,
                                                   "material": {"ca_pem": ca, "ca_key_pem": key, "generated": True,
                                                                "pems": pems}}, dep)
        self.assertEqual(job["status"], "success", logs)
        m = self.managed(dep)
        self.assertEqual(m["tls"]["mode"], "requireTLS")
        uri = self.s.box.decrypt(self.s.db.one("SELECT uri_enc FROM deployments WHERE id=?", (dep,))["uri_enc"])
        self.assertIn("tls=true", uri)
        conf = T.render_config(T.member_config(m, m["members"][0]))
        self.assertIn("certificateKeyFile", conf)
        job, logs = self.run_job("configure_tls", {"deployment_id": dep, "mode": "disabled"}, dep)
        self.assertEqual(job["status"], "success", logs)
        uri = self.s.box.decrypt(self.s.db.one("SELECT uri_enc FROM deployments WHERE id=?", (dep,))["uri_enc"])
        self.assertNotIn("tls=true", uri)


if __name__ == "__main__":
    unittest.main()
