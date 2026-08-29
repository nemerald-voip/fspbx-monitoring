#!/usr/bin/env sh
set -eu

capture_interface=
rtp_start=16384
rtp_end=32768
duration=60
packet_limit=25000
output=

usage() {
  cat <<'EOF'
Usage: canary-capture.sh --interface NAME [options]

Capture one short, bounded RTP/RTCP canary in /run for an RTCP on/off A/B test.
The capture includes media payload and is sensitive. It is never persistent
across reboot and must be deleted manually as soon as analysis is complete.

Options:
  --interface NAME    PBX media interface (required)
  --rtp-start PORT    First FreeSWITCH RTP port (default: 16384)
  --rtp-end PORT      Last FreeSWITCH RTP port (default: 32768)
  --duration SECONDS  Maximum capture time (default: 60; maximum: 180)
  --packets COUNT     Maximum packets (default: 25000; maximum: 100000)
  --output PATH       New file below /run (default: timestamped .pcap)
  -h, --help          Show this help
EOF
}

die() {
  echo "ERROR: $*" >&2
  exit 1
}

is_uint() {
  case "$1" in
    ''|*[!0-9]*) return 1 ;;
    *) return 0 ;;
  esac
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --interface)
      [ "$#" -ge 2 ] || die "--interface requires a value"
      capture_interface=$2
      shift 2
      ;;
    --rtp-start)
      [ "$#" -ge 2 ] || die "--rtp-start requires a value"
      rtp_start=$2
      shift 2
      ;;
    --rtp-end)
      [ "$#" -ge 2 ] || die "--rtp-end requires a value"
      rtp_end=$2
      shift 2
      ;;
    --duration)
      [ "$#" -ge 2 ] || die "--duration requires a value"
      duration=$2
      shift 2
      ;;
    --packets)
      [ "$#" -ge 2 ] || die "--packets requires a value"
      packet_limit=$2
      shift 2
      ;;
    --output)
      [ "$#" -ge 2 ] || die "--output requires a value"
      output=$2
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *) die "unknown option: $1" ;;
  esac
done

[ "$(id -u)" -eq 0 ] || die "run as root (for example, with sudo)"
[ -n "$capture_interface" ] || die "--interface is required"
case "$capture_interface" in
  *[!A-Za-z0-9_.:@-]*) die "interface contains unsupported characters" ;;
esac
for value in "$rtp_start" "$rtp_end" "$duration" "$packet_limit"; do
  is_uint "$value" || die "numeric setting is invalid: $value"
done
[ "$rtp_start" -ge 1024 ] && [ "$rtp_end" -le 65535 ] && \
  [ "$rtp_start" -lt "$rtp_end" ] || die "invalid RTP range"
[ "$duration" -ge 10 ] && [ "$duration" -le 180 ] || \
  die "duration must be between 10 and 180 seconds"
[ "$packet_limit" -ge 1000 ] && [ "$packet_limit" -le 100000 ] || \
  die "packet count must be between 1000 and 100000"

command -v ip >/dev/null 2>&1 || die "the ip command is required"
command -v tcpdump >/dev/null 2>&1 || die "tcpdump is required"
command -v timeout >/dev/null 2>&1 || die "timeout is required"
ip link show dev "$capture_interface" >/dev/null 2>&1 || \
  die "interface does not exist: $capture_interface"

if [ -z "$output" ]; then
  output=/run/rtcp-canary-$(date -u +%Y%m%dT%H%M%SZ).pcap
fi
case "$output" in
  /run/*.pcap)
    output_name=${output#/run/}
    case "$output_name" in
      */*) die "--output must be directly below /run" ;;
      *[!A-Za-z0-9_.-]*) die "--output filename contains unsupported characters" ;;
    esac
    ;;
  *) die "--output must be a .pcap path directly below /run" ;;
esac
[ ! -e "$output" ] || die "refusing to overwrite existing capture: $output"

umask 077
echo "Capturing at most $duration seconds or $packet_limit packets to $output"
echo "Start the controlled canary call now. This capture contains media payload."

set +e
timeout --signal=INT --kill-after=5 "$duration" \
  tcpdump -n -U -i "$capture_interface" -s 256 -c "$packet_limit" \
  -w "$output" "udp portrange $rtp_start-$rtp_end"
status=$?
set -e

case "$status" in
  0|124) ;;
  *) die "tcpdump failed with status $status" ;;
esac

chmod 0600 "$output"
echo "Capture complete: $output"
echo "Delete it after analysis: rm -- $output"
