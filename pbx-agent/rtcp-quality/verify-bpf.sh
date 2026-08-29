#!/usr/bin/env sh
set -eu

: "${INVOCATION_ID:?systemd INVOCATION_ID is required}"

attempt=0
while [ "$attempt" -lt 15 ]; do
  messages=$(/usr/bin/journalctl --quiet --no-pager -o cat \
    "_SYSTEMD_INVOCATION_ID=$INVOCATION_ID" 2>/dev/null || true)

  if printf '%s\n' "$messages" | grep -q 'Failed to set BPF filter'; then
    echo "heplify reported a kernel BPF installation failure" >&2
    exit 1
  fi

  applied=$(printf '%s\n' "$messages" | grep -c 'BPF filter applied' || true)
  if [ "$applied" -ge 2 ]; then
    echo "verified both heplify kernel BPF filters"
    exit 0
  fi

  attempt=$((attempt + 1))
  sleep 1
done

echo "expected two heplify BPF filter confirmations; observed ${applied:-0}" >&2
exit 1
