# PBX-side agent setup

Configure the telemetry you need on each FreeSWITCH host. The monitoring server
must never be required for normal call processing; HEP, logs, and metrics are
best-effort outbound telemetry.

For SIP messages and call flows in HOMER, use the three steps below. This uses
native FreeSWITCH capture. For HOMER's QoS statistics, continue with
[Enable RTCP quality stats](#enable-rtcp-quality-stats-on-the-new-node).
Alloy is only needed for separate
[application and system log files in Grafana/Loki](#3-logs-with-grafana-alloy).

## 1. Native HEP capture

### Add another FS PBX to an existing HOMER

These steps assume MON01 already runs HOMER and the persistent
`fspbx-hep-firewall.service` allowlist. For a new monitoring host, complete
[Getting started](../docs/GETTING_STARTED.md) first.

**1. Configure and save SIP Capture on the new FS PBX.**

Open **System Settings -> SIP Capture**:

| Field | Value |
|---|---|
| Enable SIP capture | On |
| Transport | UDP, or the same transport as the working PBX |
| Collector host | MON01's reachable address, as used on the working PBX; no protocol or port |
| Collector port | `9060` unless your collector uses a custom port |
| SIP profiles | `internal`, `external`, and any additional profiles you want captured |
| Current server | Confirm this is the new node's hostname |

Click **Save on this node**. **Capture ID** should become a numeric value instead
of **Assigned when saved**. FS PBX assigns each physical server its own ID;
do not copy another node's ID. Saving applies capture without restarting Sofia
profiles.

**2. Add the new PBX's source IP to MON01's firewall environment file.**

On MON01:

```bash
sudoedit /etc/fs-monitoring/hep-firewall.env
```

Append `NEW_PBX_IP/32` to `ALLOWED_HEP_SOURCES`, keeping every existing entry
inside the quotes. Use the source IPv4 address MON01 sees (the PBX's public/NAT
or VPN address). For example, with an existing source and one new source:

```dotenv
ALLOWED_HEP_SOURCES="203.0.113.10/32 198.51.100.24/32"
```

Those are documentation addresses; use your real addresses. This is
`/etc/fs-monitoring/hep-firewall.env`, **not** `/opt/monitoring/.env`.
If a provider firewall also restricts HEP, allow the new source there too.

**3. Restart the HEP firewall service on MON01.**

```bash
sudo systemctl restart fspbx-hep-firewall.service
```

Make a new test call and check HOMER's recent time range for the new capture ID.
If it is missing, use the [troubleshooting checks](../docs/OPERATIONS.md#sip-is-absent-from-homer).

### Enable RTCP quality stats on the new node

Complete the SIP capture steps above first, then do the following on **each
physical FS PBX**. RTCP stats use a separate reporter service, but the same
capture ID and HEP port `9060` allowlist; no additional MON01 port is needed.

**1. Enable RTCP and its events in FS PBX.**

For each SIP profile carrying media, open **Edit SIP Profile -> Settings** and
save this setting:

```text
Name:    rtcp-audio-interval-msec
Value:   5000
Enabled: On
```

In the managed dialplan, add this action early in each applicable call path,
before media starts (before answering, playback, or bridging):

```text
Application: export
Data:        fire_rtcp_events=true
```

Use the FS PBX editors so configuration regeneration preserves the settings.
If applying a SIP profile change requires a restart, use a maintenance window;
it can affect calls and registrations.

**2. Install the RTCP reporter on the new PBX.**

Run these commands on the **PBX, not MON01**. Replace `MON01_IP` with the same
collector address used on the SIP Capture page:

```bash
curl -fsSLo /tmp/install-freeswitch-rtcp-to-hep.sh \
  https://raw.githubusercontent.com/nemerald-voip/fspbx-monitoring/main/pbx-agent/rtcp-quality/install.sh
less /tmp/install-freeswitch-rtcp-to-hep.sh
sudo sh /tmp/install-freeswitch-rtcp-to-hep.sh --monitoring-ip MON01_IP
```

This requires `fs_cli`, Python 3, and local ESL on `127.0.0.1:8021`. The installer
detects the node name, native capture ID, and ESL credential, then starts and
enables `freeswitch-rtcp-to-hep.service` for reboot. It does not enable the FS PBX
settings in step 1 or restart FreeSWITCH. If using HEP over TCP, add
`--transport tcp` to the installer command.

**3. Verify a new call in HOMER.**

Place a new call lasting at least 15 seconds. On the PBX, check:

```bash
systemctl status freeswitch-rtcp-to-hep.service --no-pager
journalctl -u freeswitch-rtcp-to-hep.service --since '5 minutes ago' --no-pager
```

Look for `connected to local ESL and subscribed to RTCP events`. Diagnostic
summaries appear about once per minute; `rtcp_events` and `hep_sent` should
increase during calls. `send` counts PBX-generated reports and `recv` counts
received peer reports. A running service or a setting visible in the UI alone
does not prove reports are flowing.

In HOMER, open the new call's **original inbound/A-leg transaction -> QoS** and
confirm new quality reports are present. This verifies delivery; ordinary SIP
traffic on port `9060` does not prove RTCP delivery. RTCP quality uses HEP type
`5`, while SIP uses type `1`.

For troubleshooting, legacy-sensor upgrades, and rollback, see the
[full RTCP guide](../docs/RTP_QUALITY.md).

### Redundant FS PBX servers

Repeat the SIP capture steps and install the RTCP reporter on each physical
node where quality stats are required. After saving one node, let its
switch-variable database row replicate before saving the next. Replicated
settings alone do not apply capture locally: open and save the SIP Capture page
on every node, checking **Current server** each time.

### Verify the PBX configuration

Detailed runtime, saved-ID, packet-arrival, and firewall checks are in
[Operations: SIP is absent from HOMER](../docs/OPERATIONS.md#sip-is-absent-from-homer).
Capture starts with new SIP traffic; it does not backfill earlier calls. Clear
any HOMER filter restricted to an older PBX when checking the new node.

### Manual XML fallback

Use manual XML only on older or unmanaged FreeSWITCH systems without the native
FS PBX SIP Capture page. `capture-server` is a global Sofia setting;
`sip-capture` belongs inside each selected profile's `<settings>` section. Copy
and adapt `freeswitch/capture-server.xml.snippet`, replace `MONITORING_IP`, and
assign a unique unsigned numeric `capture_id` to every physical FreeSWITCH
server.

Apply the configuration using the change procedure appropriate for that system.
Manual changes may require a profile rescan, reload, or restart; schedule any
potentially disruptive action in a maintenance window. Do not use
`sofia global capture on` as the normal FS PBX workflow because it enables
capture for every profile rather than only those selected in the UI.

## 2. Host and FreeSWITCH metrics

Run `node_exporter` on TCP 9100. Run
[`freeswitch_exporter`](https://github.com/mroject/freeswitch_exporter) locally
and install the supplied service and environment file:

```bash
install -m 0755 freeswitch_exporter /usr/local/bin/freeswitch_exporter
install -m 0644 freeswitch-exporter/freeswitch-exporter.service /etc/systemd/system/
install -m 0600 freeswitch-exporter/freeswitch-exporter.env.example /etc/default/freeswitch-exporter
systemctl daemon-reload
systemctl enable --now freeswitch-exporter
```

Keep ESL on `127.0.0.1:8021`, as shown in the supplied snippet. Permit TCP 9100
and 9282 only from MON01 or the monitoring VPN, then replace the examples under
`../prometheus/targets/` with real private addresses.

## 3. Logs with Grafana Alloy

This section is for application and system log files in Grafana/Loki. Skip it
when the requested "logs" are SIP messages or call flows in HOMER; use
[native SIP capture](#1-native-hep-capture) instead. The presence of this Alloy
template in the repository does not mean Alloy is installed on an existing PBX.

Install the current Grafana Alloy package, copy `alloy/config.alloy` into its
configuration directory, and set `PBX_NAME` and `LOKI_URL` in the Alloy service
environment. The example assumes a private VPN address. For that layout, bind
the central services to MON01's VPN address with `UI_BIND_ADDRESS`. If using an
authenticated TLS reverse proxy instead, add its `basic_auth` block to the Alloy
endpoint. The central Compose stack binds Loki to loopback by default.

`UI_BIND_ADDRESS` also controls Grafana, HOMER UI, Prometheus, Alertmanager,
and Pushgateway; changing it affects all those published interfaces, not just
Loki. It is not part of adding a native HEP sender.

Grant the Alloy service account read access to only the selected log files.
Remove unused paths from `config.alloy` so nonexistent or sensitive logs are not
collected.

## 4. Centralized RTCP quality

Follow [Enable RTCP quality stats on the new node](#enable-rtcp-quality-stats-on-the-new-node)
above for the FS PBX settings, reporter installation, and test call.

The reporter reads parsed RTCP events from loopback-only ESL. FreeSWITCH has
already authenticated and decrypted SRTCP, so no SRTP keys or media payloads
leave the process. Reports go to HOMER over the same HEP 9060 allowlist and
populate the original inbound/A-leg transaction's **QoS** view. The reporter
groups FreeSWITCH B2BUA channels by `Channel-Call-UUID` and uses that A-leg SIP
Call-ID for phone-facing and carrier-facing reports, rather than splitting QoS
between the two SIP dialogs. It preserves cumulative RTCP counters and every
reported `SourceN` block, adds FreeSWITCH's calculated RTT to the stored JSON,
and logs per-direction forwarding and skip counters about once per minute. Read
the complete
[centralized RTP/RTCP procedure](../docs/RTP_QUALITY.md), including its
directional-loss interpretation, ESL credential handling, resource limits, and
RTCP-off/RTCP-on media canary before rollout.

## Per-PBX acceptance checklist

- [ ] FreeSWITCH continues normal call handling when MON01 is unreachable.
- [ ] ESL listens only on `127.0.0.1:8021`.
- [ ] HEP reaches MON01 and appears under the PBX's unique capture ID.
- [ ] TCP 9100 and 9282 accept connections only from MON01/VPN.
- [ ] Prometheus shows both PBX metric targets as healthy.
- [ ] Alloy sends only approved log paths over a private or authenticated route.
- [ ] Firewall changes are documented and source-restricted.
- [ ] RTCP reports appear in the HOMER QoS view under the native HEP capture ID.
- [ ] The RTCP reporter connects only to loopback ESL and has no capabilities.
- [ ] The RTCP media canary passed before enabling additional PBXs.
- [ ] The reporter's actual RSS/CPU remain comfortably below its limits.
- [ ] No PBX credential, log, or packet capture was committed to Git.

For synthetic transaction tests, follow
[`docs/SYNTHETIC_SIP.md`](../docs/SYNTHETIC_SIP.md) on MON01.
