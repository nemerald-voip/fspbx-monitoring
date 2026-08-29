# Centralized RTP/RTCP quality

This procedure sends FreeSWITCH's parsed RTCP quality events to HOMER on MON01.
The PBX-side reporter connects to the local Event Socket Layer (ESL), converts
`RECV_RTCP_MESSAGE` and `SEND_RTCP_MESSAGE` events to HEP type 5 JSON, and
correlates them to the SIP Call-ID for HOMER's transaction **QoS** tab.

The reporter does not capture packets, inspect RTP audio, retain media, or read
SRTP keys. It is outside the call path: if the reporter or MON01 fails, quality
history is incomplete but calls continue normally.

## Why ESL handles SRTCP safely

An interface capture sees SRTCP headers but cannot trust the encrypted report
blocks without the session keys. FreeSWITCH already owns those keys because it
terminates the secure media leg. It authenticates and decrypts received SRTCP,
parses the RTCP report, and then publishes the resulting fields as a
`RECV_RTCP_MESSAGE` event. PBX-generated `SEND_RTCP_MESSAGE` events are created
from FreeSWITCH's own receiver statistics before wire encryption.

This avoids exporting keys and prevents encrypted bytes from being interpreted
as loss, jitter, or MOS inputs. It applies only when FreeSWITCH is in the media
path. Bypass-media calls remain unobservable from this reporter.

## What the reports can prove

A FreeSWITCH bridge has two call legs and two media directions on each leg:

```text
caller/phone <-- A leg --> FreeSWITCH <-- B leg --> carrier/destination
```

RTCP is receiver-reported evidence. A report block describes packets received
by the endpoint that generated the report.

| RTCP reporter | Loss applies to | Likely problem path |
|---|---|---|
| FreeSWITCH/PBX | Media received by FreeSWITCH | remote endpoint/carrier → PBX |
| Remote phone or carrier | Media received by that remote | PBX → remote endpoint/carrier |

Use the SIP ladder and report addresses to identify the A or B leg. Compare
both legs, host interface counters, other calls using the same carrier, and
provider evidence. A report localizes a direction; it does not by itself prove
which network owner caused the loss.

Not every endpoint sends receiver reports. When only FreeSWITCH reports are
available, remote-to-PBX media is measured but PBX-to-remote delivery is not.
Missing peer reports, bypass media, and missing media variables can leave a
direction absent or reduce address detail.

## Design and resource profile

The reporter subscribes only to RTCP and the small set of channel lifecycle
events needed to map FreeSWITCH UUIDs and media addresses to SIP Call-IDs. It
uses Python's standard library and one local TCP connection to
`127.0.0.1:8021`; there are no npm packages, packet sockets, disk queues, or
background workers.

The systemd unit uses a dynamic unprivileged identity, an empty capability
bounding set, a 48 MiB soft memory threshold, 96 MiB hard limit, no swap, a
16-task limit, low CPU weight, and `OOMScoreAdjust=500`. These are ceilings, not
measured steady-state consumption. Confirm actual RSS and CPU on the canary PBX
before cluster rollout.

The ESL password is copied to a root-only file and passed with systemd's
credential mechanism. The non-secret JSON configuration fixes ESL to loopback;
the reporter rejects a non-loopback ESL address. HEP still leaves the PBX for
MON01 on the existing source-restricted port 9060 path.

## 1. Confirm prerequisites on the test PBX

Run:

```bash
fs_cli -x 'switchname'
fs_cli -x 'global_getvar hep_capture_id'
ss -lnt | grep '127.0.0.1:8021'
grep -n 'listen-ip' /etc/freeswitch/autoload_configs/event_socket.conf.xml
```

The capture ID must be a nonzero unsigned 32-bit integer and match this PBX's
native HEP ID. ESL must listen on `127.0.0.1:8021`, never a public or provider
interface.

MON01's provider and Docker-aware host firewalls must allow this PBX source IP
to UDP 9060. TCP is optional. The installer does not change either firewall;
follow [the HEP allowlist procedure](FIREWALL.md).

## 2. Enable RTCP and FreeSWITCH events

For every Sofia profile that carries media, add this setting through the FS PBX
profile editor:

```xml
<param name="rtcp-audio-interval-msec" value="5000"/>
```

Then set this channel variable early in the applicable dialplan, before media
is activated, and export it to the channel's originated leg:

```xml
<action application="export" data="fire_rtcp_events=true"/>
```

The RTCP interval makes FreeSWITCH send receiver reports. Received peer reports
produce `RECV_RTCP_MESSAGE`; `fire_rtcp_events=true` additionally publishes the
PBX-generated report as `SEND_RTCP_MESSAGE`. Apply both settings to every call
leg whose two directions need reporting.

Use UI-managed settings so regeneration does not erase them. A Sofia profile
restart can affect active calls and registrations, so schedule it in a
maintenance window. The reporter installer never restarts FreeSWITCH.

Verify the generated configuration:

```bash
grep -RIn 'rtcp-audio-interval-msec' /etc/freeswitch/sip_profiles
grep -RIn 'fire_rtcp_events' /etc/freeswitch/dialplan
```

During a test call, use `show channels` to obtain each UUID and confirm the
variable is true:

```bash
fs_cli -x 'show channels'
fs_cli -x 'uuid_getvar CALL_UUID fire_rtcp_events'
```

## 3. Back up and install

The existing packet-capture sensor can be restored without changing HOMER
data. Before replacing it, preserve its local configuration if present:

```bash
sudo install -d -m 0700 /root/rtcp-sensor-backup
sudo cp -a /etc/heplify-rtcp /root/rtcp-sensor-backup/ 2>/dev/null || true
sudo cp -a /etc/systemd/system/heplify-rtcp.service \
  /root/rtcp-sensor-backup/ 2>/dev/null || true
```

Download and review the installer, then use a release tag for production when
one exists:

```bash
curl -fsSLo /tmp/install-freeswitch-rtcp-to-hep.sh \
  https://raw.githubusercontent.com/nemerald-voip/fspbx-monitoring/main/pbx-agent/rtcp-quality/install.sh
less /tmp/install-freeswitch-rtcp-to-hep.sh
sudo sh /tmp/install-freeswitch-rtcp-to-hep.sh \
  --monitoring-ip 212.56.38.50
```

The installer detects the switch name, native HEP capture ID, and ESL password.
If the password uses an unsupported generated configuration layout, provide a
root-readable one-line file instead of putting the secret on the command line:

```bash
sudo sh /tmp/install-freeswitch-rtcp-to-hep.sh \
  --monitoring-ip 212.56.38.50 \
  --node-name tx01 \
  --capture-id 3732434656 \
  --esl-password-file /root/esl-password
```

The installer creates `/etc/freeswitch-rtcp-to-hep` with mode `0755` and renders
its non-secret `config.json` with mode `0644`. The ESL credential remains
`root:root` mode `0600` at `esl-password` and is exposed to the dynamic service
identity only through `LoadCredential`. The installer then enables
`freeswitch-rtcp-to-hep.service`. During an upgrade it stops the old
`heplify-rtcp.service`; if the new unit fails to start, it attempts to restart
the old unit. It leaves the old binary and configuration in place for rollback.

## 4. Verify live reporting and load

Check the PBX:

```bash
systemctl is-enabled freeswitch-rtcp-to-hep.service
systemctl is-active freeswitch-rtcp-to-hep.service
systemctl status freeswitch-rtcp-to-hep.service --no-pager
journalctl -u freeswitch-rtcp-to-hep.service -n 100 --no-pager
systemctl show freeswitch-rtcp-to-hep.service \
  -p DynamicUser -p CPUWeight -p MemoryCurrent -p MemoryPeak \
  -p MemoryHigh -p MemoryMax -p MemorySwapMax -p TasksCurrent -p TasksMax \
  -p OOMScoreAdjust
```

The journal should say it connected to local ESL and subscribed to RTCP events.
It never logs event payloads or the ESL password. Place a test call lasting at
least 15 seconds, then confirm outbound HEP metadata without printing media:

```bash
sudo tcpdump -n -q -i any \
  'udp dst host 212.56.38.50 and dst port 9060'
```

In HOMER:

1. Find the test call and open its transaction.
2. Open **QoS** and look for correlated RTCP reports.
3. Inspect report direction before interpreting loss or jitter.
4. Confirm phone-facing SRTCP reports produce plausible values rather than the
   arbitrary values seen when encrypted report blocks are decoded as plaintext.

Native SIP capture and this reporter intentionally share a capture ID. The
reporter sends HEP type 5 only, so it cannot duplicate SIP messages.

## 5. Canary-test FreeSWITCH RTCP audio behavior

The completed A/B canary found no repeating marker packets, sequence loss, or
RTCP-aligned timestamp discontinuities when RTCP was enabled. Retain the helper
for a clean final pair or future FreeSWITCH upgrades.

Capture only controlled calls. The helper writes a bounded, sensitive capture
under RAM-backed `/run` and never runs automatically:

```bash
sudo /usr/local/sbin/rtcp-canary-capture \
  --interface eth0 \
  --rtp-start 16384 --rtp-end 32768 \
  --duration 60 \
  --output /run/rtcp-canary-on.pcap
```

It stops after 60 seconds or 25,000 packets and retains at most 256 bytes per
frame. Delete it immediately after authorized analysis:

```bash
sudo rm -- /run/rtcp-canary-on.pcap
```

For future A/B tests, use the same caller, destination, carrier, codec, and
continuous audio. Compare RTP sequence errors, timestamp progression, markers,
jitter, loss, and remote recordings between RTCP-off and RTCP-on calls. Stop a
rollout if a new audible or packet-level defect follows the RTCP cadence.

## 6. Cluster rollout

Install and verify one test server first. Then repeat on every physical PBX:

1. Confirm its unique live `hep_capture_id`.
2. Confirm ESL is loopback-only and the password source is local and protected.
3. Enable RTCP and `fire_rtcp_events` on the required legs.
4. Ensure MON01 allows that PBX source IP to HEP 9060.
5. Install the reporter and place a test call through that node.
6. Record actual service RSS/CPU and verify call processing is unchanged.

Each reporter is independent. No local state is replicated, and loss of one
reporter does not affect the other node or calls.

## Troubleshooting and rollback

If QoS is empty, check in this order:

```bash
grep -RIn 'rtcp-audio-interval-msec' /etc/freeswitch/sip_profiles
grep -RIn 'fire_rtcp_events' /etc/freeswitch/dialplan
ss -lnt | grep '127.0.0.1:8021'
journalctl -u freeswitch-rtcp-to-hep.service --since '10 minutes ago' --no-pager
```

An ESL authentication error means the root-only credential does not match
`event_socket.conf.xml`. A connected reporter with no RTCP events usually means
RTCP is not active on that leg or no peer report arrived. `RECV_RTCP_MESSAGE`
can still cover peer-generated reports without `fire_rtcp_events`; missing
`SEND_RTCP_MESSAGE` specifically points to the channel variable or its timing.

Stop the reporter without affecting FreeSWITCH:

```bash
sudo systemctl disable --now freeswitch-rtcp-to-hep.service
```

To roll back temporarily, stop the ESL reporter and re-enable the preserved
packet sensor:

```bash
sudo systemctl disable --now freeswitch-rtcp-to-hep.service
sudo systemctl enable --now heplify-rtcp.service
```

The old sensor cannot decode SRTCP report bodies and should not be used for
phone-facing secure legs. Neither stop nor rollback command deletes HOMER
history.

## Implementation references

- [FreeSWITCH authenticates/decrypts SRTCP before RTCP processing](https://github.com/signalwire/freeswitch/blob/master/src/switch_rtp.c#L7028-L7094)
- [FreeSWITCH publishes parsed received-report fields](https://github.com/signalwire/freeswitch/blob/master/src/switch_core_media.c#L2732-L2794)
- [`fire_rtcp_events` enables generated-report events](https://github.com/signalwire/freeswitch/blob/master/src/switch_core_media.c#L7918-L7924)
- [Official FreeSWITCH Event Socket protocol](https://developer.signalwire.com/freeswitch/integration/event-socket/)
- [SIPCAPTURE HEPipe ESL-to-HEP reference implementation](https://github.com/sipcapture/hepipe.js)
