# PBX-side agent setup

Install these pieces on every FreeSWITCH host. The monitoring server must never
be required for normal call processing; HEP, logs, and metrics are best-effort
outbound telemetry.

## 1. Native HEP capture

Copy the settings from
`freeswitch/capture-server.xml.snippet` into every relevant Sofia profile. Set
`MONITORING_IP` to the monitoring server's private/VPN address and assign a
different numeric `capture_id` to each PBX.

Reload the profile or restart FreeSWITCH, then verify:

```bash
fs_cli -x "sofia global capture on"
tcpdump -ni any udp port 9060
```

Allow PBX source addresses to reach MON01 on UDP 9060. TCP 9060 is supported as
an alternative. Do not expose the HOMER UI publicly just to ingest HEP.

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

## 4. Bounded RTP header capture (optional)

This captures only the configured RTP UDP range, truncates every packet to 128
bytes, and enforces a fixed-size ring. It does not forward RTP to MON01.

```bash
apt install wireshark-common
install -d -m 0750 /var/capture
install -m 0755 rtp-capture/rtp-capture.sh /usr/local/sbin/fs-rtp-capture
install -m 0644 rtp-capture/rtp-capture.service /etc/systemd/system/
install -m 0600 rtp-capture/rtp-capture.env.example /etc/default/fs-rtp-capture
systemctl daemon-reload
systemctl enable --now rtp-capture
```

Confirm the interface and RTP port range before starting. The default cap is 8
GiB (`262144 KiB x 32`), with oldest files overwritten. Prefer a dedicated
`/var/capture` filesystem. Header-only capture preserves addressing, sequence,
timestamp, SSRC, timing, jitter, and loss analysis but not playable audio.

Check capture drops and disk use regularly:

```bash
systemctl status rtp-capture
journalctl -u rtp-capture
du -sh /var/capture
```
