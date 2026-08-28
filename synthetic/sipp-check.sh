#!/usr/bin/env sh
set -u

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
: "${SIP_TARGET:?Set SIP_TARGET to a hostname or IP}"
: "${PUSHGATEWAY_URL:=http://127.0.0.1:9091}"
: "${PBX_NAME:=$SIP_TARGET}"
: "${SIP_PORT:=5060}"
: "${SIP_TRANSPORT:=udp}"
: "${SIP_TIMEOUT_SECONDS:=15}"

command -v sipp >/dev/null 2>&1 || {
  echo "sipp is required" >&2
  exit 2
}
command -v curl >/dev/null 2>&1 || {
  echo "curl is required" >&2
  exit 2
}

case "$PBX_NAME" in
  *[!A-Za-z0-9_.-]*) echo "PBX_NAME contains an unsupported character" >&2; exit 2 ;;
esac
case "$SIP_PORT:$SIP_TIMEOUT_SECONDS" in
  *[!0-9:]*) echo "SIP_PORT and SIP_TIMEOUT_SECONDS must be integers" >&2; exit 2 ;;
esac
if [ "$SIP_PORT" -lt 1 ] || [ "$SIP_PORT" -gt 65535 ] || [ "$SIP_TIMEOUT_SECONDS" -lt 1 ]; then
  echo "SIP port or timeout is outside the accepted range" >&2
  exit 2
fi

case "$SIP_TRANSPORT" in
  udp) sipp_transport=u1 ;;
  tcp) sipp_transport=t1 ;;
  tls) sipp_transport=l1 ;;
  *) echo "SIP_TRANSPORT must be udp, tcp, or tls" >&2; exit 2 ;;
esac

start=$(date +%s)
sipp "$SIP_TARGET:$SIP_PORT" \
  -sf "$script_dir/options.xml" \
  -s monitor \
  -t "$sipp_transport" \
  -m 1 \
  -timeout "$(($SIP_TIMEOUT_SECONDS * 1000))" \
  -nostdin \
  -trace_err
result=$?
end=$(date +%s)
duration=$((end - start))

if [ "$result" -eq 0 ]; then
  success=1
else
  success=0
fi

payload=$(printf '%s\n' \
  '# TYPE synthetic_sip_success gauge' \
  "synthetic_sip_success{instance=\"$PBX_NAME\",transport=\"$SIP_TRANSPORT\"} $success" \
  '# TYPE synthetic_sip_duration_seconds gauge' \
  "synthetic_sip_duration_seconds{instance=\"$PBX_NAME\"} $duration" \
  '# TYPE synthetic_sip_last_run_timestamp_seconds gauge' \
  "synthetic_sip_last_run_timestamp_seconds{instance=\"$PBX_NAME\"} $end")

printf '%s\n' "$payload" | curl --fail --silent --show-error \
  --data-binary @- \
  "$PUSHGATEWAY_URL/metrics/job/synthetic-sip/instance/$PBX_NAME"

exit "$result"
