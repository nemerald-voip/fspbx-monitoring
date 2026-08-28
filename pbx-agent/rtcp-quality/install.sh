#!/usr/bin/env sh
set -eu

default_repository=nemerald-voip/fspbx-monitoring
default_version=main
heplify_version=2.0.27
heplify_amd64_sha256=7e6210e86d0ff619a23f362e4cb225071459a27ca5fb3e5d5247ca3926217bc8
heplify_arm64_sha256=477b0ce112f9be1a165e2606c03dd5947297a30cea460be82c1e12b8a58d8a1e

repository=${RTCP_QUALITY_REPOSITORY:-$default_repository}
version=${RTCP_QUALITY_VERSION:-$default_version}
source_dir=${RTCP_QUALITY_SOURCE_DIR:-}
install_root=${RTCP_QUALITY_INSTALL_ROOT:-}
monitoring_host=
capture_interface=
node_name=
capture_id=
sip_start=
sip_end=
rtp_start=
rtp_end=
hep_transport=udp
binary_source=
start_service=true
cleanup_dir=

usage() {
  cat <<'EOF'
Usage: install.sh --monitoring-ip HOST [options]

Install a live heplify RTCP quality sensor that sends decoded reports to HOMER.
It does not write PCAPs, retain RTP payloads, or forward duplicate SIP HEP.

Required:
  --monitoring-ip HOST    MON01 IP address or private DNS name

Usually detected from FreeSWITCH and the host:
  --interface NAME        Capture interface (default: route to MON01)
  --node-name NAME        HOMER node name (default: fs_cli switchname)
  --capture-id UINT32     HOMER ID (default: fs_cli hep_capture_id)
  --sip-start PORT        Lowest Sofia SIP/TLS port (default: detected)
  --sip-end PORT          Highest Sofia SIP/TLS port (default: detected)
  --rtp-start PORT        First FreeSWITCH RTP port (default: configured or 16384)
  --rtp-end PORT          Last FreeSWITCH RTP port (default: configured or 32768)

Other options:
  --transport udp|tcp     HEP transport to MON01 (default: udp)
  --repo OWNER/REPO       Public GitHub repository for remote templates
  --version REF           Branch or tag for remote templates (default: main)
  --source-dir PATH       Use templates from a repository checkout
  --binary PATH           Use an existing heplify binary instead of downloading
  --no-start              Install without enabling or starting the service
  --install-root PATH     Stage files below PATH; implies --no-start
  -h, --help              Show this help

FreeSWITCH must be in the media path and RTCP must be enabled on each relevant
Sofia profile. The installer does not edit generated FS PBX configuration or
change any firewall.
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
    --interface)
      [ "$#" -ge 2 ] || die "--interface requires a value"
      capture_interface=$2
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
    --sip-start)
      [ "$#" -ge 2 ] || die "--sip-start requires a value"
      sip_start=$2
      shift 2
      ;;
    --sip-end)
      [ "$#" -ge 2 ] || die "--sip-end requires a value"
      sip_end=$2
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
    --binary)
      [ "$#" -ge 2 ] || die "--binary requires a value"
      binary_source=$2
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
  [ "$install_root" != / ] || install_root=
fi

if [ "$start_service" = true ]; then
  [ "$(ps -p 1 -o comm=)" = systemd ] || die "systemd must be PID 1"
  command -v fs_cli >/dev/null 2>&1 || die "fs_cli is required for live installation"
  command -v ip >/dev/null 2>&1 || die "the ip command is required"
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

if [ -z "$capture_interface" ]; then
  [ "$start_service" = true ] || die "--interface is required when staging"
  capture_interface=$(ip route get "$monitoring_host" | awk '
    NR == 1 { for (i = 1; i <= NF; i++) if ($i == "dev") { print $(i + 1); exit } }')
fi
case "$capture_interface" in
  ''|*[!A-Za-z0-9_.:@-]*) die "capture interface is invalid" ;;
esac
if [ "$start_service" = true ]; then
  ip link show dev "$capture_interface" >/dev/null 2>&1 || \
    die "capture interface does not exist: $capture_interface"
fi

if [ -z "$sip_start" ] || [ -z "$sip_end" ]; then
  [ "$start_service" = true ] || die "--sip-start and --sip-end are required when staging"
  sofia_ports=$(fs_cli -x 'sofia status' | sed -n \
    's/.*sip:mod_sofia@.*:\([0-9][0-9]*\).*RUNNING.*/\1/p' | sort -nu)
  [ -n "$sofia_ports" ] || die "could not detect running Sofia profile ports"
  [ -n "$sip_start" ] || sip_start=$(printf '%s\n' "$sofia_ports" | sed -n '1p')
  [ -n "$sip_end" ] || sip_end=$(printf '%s\n' "$sofia_ports" | sed -n '$p')
fi

switch_config=/etc/freeswitch/autoload_configs/switch.conf.xml
if [ -z "$rtp_start" ]; then
  if [ "$start_service" = true ] && [ -r "$switch_config" ]; then
    rtp_start=$(grep 'name="rtp-start-port"' "$switch_config" | grep -v '<!--' | \
      sed -n 's/.*value="\([0-9][0-9]*\)".*/\1/p' | sed -n '$p')
  fi
  rtp_start=${rtp_start:-16384}
fi
if [ -z "$rtp_end" ]; then
  if [ "$start_service" = true ] && [ -r "$switch_config" ]; then
    rtp_end=$(grep 'name="rtp-end-port"' "$switch_config" | grep -v '<!--' | \
      sed -n 's/.*value="\([0-9][0-9]*\)".*/\1/p' | sed -n '$p')
  fi
  rtp_end=${rtp_end:-32768}
fi

for value in "$sip_start" "$sip_end" "$rtp_start" "$rtp_end"; do
  is_uint "$value" || die "port is invalid: $value"
  [ "$value" -ge 1 ] && [ "$value" -le 65535 ] || die "port is out of range: $value"
done
[ "$sip_start" -le "$sip_end" ] || die "SIP start port must not exceed SIP end port"
[ "$rtp_start" -lt "$rtp_end" ] || die "RTP start port must be lower than RTP end port"
if [ "$sip_start" -le "$rtp_end" ] && [ "$sip_end" -ge "$rtp_start" ]; then
  die "SIP and RTP ranges must not overlap"
fi

if [ -z "$source_dir" ]; then
  script_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" 2>/dev/null && pwd || true)
  if [ -n "$script_dir" ] && [ -f "$script_dir/heplify.json.template" ] && \
      [ -f "$script_dir/heplify-rtcp.service" ]; then
    source_dir=$script_dir
  else
    command -v curl >/dev/null 2>&1 || die "curl is required for remote installation"
    cleanup_dir=$(mktemp -d)
    source_dir=$cleanup_dir
    base_url="https://raw.githubusercontent.com/$repository/$version/pbx-agent/rtcp-quality"
    log "Downloading RTCP sensor templates from $repository at $version"
    curl -fsSL "$base_url/heplify.json.template" -o "$source_dir/heplify.json.template"
    curl -fsSL "$base_url/heplify-rtcp.service" -o "$source_dir/heplify-rtcp.service"
  fi
else
  case "$source_dir" in
    /*) ;;
    *) source_dir=$(CDPATH='' cd -- "$source_dir" && pwd) ;;
  esac
fi
[ -f "$source_dir/heplify.json.template" ] || die "heplify.json.template is missing"
[ -f "$source_dir/heplify-rtcp.service" ] || die "heplify-rtcp.service is missing"

if [ -z "$binary_source" ]; then
  command -v curl >/dev/null 2>&1 || die "curl is required to download heplify"
  command -v sha256sum >/dev/null 2>&1 || die "sha256sum is required"
  [ -n "$cleanup_dir" ] || cleanup_dir=$(mktemp -d)
  case "$(uname -m)" in
    x86_64|amd64)
      binary_arch=amd64
      binary_sha256=$heplify_amd64_sha256
      ;;
    aarch64|arm64)
      binary_arch=arm64
      binary_sha256=$heplify_arm64_sha256
      ;;
    *) die "unsupported architecture: $(uname -m)" ;;
  esac
  binary_source=$cleanup_dir/heplify
  binary_url="https://github.com/sipcapture/heplify/releases/download/$heplify_version/heplify_linux_$binary_arch"
  log "Downloading pinned heplify $heplify_version for $binary_arch"
  curl -fsSL "$binary_url" -o "$binary_source"
  printf '%s  %s\n' "$binary_sha256" "$binary_source" | sha256sum -c -
fi
[ -f "$binary_source" ] || die "heplify binary is missing: $binary_source"

bin_path=$(root_path /usr/local/sbin/heplify)
config_dir=$(root_path /etc/heplify-rtcp)
config_path=$config_dir/heplify.json
unit_path=$(root_path /etc/systemd/system/heplify-rtcp.service)
install -d -m 0755 "$(dirname "$bin_path")" "$(dirname "$unit_path")"
if [ "$start_service" = true ]; then
  if ! getent passwd heplify >/dev/null 2>&1; then
    useradd --system --no-create-home --home-dir /nonexistent \
      --shell /usr/sbin/nologin heplify
  fi
  install -d -o root -g heplify -m 0750 "$config_dir"
else
  install -d -m 0750 "$config_dir"
fi
install -m 0755 "$binary_source" "$bin_path"
install -m 0644 "$source_dir/heplify-rtcp.service" "$unit_path"

config_tmp=$(mktemp)
sed \
  -e "s/@@INTERFACE@@/$capture_interface/g" \
  -e "s/@@MONITORING_HOST@@/$monitoring_host/g" \
  -e "s/@@HEP_TRANSPORT@@/$hep_transport/g" \
  -e "s/@@SIP_START@@/$sip_start/g" \
  -e "s/@@SIP_END@@/$sip_end/g" \
  -e "s/@@RTP_START@@/$rtp_start/g" \
  -e "s/@@RTP_END@@/$rtp_end/g" \
  -e "s/@@NODE_NAME@@/$node_name/g" \
  -e "s/@@CAPTURE_ID@@/$capture_id/g" \
  "$source_dir/heplify.json.template" >"$config_tmp"

if [ "$start_service" = true ]; then
  install -o root -g heplify -m 0640 "$config_tmp" "$config_path"
else
  install -m 0640 "$config_tmp" "$config_path"
fi
rm -f "$config_tmp"

if [ "$start_service" = true ]; then
  systemctl daemon-reload
  systemctl enable heplify-rtcp.service
  systemctl restart heplify-rtcp.service
  systemctl is-active --quiet heplify-rtcp.service || {
    journalctl -u heplify-rtcp.service -n 80 --no-pager >&2 || true
    die "heplify-rtcp.service failed to start"
  }
  log "RTCP quality sensor is active and enabled"
else
  log "RTCP quality sensor files installed; service start skipped"
fi

echo "Node: $node_name (capture ID $capture_id)"
echo "Interface: $capture_interface"
echo "SIP correlation range: $sip_start-$sip_end"
echo "RTP/RTCP range: $rtp_start-$rtp_end/udp"
echo "HOMER: $monitoring_host:9060/$hep_transport"
echo "Local PCAP writing: disabled"
echo "Disk HEP buffering: disabled"
if [ "$start_service" = false ] && [ -z "$install_root" ]; then
  echo "To start: systemctl enable --now heplify-rtcp.service"
fi
