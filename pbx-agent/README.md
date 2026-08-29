# PBX-side agent setup

Install these pieces on every FreeSWITCH host. The monitoring server must never
be required for normal call processing; HEP, logs, and metrics are best-effort
outbound telemetry.

Before changing a PBX, complete the central
[new-server checklist](../docs/GETTING_STARTED.md), choose private/VPN addresses,
and record these per-node values:

| Value | Requirement |
|---|---|
| PBX name | Stable label used consistently in Grafana and Prometheus |
| HEP capture ID | Unique numeric ID for this PBX |
| MON01 address | Private/VPN address whenever possible |
| ESL password | Long random local secret; never stored in this repository |
| SIP and RTP ranges | Exact ranges used by this FreeSWITCH configuration |

Apply one telemetry layer at a time and confirm that disabling it does not alter
call processing.

## 1. Native HEP capture

### Configure current FS PBX systems

In FS PBX, open **System Settings -> SIP Capture**.

1. Enable SIP capture.
2. Select UDP unless the monitoring network requires TCP.
3. Enter the reachable private/VPN address of the HOMER monitoring server.
4. Keep port `9060` unless HOMER uses a different HEP port.
5. Select the Sofia profiles to capture, normally `internal` and `external`.
6. Confirm that **Current server** shows the expected FreeSWITCH hostname, then
   save.

FS PBX automatically:

- identifies the current physical server using its FreeSWITCH hostname;
- creates a hostname-scoped `hep_capture_id` switch variable;
- generates and persists a random, nonzero unsigned 32-bit capture ID scoped to
  the current server's hostname;
- stores the collector with `capture_id=$${hep_capture_id}`;
- rebuilds the local `vars.xml`;
- sets the live FreeSWITCH global variable;
- regenerates the local Sofia configuration; and
- rescans the selected profiles and enables capture without restarting them.

HEP capture IDs are unsigned numeric identifiers. They identify physical
FreeSWITCH servers in HOMER, not logical PBX systems. The monitoring server
remains outside the live call path: losing HOMER or HEP connectivity does not
interrupt calls or registrations.

### Redundant FS PBX servers

Configure the same collector on every server in the redundant system. Save the
first server, allow its switch-variable database row to replicate, and then
open and save **System Settings -> SIP Capture** on the next server. Repeat this
sequence for every node.

Each physical FreeSWITCH server must have its own capture ID, even when several
servers form one redundant PBX system. Saving on each server is required because
`vars.xml`, the live global variable, the generated Sofia cache, and the profile
rescan are local to that server. Before saving each node, confirm that
**Current server** displays its expected FreeSWITCH hostname.

### Verify the PBX configuration

Check the current server identity and live capture ID:

```bash
fs_cli -x 'switchname'
fs_cli -x 'global_getvar hep_capture_id'
```

Check the persistent variable and confirm that the selected profiles remain
running:

```bash
grep 'hep_capture_id' /etc/freeswitch/vars.xml
fs_cli -x 'sofia status' | grep -E 'internal|external'
```

Confirm that the PBX emits HEP traffic, replacing `MONITORING_IP` first:

```bash
sudo tcpdump -n -q -i any \
  'udp dst host MONITORING_IP and dst port 9060'
```

Use `tcp` instead of `udp` in the filter if TCP was selected. Place or receive a
test call, then run a corresponding short capture on MON01 and confirm that the
call appears in HOMER under the expected capture ID. This distinguishes local
HEP generation from routing, firewall, and HOMER ingestion success.

Using `127.0.0.1` as the collector is useful only for confirming local HEP
generation. It does not test network routing, firewalls, MON01, or HOMER
ingestion. Do not print or publish complete generated Sofia XML because it can
contain SIP gateway credentials.

Allow PBX source addresses to reach MON01 on UDP 9060. TCP 9060 is supported as
an alternative. Do not expose the HOMER UI publicly just to ingest HEP.

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

Install the current Grafana Alloy package, copy `alloy/config.alloy` into its
configuration directory, and set `PBX_NAME` and `LOKI_URL` in the Alloy service
environment. The example assumes a private VPN address. For that layout, bind
the central services to MON01's VPN address with `UI_BIND_ADDRESS`. If using an
authenticated TLS reverse proxy instead, add its `basic_auth` block to the Alloy
endpoint. The central Compose stack binds Loki to loopback by default.

Grant the Alloy service account read access to only the selected log files.
Remove unused paths from `config.alloy` so nonexistent or sensitive logs are not
collected.

## 4. Centralized RTCP quality

Enable `rtcp-audio-interval-msec=5000` through the FS PBX Sofia profile editor
on every profile that carries media. Applying a profile change can affect calls
and registrations, so follow the platform's maintenance procedure.

Then install the live sensor on each physical PBX:

```bash
sudo ./rtcp-quality/install.sh --monitoring-ip MON01_IP
```

The sensor uses SIP/SDP in memory to correlate RTCP but does not forward SIP,
RTP payloads, or packet captures. Decoded RTCP reports go to HOMER over the same
HEP 9060 allowlist and populate the transaction **QoS** view. Read the complete
[centralized RTP/RTCP procedure](../docs/RTP_QUALITY.md), including its
directional-loss interpretation, fail-closed BPF checks, resource limits, and
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
- [ ] Both kernel BPF filters were confirmed for the current service invocation.
- [ ] The RTCP media canary passed before enabling additional PBXs.
- [ ] The sensor has PCAP writing and disk HEP buffering disabled.
- [ ] No PBX credential, log, or packet capture was committed to Git.

For synthetic transaction tests, follow
[`docs/SYNTHETIC_SIP.md`](../docs/SYNTHETIC_SIP.md) on MON01.
