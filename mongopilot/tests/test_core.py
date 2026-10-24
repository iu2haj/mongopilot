"""Tests for metrics, monitoring, alerting and automation against the simulated world."""

from __future__ import annotations

import asyncio
import datetime as dt
import tempfile
import time
import unittest
from pathlib import Path

from mongopilot import hostmetrics
from mongopilot import metrics as M
from mongopilot.app import Services
from mongopilot.automation import templates as T
from mongopilot.config import Settings
from mongopilot.demo import DemoNotifier, DemoOps, DemoProbe, DemoRunner, DemoWorld


def make_services(world: DemoWorld | None = None) -> tuple[Services, DemoWorld, DemoNotifier, DemoRunner]:
    tmp = Path(tempfile.mkdtemp())
    world = world or DemoWorld(speed=0.01)
    notifier = DemoNotifier()
    runner = DemoRunner(world)
    settings = Settings(data_dir=tmp, admin_password="test", secret_key="k", poll_interval=1,
                        disable_background=True, demo=False)
    s = Services(settings, probe=DemoProbe(world), runner=runner, ops=DemoOps(world), notifier=notifier)
    s.jobs.speed = 0.01
    return s, world, notifier, runner


class MetricsTest(unittest.TestCase):
    def test_gauges(self):
        ss = {"connections": {"current": 80, "available": 20, "active": 7}, "uptime": 100,
              "mem": {"resident": 4000, "virtual": 6000},
              "wiredTiger": {"cache": {"maximum bytes configured": 1000, "bytes currently in the cache": 500,
                                       "tracked dirty bytes in the cache": 100}},
              "globalLock": {"currentQueue": {"readers": 2, "writers": 1, "total": 3},
                             "activeClients": {"readers": 4, "writers": 1}},
              "queues": {"execution": {"read": {"available": 120}, "write": {"available": 110}}},
              "metrics": {"cursor": {"open": {"total": 9, "noTimeout": 1}}},
              "transactions": {"currentOpen": 2, "currentActive": 1}, "flowControl": {"isLagged": True}}
        g = M.gauges(ss, {"mem_mb": 16000})
        self.assertEqual(g["connections_used_pct"], 80.0)
        self.assertEqual(g["connections_active"], 7)
        self.assertEqual(g["cache_used_pct"], 50.0)
        self.assertEqual(g["cache_dirty_pct"], 10.0)
        self.assertEqual(g["tickets_read_avail"], 120)
        self.assertEqual(g["queue_total"], 3)
        self.assertEqual(g["cursors_open"], 9)
        self.assertEqual(g["mem_resident_pct"], 25.0)
        self.assertEqual(g["flow_control_lagged"], 1.0)

    def test_rates_latency_targeting_cpu(self):
        def ss(q, ret, scanned, lat, lat_ops, cpu_us):
            return {"opcounters": {"insert": 0, "query": q, "update": 0, "delete": 0, "getmore": 0, "command": 0},
                    "metrics": {"document": {"returned": ret}, "queryExecutor": {"scanned": scanned // 2,
                                                                                 "scannedObjects": scanned},
                                "operation": {"scanAndOrder": 0, "writeConflicts": 0}},
                    "opLatencies": {"reads": {"latency": lat, "ops": lat_ops}},
                    "extra_info": {"user_time_us": cpu_us, "system_time_us": 0}}
        c0 = M.counters(ss(1000, 500, 1000, 1_000_000, 100, 0))
        c1 = M.counters(ss(1600, 700, 41000, 6_000_000, 600, 20_000_000))
        r = M.rates((0, c0), (10, c1), cores=4)
        self.assertEqual(r["ops_query"], 60.0)
        self.assertEqual(r["ops_total"], 60.0)
        self.assertEqual(r["docs_returned_s"], 20.0)
        self.assertEqual(r["lat_reads_ms"], 10.0)            # 5 s of latency over 500 ops
        self.assertEqual(r["query_targeting"], 200.0)       # 40000 scanned / 200 returned
        self.assertEqual(r["cpu_process_pct"], 50.0)        # 20 CPU-seconds in 10 s on 4 cores
        self.assertEqual(r["scan_and_order_s"], 0.0)
        # after a restart (uptime went down) no rates
        self.assertEqual(M.rates((0, c0), (10, c1), 100, 5), {})

    def test_oplog_and_storage(self):
        o = M.oplog_values({"oplog_window_h": 10, "oplog_used_mb": 20480})
        self.assertEqual(o["oplog_gb_per_hour"], 2.0)
        st = M.storage_values({"data_bytes": 2 * 1024 ** 3, "storage_bytes": 1024 ** 3, "index_bytes": 0,
                               "collections": 4, "indexes": 9})
        self.assertEqual(st["data_size_gb"], 2.0)
        self.assertEqual(st["indexes"], 9.0)

    def test_replica_lag(self):
        now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
        st = {"set": "rs", "members": [
            {"name": "a:1", "stateStr": "PRIMARY", "health": 1, "optimeDate": now},
            {"name": "b:1", "stateStr": "SECONDARY", "health": 1, "optimeDate": now - dt.timedelta(seconds=42)},
            {"name": "c:1", "stateStr": "(not reachable/healthy)", "health": 0, "optimeDate": now}]}
        r = M.replica_metrics(st)
        self.assertEqual(r["has_primary"], 1)
        self.assertEqual(r["members_unhealthy"], 1)
        self.assertAlmostEqual(r["members"]["b:1"]["lag"], 42, delta=0.01)
        self.assertEqual(r["members"]["c:1"]["state_ok"], 0)

    def test_host_metrics(self):
        def snap(cpu, idle, iow, reads, rms, writes, wms, io):
            return "\n".join([
                "@@stat", f"cpu  {cpu - idle - iow} 0 0 {idle} {iow} 0 0 0 0 0", "@@cores", "4",
                "@@load", "2.00 1.5 1.0 1/100 99", "@@mem", "MemTotal: 1000 kB", "MemAvailable: 250 kB",
                "SwapTotal: 0 kB", "SwapFree: 0 kB", "@@disk",
                f"   8 0 sda {reads} 0 0 {rms} {writes} 0 0 {wms} 0 {io} 0 0 0 0 0",
                "   8 1 sda1 1 0 0 1 1 0 0 1 0 1 0", "   7 0 loop0 99 0 0 99 0 0 0 0 0 0 0",
                "@@df", "/dev/sda1 1000 900 100 90% /", "/dev/sdb1 1000 500 500 50% /data", "@@end"])
        s0 = hostmetrics.parse(snap(1000, 600, 50, 100, 100, 200, 400, 1000))
        s1 = hostmetrics.parse(snap(2000, 1100, 150, 150, 200, 300, 2400, 6000))
        self.assertEqual(list(s1["disks"]), ["sda"])
        v = hostmetrics.compute((0, s0), (10, s1))
        self.assertEqual(v["sys_cpu_pct"], 40.0)          # 1000 jiffies: 500 idle, 100 iowait
        self.assertEqual(v["sys_iowait_pct"], 10.0)
        self.assertEqual(v["disk_read_iops"], 5.0)
        self.assertEqual(v["disk_write_iops"], 10.0)
        self.assertEqual(v["disk_read_latency_ms"], 2.0)
        self.assertEqual(v["disk_write_latency_ms"], 20.0)
        self.assertEqual(v["disk_util_pct"], 50.0)
        self.assertEqual(v["disk_free_pct"], 10.0)        # lowest volume
        self.assertEqual(v["sys_mem_used_pct"], 75.0)
        self.assertEqual(v["load_per_core"], 0.5)

    def test_upgrade_rules(self):
        self.assertTrue(T.check_upgrade_path("7.0.14", "8.0.4", "7.0"))
        self.assertEqual(T.check_upgrade_path("7.0.12", "7.0.14", "7.0"), [])
        with self.assertRaisesRegex(ValueError, "can only be upgraded to: 7.0"):
            T.check_upgrade_path("6.0.16", "8.0.4", "6.0")
        # MongoDB 9.0: direct from 8.0 or 8.3, but 8.2 must go through 8.3
        T.check_upgrade_path("8.0.12", "9.0.1", "8.0")
        T.check_upgrade_path("8.3.1", "9.0.1", "8.3")
        with self.assertRaisesRegex(ValueError, "8.3"):
            T.check_upgrade_path("8.2.1", "9.0.1", "8.2")
        self.assertEqual(T.fcv_options("9.0")[0], "9.0")
        self.assertIn("8.0", T.fcv_options("9.0"))
        with self.assertRaisesRegex(ValueError, "featureCompatibilityVersion"):
            T.check_upgrade_path("7.0.14", "8.0.4", "6.0")
        with self.assertRaisesRegex(ValueError, "Downgrades"):
            T.check_upgrade_path("8.0.4", "7.0.14", "8.0")

    def test_download_targets(self):
        self.assertEqual(T.download_targets({"os_id": "ubuntu", "os_version": "22.04"}), ["ubuntu2204"])
        self.assertIn("rhel93", T.download_targets({"os_id": "rocky", "os_version": "9.4"}))
        urls = T.candidate_urls({"os_id": "debian", "os_version": "12", "arch": "aarch64"}, "8.0.4")
        self.assertEqual(urls, ["https://fastdl.mongodb.org/linux/mongodb-linux-aarch64-debian12-8.0.4.tgz"])

    def test_config_rendering(self):
        paths = T.member_paths("orders-prod", 27017)
        managed = {"kind": "replicaset", "rs_name": "orders", "dep_slug": "orders-prod", "cache_gb": 4,
                   "bind_ip": "0.0.0.0", "members": []}
        member = {"host": "db1:27017", "port": 27017, "role": "replica", "rs_name": "orders", "paths": paths}
        conf = T.render_config(T.build_config(managed, member))
        self.assertIn("replSetName: orders", conf)
        self.assertIn("authorization: enabled", conf)
        self.assertIn("cacheSizeGB: 4", conf)
        unit = T.systemd_unit("Orders Prod", 27017, "7.0.14", paths)
        self.assertIn("/opt/mongopilot/mongodb/7.0.14/bin/mongod --config", unit)
        self.assertEqual(T.slug("Orders Prod!"), "orders-prod")


class MonitorAlertTest(unittest.TestCase):
    def setUp(self):
        self.s, self.world, self.notifier, self.runner = make_services()
        hosts = ["db1:27017", "db2:27017", "db3:27017"]
        self.cluster = self.world.add_replicaset("t", "rs0", hosts, "7.0.14")
        self.dep = self.s.db.insert("deployments", {"name": "t", "managed": 0, "created_at": 0,
                                                    "uri_enc": self.s.box.encrypt("mongodb://u:p@db1:27017/?replicaSet=rs0")})
        self.s.db.insert("channels", {"name": "c", "type": "webhook", "config_enc": self.s.box.encrypt_json({"url": "x"}),
                                      "is_default": 1, "created_at": 0})

    def test_default_rules_are_english_and_seeded_once(self):
        names = {r["name"] for r in self.s.db.query("SELECT name FROM alert_rules")}
        self.assertIn("Process unreachable", names)
        self.assertIn("Poor query targeting", names)
        self.s.db.execute("DELETE FROM alert_rules WHERE name = 'Poor query targeting'")
        from mongopilot.alerts import seed_default_rules
        seed_default_rules(self.s.db)
        self.assertIsNone(self.s.db.one("SELECT id FROM alert_rules WHERE name = 'Poor query targeting'"))

    def test_poll_discovers_and_rates(self):
        st = self.s.collector.poll_deployment(self.dep)
        self.assertEqual(st["kind"], "replicaset")
        procs = self.s.db.query("SELECT * FROM processes ORDER BY host")
        self.assertEqual([p["host"] for p in procs], ["db1:27017", "db2:27017", "db3:27017"])
        self.assertEqual(sum(p["up"] for p in procs), 3)
        time.sleep(1.1)
        self.s.collector.poll_deployment(self.dep)
        latest = self.s.alerts.latest_values(60)
        primary = next(p for p in procs if p["role"] == "PRIMARY")
        secondary = next(p for p in procs if p["role"] == "SECONDARY")
        pv = latest[f"proc:{primary['id']}"]
        for key in ("ops_total", "lat_reads_ms", "query_targeting", "cpu_process_pct", "oplog_gb_per_hour",
                    "data_size_gb", "cursors_open", "docs_returned_s"):
            self.assertIn(key, pv)
        self.assertGreater(pv["ops_total"], 0)
        self.assertIn("repl_headroom_h", latest[f"proc:{secondary['id']}"])
        self.assertEqual(latest[f"dep:{self.dep}"]["has_primary"], 1)

    def test_primary_change_counted(self):
        self.s.collector.poll_deployment(self.dep)
        DemoOps(self.world).step_down("mongodb://db1:27017")
        self.s.collector.poll_deployment(self.dep)
        self.assertEqual(self.s.alerts.latest_values(60)[f"dep:{self.dep}"]["primary_changes"], 1)

    def test_host_down_alert_lifecycle(self):
        self.s.collector.poll_deployment(self.dep)
        rule = self.s.db.one("SELECT * FROM alert_rules WHERE metric = 'up'")
        self.assertEqual(rule["duration_s"], 60)
        up_events = lambda evs: [e for e in evs if e["rule_id"] == rule["id"]]  # noqa: E731
        victim = self.cluster.sets["rs0"][2]
        victim.running = False
        self.s.collector.poll_deployment(self.dep)
        self.assertEqual(up_events(self.s.alerts.evaluate()), [])   # pending (duration 60 s)
        self.s.db.execute("UPDATE alert_pending SET since = since - 120")
        opened = [e for e in up_events(self.s.alerts.evaluate()) if e["event"] == "open"]
        self.assertTrue(opened)
        self.assertIn(victim.host, opened[0]["subject_label"])
        self.assertIn("Process unreachable", opened[0]["message"])
        self.assertTrue(any(k == "open" for k, _ in self.notifier.sent))
        victim.running = True
        self.s.collector.poll_deployment(self.dep)
        self.assertTrue(any(e["event"] == "resolved" for e in up_events(self.s.alerts.evaluate())))

    def test_unreachable_deployment(self):
        self.s.collector.poll_deployment(self.dep)
        self.world.clusters.clear()
        self.assertIsNone(self.s.collector.poll_deployment(self.dep))
        d = self.s.db.one("SELECT last_error FROM deployments WHERE id = ?", (self.dep,))
        self.assertIn("No servers found", d["last_error"])
        self.assertEqual(self.s.db.one("SELECT SUM(up) n FROM processes")["n"], 0)

    def test_metric_series(self):
        self.s.collector.poll_deployment(self.dep)
        p = self.s.db.one("SELECT id FROM processes LIMIT 1")
        series = self.s.db.metric_series(f"proc:{p['id']}", 0, ["up"])
        self.assertEqual(series["series"]["up"], [1])

    def test_host_collector_and_alert_subjects(self):
        sid = self.s.db.insert("servers", {"name": "vm1", "hostname": "db1", "ssh_port": 22, "ssh_user": "vagrant",
                                           "kind": "vagrant", "ssh_key_enc": self.s.box.encrypt("KEY"),
                                           "created_at": 0})

        async def go():
            await self.s.hosts.poll_all()
            await asyncio.sleep(1.05)
            await self.s.hosts.poll_all()
        asyncio.run(go())
        v = self.s.alerts.latest_values(60)[f"srv:{sid}"]
        self.assertEqual(v["host_up"], 1.0)
        self.assertIn("sys_cpu_pct", v)
        self.assertIn("disk_write_latency_ms", v)
        self.assertIn(f"srv:{sid}", self.s.alerts.subjects())


class AutomationTest(unittest.TestCase):
    def setUp(self):
        self.s, self.world, _, self.runner = make_services()
        self.servers = [self.s.db.insert("servers", {"name": f"vm{i}", "hostname": f"192.168.56.1{i}", "ssh_port": 22,
                                                     "ssh_user": "vagrant", "kind": "vagrant",
                                                     "ssh_key_enc": self.s.box.encrypt("KEY"), "created_at": 0})
                        for i in range(3)]

    def run_job(self, kind, params, dep_id, title="t"):
        async def go():
            jid = self.s.jobs.submit(kind, title, params, dep_id, "test")
            await self.s.jobs.wait_all()
            return jid
        jid = asyncio.run(go())
        job = self.s.db.one("SELECT * FROM jobs WHERE id = ?", (jid,))
        logs = "\n".join(r["message"] for r in self.s.db.query("SELECT message FROM job_logs WHERE job_id = ?", (jid,)))
        return job, logs

    def create_rs(self):
        hosts = ",".join(f"192.168.56.1{i}:27017" for i in range(3))
        params = {"name": "Orders", "rs_name": "orders", "version": "7.0.14", "admin_user": "admin",
                  "admin_password": "a-long-password", "monitor_password": "mon",
                  "members": [{"server_id": sid, "port": 27017} for sid in self.servers]}
        dep = self.s.db.insert("deployments", {"name": "Orders", "managed": 1, "paused": 1, "created_at": 0,
                                               "uri_enc": self.s.box.encrypt(
                                                   f"mongodb://mongopilot-monitor:mon@{hosts}/?replicaSet=orders")})
        params["deployment_id"] = dep
        job, logs = self.run_job("create_replicaset", params, dep)
        self.assertEqual(job["status"], "success", logs)
        return dep, logs

    def test_create_replicaset(self):
        dep, logs = self.create_rs()
        self.assertIn("primary elected", logs)
        d = self.s.db.one("SELECT * FROM deployments WHERE id = ?", (dep,))
        self.assertEqual(d["paused"], 0)
        managed = self.s.box.decrypt_json(d["managed_enc"])
        self.assertEqual(len(managed["members"]), 3)
        cluster = self.world.find("192.168.56.10:27017")[0]
        self.assertEqual(set(cluster.users), {"admin", "mongopilot-monitor"})
        self.assertIn("mongopilotIndexInspector", cluster.roles)
        scripts = [h[1] for h in self.runner.history]
        self.assertTrue(any("useradd" in sc for sc in scripts))
        configs = [h[2] for h in self.runner.history if h[2] and "replSetName" in h[2]]
        self.assertEqual(len(configs), 3)
        st = self.s.collector.poll_deployment(dep)
        self.assertEqual(st["replica_sets"]["orders"]["has_primary"], 1)

    def test_forwarded_port_without_private_ip_is_refused(self):
        self.s.db.update("servers", self.servers[0], {"hostname": "host.docker.internal", "ssh_port": 2222})
        params = {"name": "X", "rs_name": "x", "version": "7.0.14", "admin_user": "admin",
                  "admin_password": "a-long-password", "monitor_password": "mon", "deployment_id": None,
                  "members": [{"server_id": sid, "port": 27017} for sid in self.servers]}
        job, _ = self.run_job("create_replicaset", params, None)
        self.assertEqual(job["status"], "failed")
        self.assertIn("private network IP", job["error"])

    def test_rolling_upgrade_and_fcv(self):
        dep, _ = self.create_rs()
        cluster = self.world.find("192.168.56.10:27017")[0]
        old_primary = next(m.host for m in cluster.sets["orders"] if m.state == "PRIMARY")
        self.runner.history.clear()
        job, logs = self.run_job("rolling_upgrade", {"deployment_id": dep, "version": "8.0.4"}, dep)
        self.assertEqual(job["status"], "success", logs)
        self.assertEqual({m.version for m in cluster.sets["orders"]}, {"8.0.4"})
        self.assertIn(f"Stepping down primary {old_primary}", logs)
        restarts = [h[0] for h in self.runner.history if "systemctl restart" in h[1]]
        self.assertEqual(restarts[-1], old_primary.split(":")[0], "the primary must be restarted last")
        managed = self.s.box.decrypt_json(self.s.db.one("SELECT managed_enc FROM deployments WHERE id=?", (dep,))["managed_enc"])
        self.assertEqual(managed["version"], "8.0.4")
        job, logs = self.run_job("set_fcv", {"deployment_id": dep, "fcv": "8.0"}, dep)
        self.assertEqual(job["status"], "success", logs)
        self.assertEqual(cluster.fcv, "8.0")
        job, logs = self.run_job("rolling_upgrade", {"deployment_id": dep, "version": "7.0.20"}, dep)
        self.assertEqual(job["status"], "failed")
        self.assertIn("Downgrades", job["error"])

    def test_rolling_restart_refuses_unhealthy(self):
        dep, _ = self.create_rs()
        cluster = self.world.find("192.168.56.10:27017")[0]
        cluster.sets["orders"][1].running = False
        job, _ = self.run_job("rolling_restart", {"deployment_id": dep}, dep)
        self.assertEqual(job["status"], "failed")
        self.assertIn("Unhealthy members", job["error"])

    def test_validation(self):
        from mongopilot.automation.jobs import validate_replicaset_params
        base = {"version": "7.0.14", "admin_user": "a", "admin_password": "x" * 12}
        with self.assertRaisesRegex(ValueError, "arbiter"):
            validate_replicaset_params(self.s.db, {**base, "members": [{"server_id": self.servers[0], "port": 1,
                                                                       "arbiter": True}]})
        with self.assertRaisesRegex(ValueError, "same server and port"):
            validate_replicaset_params(self.s.db, {**base, "members": [{"server_id": self.servers[0], "port": 1}] * 2})
        warns = validate_replicaset_params(self.s.db, {**base, "members": [
            {"server_id": self.servers[0], "port": 1}, {"server_id": self.servers[1], "port": 1}]})
        self.assertTrue(any("odd number" in w for w in warns))


if __name__ == "__main__":
    unittest.main()
