#!/usr/bin/env sh
set -eu

default_repository=nemerald-voip/fspbx-monitoring
default_version=main

repository=${RTCP_QUALITY_REPOSITORY:-$default_repository}
version=${RTCP_QUALITY_VERSION:-$default_version}
source_dir=${RTCP_QUALITY_SOURCE_DIR:-}
install_root=${RTCP_QUALITY_INSTALL_ROOT:-}
monitoring_host=
node_name=
capture_id=
hep_transport=udp
esl_password_file=
start_service=true
cleanup_dir=

usage() {
  cat <<'EOF'
Usage: install.sh --monitoring-ip HOST [options]

Install a lightweight FreeSWITCH ESL RTCP reporter that sends HEPv3 quality
events to HOMER. It reads parsed RTCP statistics after FreeSWITCH has handled
SRTCP; it never captures packets, media payload, or SRTP keys.

Required:
  --monitoring-ip HOST      MON01 IP address or private DNS name

Usually detected from FreeSWITCH:
  --node-name NAME          HOMER node name (default: fs_cli switchname)
  --capture-id UINT32       HOMER ID (default: fs_cli hep_capture_id)
  --esl-password-file PATH  One-line ESL password file (default: resolve from
                            event_socket.conf.xml on a live installation)

Other options:
  --transport udp|tcp       HEP transport to MON01 (default: udp)
  --repo OWNER/REPO         Public GitHub repository for remote templates
  --version REF             Branch or tag for remote templates (default: main)
  --source-dir PATH         Use files from a repository checkout
  --no-start                Install without enabling or starting the service
  --install-root PATH       Stage files below PATH; implies --no-start
  -h, --help                Show this help

FreeSWITCH ESL must remain bound to 127.0.0.1. RTCP must be enabled, and the
fire_rtcp_events channel variable must be set before media starts to include
PBX-generated reports. The installer does not edit PBX call routing or firewall.
EOF
}

die() {
  echo "ERROR: $*" >&2
  exit 1
}

log() {
  echo "==> $*"
}

is_uint() {
  case "$1" in
    ''|*[!0-9]*) return 1 ;;
    *) return 0 ;;
  esac
}

root_path() {
  printf '%s%s\n' "$install_root" "$1"
}

ensure_cleanup_dir() {
  if [ -z "$cleanup_dir" ]; then
    cleanup_dir=$(mktemp -d)
  fi
}

cleanup() {
  if [ -n "$cleanup_dir" ] && [ -d "$cleanup_dir" ]; then
    rm -rf -- "$cleanup_dir"
  fi
}
trap cleanup EXIT HUP INT TERM

while [ "$#" -gt 0 ]; do
  case "$1" in
    --monitoring-ip)
      [ "$#" -ge 2 ] || die "--monitoring-ip requires a value"
      monitoring_host=$2
      shift 2
      ;;
    --node-name)
      [ "$#" -ge 2 ] || die "--node-name requires a value"
      node_name=$2
      shift 2
      ;;
    --capture-id)
      [ "$#" -ge 2 ] || die "--capture-id requires a value"
      capture_id=$2
      shift 2
      ;;
    --esl-password-file)
      [ "$#" -ge 2 ] || die "--esl-password-file requires a value"
      esl_password_file=$2
      shift 2
      ;;
    --transport)
      [ "$#" -ge 2 ] || die "--transport requires a value"
      hep_transport=$2
      shift 2
      ;;
    --repo)
      [ "$#" -ge 2 ] || die "--repo requires a value"
      repository=$2
      shift 2
      ;;
    --version)
      [ "$#" -ge 2 ] || die "--version requires a value"
      version=$2
      shift 2
      ;;
    --source-dir)
      [ "$#" -ge 2 ] || die "--source-dir requires a value"
      source_dir=$2
      shift 2
      ;;
    --no-start)
      start_service=false
      shift
      ;;
    --install-root)
      [ "$#" -ge 2 ] || die "--install-root requires a value"
      install_root=$2
      start_service=false
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *) die "unknown option: $1" ;;
  esac
done

[ "$(id -u)" -eq 0 ] || die "run this installer as root (for example, with sudo)"
[ -n "$monitoring_host" ] || die "--monitoring-ip is required"
case "$monitoring_host" in
  *[!A-Za-z0-9_.:-]*) die "--monitoring-ip contains unsupported characters" ;;
esac
case "$hep_transport" in
  udp|tcp) ;;
  *) die "--transport must be udp or tcp" ;;
esac
case "$repository" in
  */*) ;;
  *) die "--repo must use OWNER/REPO format" ;;
esac

if [ -n "$install_root" ]; then
  case "$install_root" in
    /*) ;;
    *) die "--install-root must be an absolute path" ;;
  esac
  [ "$install_root" != / ] || die "--install-root must not be /"
fi

if [ "$start_service" = true ]; then
  [ "$(ps -p 1 -o comm=)" = systemd ] || die "systemd must be PID 1"
  command -v fs_cli >/dev/null 2>&1 || die "fs_cli is required for live installation"
  command -v python3 >/dev/null 2>&1 || die "python3 is required"
fi

if [ -z "$node_name" ]; then
  [ "$start_service" = true ] || die "--node-name is required when staging"
  node_name=$(fs_cli -x switchname | sed -n '1p')
fi
case "$node_name" in
  ''|*[!A-Za-z0-9_.-]*) die "node name is empty or contains unsupported characters" ;;
esac

if [ -z "$capture_id" ]; then
  [ "$start_service" = true ] || die "--capture-id is required when staging"
  capture_id=$(fs_cli -x 'global_getvar hep_capture_id' | sed -n '1p')
fi
is_uint "$capture_id" || die "capture ID must be an unsigned integer"
[ "$capture_id" -ge 1 ] && [ "$capture_id" -le 4294967295 ] || \
  die "capture ID must be between 1 and 4294967295"

if [ -z "$esl_password_file" ]; then
  [ "$start_service" = true ] || \
    die "--esl-password-file is required when staging"
  event_socket_config=/etc/freeswitch/autoload_configs/event_socket.conf.xml
  [ -r "$event_socket_config" ] || \
    die "cannot read $event_socket_config; use --esl-password-file"
  configured_password=$(sed -n \
    's/.*<param[[:space:]][^>]*name="password"[^>]*value="\([^"]*\)".*/\1/p' \
    "$event_socket_config" | sed -n '$p')
  [ -n "$configured_password" ] || \
    die "could not resolve the ESL password; use --esl-password-file"
  case "$configured_password" in
    '$${'*'}')
      variable_name=${configured_password#'$${'}
      variable_name=${variable_name%'}'}
      configured_password=$(fs_cli -x "global_getvar $variable_name" | sed -n '1p')
      ;;
  esac
  [ -n "$configured_password" ] || \
    die "resolved ESL password is empty; use --esl-password-file"
  ensure_cleanup_dir
  esl_password_file=$cleanup_dir/esl-password
  printf '%s' "$configured_password" >"$esl_password_file"
fi

[ -r "$esl_password_file" ] || die "cannot read ESL password file: $esl_password_file"
password_line=$(sed -n '1p' "$esl_password_file")
[ -n "$password_line" ] || die "ESL password file must contain one non-empty line"
line_count=$(awk 'END { print NR }' "$esl_password_file")
[ "$line_count" -le 1 ] || die "ESL password file must contain exactly one line"
unset password_line configured_password

if [ -z "$source_dir" ]; then
  script_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" 2>/dev/null && pwd || true)
  if [ -n "$script_dir" ] && \
      [ -f "$script_dir/freeswitch_rtcp_to_hep.py" ] && \
      [ -f "$script_dir/freeswitch-rtcp-to-hep.json.template" ] && \
      [ -f "$script_dir/freeswitch-rtcp-to-hep.service" ] && \
      [ -f "$script_dir/canary-capture.sh" ]; then
    source_dir=$script_dir
  else
    command -v curl >/dev/null 2>&1 || die "curl is required for remote installation"
    ensure_cleanup_dir
    source_dir=$cleanup_dir
    base_url="https://raw.githubusercontent.com/$repository/$version/pbx-agent/rtcp-quality"
    log "Downloading ESL RTCP reporter files from $repository at $version"
    curl -fsSL "$base_url/freeswitch_rtcp_to_hep.py" \
      -o "$source_dir/freeswitch_rtcp_to_hep.py"
    curl -fsSL "$base_url/freeswitch-rtcp-to-hep.json.template" \
      -o "$source_dir/freeswitch-rtcp-to-hep.json.template"
    curl -fsSL "$base_url/freeswitch-rtcp-to-hep.service" \
      -o "$source_dir/freeswitch-rtcp-to-hep.service"
    curl -fsSL "$base_url/canary-capture.sh" -o "$source_dir/canary-capture.sh"
  fi
else
  case "$source_dir" in
    /*) ;;
    *) source_dir=$(CDPATH='' cd -- "$source_dir" && pwd) ;;
  esac
fi
for required_file in freeswitch_rtcp_to_hep.py \
  freeswitch-rtcp-to-hep.json.template freeswitch-rtcp-to-hep.service \
  canary-capture.sh; do
  [ -f "$source_dir/$required_file" ] || die "$required_file is missing"
done

bin_path=$(root_path /usr/local/sbin/freeswitch-rtcp-to-hep)
canary_path=$(root_path /usr/local/sbin/rtcp-canary-capture)
config_dir=$(root_path /etc/freeswitch-rtcp-to-hep)
config_path=$config_dir/config.json
secret_path=$config_dir/esl-password
unit_path=$(root_path /etc/systemd/system/freeswitch-rtcp-to-hep.service)

install -d -m 0755 "$(dirname "$bin_path")" "$(dirname "$unit_path")"
install -d -o root -g root -m 0755 "$config_dir"
install -m 0755 "$source_dir/freeswitch_rtcp_to_hep.py" "$bin_path"
install -m 0755 "$source_dir/canary-capture.sh" "$canary_path"
install -m 0644 "$source_dir/freeswitch-rtcp-to-hep.service" "$unit_path"
install -o root -g root -m 0600 "$esl_password_file" "$secret_path"

config_tmp=$(mktemp)
sed \
  -e "s/@@MONITORING_HOST@@/$monitoring_host/g" \
  -e "s/@@HEP_TRANSPORT@@/$hep_transport/g" \
  -e "s/@@NODE_NAME@@/$node_name/g" \
  -e "s/@@CAPTURE_ID@@/$capture_id/g" \
  "$source_dir/freeswitch-rtcp-to-hep.json.template" >"$config_tmp"
install -o root -g root -m 0644 "$config_tmp" "$config_path"
rm -f "$config_tmp"

if [ "$start_service" = true ]; then
  old_was_active=false
  if systemctl is-active --quiet heplify-rtcp.service; then
    old_was_active=true
    systemctl stop heplify-rtcp.service
  fi
  systemctl daemon-reload
  systemctl enable freeswitch-rtcp-to-hep.service
  if ! systemctl restart freeswitch-rtcp-to-hep.service || \
      ! systemctl is-active --quiet freeswitch-rtcp-to-hep.service; then
    journalctl -u freeswitch-rtcp-to-hep.service -n 80 --no-pager >&2 || true
    if [ "$old_was_active" = true ]; then
      systemctl start heplify-rtcp.service || true
    fi
    die "freeswitch-rtcp-to-hep.service failed to start"
  fi
  systemctl disable heplify-rtcp.service >/dev/null 2>&1 || true
  log "ESL RTCP quality reporter is active and enabled"
else
  log "ESL RTCP quality reporter files installed; service start skipped"
fi

echo "Node: $node_name (capture ID $capture_id)"
echo "ESL: 127.0.0.1:8021 only"
echo "HOMER: $monitoring_host:9060/$hep_transport"
echo "Packet capture: none"
echo "Media payload and SRTP keys: not read"
echo "Memory ceiling: 96 MiB"
if [ "$start_service" = false ] && [ -z "$install_root" ]; then
  echo "To start: systemctl enable --now freeswitch-rtcp-to-hep.service"
fi
