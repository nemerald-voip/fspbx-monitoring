# Centralized RTP/RTCP quality

This procedure sends FreeSWITCH's parsed RTCP quality events to HOMER on MON01.
The PBX-side reporter connects to the local Event Socket Layer (ESL), converts
`RECV_RTCP_MESSAGE` and `SEND_RTCP_MESSAGE` events to HEP type 5 JSON, and
correlates every FreeSWITCH channel in a logical call to one canonical SIP
Call-ID for HOMER's transaction **QoS** tab.

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

## Field semantics and HOMER limits

The reporter preserves the values FreeSWITCH parsed from each RTCP packet:

- `sender_information.packets` and `sender_information.octets` are cumulative
  since the sender started the RTP stream. They should rise over a call; they
  are not five-second interval deltas.
- `report_blocks[].packets_lost` is the signed cumulative loss count from the
  report block. `fraction_lost` is the interval loss fraction on a 0-255 scale.
- `ia_jitter` is expressed in the RTP stream's timestamp units, not necessarily
  milliseconds.
- `lsr` is the middle 32 bits of an NTP timestamp and `dlsr` is in 1/65536
  second units. Neither is a direct RTT measurement.
- For received reports, FreeSWITCH's smoothed `RttN-Avg` is retained as
  `rtt_avg_seconds` and converted to `rtt_avg_ms` in the corresponding report
  block. A zero value means FreeSWITCH had not derived a usable RTT sample.

All valid `Source0`, `Source1`, and later report blocks are retained, and
`report_count` equals the number sent. FreeSWITCH currently supports at most
five blocks. HOMER 11.0.333 stores the complete JSON payload but its QoS graph
reads only `report_blocks[0]`; inspect the RTCP payload through HOMER's message
or export view when later blocks matter. That HOMER version also does not graph
`rtt_avg_ms`, so RTT is available in the stored/exported JSON rather than as a
QoS series. It also requires SR `sender_information` before plotting a record,
so a standards-correct type `201` RR can be stored and exported without
appearing as a graph point.

FreeSWITCH retains the received RTCP packet type internally but does not expose
it in `RECV_RTCP_MESSAGE`. It emits `SEND_RTCP_MESSAGE` only for a locally
generated sender report, so those events are reliably type `200`. For received
events, the reporter honors `RTCP-Packet-Type`, `Packet-Type`, or `RTCP-Type`
when a patched or future FreeSWITCH supplies one. Otherwise it uses type `200`
for HOMER compatibility, sets `type_source` to `compatibility_fallback`, and
counts the event in `received_type_fallbacks`; it does not claim that SR and RR
were distinguished. An explicitly identified RR is sent as type `201` without
SR-only `sender_information`.

## Correlation across FreeSWITCH SIP legs

FreeSWITCH is a back-to-back user agent: an A-leg and its originated B-leg
normally have different SIP Call-IDs even though they belong to one call. HOMER
queries the QoS tab by one exact HEP correlation ID, so sending each media leg
under its own SIP Call-ID splits the quality view between transactions.

The reporter uses FreeSWITCH's `Channel-Call-UUID` to group the channels. It
selects the original inbound leg's SIP Call-ID as the canonical ID and sends
RTCP from every grouped leg under that value. For an unbridged or entirely
outbound call, it uses the first available SIP Call-ID. Open the original
inbound/A-leg SIP transaction in HOMER to see all available media directions;
a separately opened B-leg transaction is not expected to repeat the same QoS
records.

Correlation changes apply only to newly ingested reports. HOMER does not
rewrite historical RTCP records. If the reporter starts in the middle of an
active call before it has observed the inbound channel, its earliest reports
may use the available leg's SIP Call-ID; later reports use the inbound ID once
that channel is observed.

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
2. Open the original inbound/A-leg transaction, then open **QoS** and look for
   every available FreeSWITCH media leg under that one transaction.
3. Inspect report direction before interpreting loss or jitter.
4. Confirm phone-facing SRTCP reports produce plausible values rather than the
   arbitrary values seen when encrypted report blocks are decoded as plaintext.

Native SIP capture and this reporter intentionally share a capture ID. The
reporter sends HEP type 5 only, so it cannot duplicate SIP messages.

The reporter writes a cumulative diagnostic summary to the journal roughly
once per minute. Show only those summaries with:

```bash
journalctl -u freeswitch-rtcp-to-hep.service \
  --since '10 minutes ago' --no-pager | grep 'diagnostics '
```

Interpret the counters as follows:

| Counter | Meaning |
|---|---|
| `rtcp_events`, `recv`, `send` | All RTCP ESL events and their two event classes |
| `hep_sent`, `hep_failed` | HEP messages accepted by the local socket call or rejected with an error |
| `blocks_sent` | Valid report blocks included in successful HEP messages |
| `pbx_to_peer` | Received peer reports describing RTP sent by the PBX |
| `peer_to_pbx` | PBX-generated reports describing RTP received by the PBX |
| `missing_uuid` | Events skipped because no FreeSWITCH channel UUID was present |
| `correlation_fallbacks` | Events sent under a channel UUID because no SIP Call-ID was available |
| `invalid_sender_ssrc`, `invalid_blocks`, `no_valid_blocks` | Malformed or incomplete reports that were partly or wholly skipped |
| `received_type_fallbacks` | Received reports whose SR/RR type FreeSWITCH did not expose |
| `invalid_explicit_types` | Unrecognized explicit packet-type headers |
| `rtt_blocks` | Parsed report blocks containing a usable FreeSWITCH RTT value |

The values are process-lifetime totals and reset when the service restarts.
For UDP, `hep_sent` means the kernel accepted the datagram; it cannot prove
MON01 or HOMER stored it. Compare these totals with HOMER ingest and the HEP
allowlist when diagnosing delivery.

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
If `send` rises but `recv` does not for the carrier-facing channel, FreeSWITCH
is generating its report but no carrier RTCP is reaching that leg. Check the
carrier SDP RTCP address and port, RTCP mux negotiation, firewall/NAT state, and
an authorized bounded capture separately; Call-ID correlation cannot
manufacture a report the carrier did not send.

If the phone-facing and carrier-facing reports are split between the two SIP
Call-IDs, confirm the PBX runs a reporter version with canonical
`Channel-Call-UUID` correlation. After upgrading, place a new call: existing
HOMER records remain under the correlation IDs with which they were ingested.

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

- [FreeSWITCH authenticates/decrypts SRTCP before RTCP processing](https://github.com/signalwire/freeswitch/blob/master/src/switch_rtp.c#L7638-L7671)
- [FreeSWITCH stores received packet type and calculates RTT](https://github.com/signalwire/freeswitch/blob/master/src/switch_rtp.c#L7264-L7405)
- [FreeSWITCH exports every report block and RTT to the received event](https://github.com/signalwire/freeswitch/blob/master/src/switch_core_media.c#L2960-L3030)
- [RFC 3550 sender and receiver report fields](https://www.rfc-editor.org/rfc/rfc3550.html#section-6.4)
- [HOMER RTPEngine example with cumulative counters](https://github.com/sipcapture/homer/wiki/Examples:-RTPEngine)
- [HOMER 11.0.333 QoS parser reads the first report block](https://github.com/sipcapture/homer/blob/11.0.333/src/ui/src/dashboard/QosPanel.tsx#L128-L182)
- [`fire_rtcp_events` enables generated-report events](https://github.com/signalwire/freeswitch/blob/master/src/switch_core_media.c#L8588-L8593)
- [Official FreeSWITCH Event Socket protocol](https://developer.signalwire.com/freeswitch/integration/event-socket/)
- [SIPCAPTURE HEPipe ESL-to-HEP reference implementation](https://github.com/sipcapture/hepipe.js)
