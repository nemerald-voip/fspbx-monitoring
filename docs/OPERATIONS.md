# Operations

Commands assume the default deployment directory:

```bash
cd /opt/monitoring
```

Use root or `sudo` for Docker and systemd operations. No user needs membership
in the root-equivalent `docker` group.

## Daily checks

```bash
docker compose ps
docker compose logs --tail=100
systemctl is-active docker.service containerd.service
systemctl is-enabled docker.service containerd.service monitoring-reconcile.service
df -h
docker system df
```

In Prometheus, check **Status → Targets** for scrape and probe health. In Grafana,
review the provisioned FreeSWITCH/PBX overview. In HOMER, confirm recent HEP
traffic from every expected capture ID.

## Common lifecycle commands

```bash
make ps
make logs
make validate
make up
docker compose restart prometheus
docker compose stop
docker compose start
docker compose down
```

`docker compose down` removes containers and the Compose network but keeps named
volumes. Do not add `--volumes` unless permanent data deletion is explicitly
intended and backups have been verified.

## Validate configuration

Repository/template validation, with no real `.env` required:

```bash
./scripts/check-repository.sh
docker compose --env-file .env.example config --quiet
```

Configured-host validation:

```bash
./scripts/check-config.sh
```

The configured check rejects missing `.env`, placeholder credentials, a missing
Compose plugin, and an invalid Compose model.

## Add or change PBX targets

Edit the ignored local inventories:

```bash
sudoedit prometheus/targets/pbx-nodes.yml
sudoedit prometheus/targets/freeswitch.yml
sudoedit prometheus/targets/http-probes.yml
sudoedit prometheus/targets/tcp-probes.yml
```

Prometheus file discovery refreshes every minute. A target-only change does not
require a container restart. Changes to `prometheus/prometheus.yml` or rule files
should be validated and reloaded:

```bash
make reload-prometheus
```

Confirm the reload in Prometheus logs and target status.

## Logs and troubleshooting

All stack logs:

```bash
docker compose logs -f --tail=200
```

One service:

```bash
docker compose logs -f --tail=200 homer
docker compose logs -f --tail=200 prometheus
docker compose logs -f --tail=200 loki
```

Boot reconciliation:

```bash
systemctl status monitoring-reconcile.service --no-pager
journalctl -u monitoring-reconcile.service -b --no-pager
systemctl show monitoring-reconcile.service -p Result -p ExecMainStatus
```

The reconciler is `Type=oneshot` without `RemainAfterExit`. An inactive state
after a successful exit is expected. If `.env` is absent, its path condition
prevents a run.

Docker daemon:

```bash
systemctl status docker.service containerd.service --no-pager
journalctl -u docker.service -b --no-pager
docker info
```

## Common failure cases

### Missing `.env`

Symptom: Compose reports required variables are unset, or the boot reconciler is
skipped.

```bash
cd /opt/monitoring
./scripts/init-secrets.sh
./scripts/check-config.sh
docker compose up -d
```

`init-secrets.sh` refuses to overwrite an existing `.env`.

### Placeholder credentials

Symptom: `check-config.sh` reports placeholder credentials.

Do not use `.env.example` as a production `.env`. If no real state exists yet,
remove the bad local file deliberately and run `init-secrets.sh`. If the stack
has already been used, rotate credentials carefully instead of regenerating
them blindly.

### Port already in use

```bash
ss -lntup
docker compose config
```

Change the relevant port or bind value in `.env`, validate, and reconcile. Keep
UIs on loopback unless a secure access layer is already in place.

### A container repeatedly restarts

```bash
docker compose ps
docker compose logs --tail=300 SERVICE
docker inspect "$(docker compose ps -q SERVICE)"
```

Check permissions, volume capacity, configuration parsing, and dependency
availability before recreating data.

### A PBX is absent from Prometheus

1. Confirm its YAML entry is valid.
2. Confirm MON01 can reach TCP 9100 or 9282 over the intended private path.
3. Confirm provider and host firewalls permit MON01.
4. Confirm the exporter listens on the expected address.
5. Confirm FreeSWITCH ESL remains loopback-only and the local exporter has the
   correct ESL password.

### SIP is absent from HOMER

Trace the capture path in order so a local configuration success is not mistaken
for successful HOMER delivery:

1. **Persisted:** On the affected PBX, confirm `vars.xml` contains its
   hostname-scoped capture ID:

   ```bash
   grep 'hep_capture_id' /etc/freeswitch/vars.xml
   ```

2. **Runtime:** Confirm FreeSWITCH is running as the expected physical server,
   has the same live ID, and still has the selected profiles running:

   ```bash
   fs_cli -x 'switchname'
   fs_cli -x 'global_getvar hep_capture_id'
   fs_cli -x 'sofia status' | grep -E 'internal|external'
   ```

3. **PBX emission:** Replace `MONITORING_IP`, start a short capture, and place or
   receive a test call:

   ```bash
   sudo tcpdump -n -q -i any \
     'udp dst host MONITORING_IP and dst port 9060'
   ```

   Use `tcp` instead of `udp` when configured. If no packet appears, review the
   collector, port, selected Sofia profiles, and the
   [native FS PBX setup](../pbx-agent/README.md#1-native-hep-capture).

4. **MON01 ingest:** Confirm the PBX source is permitted by the provider and
   Docker-aware host firewalls. Run a corresponding short, authorized capture
   on MON01, then verify that the test call appears in HOMER under the PBX's
   expected capture ID.

A successful capture with `127.0.0.1` as the collector proves only persistence,
runtime state, and local HEP generation. It does not prove network routing,
firewall access, MON01 receipt, or HOMER ingestion. Every physical FreeSWITCH
server, including each node in a redundant system, must have a unique unsigned
numeric capture ID.

Do not commit packet captures or leave broad captures running indefinitely.

### RTCP quality is absent from HOMER

The SIP ladder can work while the **QoS** tab remains empty. Confirm that RTCP
is enabled and RTCP events are requested on each applicable call path, then
check the local ESL reporter:

```bash
grep -RIn 'rtcp-audio-interval-msec' /etc/freeswitch/sip_profiles
grep -RIn 'fire_rtcp_events' /etc/freeswitch/dialplan
systemctl status freeswitch-rtcp-to-hep.service --no-pager
journalctl -u freeswitch-rtcp-to-hep.service -n 100 --no-pager
journalctl -u freeswitch-rtcp-to-hep.service \
  --since '10 minutes ago' --no-pager | grep 'diagnostics '
```

The journal should confirm a connection to `127.0.0.1:8021`. Authentication
errors mean the root-only service credential no longer matches FreeSWITCH.
Never solve that by exposing ESL remotely; rerun the installer with a protected
one-line password file.

Place a call lasting at least 15 seconds. Missing remote reports can leave
PBX-to-remote loss unmeasured even when FreeSWITCH's own reports are present.
Missing `SEND_RTCP_MESSAGE` events point to `fire_rtcp_events` not being set
before media activation. For a bridged B2BUA call, open the original
inbound/A-leg SIP transaction: the reporter uses its Call-ID as the canonical
QoS correlation key for every FreeSWITCH media leg. A reporter upgrade affects
new reports only and does not regroup historical HOMER data. Follow
[Centralized RTP/RTCP quality](RTP_QUALITY.md) for SRTCP handling, correlation,
direction interpretation, cumulative field semantics, HOMER graph limits, and
the diagnostic counter definitions. A carrier-facing `send` count without a
matching `recv` count is evidence to investigate carrier RTCP, SDP ports,
RTCP-mux, and NAT/firewall behavior separately from Call-ID correlation.

## Reboot behavior

No manual command should be required after a normal host reboot:

- systemd starts Docker and containerd;
- Docker restores existing `unless-stopped` containers;
- the one-shot reconciler applies the current Compose declaration;
- named volumes retain application data.

Validate without rebooting by checking enablement, then—during an approved
maintenance window—restart only the Docker daemon and confirm recovery:

```bash
systemctl is-enabled docker.service containerd.service monitoring-reconcile.service
systemctl restart docker.service
docker compose ps
```

A real host reboot is a separate operational action and should not be performed
implicitly as part of configuration acceptance.

## Update the deployment

The installed application directory is a deployed copy, not necessarily a Git
checkout. The same one-line command used for a new installation is also the
supported in-place update command:

```bash
curl -fsSL https://raw.githubusercontent.com/nemerald-voip/fspbx-monitoring/main/install.sh | sudo sh
```

On every run, the installer:

1. Downloads the selected branch or release into a temporary directory.
2. Validates its Compose model before changing `/opt/monitoring`.
3. Overwrites repository-managed files with the selected revision.
4. Removes only obsolete paths listed by the previous `.installed-files`
   manifest; it does not recursively clean the deployment directory.
5. Preserves `.env`, `prometheus/targets/*.yml`,
   `alertmanager/alertmanager.local.yml`, backups, captures, data directories,
   and other untracked operator files.
6. Records the source, requested reference, and exact Git revision in
   `.installed-version`.
7. Validates the configured deployment, pulls pinned images, and runs
   `docker compose up -d --remove-orphans`.

Compose recreates services whose image or declaration changed and leaves
unchanged containers running. Named volumes are not deleted. Review the result:

```bash
cat /opt/monitoring/.installed-version
cd /opt/monitoring
docker compose ps
```

This central update does not modify reporters or exporters installed on PBX
hosts. Rerun the relevant PBX-side installer on each PBX when those components
change.

For production, prefer a release tag once one is available:

```bash
curl -fsSL https://raw.githubusercontent.com/nemerald-voip/fspbx-monitoring/v1.1.0/install.sh | \
  sudo sh -s -- --version v1.1.0
```

Before upgrading:

1. Read the repository and upstream image release notes.
2. Back up named volumes and test the restore process.
3. Review Compose and configuration diffs.
4. Change one major component at a time when possible.
5. Confirm disk headroom for image and migration work.

To roll application files back, rerun the installer with a previously reviewed
release tag. This does not by itself reverse a container application's data
migration; use that component's tested backup and restore procedure when a
release changes a storage schema.

Never switch production image references to floating `latest` tags.

## Backup and restore expectations

Back up these named volumes:

- `homer_data`
- `prometheus_data`
- `alertmanager_data`
- `loki_data`
- `grafana_data`
- `pushgateway_data`

The actual Docker volume names normally include the Compose project prefix,
such as `fs-monitoring_homer_data`. Confirm with:

```bash
docker compose config --volumes
docker volume ls --filter label=com.docker.compose.project=fs-monitoring
```

Use provider snapshots or a documented volume-aware backup process. For
consistent backups, coordinate service quiescence and database/application
requirements. A backup is not complete until a restore has been tested on a
separate host.

Also back up the local configuration separately:

- `/opt/monitoring/.env`
- `/opt/monitoring/prometheus/targets/`
- any local Alertmanager delivery configuration
- PBX-local exporter, Alloy, SIPp, and capture environments

Store backups encrypted and outside MON01.

## Retention and capacity

Review these values in `.env`:

- `HOMER_RETENTION_DAYS`
- `PROMETHEUS_RETENTION`
- `PROMETHEUS_RETENTION_SIZE`

Loki retention is configured in `loki/loki.yml`. Watch filesystem free space,
Prometheus cardinality, Loki ingestion, and HOMER growth. Default alerts warn at
15% free and become critical at 7% free.

Homer defaults to 14 days of age-based retention. It has no filesystem
free-space trigger, so size the retention window from measured ingest and keep
the disk alerts routed to an operator.

## Security maintenance

- Keep the OS, Docker Engine, and pinned images patched deliberately.
- Audit published ports after configuration changes.
- Verify the HEP source allowlist using [the firewall guide](FIREWALL.md).
- Rotate credentials after suspected exposure.
- Keep `.env` mode `0600`.
- Keep real target inventory and notification credentials out of Git.
- Review GitHub Actions and installer changes before deploying `main`.
- Prefer immutable versioned releases for stable deployments.

See [`SECURITY.md`](../SECURITY.md) for reporting and repository secret rules.
