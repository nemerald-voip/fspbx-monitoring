# Centralized RTP/RTCP quality

This procedure sends live, decoded RTCP quality reports from each FreeSWITCH
host to HOMER on MON01. It does not forward RTP audio, create packet captures,
or keep a disk buffer on the PBX. HOMER stores the reports in its existing
named volume and correlates them to SIP Call-IDs for the transaction **QoS** tab.

The sensor is passive and outside the call path. If the sensor or MON01 fails,
quality history is incomplete but calls continue normally.

## What the reports can prove

A FreeSWITCH bridge has two call legs and two media directions on each leg:

```text
caller/phone <-- A leg --> FreeSWITCH <-- B leg --> carrier/destination
```

RTCP is receiver-reported evidence. The source address of an RTCP report tells
you which endpoint made the measurement; its report block describes packets
that endpoint received.

| RTCP reporter | Loss applies to | Likely problem path |
|---|---|---|
| FreeSWITCH/PBX | Media received by FreeSWITCH | remote endpoint/carrier → PBX |
| Remote phone or carrier | Media received by that remote | PBX → remote endpoint/carrier |

Use the SIP ladder and the RTCP source/destination addresses to identify the A
or B leg. Compare reports from both ends, host interface counters, other calls
using the same carrier, and provider evidence. A report localizes the affected
direction and path; it does not alone prove which network owner caused loss.

Not every endpoint sends RTCP receiver reports. When only FreeSWITCH reports,
you can measure traffic arriving at the PBX but cannot directly measure what the
remote endpoint received from it. Encrypted SRTCP, bypass-media calls, missing
or incorrect SDP addresses, and NAT rewriting can also limit correlation.

## Design

The pinned heplify sensor uses two live capture sockets on the PBX:

1. `sip-correlation-only` reads SIP/SDP and builds the in-memory RTP/Call-ID
   map. It has no configured HEP transport, so native FreeSWITCH HEP remains the
   only SIP copy sent to HOMER. Native capture continues to cover SIP over TLS.
2. `rtcp-to-homer` reads only RTCP packets, converts them to HEP protocol type
   5 JSON, attaches the correlated Call-ID, and sends them to MON01 port 9060.

Each socket has its own explicit kernel `bpf_filter`. The SIP socket can receive
only the configured Sofia port range. The RTCP socket requires an IPv4 UDP
packet in the configured RTP range with RTP version 2 and an RTCP packet type
from 200 through 223; ordinary RTP does not reach its userspace decoder.
An `ExecStartPost` check requires two `BPF filter applied` journal messages from
the current systemd invocation. A compilation or installation failure makes the
unit fail instead of continuing unfiltered.

The service disables PCAP output, disk HEP buffering, its HTTP API, and its
Prometheus listener. It logs operational messages only to the systemd journal.
The same unique capture ID used by native FreeSWITCH HEP is reused for RTCP.
It runs at low CPU weight with a 384 MiB soft memory threshold, 512 MiB hard
limit, no swap allowance, 128-task limit, and `OOMScoreAdjust=500`, so the
monitoring sensor yields resources before call processing under pressure.

## 1. Confirm prerequisites on the test PBX

Run:

```bash
fs_cli -x 'switchname'
fs_cli -x 'global_getvar hep_capture_id'
fs_cli -x 'sofia status' | grep -E 'profile.*RUNNING'
grep -nE 'rtp-(start|end)-port' \
  /etc/freeswitch/autoload_configs/switch.conf.xml
ip route get 212.56.38.50
```

The capture ID must be a nonzero unsigned 32-bit integer and must match this
physical PBX's native HEP ID. The test host shown in this deployment uses the
FreeSWITCH default RTP range `16384-32768` because both settings are commented.

MON01's provider and Docker-aware host firewalls must allow this PBX source IP
to UDP 9060. The installer does not change either firewall. Follow
[the HEP allowlist procedure](FIREWALL.md) before starting a new PBX sensor.

## 2. Enable RTCP on the Sofia profiles

On current FS PBX systems, add this setting through the FS PBX Sofia profile
editor for every profile that carries media:

```xml
<param name="rtcp-audio-interval-msec" value="5000"/>
```

Use the UI-managed setting so regeneration does not erase it. Apply the FS PBX
change procedure to each physical server separately. A Sofia profile restart
can affect active calls and registrations, so schedule it in a maintenance
window; the sensor installer itself never restarts FreeSWITCH.

Verify the generated configuration after the UI change:

```bash
grep -RIn 'rtcp-audio-interval-msec' /etc/freeswitch/sip_profiles
```

The value causes FreeSWITCH to send RTCP at a five-second interval and to
produce receiver reports for media it receives. Remote-side measurements still
depend on the phone, SBC, or carrier sending its own RTCP reports.

## 3. Install the sensor

After this repository change is pushed, the test server can auto-detect its
interface, switch name, capture ID, running Sofia port range, and RTP defaults:

```bash
curl -fsSL \
  https://raw.githubusercontent.com/nemerald-voip/fspbx-monitoring/main/pbx-agent/rtcp-quality/install.sh | \
  sudo sh -s -- --monitoring-ip 212.56.38.50
```

For production, download and review the installer first and use a release tag
once one exists:

```bash
curl -fsSLo /tmp/install-heplify-rtcp.sh \
  https://raw.githubusercontent.com/nemerald-voip/fspbx-monitoring/main/pbx-agent/rtcp-quality/install.sh
less /tmp/install-heplify-rtcp.sh
sudo sh /tmp/install-heplify-rtcp.sh --monitoring-ip 212.56.38.50
```

Override auto-detection when necessary. For the port layout shown on `tx01`:

```bash
sudo sh /tmp/install-heplify-rtcp.sh \
  --monitoring-ip 212.56.38.50 \
  --interface eth0 \
  --node-name tx01 \
  --capture-id 3732434656 \
  --sip-start 35000 --sip-end 36005 \
  --rtp-start 16384 --rtp-end 32768
```

The installer downloads heplify `2.0.27` for amd64 or arm64, verifies the
release SHA-256 digest, installs a least-privilege systemd service, renders
`/etc/heplify-rtcp/heplify.json`, and enables the service after reboot. It does
not install `dumpcap`, write under `/var/capture`, or modify FreeSWITCH.

## 4. Verify live reporting

Check the PBX:

```bash
systemctl is-enabled heplify-rtcp.service
systemctl is-active heplify-rtcp.service
systemctl status heplify-rtcp.service --no-pager
journalctl -u heplify-rtcp.service -n 100 --no-pager
grep -E '"(write_file|enable)"' /etc/heplify-rtcp/heplify.json
systemctl show heplify-rtcp.service \
  -p CPUWeight -p MemoryHigh -p MemoryMax -p MemorySwapMax \
  -p TasksMax -p OOMScoreAdjust
```

The journal must contain both the helper confirmation and two upstream filter
messages:

```bash
journalctl -u heplify-rtcp.service -b --no-pager | \
  grep -E 'BPF filter applied|verified both heplify kernel BPF filters|Failed to set BPF'
```

Place a test call lasting at least 15 seconds. Confirm outbound HEP without
printing SIP or media payloads:

```bash
sudo tcpdump -n -q -i any \
  'udp dst host 212.56.38.50 and dst port 9060'
```

In HOMER:

1. Find the test call and open its transaction.
2. Open **QoS** and look for correlated RTCP reports.
3. Inspect each report's source and destination IP before interpreting loss.
4. Compare reports on both call legs and both directions when present.

Native SIP capture and the RTCP sensor intentionally share a capture ID. There
should not be a second copy of each SIP message from heplify.

## 5. Canary-test FreeSWITCH RTCP audio behavior

Before enabling RTCP across a cluster, run a controlled A/B test on one
non-production call path. An open FreeSWITCH 1.10.3 report described periodic
PCMA silence and marker/timestamp anomalies aligned with the configured RTCP
interval. It is not evidence that FreeSWITCH 1.11.1 has the same behavior, but
the failure would be media-affecting and warrants a canary.

Use the same caller, destination, carrier, codec, and approximately 45-second
continuous tone or speech sample for both calls. A continuous tone makes a
five-second cadence easier to hear. Record at the remote endpoint when possible
so the observation includes what actually left the PBX.

### Baseline with RTCP disabled

Before adding `rtcp-audio-interval-msec`, capture one call in RAM-backed
`/run`. The helper is installed with the sensor templates but does not run
automatically:

```bash
sudo /usr/local/sbin/rtcp-canary-capture \
  --interface eth0 \
  --rtp-start 16384 --rtp-end 32768 \
  --duration 60 \
  --output /run/rtcp-canary-off.pcap
```

The command stops after 60 seconds or 25,000 packets and retains at most 256
bytes per frame. That is enough to include common G.711 payloads, so the file is
sensitive even though it is temporary. `/run` does not survive reboot.

### Repeat with RTCP enabled

Enable the five-second RTCP setting on only the Sofia profile used by the test
route, apply it during a maintenance window, and verify the generated profile.
Then make the otherwise identical call:

```bash
sudo /usr/local/sbin/rtcp-canary-capture \
  --interface eth0 \
  --rtp-start 16384 --rtp-end 32768 \
  --duration 60 \
  --output /run/rtcp-canary-on.pcap
```

Do not run both captures at once. Keep other calls off the test PBX if possible
so the comparison contains only the canary.

### Compare the two calls

Copy the two files over SSH to an authorized workstation and open them in
Wireshark. If Wireshark does not infer RTP, select one stream and use
**Analyze → Decode As… → RTP**. For each capture:

1. Open **Telephony → RTP → RTP Streams**, select each direction, and choose
   **Analyze**.
2. Compare sequence errors, timestamp progression, marker counts, jitter, and
   lost packets between RTCP-off and RTCP-on calls.
3. Use display filter `rtp.marker == 1` and check for a new repeating pair of
   marker packets every five seconds.
4. Inspect packets around each `rtcp` packet. A reproduction resembles the
   reported pattern: normal incoming media, followed by PBX-originated marker
   packets and a silence payload with an abnormal timestamp at the RTCP cadence.
5. Compare the remote recording for new clicks, gaps, or silence at the same
   cadence.

Pass the canary only when the RTCP-on call has no new audible cadence, marker
pair, inserted silence packet, or timestamp discontinuity relative to the
baseline. If the anomaly appears only on packets leaving FreeSWITCH while the
corresponding inbound stream remains continuous, stop rollout and preserve the
small captures for a FreeSWITCH defect report.

After recording the result, delete both sensitive captures from the PBX and
the workstation:

```bash
sudo rm -- /run/rtcp-canary-off.pcap /run/rtcp-canary-on.pcap
```

## 6. Cluster rollout

Install and verify one test server first. Then repeat on every physical server:

1. Confirm its unique live `hep_capture_id`.
2. Confirm its interface, Sofia ports, and RTP range independently.
3. Enable RTCP on that node's active Sofia profiles.
4. Ensure MON01 allows that node's source IP to HEP 9060.
5. Run the installer locally and place a test call through that node.

Each sensor is independent. No local capture state is replicated between
redundant PBXs, and loss of one sensor does not affect the other node or calls.

## Troubleshooting

No QoS rows usually means no RTCP is present or it could not be correlated.
Check in this order:

```bash
grep -RIn 'rtcp-audio-interval-msec' /etc/freeswitch/sip_profiles
sudo tcpdump -n -q -i eth0 'udp portrange 16384-32768'
journalctl -u heplify-rtcp.service --since '10 minutes ago' --no-pager
```

If RTP is visible but RTCP is not, confirm the active Sofia profiles loaded the
RTCP interval and that the peer supports RTCP. If RTCP is visible but HOMER has
no QoS row, confirm the sensor also sees the unencrypted SIP/SDP for that leg,
the SDP contains usable media addresses, and HEP reaches MON01.

Stop the sensor without affecting FreeSWITCH:

```bash
sudo systemctl disable --now heplify-rtcp.service
```

This stops new quality telemetry only; it does not delete HOMER history.
