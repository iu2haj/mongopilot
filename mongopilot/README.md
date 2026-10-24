# MongoPilot

A lightweight, self-hosted alternative to MongoDB Ops Manager for MongoDB **Community** deployments on Linux VMs
(including Vagrant VMs): monitoring, alerting, query performance troubleshooting with index recommendations, and
automation (replica set and sharded cluster creation, rolling restart, rolling upgrade, FCV, TLS, parameters and
configuration files). One container, no agent on the servers:
monitoring uses the MongoDB protocol, automation and hardware metrics use SSH.

## What's new in 0.5

- **Servers** moved in the menu, right after Overview.
- **mongorestore** in the Tools column: restore any mongodump archive kept by MongoPilot (from any deployment) or an
  uploaded `.archive` / `.archive.gz` file, with namespace include/exclude, rename (`--nsFrom`/`--nsTo`), `--drop`,
  `--dryRun`, `--oplogReplay`, `--noIndexRestore`, ordering, error and parallelism options.
- **mongoimport** in the Tools column: import JSON (lines or array), CSV or TSV files (optionally gzipped) uploaded
  from the browser or produced by mongoexport, with header line or field list, typed columns, insert / upsert /
  merge / delete modes, `--upsertFields`, `--drop` and error options.
- Both write data, so they only run against a primary, a mongos or a standalone (the page links to the right node).
  A confirmation shows the target; with `--drop` you type the deployment name. Each run is listed in a history with
  status, command (no password) and output. Uploaded files are listed too and can be downloaded or deleted.



## Feature overview

| Feature | MongoPilot 0.5 | Notes |
|---|---|---|
| Monitoring of replica sets, sharded clusters, standalones | ✅ | Automatic discovery of members, shards, config servers and mongos |
| Metrics and history charts | ✅ | ~90 metrics in 12 groups, 1 h to 7 days |
| Host hardware metrics | ✅ | Read from `/proc` over SSH for registered servers, no sudo |
| Slow query analysis | ✅ | From the mongod log (`getLog`), grouped by query shape, values redacted |
| Index advisor | ✅ | Indexes to create, unused and redundant indexes |
| Alerts with threshold and duration | ✅ | Email, Slack, generic webhook; acknowledge and reminders |
| Replica set creation on VMs | ✅ | Binaries, keyfile, auth, systemd, `rs.initiate`, users; hidden, delayed, arbiter members |
| Sharded cluster creation | ✅ | Config server replica set, shards, mongos, `addShard` |
| MongoDB versions | ✅ | Community 4.4 to 9.0 |
| TLS | ✅ | Generated private CA or your certificates, staged allowTLS → preferTLS → requireTLS |
| Parameters and configuration files | ✅ | `getParameter`/`setParameter`, YAML editor validated by MongoDB |
| Tags and filters | ✅ | Organization, project, cluster type, license key and type |
| mongosh, mongodump, mongorestore, mongoexport, mongoimport from the browser | ✅ | Per node; downloadable dumps and exports, uploads for restore and import |
| Schema analysis | ✅ | Compass-like field statistics on a sample |
| Rolling restart / rolling upgrade, FCV, process start/stop | ✅ | Secondaries first, step-down, primary last; upgrade path checks |
| Activity log | ✅ | |
| Scheduled backups and point-in-time restore | ❌ | On-demand mongodump only; restore with mongorestore |
| Adding/removing members or shards after creation | ❌ | Natural next step |
| Automation of deployments not created by MongoPilot | ❌ | They can be monitored |
| Multiple users and roles, LDAP/SSO | ❌ | One admin account (HTTP Basic) |

## Try it in one minute (demo, no servers needed)

```bash
cp .env.example .env          # Windows: copy .env.example .env
# edit .env: set MP_ADMIN_PASSWORD and MP_DEMO=true
docker compose up -d --build
```
Note: (example of file ".env")
```bash
# Copy this file to .env and adjust it.

# Web interface login (HTTP Basic). Use a long password.
MP_ADMIN_USER=admin
MP_ADMIN_PASSWORD=change-me-to-a-long-password

# Key that encrypts MongoDB credentials, SSH private keys and channel settings.
# Generate it once (e.g. `openssl rand -base64 48`) and DO NOT lose it:
# without the same key the stored secrets can no longer be read.
MP_SECRET_KEY=

# Address users open MongoPilot at (used in notification links)
MP_PUBLIC_URL=http://mongopilot.company.local:8080

# Monitoring
MP_POLL_INTERVAL=30
MP_RETENTION_DAYS=14
MP_SLOW_OP_SECONDS=5

# Email (optional)
MP_SMTP_HOST=
MP_SMTP_PORT=587
MP_SMTP_USER=
MP_SMTP_PASSWORD=
MP_SMTP_FROM=mongopilot@company.local
MP_SMTP_STARTTLS=true

# Isolated networks: internal mirror of the MongoDB tarballs (same file names as fastdl.mongodb.org/linux)
MP_MONGODB_MIRROR=

# Demo with simulated clusters: true to try the interface without real servers
MP_DEMO=false

# Tools pages (mongosh, mongodump, mongoexport, Schema Analyzer). The Docker image installs the tools;
# change these only to use other binaries.
# MP_MONGOSH=mongosh
# MP_MONGODUMP=mongodump
# MP_MONGOEXPORT=mongoexport
# Largest sample the Schema Analyzer may request
# MP_SCHEMA_MAX_SAMPLE=10000
# MP_MONGORESTORE=mongorestore
# MP_MONGOIMPORT=mongoimport
# Largest file that can be uploaded for mongorestore / mongoimport (MB)
# MP_MAX_UPLOAD_MB=20480
```
Open http://localhost:8080 and sign in with `admin` and the password from `MP_ADMIN_PASSWORD`. The demo has three
simulated clusters (a managed replica set with a realistic slow-query workload, a monitored replica set with an
almost full disk, a sharded cluster) and six simulated Vagrant VMs.

## Installation

1. `cp .env.example .env` and set at least `MP_ADMIN_PASSWORD` and `MP_SECRET_KEY` (`openssl rand -base64 48`).
   **Keep `MP_SECRET_KEY`**: it encrypts stored credentials and SSH keys.
2. `docker compose up -d --build`
3. If other people use it, put MongoPilot behind an HTTPS reverse proxy (nginx, Traefik): login uses HTTP Basic.

Data (SQLite, generated key, `known_hosts`) lives in the `mongopilot-data` volume. Back that volume up.

### Network

- MongoPilot must reach **every member** on its MongoDB port, not only the seed hosts.
- For automation and hardware metrics it must reach the servers over SSH.
- Servers download MongoDB from `fastdl.mongodb.org`; on isolated networks set `MP_MONGODB_MIRROR` (same file names)
  or give a tarball URL in the form.
- With Docker Desktop the container already reaches your computer's network. If names do not resolve, or your network
  uses 172.16–172.31 addresses, see the commented options in `docker-compose.yml` (`extra_hosts`, `dns`, `networks`).

## Servers deployed with Vagrant

1. Start the VMs. To try it, use the sample lab: `cd vagrant && vagrant up` (three Ubuntu 22.04 VMs,
   192.168.56.11–13, 2 GB RAM each, VirtualBox provider).
2. Run `vagrant ssh-config` in the folder with the Vagrantfile.
3. In MongoPilot open **Servers → Import Vagrant machines**, paste the output, then drop each machine's
   `private_key` file. The file is at `.vagrant/machines/<name>/<provider>/private_key`; the import shows the exact
   path for every machine.
4. Press **Check** on each server: MongoPilot verifies SSH, passwordless sudo (the `vagrant` user has it), the
   operating system and the MongoDB platform.

Things to know:

- **Use a private network IP.** Replica set members must reach each other, so each VM needs an address on a shared
  network (`config.vm.network "private_network", ip: "192.168.56.11"`). Put it in **MongoDB host name**.
  If `vagrant ssh-config` shows `HostName 127.0.0.1` (a forwarded port), MongoPilot uses `host.docker.internal` for
  SSH, but it refuses to create a replica set until a private IP is set.
- **Host keys.** Servers marked as *Vagrant VM* do not pin the SSH host key, so `vagrant destroy && vagrant up` does
  not break the connection. For standard servers the key is pinned on first use; after a legitimate re-install use
  **Forget host key** on the server page.
- **The key must not have a passphrase.** Vagrant's generated keys don't. Certificates (`BEGIN CERTIFICATE`), public
  keys (`.pub`) and PuTTY keys (`.ppk`) are rejected with an explanation.
- **Windows hosts.** Docker Desktop and VirtualBox can run side by side (VirtualBox 7 works with Hyper-V enabled,
  with some performance cost). The container reaches the 192.168.56.x host-only network through your computer.

## Monitoring an existing deployment

Create a dedicated user (on the primary or on a mongos). The custom role adds the read-only privileges used by the
Query performance section; it never grants access to documents:

```javascript
const admin = db.getSiblingDB("admin");
admin.createRole({
  role: "mongopilotIndexInspector",
  privileges: [{ resource: { db: "", collection: "" },
                 actions: ["listCollections", "listIndexes", "indexStats", "collStats", "dbStats"] }],
  roles: []
});
admin.createUser({
  user: "mongopilot-monitor",
  pwd: passwordPrompt(),
  roles: [ { role: "clusterMonitor", db: "admin" },
           { role: "read", db: "local" },
           { role: "mongopilotIndexInspector", db: "admin" } ]
});
```

Then in **Overview → Monitor an existing deployment** paste the connection string, for example
`mongodb://mongopilot-monitor:PASSWORD@db1:27017,db2:27017/?replicaSet=rs0&authSource=admin`.
For a sharded cluster list the mongos routers. TLS options (`tls=true&tlsCAFile=...`) are reused for every member.
Replica sets created by MongoPilot get this user and role automatically.

## Metrics

Metrics are collected every `MP_POLL_INTERVAL` seconds (default 30) and kept for `MP_RETENTION_DAYS` (default 14).

| Group | Metrics |
|---|---|
| Throughput | Operations per second by type, documents returned/inserted/updated/deleted, network requests |
| Latency | Average read, write, command and transaction execution time (`opLatencies`) |
| Query efficiency | Query targeting (documents and keys scanned per document returned), scan and order, write conflicts, slow operations in progress, longest operation |
| Connections & cursors | Current, active and new connections, connections used %, open cursors, cursors without timeout, cursors timed out |
| Concurrency | Queued readers/writers, active clients, read/write tickets available |
| WiredTiger cache | Used and dirty %, bytes read into / written from cache, page evictions (all and by application threads), checkpoint duration |
| Resources | Normalized process CPU, resident/virtual memory, resident memory as % of host RAM, page faults, network in/out, data volume used % |
| Storage | Data, storage and index size, collections and indexes (summed over user databases on the primary, refreshed every 5 minutes) |
| Replication | Replication lag, oplog window, oplog GB per hour, replication headroom (primary oplog window minus lag), member health, flow control |
| Transactions & other | Open/active transactions, commits/aborts per second, TTL deletions, asserts |
| Host (SSH) | Normalized system CPU and I/O wait, load per core, memory and swap used, disk read/write IOPS and latency, disk utilization, free space on the fullest volume |
| Deployment | Primary present, unhealthy members, unreachable processes, primary changes (elections), balancer enabled, jumbo chunks |

Host metrics appear in the deployment charts when the process's host name matches a registered server (its SSH host
or MongoDB host name), and on each server's page. Disk latency and IOPS come from `/proc/diskstats` of whole devices.

## Query performance

**Slow queries.** Every polling cycle MongoPilot reads the in-memory log of each data-bearing member (`getLog`,
included in `clusterMonitor`) and stores operations slower than MongoDB's `slowms` threshold (100 ms by default).
Literal values are replaced with type placeholders before storage, so only field names and operators are kept.
Operations are grouped by query shape (collection, operation and redacted filter/sort) with count, average, P95 and
maximum duration, documents examined versus returned, plan summary and in-memory sorts. The log buffer holds the last
1024 lines, so on very busy members with verbose logging some entries may be missed between two polls. To capture
faster queries lower the threshold on each member, for example `db.setProfilingLevel(0, { slowms: 50 })`; level 0
keeps the profiler off and only changes what is logged.

**Index advisor.** A read-only analysis combines the slow query shapes of the selected period with each collection's
index definitions, sizes and `$indexStats` usage counters:

- *Indexes to create* for shapes that do collection scans, examine at least 10× more documents than they return, or
  sort in memory. Keys follow the Equality → Sort → Range rule; `$or` queries get one index per branch. A suggestion
  is dropped when an existing index already serves it. Impact is the share of total slow-query time involved.
- *Unused indexes*: zero accesses on every data-bearing member since the counters started (they reset on restart; a
  window shorter than 7 days is flagged). Unique, TTL and shard key indexes are listed as "keep". The advice is to
  hide the index first (`hideIndex`, MongoDB 4.4+), watch for regressions, then drop it.
- *Redundant indexes*: an index whose key is a prefix of another index (unique, partial, sparse, TTL and collation
  indexes are never reported).

Review every suggestion before applying it: each index adds write overhead and storage, and on large collections it
should be built during a quiet period.

## Tools

Open them from the **Tools** column of a deployment's process table. Each tool connects directly to the chosen node
(`directConnection=true`, the TLS options of the deployment are kept); reading from a secondary keeps load off the
primary. Users and privileges:

| Tool | Minimum role of the user |
|---|---|
| Schema Analyzer, mongoexport | `read` on the database |
| mongodump | `backup` (full dumps, `--oplog`, users and roles) or `read` for one database |
| mongorestore | `restore` (or `readWrite` on the target databases for simple restores) |
| mongoimport | `readWrite` on the target database |
| mongosh | whatever the commands you run need |

The monitoring user can list databases and collections but cannot read documents. For deployments created by
MongoPilot the admin user is available. Passwords typed in the Tools pages stay in the browser page and are passed to
the tools through a temporary file readable only by MongoPilot (mongodump, mongoexport) or the process arguments inside
the container (mongosh); command lines shown in the interface have the password removed. Every session, dump, restore, export, import,
upload, download and deletion is written to the Activity log. mongorestore and mongoimport only run against a
primary, a mongos or a standalone. Dumps and exports take space in the data volume: delete the ones
you have downloaded.

## Automation

Requirements for each VM: Linux with systemd (Ubuntu 20.04/22.04/24.04, Debian 11/12/13, RHEL/Rocky/Alma 8–10, Amazon
Linux, SUSE 15; MongoDB 9.0 needs Ubuntu 22.04 or newer), an SSH user with a private key without passphrase and **passwordless sudo**, and `curl` or `wget`.
For non-Vagrant servers: `echo 'mongopilot ALL=(ALL) NOPASSWD:ALL' | sudo tee /etc/sudoers.d/mongopilot`.

### What it writes on the servers

| Path | Content |
|---|---|
| `/opt/mongopilot/mongodb/<version>/` | MongoDB binaries (several versions side by side, used by upgrades) |
| `/etc/mongopilot/<deployment>/mongod-<port>.conf`, `mongos-<port>.conf` | Configuration (edited from MongoPilot) |
| `/etc/mongopilot/<deployment>/tls/` | CA and server certificate + key, when TLS is enabled |
| `/etc/mongopilot/<deployment>/keyfile` | Replica set keyfile |
| `/var/lib/mongopilot/<deployment>/<port>/` | Data (configurable) |
| `/var/log/mongopilot/<deployment>/` | Logs |
| `/etc/systemd/system/mongopilot-<deployment>-<port>.service` | systemd unit |

Creation refuses to continue if the data directory is not empty or the port is in use. The replica set starts with
authentication enabled: `rs.initiate` and the first user run through an SSH tunnel, using MongoDB's localhost
exception.

### Upgrades

MongoPilot applies MongoDB's rules: 4.4 → 5.0 → 6.0 → 7.0 → 8.0 one series at a time, then 8.0 → 9.0 directly or
8.0 → 8.2 → 8.3 → 9.0, FCV equal to the current series before moving up, no downgrades. Sharded clusters: balancer
stopped, config servers, then each shard, then the mongos routers, balancer restarted. It installs the binaries on every server first, then restarts one member at a time
and waits for it to be healthy, stepping down the primary last. FCV is raised separately, once you are sure you won't
roll back.

## Default alert rules

Process unreachable · replica set has no primary · member in unhealthy state · replication lag > 10 s · replication
headroom < 12 h · oplog window < 24 h · connections > 80% · data volume > 85% · query targeting > 1000 · in-memory
sorts · read or write latency > 100 ms · more than 10 queued operations · cache dirty > 20% · process CPU > 80% ·
operation running for over 5 minutes · primary changed · server unreachable over SSH · system CPU > 85% · disk write
latency > 20 ms · free disk < 10%. All of them can be changed in **Alert rules**.

Webhooks receive a JSON POST with `kind` (`open`, `reminder`, `resolved`), `alert`, `rule` and `url`.

## Configuration

All variables are listed in `.env.example`. The main ones: `MP_POLL_INTERVAL` (seconds, default 30),
`MP_RETENTION_DAYS` (default 14), `MP_SLOW_OP_SECONDS` (threshold for "operations in progress", default 5),
`MP_SMTP_*`.

## Development and tests

```bash
pip install -r requirements.txt
python -m unittest discover -s tests -t .
MP_DEMO=true MP_ADMIN_PASSWORD=dev uvicorn --factory mongopilot.app:create_app --reload --port 8080
```

The tests use the simulated world in `mongopilot/demo.py`, which implements the same interfaces as pymongo and SSH.
They cover metric computation, host metrics parsing, topology discovery, the alert lifecycle, slow-query parsing and
redaction, ESR suggestions, the index advisor, private key validation, `vagrant ssh-config` import, replica set
creation, sharded cluster creation, 9.0 upgrade paths and platform checks, TLS certificate generation and
validation, the staged TLS job, configuration validation and protected keys, parameter coercion, rolling upgrades
the schema analyzer, tool command lines and validation, dump/export runs, the
mongosh pseudo-terminal, CPU from `extra_info` and the HTTP API.

## Layout

```
mongopilot/
  app.py            REST API, authentication, background loop
  probe.py          topology discovery, sampling, slow-query log and index inventory via pymongo
  monitor.py        metric collection, slow-query capture, host metrics over SSH
  metrics.py        metric catalog and computation
  hostmetrics.py    /proc parsing for host hardware metrics
  insights.py       query shapes, redaction, ESR suggestions, index advisor
  schema.py         schema analysis of sampled documents
  tools.py          mongosh sessions, mongodump/mongoexport runs, node connection strings
  demo_tools.py     generated documents, simulated mongosh and tools for demo mode
  advisor.py        index advisor runs and reports
  alerts.py         rules, alert states, notifications
  automation/       SSH, key validation, templates (config, systemd, scripts), TLS certificates, jobs
  demo.py           simulated clusters, workloads and servers
  static/           web interface (HTML/CSS/JS, no build step)
vagrant/Vagrantfile sample 3-VM lab
lab/                docker compose lab with a real 3-node replica set
```
