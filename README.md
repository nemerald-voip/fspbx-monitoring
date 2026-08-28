# FreeSWITCH monitoring stack

This repository deploys the layered design from the specification on one
monitoring server:

- HOMER 11: HEP/SIP capture, search, and call flows
- Prometheus and node_exporter: monitoring-server and PBX metrics
- Grafana: dashboards and log/metric exploration
- Loki: centralized FreeSWITCH, nginx, application, and system logs
- Alertmanager: alert grouping and delivery
- Blackbox Exporter: HTTP/TCP availability probes
- Pushgateway and SIPp: scheduled synthetic SIP transaction results
- Optional PBX-side, hard-capped RTP-header ring capture

HOMER, Loki, Prometheus, Grafana, and Alertmanager run centrally. ESL stays
loopback-only on each PBX; a local FreeSWITCH exporter exposes metrics to MON01.

## Deploy MON01

Requirements: a current Docker Engine with the Compose plugin, Linux, and
OpenSSL. The defaults target an 8-vCPU, 24-GB-RAM, 300-GB-SSD VPS.

```bash
cd /opt/monitoring
./scripts/init-secrets.sh
./scripts/check-config.sh
docker compose pull
docker compose up -d
docker compose ps
```

Save the generated passwords immediately. Open the web interfaces through SSH:

```bash
ssh -L 3000:127.0.0.1:3000 -L 8080:127.0.0.1:8080 user@MON01
```

Then browse to `http://127.0.0.1:3000` for Grafana and
`http://127.0.0.1:8080` for HOMER. Prometheus is on local port 9090 and
Alertmanager on 9093.

## Configure PBXs

Follow [pbx-agent/README.md](pbx-agent/README.md). At minimum, each PBX needs:

1. Native FreeSWITCH HEP capture to MON01 UDP 9060.
2. node_exporter on TCP 9100, reachable only by MON01.
3. freeswitch_exporter on TCP 9282, connected to local ESL at 127.0.0.1:8021.
4. Grafana Alloy shipping selected logs to Loki through a private, authenticated
   route.

Populate the empty target lists under `prometheus/targets/` with private/VPN
addresses, then reload Prometheus:

```bash
curl -X POST http://127.0.0.1:9090/-/reload
```

## Synthetic SIP checks

The included SIPp scenario sends an OPTIONS transaction and publishes success,
duration, and last-run time to Pushgateway. Install `sipp` on MON01, install the
two `synthetic-sip@` systemd units, and create one environment file per PBX:

```bash
install -d -m 0750 /etc/fs-monitoring
install -m 0644 synthetic/synthetic-sip@.service synthetic/synthetic-sip@.timer /etc/systemd/system/
install -m 0640 synthetic/synthetic-sip.env.example /etc/fs-monitoring/synthetic-sip-pbx1.env
systemctl daemon-reload
systemctl enable --now synthetic-sip@pbx1.timer
```

Repeat with another safe instance name and environment file for each PBX.

An OPTIONS response tests the SIP transaction path. For true end-to-end call and
RTP verification, add an environment-specific authenticated INVITE scenario and
test destination; those credentials and routing details cannot safely be
predefined here.

## Retention and disk budget

Defaults are explicit so a telemetry surge cannot silently create infinite
retention:

| Data | Default | Suggested 300-GB budget |
|---|---:|---:|
| HOMER SIP | 30 days | about 140 GB |
| Loki logs | 14 days | about 70 GB |
| Prometheus metrics | 90 days, max 40 GB | about 40 GB |
| OS, Docker, reserve | n/a | about 50 GB |

Actual usage depends on call volume and log verbosity. Alert rules fire at 15%
and 7% filesystem free. Review ingest after the first week and reduce retention
before the disk becomes constrained. Docker JSON logs rotate at 20 MB x 3 per
container.

## Security model

- All web interfaces bind to `127.0.0.1` by default.
- Only HEP UDP/TCP 9060 binds publicly; firewall it to known PBX addresses.
- Generated secrets live in `.env` with mode 0600 and are ignored by Git.
- HOMER's node query service stays on loopback with an explicit token.
- Containers drop capabilities where possible and use no-new-privileges.
- Do not expose Loki's unauthenticated push API. Put remote collection on a VPN
  or authenticated TLS reverse proxy.
- Alertmanager ships with no-op receivers; configure real delivery privately in
  [alertmanager/README.md](alertmanager/README.md).

## Operations

```bash
docker compose ps
docker compose logs -f --tail=200
docker compose restart prometheus
docker compose down                 # keeps named-volume data
```

Back up the HOMER, Grafana, Prometheus, Alertmanager, Loki, and Pushgateway named
volumes before upgrades. Test restores. Upgrade one pinned image at a time and
read its release notes; do not switch the deployment to floating `latest` tags.

The monitoring server is deliberately outside the call path. A MON01 outage
must reduce visibility only; it must not interrupt calls, registrations, or RTP.

## One-command installation

On a new Debian server, install the current development branch with:

```bash
curl -fsSL https://raw.githubusercontent.com/nemerald-voip/fspbx-monitoring/main/install.sh | sudo sh
```

The installer supports Debian 11, 12, and 13 with systemd. It installs Docker CE
from Docker's official APT repository, generates local credentials, enables the
boot reconciler, and launches the stack. It is safe to run again: an existing
`.env` and host-specific Prometheus targets are preserved.

Use `--no-start` to install and configure everything without pulling or starting
the monitoring containers. Use `--no-secrets --no-start` to leave `.env` absent.
Run `./install.sh --help` for all options.

For a production deployment, use a version tag rather than `main`:

```bash
curl -fsSL https://raw.githubusercontent.com/nemerald-voip/fspbx-monitoring/v1.0.0/install.sh | \
  sudo sh -s -- --version v1.0.0
```

GitHub release immutability should be enabled so published tags cannot be moved.

## Updating

Re-run the installer with the desired release tag:

```bash
curl -fsSL https://raw.githubusercontent.com/nemerald-voip/fspbx-monitoring/v1.1.0/install.sh | \
  sudo sh -s -- --version v1.1.0
```

Application data stays in named Docker volumes. The installer preserves `.env`
and `prometheus/targets/*.yml`; review release notes before changing pinned image
versions. Back up named volumes before upgrades.

Repository changes are checked with:

```bash
make validate-repository
```
