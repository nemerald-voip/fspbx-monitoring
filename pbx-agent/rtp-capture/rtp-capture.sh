#!/usr/bin/env sh
set -eu

: "${CAPTURE_INTERFACE:?CAPTURE_INTERFACE is required}"
: "${RTP_PORT_START:?RTP_PORT_START is required}"
: "${RTP_PORT_END:?RTP_PORT_END is required}"
: "${SNAP_LENGTH:=128}"
: "${RING_FILE_KIB:=262144}"
: "${RING_FILE_COUNT:=32}"
: "${CAPTURE_DIRECTORY:=/var/capture}"

for value in "$RTP_PORT_START" "$RTP_PORT_END" "$SNAP_LENGTH" "$RING_FILE_KIB" "$RING_FILE_COUNT"; do
  case "$value" in
    ''|*[!0-9]*) echo "Capture numeric setting is invalid: $value" >&2; exit 2 ;;
  esac
done

if [ "$RTP_PORT_START" -lt 1024 ] || [ "$RTP_PORT_END" -gt 65535 ] || [ "$RTP_PORT_START" -ge "$RTP_PORT_END" ]; then
  echo "Invalid RTP port range" >&2
  exit 2
fi
if [ "$SNAP_LENGTH" -lt 96 ] || [ "$SNAP_LENGTH" -gt 256 ]; then
  echo "SNAP_LENGTH must be between 96 and 256 bytes" >&2
  exit 2
fi
if [ "$RING_FILE_KIB" -gt 1048576 ] || [ "$RING_FILE_COUNT" -gt 128 ]; then
  echo "Ring limit is unexpectedly large; review the configuration" >&2
  exit 2
fi

install -d -m 0750 "$CAPTURE_DIRECTORY"

exec /usr/bin/dumpcap \
  -i "$CAPTURE_INTERFACE" \
  -p \
  -f "udp portrange $RTP_PORT_START-$RTP_PORT_END" \
  -s "$SNAP_LENGTH" \
  -b "filesize:$RING_FILE_KIB" \
  -b "files:$RING_FILE_COUNT" \
  -w "$CAPTURE_DIRECTORY/rtp.pcapng"
