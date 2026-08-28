# FreeSWITCH PBX monitoring

A single-node observability stack for FreeSWITCH/PBX servers, built with HOMER,
Prometheus, Grafana, Loki, Alertmanager, Blackbox Exporter, Pushgateway, and
optional SIPp checks.

The monitoring server stays outside the live call path. If it fails, calls,
registrations, routing, and RTP must continue normally.

## Start on a new server

Supported hosts are Debian 11, 12, and 13 with systemd. The repository defaults
are sized for roughly 8 vCPU, 24 GB RAM, and a 300 GB SSD, but actual needs depend
on SIP and log volume.

Install the current `main` branch:

```bash
curl -fsSL https://raw.githubusercontent.com/nemerald-voip/fspbx-monitoring/main/install.sh | sudo sh
```

This command installs Docker CE when needed, deploys to `/opt/monitoring`,
generates credentials, installs the reboot reconciler, pulls the pinned images,
and starts the stack. It does **not** configure firewall rules, DNS, TLS, a VPN,
PBX agents, or external alert delivery.

Read the complete [new-server guide](docs/GETTING_STARTED.md) before using this
in production. It includes the review-before-execution flow, prepare-only mode,
network requirements, validation, secure UI access, and first-PBX checklist.

To prepare a host without creating `.env`, pulling monitoring images, or
starting containers:

```bash
curl -fsSL https://raw.githubusercontent.com/nemerald-voip/fspbx-monitoring/main/install.sh | \
  sudo sh -s -- --no-secrets --no-start
```

No stable release tag has been published yet. Use `main` for development and
switch production commands to a pinned release once one exists.

## What it provides

| Component | Role |
|---|---|
| HOMER 11 | HEP/SIP capture, search, and call flows |
| Prometheus | Central and PBX metrics, rules, and target health |
| Grafana | Provisioned dashboards and metric/log exploration |
| Loki | Centralized selected PBX and application logs |
| Alertmanager | Alert grouping and delivery integration |
| Blackbox Exporter | HTTP and TCP availability probes |
| Pushgateway + SIPp | Scheduled synthetic SIP transaction results |
| node_exporter | MON01 and PBX host metrics |
| RTCP quality sensor | Directional loss, jitter, and MOS reports in HOMER |

Central services run in Docker Compose on MON01. PBX-side capture and exporters
remain local to each FreeSWITCH host.

## Default access model

- HOMER HEP ingest: UDP/TCP 9060 on `0.0.0.0`; restrict it to known PBXs/VPN.
- Grafana: TCP 3000 on `127.0.0.1`.
- HOMER UI: TCP 8080 on `127.0.0.1`.
- Prometheus: TCP 9090 on `127.0.0.1`.
- Alertmanager: TCP 9093 on `127.0.0.1`.
- Loki: TCP 3100 on `127.0.0.1`.
- Pushgateway: TCP 9091 on `127.0.0.1`.

Use an SSH tunnel, VPN, or authenticated TLS reverse proxy for operator access.
The installer intentionally does not modify firewalls.

## After installation

Verify the stack:

```bash
cd /opt/monitoring
docker compose ps
systemctl is-enabled docker.service containerd.service monitoring-reconcile.service
```

Open Grafana and HOMER through an SSH tunnel:

```bash
ssh -L 3000:127.0.0.1:3000 -L 8080:127.0.0.1:8080 admin@MON01
```

Then visit:

- Grafana: `http://127.0.0.1:3000`
- HOMER: `http://127.0.0.1:8080`

Next, configure each PBX using the
[native FS PBX SIP Capture workflow](pbx-agent/README.md#1-native-hep-capture)
and the rest of the [PBX agent guide](pbx-agent/README.md), then add its private
addresses under `/opt/monitoring/prometheus/targets/`.

## Reboot behavior

- systemd starts Docker and containerd.
- Docker restores existing containers using `restart: unless-stopped`.
- `monitoring-reconcile.service` runs Compose once after boot when `.env` exists.
- Named volumes retain application state.

The reconciler is intentionally one-shot and may appear inactive after a
successful run. See [Operations](docs/OPERATIONS.md#reboot-behavior) for the
exact checks.

## Documentation

- [Getting started on a new server](docs/GETTING_STARTED.md)
- [Architecture, data flows, ports, and persistence](docs/ARCHITECTURE.md)
- [Operations, upgrades, backups, and troubleshooting](docs/OPERATIONS.md)
- [Restrict HEP to approved PBX addresses](docs/FIREWALL.md)
- [Centralized RTP/RTCP quality](docs/RTP_QUALITY.md)
- [Synthetic SIP checks](docs/SYNTHETIC_SIP.md)
- [PBX-side agent setup](pbx-agent/README.md)
- [Alert delivery](alertmanager/README.md)
- [Security policy](SECURITY.md)
- [AI/automation repository guide](AGENTS.md)

## Common operations

```bash
cd /opt/monitoring
make ps
make logs
make validate
make reload-prometheus
docker compose restart prometheus
docker compose down          # keeps named-volume data
```

Do not use `docker compose down --volumes` unless permanent data deletion is
explicitly intended and verified backups exist.

## Configuration and local state

- `.env` contains generated credentials, bind settings, retention, and image
  pins. It is mode `0600` and ignored by Git.
- `prometheus/targets/*.yml` contains local PBX inventories and is ignored.
- Tracked target templates live in `prometheus/targets.example/`.
- Central application data lives in named Docker volumes.
- Alertmanager has no external receiver by default.

Validate repository changes without a real `.env` or image pulls:

```bash
./scripts/check-repository.sh
```

Validate a configured deployment:

```bash
./scripts/check-config.sh
```

## Retention defaults

| Data | Default | Suggested share of a 300 GB host |
|---|---:|---:|
| HOMER SIP | 30 days | about 140 GB |
| Loki logs | 14 days | about 70 GB |
| Prometheus metrics | 90 days, maximum 40 GB | about 40 GB |
| OS, Docker, reserve | n/a | about 50 GB |

Review actual ingest after the first week. Alerts warn below 15% filesystem free
and become critical below 7%.

## Development

The canonical repository is
[`nemerald-voip/fspbx-monitoring`](https://github.com/nemerald-voip/fspbx-monitoring).
Pushes and pull requests run Compose, shell, systemd-template, and secret-safety
checks through GitHub Actions.

Future AI systems and contributors must read [`AGENTS.md`](AGENTS.md) before
changing deployment behavior.
