# AGENTS.md

This file is the repository-level operating guide for AI coding systems and
automation working on `nemerald-voip/fspbx-monitoring`. It applies to the entire
repository unless a more specific `AGENTS.md` is added in a subdirectory.

## Mission

This repository deploys an observability stack for FreeSWITCH/PBX servers. The
central monitoring host is called MON01 in examples. It collects SIP signaling,
metrics, logs, endpoint probes, and synthetic SIP results without becoming part
of the live call path.

The most important architectural invariant is:

> A failure of MON01 must reduce visibility only. It must not interrupt calls,
> registrations, routing, media, or PBX administration.

## Read this first

Before making changes, read these files:

1. `README.md` for the repository overview and supported entry points.
2. `docs/GETTING_STARTED.md` for a fresh-server deployment.
3. `docs/ARCHITECTURE.md` for data flows, services, ports, and persistence.
4. `docs/OPERATIONS.md` for lifecycle, reboot, upgrades, and troubleshooting.
5. `compose.yaml`, `.env.example`, and `install.sh` for the executable truth.
6. `pbx-agent/README.md` when changing anything installed on a PBX.

Documentation explains intent, but executable configuration is authoritative.
If documentation and code disagree, confirm the code behavior, fix the docs in
the same change, and call out the discrepancy.

## Deployment model

- Supported installer hosts: Debian 11, 12, or 13 with systemd as PID 1.
- Default installation directory: `/opt/monitoring`.
- Default Compose project: `fs-monitoring`.
- Docker CE and the Compose plugin come from Docker's official APT repository.
- Docker and containerd are managed by systemd.
- `monitoring-reconcile.service` runs `docker compose up -d --remove-orphans`
  once after boot when both `compose.yaml` and `.env` exist.
- Long-running containers use `restart: unless-stopped`.
- Application state is stored in named Docker volumes.
- Routine operation uses `docker compose`, not Supervisor.

The boot reconciler has no `ExecStop` and no `RemainAfterExit`. Seeing it as
`inactive (dead)` after a successful one-shot run can be normal. Inspect its
journal and result before treating that state as a failure.

## Central services

| Compose service | Purpose | Default host exposure | Persistent volume |
|---|---|---|---|
| `homer` | HEP ingest, SIP search, call-flow UI | UDP/TCP 9060 on all interfaces; UI TCP 8080 on loopback | `homer_data` |
| `prometheus` | Metrics, recording/evaluation, alerts | TCP 9090 on loopback | `prometheus_data` |
| `grafana` | Dashboards and exploration | TCP 3000 on loopback | `grafana_data` |
| `loki` | Central log storage | TCP 3100 on loopback | `loki_data` |
| `alertmanager` | Alert grouping and routing | TCP 9093 on loopback | `alertmanager_data` |
| `pushgateway` | Synthetic SIP result ingestion | TCP 9091 on loopback | `pushgateway_data` |
| `node-exporter` | MON01 host metrics | Compose network only | none |
| `blackbox-exporter` | HTTP/TCP probes | Compose network only | none |
| `volume-init` | One-time named-volume ownership | none | touches all named volumes |

Do not assume a container is healthy merely because it is running. Check its
logs and the relevant Prometheus target or UI.

## Repository map

- `install.sh`: idempotent fresh-host installer and upgrade entry point.
- `compose.yaml`: complete central service topology and image pins.
- `.env.example`: safe template, bind defaults, retention, and image versions.
- `systemd/monitoring-reconcile.service`: boot-time Compose reconciler template.
- `systemd/fspbx-hep-firewall.service`: optional, explicit HEP allowlist unit.
- `ops/apply-hep-firewall.sh`: optional Docker iptables IPv4 allowlist helper.
- `scripts/init-secrets.sh`: creates `.env` once with mode `0600`.
- `scripts/check-config.sh`: validates the local deployment using `.env`.
- `scripts/check-repository.sh`: CI-safe repository validation using
  `.env.example`.
- `prometheus/prometheus.yml`: scrape jobs and Alertmanager integration.
- `prometheus/rules/monitoring.yml`: availability and host-health alerts.
- `prometheus/targets.example/`: tracked target templates.
- `prometheus/targets/`: ignored, host-specific target inventory.
- `grafana/provisioning/`: provisioned data sources and dashboard loader.
- `grafana/dashboards/`: tracked dashboard JSON.
- `loki/loki.yml`: single-node filesystem storage and retention.
- `alertmanager/`: default no-op alert delivery configuration.
- `blackbox/`: HTTP and TCP probe modules.
- `pbx-agent/`: PBX-side FreeSWITCH, exporter, Alloy, and RTCP sensor examples.
- `pbx-agent/rtcp-quality/`: live RTCP-to-HEP sensor, fail-closed BPF check,
  bounded canary capture, and repeatable installer.
- `synthetic/`: SIPp OPTIONS scenario, publisher, and timer/service templates.
- `docs/SYNTHETIC_SIP.md`: scheduled SIP transaction setup and limitations.
- `docs/RTP_QUALITY.md`: centralized RTCP quality setup and interpretation.
- `docs/FIREWALL.md`: provider/VPN and Docker-aware HEP restriction examples.
- `docs/`: operator-facing deployment, architecture, and operations guides.
- `.github/workflows/`: validation and tag-triggered release workflows.

## Security invariants

Never weaken these without an explicit, reviewed requirement:

- Never commit `.env`, real passwords, webhook URLs, API tokens, private keys,
  SIP credentials, packet captures, logs, or populated private inventories.
- Keep FreeSWITCH ESL bound to `127.0.0.1`; exporters connect to it locally.
- Keep Grafana, HOMER UI, Prometheus, Loki, Alertmanager, and Pushgateway on
  loopback unless they are placed behind a VPN or authenticated TLS proxy.
- Treat Loki's push endpoint as unauthenticated. Do not expose it directly to
  untrusted networks.
- Restrict HEP UDP/TCP 9060 to known PBX/VPN source addresses. The installer
  deliberately does not alter host or provider firewalls.
- Do not add users to the `docker` group. Membership is effectively root access.
- Keep container images pinned. Do not replace pins with floating `latest` tags.
- Preserve `no-new-privileges` and dropped capabilities where present.
- Do not enable anonymous Grafana access or public sign-up.
- Alertmanager ships without external delivery. Notification credentials must
  remain local and untracked.

If a requested change affects firewall rules, public exposure, authentication,
secret storage, named volumes, or production PBXs, state the impact and obtain
explicit authorization before applying it.

## Data and destructive operations

Named volumes survive Docker daemon restarts, host reboots, and ordinary
`docker compose down`. They do not survive an explicit volume deletion.

Never run these as a routine validation step:

- `docker compose down --volumes`
- `docker volume rm ...`
- `docker system prune --volumes`
- recursive removal of `/opt/monitoring`, `/var/lib/docker`, or backup paths

Before changing storage schemas, volume ownership, retention, or image major
versions, document backup and rollback steps. Never claim backups work until a
restore has been tested.

## Host-specific state

The following files are intentionally local and must remain ignored:

- `.env`
- `.installed-version`
- `.installed-files`
- `prometheus/targets/*.yml`
- `*.pcap` and `*.pcapng`
- `backups/` and `data/`
- `/etc/fs-monitoring/hep-firewall.env` (outside Git, when the optional host
  allowlist is enabled)

Do not infer that `.env` exists or that the stack is running. Inspect first:

```bash
test -f .env && echo configured || echo unconfigured
if [ -f .env ]; then docker compose ps; else docker ps --all; fi
systemctl is-enabled monitoring-reconcile.service
```

Do not create credentials, pull images, start containers, restart services, or
modify `/etc` merely to answer a question or review a change. Those are runtime
mutations and require task scope that clearly includes deployment or operation.

## Change workflow

Keep changes small and update documentation alongside behavior.

1. Inspect `git status` and preserve unrelated user changes.
2. Change templates rather than generated/local files.
3. Keep the installer idempotent: it must not overwrite an existing `.env` or
   host-specific Prometheus targets.
4. When adding an environment variable, update `.env.example`, Compose usage,
   installer behavior if relevant, and operator documentation together.
5. When changing a port, service name, volume, image, or retention default,
   update `README.md`, `docs/ARCHITECTURE.md`, and `docs/OPERATIONS.md`.
6. When changing PBX behavior, update `pbx-agent/README.md` and preserve the
   monitoring-outside-the-call-path invariant.
7. Run the validation commands below before committing.

## Required validation

Repository-only validation must not require a real `.env` or pull images:

```bash
./scripts/check-repository.sh
./scripts/check-docs.sh
docker compose --env-file .env.example config --quiet
git diff --check
```

If ShellCheck is installed, `check-repository.sh` runs it automatically. CI also
installs ShellCheck. Do not silence a warning without a narrow explanation.

For a configured deployment, additionally run:

```bash
./scripts/check-config.sh
docker compose ps
```

Only run service-specific validators or live API checks when the relevant image
or service is already available, or when pulling/starting it is explicitly in
scope.

## Installer behavior that must remain true

- `install.sh` requires root and validates Debian/systemd.
- It accepts `--repo`, `--version`, `--install-dir`, `--source-dir`,
  `--skip-docker`, `--no-secrets`, and `--no-start`.
- `--no-secrets` implies `--no-start`.
- Existing Docker CE is reused; missing Docker CE is installed officially.
- Existing `.env` is preserved.
- Existing `prometheus/targets/*.yml` files are preserved.
- Existing local Alertmanager routing and other untracked operator files are
  preserved.
- A source Compose model is validated before managed files are refreshed.
- `.installed-files` limits update cleanup to paths managed by the prior run,
  and `.installed-version` records the exact source revision.
- The reconciler is rendered for a custom installation directory.
- Default behavior generates secrets, validates Compose, pulls images, and
  starts the stack.

Test installer changes with an isolated temporary installation directory and a
temporary systemd unit directory. Do not point installer tests at production
state.

## Git and releases

- Canonical remote: `https://github.com/nemerald-voip/fspbx-monitoring.git`.
- Default branch: `main`.
- Pushes and pull requests run `.github/workflows/validate.yml`.
- Tags matching `v*` run validation and create a GitHub release.
- Prefer semantic version tags and immutable releases for production commands.
- Do not create, move, or delete release tags without explicit authorization.
- Do not rewrite published history unless explicitly requested.

Commit messages should describe the operational outcome, not merely list files.

## Definition of done

A change is complete when:

- behavior and documentation agree;
- secrets and local inventories remain untracked;
- repository validation and `git diff --check` pass;
- safety, persistence, and reboot behavior remain clear;
- any untested runtime behavior is explicitly identified;
- the final handoff names changed files and validation performed.
