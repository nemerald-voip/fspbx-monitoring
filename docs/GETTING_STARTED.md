# Getting started on a new monitoring server

This guide takes a new Debian VPS from an empty host to a running central
FreeSWITCH monitoring stack. The central host is called MON01 in examples.

## 1. Understand what will be installed

The default installer will:

1. Verify Debian and systemd.
2. Install Docker CE, containerd, Buildx, and Docker Compose from Docker's
   official APT repository when they are missing.
3. Copy the repository into `/opt/monitoring`.
4. enable and start `docker.service` and `containerd.service`.
5. Install and enable `monitoring-reconcile.service`.
6. Generate `/opt/monitoring/.env` with random credentials and mode `0600`.
7. Validate the Compose model.
8. Pull the pinned container images and start the stack.

It does not configure a firewall, DNS, TLS, a reverse proxy, VPN access,
Alertmanager delivery, PBX agents, or SIPp timers.

## 2. Prepare the server

Recommended baseline for the repository defaults:

- Debian 13, 12, or 11 with systemd
- root access through `sudo` or a root shell
- outbound HTTPS to Debian, Docker, GitHub, GHCR, and container registries
- approximately 8 vCPU, 24 GB RAM, and 300 GB SSD storage
- a stable private or public address reachable from the PBXs
- correct time synchronization

The sizing is a starting point, not a guarantee. SIP and log volume determine
the actual requirement.

Before deployment, decide how PBXs will reach MON01 and how operators will reach
the UIs. A private VPN is preferred. By default:

- HEP listens on UDP and TCP 9060 on every interface.
- Grafana, HOMER UI, Prometheus, Loki, Alertmanager, and Pushgateway listen only
  on `127.0.0.1`.
- The installer makes no firewall changes.

Restrict HEP 9060 to known PBX source addresses using the VPS provider firewall,
a VPN, and/or host rules appropriate for Docker. Review Docker's
[packet-filtering and firewall guidance](https://docs.docker.com/engine/network/packet-filtering-firewalls/)
before relying on UFW or firewalld alone.

## 3. Install

### Fast path

The current development branch can be installed with one command:

```bash
curl -fsSL https://raw.githubusercontent.com/nemerald-voip/fspbx-monitoring/main/install.sh | sudo sh
```

The installer prints the generated HOMER and Grafana passwords once. Save them
in a password manager. They also remain in `/opt/monitoring/.env`, readable only
by root by default.

At the time this guide was written, no stable release tag had been published.
Once releases exist, use a pinned tag instead of `main`, for example:

```bash
curl -fsSL https://raw.githubusercontent.com/nemerald-voip/fspbx-monitoring/v1.0.0/install.sh | \
  sudo sh -s -- --version v1.0.0
```

Do not use that tagged example until the tag exists in GitHub Releases.

### Review before execution

For a security-sensitive host, download and inspect the installer first:

```bash
curl -fsSLo /tmp/fspbx-monitoring-install.sh \
  https://raw.githubusercontent.com/nemerald-voip/fspbx-monitoring/main/install.sh
less /tmp/fspbx-monitoring-install.sh
sudo sh /tmp/fspbx-monitoring-install.sh
```

### Prepare without launching containers

Install Docker and the boot reconciler, but leave `.env` absent and do not pull
or start the monitoring images:

```bash
curl -fsSL https://raw.githubusercontent.com/nemerald-voip/fspbx-monitoring/main/install.sh | \
  sudo sh -s -- --no-secrets --no-start
```

`--no-start` by itself still generates `.env`; it only skips the image pull and
Compose launch.

### Installer options

| Option | Effect |
|---|---|
| `--repo <owner>/<repository>` | Download a different public GitHub repository |
| `--version REF` | Install a branch or tag; defaults to `main` |
| `--install-dir PATH` | Use a directory other than `/opt/monitoring` |
| `--source-dir PATH` | Install from an existing local checkout |
| `--skip-docker` | Require and reuse an existing Docker Engine/Compose setup |
| `--no-secrets` | Leave `.env` absent and imply `--no-start` |
| `--no-start` | Configure the host without pulling or starting the stack |

Display the current help text with:

```bash
sudo /opt/monitoring/install.sh --help
```

## 4. Verify the installation

Run these commands as root or with `sudo`:

```bash
docker version
docker compose version
systemctl is-enabled docker.service containerd.service monitoring-reconcile.service
systemctl is-active docker.service containerd.service
cd /opt/monitoring
docker compose ps
```

For a default full install, confirm that `.env` exists and is private:

```bash
stat -c '%a %U:%G %n' /opt/monitoring/.env
```

Expected mode is `600`. Review recent logs if a service is not running:

```bash
cd /opt/monitoring
docker compose logs --tail=200
journalctl -u monitoring-reconcile.service -b --no-pager
```

The reconciler is a one-shot service. It may show `inactive (dead)` after it ran
successfully; inspect `Result`, the journal, and container state rather than
expecting it to remain active.

## 5. Open the web interfaces safely

The default UIs are loopback-only. From an operator workstation, create an SSH
tunnel:

```bash
ssh \
  -L 3000:127.0.0.1:3000 \
  -L 8080:127.0.0.1:8080 \
  -L 9090:127.0.0.1:9090 \
  -L 9093:127.0.0.1:9093 \
  admin@MON01
```

Then open:

- Grafana: `http://127.0.0.1:3000`
- HOMER: `http://127.0.0.1:8080`
- Prometheus: `http://127.0.0.1:9090`
- Alertmanager: `http://127.0.0.1:9093`

Loki and Pushgateway are APIs rather than general operator UIs. Keep them
private. If you later publish an interface through a reverse proxy, configure
TLS and authentication first and review the bind settings in `.env`.

## 6. Configure the first PBX

Central installation alone does not produce PBX telemetry. Follow
[`pbx-agent/README.md`](../pbx-agent/README.md) on each FreeSWITCH host.

At minimum:

1. Enable native FreeSWITCH HEP capture to MON01 UDP 9060.
2. Expose node_exporter TCP 9100 only to MON01/VPN.
3. Run freeswitch_exporter TCP 9282 beside FreeSWITCH, with ESL kept on
   `127.0.0.1:8021`.
4. Ship selected logs with Grafana Alloy through a private or authenticated
   route.

Add the PBX to the local Prometheus discovery files:

```bash
cd /opt/monitoring
sudoedit prometheus/targets/pbx-nodes.yml
sudoedit prometheus/targets/freeswitch.yml
sudoedit prometheus/targets/tcp-probes.yml
```

These files are intentionally ignored by Git. File-based discovery refreshes
within one minute. Changes to `prometheus.yml` or alert rules require a reload:

```bash
cd /opt/monitoring
make reload-prometheus
```

## 7. Configure notifications

Alertmanager initially keeps alerts local and sends no email, chat, webhook, or
PagerDuty notifications. Read [`alertmanager/README.md`](../alertmanager/README.md)
before enabling delivery. Never commit notification credentials.

## 8. Acceptance checklist

- [ ] Docker and containerd are active and enabled.
- [ ] `monitoring-reconcile.service` is enabled.
- [ ] `docker compose ps` shows the expected long-running services.
- [ ] Grafana and HOMER can be reached through the SSH tunnel.
- [ ] UIs and Loki are not unintentionally exposed publicly.
- [ ] HEP 9060 is restricted to approved PBXs or the monitoring VPN.
- [ ] Generated credentials are stored securely.
- [ ] Prometheus shows MON01 targets as healthy.
- [ ] The first PBX sends HEP and appears in HOMER.
- [ ] PBX metric targets are healthy in Prometheus.
- [ ] Alert delivery has been deliberately configured or deliberately deferred.
- [ ] A backup and restore process has been selected before production use.

Continue with [Architecture](ARCHITECTURE.md) and
[Operations](OPERATIONS.md). Add scheduled transactions using
[Synthetic SIP checks](SYNTHETIC_SIP.md) after basic metrics and HEP work.
