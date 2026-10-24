"""Alerting engine: evaluates rules against the latest metrics and sends notifications."""

from __future__ import annotations

import json
import logging
import smtplib
import ssl
import urllib.request
from email.message import EmailMessage
from typing import Any

from .config import Settings
from .crypto import SecretBox
from .db import Database, now
from .metrics import CATALOG

log = logging.getLogger("mongopilot.alerts")

OPERATORS = {
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
}
SCOPE_PREFIX = {"proc": "proc:", "dep": "dep:", "srv": "srv:"}

DEFAULT_RULES = [
    # name, metric, operator, threshold, duration (s), severity
    ("Process unreachable", "up", "<", 1, 60, "critical"),
    ("Replica set has no primary", "has_primary", "<", 1, 30, "critical"),
    ("Member in unhealthy state", "member_state_ok", "<", 1, 120, "critical"),
    ("High replication lag", "repl_lag_s", ">", 10, 120, "warning"),
    ("Low replication headroom", "repl_headroom_h", "<", 12, 600, "warning"),
    ("Oplog window below 24 hours", "oplog_window_h", "<", 24, 600, "warning"),
    ("Connections almost exhausted", "connections_used_pct", ">", 80, 120, "warning"),
    ("Data volume almost full", "disk_used_pct", ">", 85, 300, "warning"),
    ("Poor query targeting", "query_targeting", ">", 1000, 300, "warning"),
    ("In-memory sorts (scan and order)", "scan_and_order_s", ">", 1, 300, "info"),
    ("High read latency", "lat_reads_ms", ">", 100, 300, "warning"),
    ("High write latency", "lat_writes_ms", ">", 100, 300, "warning"),
    ("Operations queued", "queue_total", ">", 10, 120, "warning"),
    ("High cache dirty ratio", "cache_dirty_pct", ">", 20, 300, "warning"),
    ("High process CPU", "cpu_process_pct", ">", 80, 600, "warning"),
    ("Operation running for over 5 minutes", "longest_op_s", ">", 300, 0, "info"),
    ("Primary changed (election)", "primary_changes", ">", 0, 0, "info"),
    ("Server unreachable over SSH", "host_up", "<", 1, 120, "warning"),
    ("High system CPU", "sys_cpu_pct", ">", 85, 600, "warning"),
    ("High disk latency", "disk_write_latency_ms", ">", 20, 300, "warning"),
    ("Disk space low", "disk_free_pct", "<", 10, 300, "critical"),
]

# Default rule names from the first (Italian) release, renamed on upgrade
LEGACY_RULE_NAMES = {
    "Processo non raggiungibile": "Process unreachable",
    "Replica set senza primario": "Replica set has no primary",
    "Membro in stato non sano": "Member in unhealthy state",
    "Replication lag elevato": "High replication lag",
    "Connessioni quasi esaurite": "Connections almost exhausted",
    "Disco dati quasi pieno": "Data volume almost full",
    "Finestra oplog ridotta": "Oplog window below 24 hours",
    "Cache dirty elevata": "High cache dirty ratio",
    "Operazione in corso da oltre 5 minuti": "Operation running for over 5 minutes",
}


def seed_default_rules(db: Database) -> None:
    """Create default rules on first start; on upgrades rename legacy rules and add new defaults."""
    if db.get_meta("default_rules_version") == "2":
        return  # already seeded: rules deleted by the user stay deleted
    for old, new in LEGACY_RULE_NAMES.items():
        db.execute("UPDATE alert_rules SET name = ? WHERE name = ?", (new, old))
    existing_metrics = {r["metric"] for r in db.query("SELECT metric FROM alert_rules")}
    for name, metric, op, thr, dur, sev in DEFAULT_RULES:
        if metric in existing_metrics:
            continue
        db.insert("alert_rules", {"name": name, "metric": metric, "operator": op, "threshold": thr,
                                  "duration_s": dur, "severity": sev, "channel_ids": "[]",
                                  "renotify_s": 3600 if sev == "critical" else 0, "enabled": 1,
                                  "created_at": now()})
    db.set_meta("default_rules_version", "2")


class AlertEngine:
    def __init__(self, db: Database, box: SecretBox, settings: Settings, notifier: "Notifier | None" = None):
        self.db = db
        self.box = box
        self.settings = settings
        self.notifier = notifier or Notifier(db, box, settings)

    def latest_values(self, max_age: int) -> dict[str, dict[str, Any]]:
        rows = self.db.query(
            """SELECT m.subject, m.ts, m.data FROM metrics m
               JOIN (SELECT subject, MAX(ts) AS mt FROM metrics WHERE ts >= ? GROUP BY subject) x
                 ON m.subject = x.subject AND m.ts = x.mt""",
            (now() - max_age,),
        )
        return {r["subject"]: json.loads(r["data"]) for r in rows}

    def subjects(self) -> dict[str, dict[str, Any]]:
        """Existing subjects with their label and deployment."""
        out: dict[str, dict[str, Any]] = {}
        for d in self.db.query("SELECT id, name FROM deployments WHERE paused = 0"):
            out[f"dep:{d['id']}"] = {"label": d["name"], "deployment_id": d["id"]}
        for p in self.db.query(
            """SELECT p.id, p.host, p.deployment_id, d.name FROM processes p
               JOIN deployments d ON d.id = p.deployment_id WHERE d.paused = 0"""
        ):
            out[f"proc:{p['id']}"] = {"label": f"{p['name']} · {p['host']}", "deployment_id": p["deployment_id"]}
        for s in self.db.query("SELECT id, name FROM servers WHERE ssh_key_enc IS NOT NULL"):
            out[f"srv:{s['id']}"] = {"label": f"server {s['name']}", "deployment_id": None}
        return out

    def evaluate(self) -> list[dict[str, Any]]:
        """Evaluate every rule. Returns the generated events (openings/resolutions)."""
        ts = now()
        events: list[dict[str, Any]] = []
        values = self.latest_values(max(120, self.settings.poll_interval * 3))
        subjects = self.subjects()
        rules = self.db.query("SELECT * FROM alert_rules WHERE enabled = 1")

        # Close alerts whose subject disappeared or whose rule was disabled/deleted
        active_rule_ids = {r["id"] for r in rules}
        for a in self.db.query("SELECT * FROM alerts WHERE status != 'resolved'"):
            if a["subject"] not in subjects or a["rule_id"] not in active_rule_ids:
                self._resolve(a, ts, notify=False)

        for rule in rules:
            scope = CATALOG.get(rule["metric"], ("", "", "proc", ""))[2]
            cmp = OPERATORS.get(rule["operator"])
            if cmp is None:
                continue
            for subject, meta in subjects.items():
                if not subject.startswith(SCOPE_PREFIX[scope]):
                    continue
                if rule["deployment_id"] and rule["deployment_id"] != meta["deployment_id"]:
                    continue
                sample = values.get(subject)
                if sample is None:
                    continue
                value = sample.get(rule["metric"])
                if value is None:
                    continue  # metric missing (e.g. process down): keep the current state
                firing = cmp(float(value), float(rule["threshold"]))
                ev = self._apply(rule, subject, meta, float(value), firing, ts)
                if ev:
                    events.append(ev)
        return events

    def _apply(self, rule, subject, meta, value, firing, ts) -> dict[str, Any] | None:
        open_alert = self.db.one(
            "SELECT * FROM alerts WHERE rule_id = ? AND subject = ? AND status != 'resolved'",
            (rule["id"], subject),
        )
        if firing:
            if open_alert:
                self.db.update("alerts", open_alert["id"], {"value": value, "updated_at": ts})
                renotify = rule["renotify_s"]
                if (open_alert["status"] == "open" and renotify
                        and ts - (open_alert["last_notified"] or 0) >= renotify):
                    self.notifier.send(rule, {**open_alert, "value": value}, "reminder")
                    self.db.update("alerts", open_alert["id"], {"last_notified": ts})
                return None
            pending = self.db.one("SELECT since FROM alert_pending WHERE rule_id = ? AND subject = ?",
                                  (rule["id"], subject))
            if not pending:
                self.db.execute("INSERT INTO alert_pending (rule_id, subject, since) VALUES (?, ?, ?)",
                                (rule["id"], subject, ts))
                since = ts
            else:
                since = pending["since"]
            if ts - since < rule["duration_s"]:
                return None
            self.db.execute("DELETE FROM alert_pending WHERE rule_id = ? AND subject = ?", (rule["id"], subject))
            label, unit = CATALOG.get(rule["metric"], (rule["metric"], "", "", ""))[:2]
            message = (f"{rule['name']}: {label} = {_fmt(value)}{unit and ' ' + unit} "
                       f"(threshold {rule['operator']} {_fmt(rule['threshold'])}) on {meta['label']}")
            alert = {"rule_id": rule["id"], "subject": subject, "subject_label": meta["label"],
                     "deployment_id": meta["deployment_id"], "status": "open", "severity": rule["severity"],
                     "message": message, "value": value, "opened_at": ts, "updated_at": ts, "last_notified": ts}
            alert["id"] = self.db.insert("alerts", alert)
            self.notifier.send(rule, alert, "open")
            return {"event": "open", **alert}
        self.db.execute("DELETE FROM alert_pending WHERE rule_id = ? AND subject = ?", (rule["id"], subject))
        if open_alert:
            self._resolve(open_alert, ts, rule=rule, value=value)
            return {"event": "resolved", **open_alert}
        return None

    def _resolve(self, alert, ts, notify=True, rule=None, value=None) -> None:
        self.db.update("alerts", alert["id"], {"status": "resolved", "resolved_at": ts, "updated_at": ts,
                                               **({"value": value} if value is not None else {})})
        if notify and rule is not None:
            self.notifier.send(rule, {**alert, "status": "resolved"}, "resolved")


def _fmt(v: float) -> str:
    return str(int(v)) if float(v).is_integer() else f"{v:.2f}"


class Notifier:
    """Sends notifications on the configured channels (email, Slack, generic webhook)."""

    SEVERITY_EMOJI = {"critical": "🔴", "warning": "🟠", "info": "🔵"}

    def __init__(self, db: Database, box: SecretBox, settings: Settings):
        self.db = db
        self.box = box
        self.settings = settings

    def channels_for(self, rule) -> list[dict[str, Any]]:
        ids = json.loads(rule["channel_ids"] or "[]")
        if ids:
            marks = ",".join("?" * len(ids))
            return self.db.query(f"SELECT * FROM channels WHERE id IN ({marks})", ids)
        return self.db.query("SELECT * FROM channels WHERE is_default = 1")

    def send(self, rule, alert, kind: str) -> None:
        for ch in self.channels_for(rule):
            try:
                self.deliver(ch, rule, alert, kind)
            except Exception as exc:
                log.error("Notification failed on channel %s: %s", ch["name"], exc)
                self.db.audit("system", "notification_failed", {"channel": ch["name"], "error": str(exc)})

    def deliver(self, ch, rule, alert, kind: str) -> None:
        cfg = self.box.decrypt_json(ch["config_enc"], {})
        title = {"open": "ALERT", "reminder": "ALERT STILL OPEN", "resolved": "RESOLVED", "test": "TEST"}[kind]
        icon = "✅" if kind == "resolved" else self.SEVERITY_EMOJI.get(alert.get("severity"), "")
        text = f"{icon} [{title}] {alert['message']}"
        link = f"{self.settings.public_url.rstrip('/')}/#/alerts"
        if ch["type"] == "slack":
            _post_json(cfg["webhook_url"], {"text": f"{text}\n<{link}|Open MongoPilot>"})
        elif ch["type"] == "webhook":
            payload = {"kind": kind, "alert": {k: alert.get(k) for k in
                       ("id", "status", "severity", "message", "value", "subject_label", "deployment_id",
                        "opened_at")}, "rule": {"id": rule["id"], "name": rule["name"], "metric": rule["metric"]},
                       "url": link}
            _post_json(cfg["url"], payload, cfg.get("headers") or {})
        elif ch["type"] == "email":
            self._email(cfg["to"], f"[MongoPilot] {title}: {rule['name']}", f"{text}\n\n{link}\n")
        else:
            raise ValueError(f"Unknown channel type: {ch['type']}")

    def _email(self, to: str | list[str], subject: str, body: str) -> None:
        s = self.settings
        if not s.smtp_host:
            raise RuntimeError("SMTP is not configured (MP_SMTP_HOST)")
        recipients = [to] if isinstance(to, str) else to
        msg = EmailMessage()
        msg["From"] = s.smtp_from
        msg["To"] = ", ".join(recipients)
        msg["Subject"] = subject
        msg.set_content(body)
        with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=15) as smtp:
            if s.smtp_starttls:
                smtp.starttls(context=ssl.create_default_context())
            if s.smtp_user:
                smtp.login(s.smtp_user, s.smtp_password or "")
            smtp.send_message(msg)


def _post_json(url: str, payload: Any, headers: dict[str, str] | None = None) -> None:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=10) as resp:
        if resp.status >= 300:
            raise RuntimeError(f"HTTP {resp.status}")
