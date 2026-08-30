#!/usr/bin/env sh
set -eu

default_repository="nemerald-voip/fspbx-monitoring"
default_version="main"

install_dir=/opt/monitoring
repository=${MONITORING_REPOSITORY:-$default_repository}
version=${MONITORING_VERSION:-$default_version}
source_dir=${MONITORING_SOURCE_DIR:-}
systemd_unit_dir=${MONITORING_SYSTEMD_UNIT_DIR:-/etc/systemd/system}
install_docker=true
generate_secrets=true
start_stack=true

usage() {
  cat <<'EOF'
Usage: install.sh [options]

Install or update the FreeSWITCH monitoring stack on a Debian server.

Options:
  --repo OWNER/REPOSITORY  Public GitHub repository to download
  --version REF            Git branch or tag (default: main)
  --install-dir PATH       Deployment directory (default: /opt/monitoring)
  --source-dir PATH        Install from an existing checkout
  --skip-docker            Require an existing Docker Engine and Compose plugin
  --no-secrets             Do not create .env; implies --no-start
  --no-start               Prepare the host but do not pull or launch containers
  -h, --help               Show this help

Environment equivalents: MONITORING_REPOSITORY, MONITORING_VERSION,
MONITORING_SOURCE_DIR.
EOF
}

die() {
  echo "ERROR: $*" >&2
  exit 1
}

log() {
  echo "==> $*"
}

while [ "$#" -gt 0 ]; do
  case "$1" in
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
    --install-dir)
      [ "$#" -ge 2 ] || die "--install-dir requires a value"
      install_dir=$2
      shift 2
      ;;
    --source-dir)
      [ "$#" -ge 2 ] || die "--source-dir requires a value"
      source_dir=$2
      shift 2
      ;;
    --skip-docker)
      install_docker=false
      shift
      ;;
    --no-secrets)
      generate_secrets=false
      start_stack=false
      shift
      ;;
    --no-start)
      start_stack=false
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "unknown option: $1"
      ;;
  esac
done

[ "$(id -u)" -eq 0 ] || die "run this installer as root (for example, with sudo)"

case "$install_dir" in
  /*) ;;
  *) die "--install-dir must be an absolute path" ;;
esac
case "$install_dir" in
  *'|'*|*'&'*) die "--install-dir cannot contain | or &" ;;
esac

[ -r /etc/os-release ] || die "/etc/os-release is missing"
# shellcheck disable=SC1091
. /etc/os-release
[ "${ID:-}" = debian ] || die "this installer supports Debian; detected ${ID:-unknown}"
case "${VERSION_CODENAME:-}" in
  trixie|bookworm|bullseye) ;;
  *) die "unsupported Debian release: ${VERSION_CODENAME:-unknown}" ;;
esac
[ "$(ps -p 1 -o comm=)" = systemd ] || die "systemd must be PID 1"

export DEBIAN_FRONTEND=noninteractive

install_docker_ce() {
  log "Installing Docker CE from Docker's official APT repository"
  apt-get update
  apt-get install -y ca-certificates curl git openssl tar

  conflicting_packages=
  for package in docker.io docker-compose docker-doc docker-buildx \
    podman-docker containerd runc; do
    if dpkg-query -W -f='${db:Status-Status}' "$package" 2>/dev/null | \
      grep -q '^installed$'; then
      conflicting_packages="$conflicting_packages $package"
    fi
  done
  if [ -n "$conflicting_packages" ]; then
    log "Removing packages that conflict with Docker CE:$conflicting_packages"
    # Word splitting is intentional: the values come from the fixed list above.
    # shellcheck disable=SC2086
    apt-get remove -y $conflicting_packages
  fi

  install -m 0755 -d /etc/apt/keyrings
  key_tmp=$(mktemp)
  curl -fsSL https://download.docker.com/linux/debian/gpg -o "$key_tmp"
  install -m 0644 "$key_tmp" /etc/apt/keyrings/docker.asc
  rm -f "$key_tmp"

  architecture=$(dpkg --print-architecture)
  sources_tmp=$(mktemp)
  {
    echo "Types: deb"
    echo "URIs: https://download.docker.com/linux/debian"
    echo "Suites: $VERSION_CODENAME"
    echo "Components: stable"
    echo "Architectures: $architecture"
    echo "Signed-By: /etc/apt/keyrings/docker.asc"
  } >"$sources_tmp"
  install -m 0644 "$sources_tmp" /etc/apt/sources.list.d/docker.sources
  rm -f "$sources_tmp"

  apt-get update
  apt-get install -y docker-ce docker-ce-cli containerd.io \
    docker-buildx-plugin docker-compose-plugin
}

if [ "$install_docker" = true ]; then
  if dpkg-query -W -f='${db:Status-Status}' docker-ce 2>/dev/null | \
      grep -q '^installed$' && \
      dpkg-query -W -f='${db:Status-Status}' docker-compose-plugin 2>/dev/null | \
      grep -q '^installed$'; then
    log "Docker Engine and the Compose plugin are already installed"
    prerequisites_present=true
    for package in ca-certificates curl git openssl tar; do
      if ! dpkg-query -W -f='${db:Status-Status}' "$package" 2>/dev/null | \
          grep -q '^installed$'; then
        prerequisites_present=false
        break
      fi
    done
    if [ "$prerequisites_present" = false ]; then
      apt-get update
      apt-get install -y ca-certificates curl git openssl tar
    fi
  else
    install_docker_ce
  fi
else
  command -v docker >/dev/null 2>&1 || die "docker is required with --skip-docker"
  docker compose version >/dev/null 2>&1 || die "the Docker Compose plugin is required"
  command -v git >/dev/null 2>&1 || die "git is required with --skip-docker"
  command -v openssl >/dev/null 2>&1 || die "openssl is required with --skip-docker"
fi

systemctl enable --now containerd.service docker.service
docker info >/dev/null
docker compose version >/dev/null

cleanup_dir=
managed_manifest_tmp=
cleanup() {
  if [ -n "$managed_manifest_tmp" ]; then
    rm -f -- "$managed_manifest_tmp"
  fi
  if [ -n "$cleanup_dir" ] && [ -d "$cleanup_dir" ]; then
    rm -rf -- "$cleanup_dir"
  fi
}
trap cleanup EXIT HUP INT TERM

if [ -z "$source_dir" ]; then
  [ -n "$repository" ] || die "pass --repo OWNER/REPOSITORY for a remote install"
  case "$repository" in
    */*) ;;
    *) die "--repo must use OWNER/REPOSITORY format" ;;
  esac
  cleanup_dir=$(mktemp -d)
  source_dir=$cleanup_dir/source
  log "Downloading $repository at $version"
  git clone --quiet --depth 1 --branch "$version" \
    "https://github.com/$repository.git" "$source_dir"
  source_label=$repository
else
  case "$source_dir" in
    /*) ;;
    *) source_dir=$(CDPATH='' cd -- "$source_dir" && pwd) ;;
  esac
  source_label="local:$source_dir"
fi

source_revision=$(git -C "$source_dir" rev-parse HEAD 2>/dev/null || true)
if [ -n "$source_revision" ] && \
    ! git -C "$source_dir" diff-index --quiet HEAD -- 2>/dev/null; then
  source_revision=$source_revision-dirty
fi
[ -n "$source_revision" ] || source_revision=unknown

[ -f "$source_dir/compose.yaml" ] || die "compose.yaml is missing from $source_dir"
[ -f "$source_dir/.env.example" ] || die ".env.example is missing from $source_dir"
[ -f "$source_dir/systemd/monitoring-reconcile.service" ] || \
  die "monitoring-reconcile.service is missing from $source_dir"

log "Validating source Compose model before deployment"
(
  cd "$source_dir"
  docker compose --env-file .env.example config --quiet
)

managed_manifest_tmp=$(mktemp)
if git -C "$source_dir" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  git -C "$source_dir" ls-files >"$managed_manifest_tmp"
elif [ -f "$source_dir/.installed-files" ]; then
  cp "$source_dir/.installed-files" "$managed_manifest_tmp"
else
  (
    cd "$source_dir"
    find . \
      \( -path './.git' -o -path './.env' -o -path './.installed-*' \
         -o -path './prometheus/targets' -o -path './backups' \
         -o -path './data' -o -path './alertmanager/alertmanager.local.yml' \
         -o -name '__pycache__' -o -name '*.pyc' -o -name '*.pcap' \
         -o -name '*.pcapng' \) -prune -o \
      \( -type f -o -type l \) -print | LC_ALL=C sort
  ) >"$managed_manifest_tmp"
fi

while IFS= read -r managed_path; do
  case "$managed_path" in
    ''|/*|..|../*|*/../*|*/..) die "unsafe managed path: $managed_path" ;;
  esac
done <"$managed_manifest_tmp"

if [ -f "$install_dir/.installed-version" ]; then
  previous_version=$(tr '\n' ' ' <"$install_dir/.installed-version")
  log "Updating $install_dir from ${previous_version% }"
else
  log "Installing a new deployment in $install_dir"
fi

if [ "$source_dir" != "$install_dir" ]; then
  log "Refreshing managed application files in $install_dir"
  install -d -m 0755 "$install_dir"
  (
    cd "$source_dir"
    tar --verbatim-files-from --no-unquote -cf - -T "$managed_manifest_tmp"
  ) | (
    cd "$install_dir"
    tar -xf -
  )
fi

if [ -f "$install_dir/.installed-files" ]; then
  while IFS= read -r managed_path; do
    case "$managed_path" in
      ''|/*|..|../*|*/../*|*/..) die "unsafe installed manifest path: $managed_path" ;;
    esac
    if ! grep -Fqx -- "$managed_path" "$managed_manifest_tmp"; then
      obsolete_path=$install_dir/$managed_path
      if [ -f "$obsolete_path" ] || [ -L "$obsolete_path" ]; then
        rm -f -- "$obsolete_path"
      fi
    fi
  done <"$install_dir/.installed-files"
fi

install -m 0644 "$managed_manifest_tmp" "$install_dir/.installed-files"

install -d -m 0755 "$install_dir/prometheus/targets"
for example in "$install_dir"/prometheus/targets.example/*.yml; do
  [ -f "$example" ] || continue
  target=$install_dir/prometheus/targets/$(basename "$example")
  if [ ! -e "$target" ]; then
    install -m 0644 "$example" "$target"
  fi
done

unit_tmp=$(mktemp)
sed "s|/opt/monitoring|$install_dir|g" \
  "$install_dir/systemd/monitoring-reconcile.service" >"$unit_tmp"
install -d -m 0755 "$systemd_unit_dir"
install -m 0644 "$unit_tmp" "$systemd_unit_dir/monitoring-reconcile.service"
rm -f "$unit_tmp"
systemctl daemon-reload
systemctl enable monitoring-reconcile.service

{
  printf 'source=%s\n' "$source_label"
  printf 'reference=%s\n' "$version"
  printf 'revision=%s\n' "$source_revision"
} >"$install_dir/.installed-version"
chmod 0644 "$install_dir/.installed-version"

if [ "$generate_secrets" = true ] && [ ! -e "$install_dir/.env" ]; then
  log "Generating local credentials"
  "$install_dir/scripts/init-secrets.sh"
elif [ -e "$install_dir/.env" ]; then
  log "Preserving existing $install_dir/.env"
fi

if [ "$start_stack" = true ]; then
  [ -f "$install_dir/.env" ] || die "cannot start without $install_dir/.env"
  log "Validating and starting the monitoring stack"
  "$install_dir/scripts/check-config.sh"
  (
    cd "$install_dir"
    docker compose pull
    docker compose up -d --remove-orphans
    docker compose ps
  )
else
  log "Stack launch skipped"
fi

log "Installation/update complete"
echo "Deployment directory: $install_dir"
echo "Installed revision: $source_revision"
echo "Boot reconciler: monitoring-reconcile.service (enabled)"
if [ "$start_stack" = false ]; then
  echo "To launch later: cd $install_dir && ./scripts/init-secrets.sh && docker compose up -d"
fi
