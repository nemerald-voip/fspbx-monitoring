# Synthetic SIP checks

The repository includes a SIPp OPTIONS scenario that measures whether a target
can complete a SIP transaction. A systemd timer runs one instance per PBX, and
the script publishes success, duration, and last-run metrics to Pushgateway.

An OPTIONS response is not a complete call test. It does not prove authenticated
registration, dialplan execution, media setup, two-way RTP, audio quality, or
PSTN routing. Add a controlled authenticated INVITE scenario only when test
credentials, destinations, media behavior, and call cleanup have been designed
for the environment.

## Prerequisites

- the central Compose stack is running;
- Pushgateway is reachable at `127.0.0.1:9091` on MON01;
- SIPp is installed on MON01;
- the target PBX permits the chosen SIP transport and source;
- the test cannot affect real customers or trigger billable/unwanted calls.

Install SIPp using the package appropriate for the supported Debian release,
then confirm:

```bash
sipp -v
```

## Install the timer templates

From the repository checkout on MON01:

```bash
cd /opt/monitoring
install -d -m 0750 /etc/fs-monitoring
install -m 0644 synthetic/synthetic-sip@.service /etc/systemd/system/
install -m 0644 synthetic/synthetic-sip@.timer /etc/systemd/system/
systemctl daemon-reload
```

The service runs `/opt/monitoring/synthetic/sipp-check.sh`. If the repository is
installed elsewhere, update the service path deliberately.

## Configure one PBX

Use a short instance name containing only safe systemd-instance characters,
such as `pbx1`:

```bash
install -m 0640 \
  /opt/monitoring/synthetic/synthetic-sip.env.example \
  /etc/fs-monitoring/synthetic-sip-pbx1.env
sudoedit /etc/fs-monitoring/synthetic-sip-pbx1.env
```

Set:

```dotenv
SIP_TARGET=pbx1.example.com
SIP_PORT=5060
SIP_TRANSPORT=udp
SIP_TIMEOUT_SECONDS=15
PBX_NAME=pbx1
PUSHGATEWAY_URL=http://127.0.0.1:9091
```

Keep one environment file per instance:

```text
/etc/fs-monitoring/synthetic-sip-pbx1.env
/etc/fs-monitoring/synthetic-sip-pbx2.env
```

## Run and enable

Test once before enabling the timer:

```bash
systemctl start synthetic-sip@pbx1.service
systemctl status synthetic-sip@pbx1.service --no-pager
journalctl -u synthetic-sip@pbx1.service -n 100 --no-pager
```

Then enable the schedule:

```bash
systemctl enable --now synthetic-sip@pbx1.timer
systemctl list-timers 'synthetic-sip@*'
```

Repeat for every PBX using a unique instance and `PBX_NAME`.

## Validate metrics and alerts

The script publishes these metrics:

- `synthetic_sip_success`
- `synthetic_sip_duration_seconds`
- `synthetic_sip_last_run_timestamp_seconds`

Inspect Pushgateway locally:

```bash
curl --fail --silent http://127.0.0.1:9091/metrics | \
  grep 'synthetic_sip_'
```

Prometheus scrapes Pushgateway and evaluates:

- `SyntheticSIPCallFailed` after a failed result persists for two minutes;
- `SyntheticSIPCallStale` when no result has arrived for more than ten minutes.

Confirm both success and controlled failure behavior before considering the
check operational. A timer that never ran can look different from a timer whose
last run failed.

## Troubleshooting

```bash
systemctl status synthetic-sip@pbx1.timer --no-pager
systemctl status synthetic-sip@pbx1.service --no-pager
journalctl -u synthetic-sip@pbx1.service -b --no-pager
systemctl cat synthetic-sip@pbx1.service
```

Check DNS, transport, port, PBX access control, and whether another security
layer expects TLS or authentication. Use short, authorized captures only when
needed and never commit them.

To stop one check without deleting its configuration:

```bash
systemctl disable --now synthetic-sip@pbx1.timer
```
