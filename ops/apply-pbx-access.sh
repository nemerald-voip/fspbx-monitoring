#!/usr/bin/env sh
set -eu

# This chain still filters only Docker HEP traffic; keep its established name
# so updating the helper does not leave a second, stale chain on existing hosts.
chain=FSPBX-HEP
external_interface=${EXTERNAL_INTERFACE:-}
hep_port=${HEP_PORT:-9060}
allowed_sources=${ALLOWED_PBX_SOURCES-${ALLOWED_HEP_SOURCES:-}}

die() {
  echo "ERROR: $*" >&2
  exit 1
}

if [ "${ALLOWED_PBX_SOURCES+x}" = x ] && [ "${ALLOWED_HEP_SOURCES+x}" = x ] && \
    [ "$ALLOWED_PBX_SOURCES" != "$ALLOWED_HEP_SOURCES" ]; then
  die "set only ALLOWED_PBX_SOURCES; conflicting legacy ALLOWED_HEP_SOURCES is also set"
fi

validate_ipv4_cidr() {
  value=$1
  case "$value" in
    */*)
      address=${value%/*}
      prefix=${value##*/}
      case "${value#*/}" in
        */*) die "invalid IPv4 CIDR: $value" ;;
      esac
      ;;
    *)
      address=$value
      prefix=32
      ;;
  esac

  case "$prefix" in
    ''|*[!0-9]*) die "invalid IPv4 CIDR prefix: $value" ;;
  esac
  [ "$prefix" -le 32 ] || die "IPv4 CIDR prefix is greater than 32: $value"

  previous_ifs=$IFS
  IFS=.
  # Word splitting is intentional so each IPv4 octet becomes an argument.
  # shellcheck disable=SC2086
  set -- $address
  IFS=$previous_ifs
  [ "$#" -eq 4 ] || die "invalid IPv4 address: $value"
  for octet do
    case "$octet" in
      ''|*[!0-9]*) die "invalid IPv4 address: $value" ;;
    esac
    [ "$octet" -le 255 ] || die "invalid IPv4 address: $value"
  done
}

[ "$(id -u)" -eq 0 ] || die "run as root"
command -v iptables >/dev/null 2>&1 || die "iptables is required"

[ -n "$external_interface" ] || die "set EXTERNAL_INTERFACE"
case "$external_interface" in
  *[!A-Za-z0-9_.:-]*) die "EXTERNAL_INTERFACE contains an unsupported character" ;;
esac
case "$hep_port" in
  ''|*[!0-9]*) die "HEP_PORT must be an integer" ;;
esac
if [ "$hep_port" -lt 1 ] || [ "$hep_port" -gt 65535 ]; then
  die "HEP_PORT must be between 1 and 65535"
fi

if [ "${1:-}" = --remove ]; then
  while iptables -w -C DOCKER-USER -i "$external_interface" -j "$chain" \
      >/dev/null 2>&1; do
    iptables -w -D DOCKER-USER -i "$external_interface" -j "$chain"
  done
  if iptables -w -S "$chain" >/dev/null 2>&1; then
    iptables -w -F "$chain"
    iptables -w -X "$chain"
  fi
  echo "Removed the IPv4 HEP allowlist from $external_interface."
  exit 0
fi

[ "$#" -eq 0 ] || die "usage: apply-pbx-access.sh [--remove]"
[ -n "$allowed_sources" ] || die "set at least one source in ALLOWED_PBX_SOURCES"

# Validate every value before changing the active chain. This helper is
# intentionally IPv4-only; use a provider firewall or a reviewed ip6tables/
# nftables policy when the published port is reachable over IPv6.
# Word splitting is intentional: the environment value is a space-separated
# list whose individual entries are validated before any firewall mutation.
# shellcheck disable=SC2086
for source in $allowed_sources; do
  validate_ipv4_cidr "$source"
done

iptables -w -S DOCKER-USER >/dev/null 2>&1 || \
  die "DOCKER-USER is unavailable; start Docker and confirm its firewall backend is iptables"

if ! iptables -w -S "$chain" >/dev/null 2>&1; then
  iptables -w -N "$chain"
fi
iptables -w -F "$chain"

# Word splitting is intentional after validation above.
# shellcheck disable=SC2086
for source in $allowed_sources; do
  iptables -w -A "$chain" -p udp -s "$source" \
    -m conntrack --ctorigdstport "$hep_port" -j ACCEPT
  iptables -w -A "$chain" -p tcp -s "$source" \
    -m conntrack --ctorigdstport "$hep_port" -j ACCEPT
done

iptables -w -A "$chain" -p udp \
  -m conntrack --ctorigdstport "$hep_port" -j DROP
iptables -w -A "$chain" -p tcp \
  -m conntrack --ctorigdstport "$hep_port" -j DROP
iptables -w -A "$chain" -j RETURN

if ! iptables -w -C DOCKER-USER -i "$external_interface" -j "$chain" \
    >/dev/null 2>&1; then
  iptables -w -I DOCKER-USER 1 -i "$external_interface" -j "$chain"
fi

echo "Applied the IPv4 HEP allowlist on $external_interface for port $hep_port."
iptables -w -n -v -L "$chain" --line-numbers

# Reuse this same effective source list for an already provisioned API proxy.
# No certificate issuance, package installation, or container changes are needed.
script_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
if [ -x "$script_dir/install-homer-api.sh" ] && \
    [ -f /etc/nginx/nginx.conf ] && \
    head -n 1 /etc/nginx/nginx.conf | \
      grep -Fxq '# Managed by fspbx-monitoring install-homer-api.sh'; then
  if ! ALLOWED_PBX_SOURCES="$allowed_sources" \
      "$script_dir/install-homer-api.sh" --sync-allowlist; then
    die "HEP rules were applied, but API allowlist synchronization failed; inspect Nginx before retrying"
  fi
fi
