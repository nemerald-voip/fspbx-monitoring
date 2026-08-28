#!/usr/bin/env sh
set -eu

project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$project_dir"

for script in install.sh scripts/*.sh synthetic/*.sh pbx-agent/rtp-capture/*.sh; do
  sh -n "$script"
done

./install.sh --help >/dev/null

if command -v shellcheck >/dev/null 2>&1; then
  shellcheck install.sh scripts/*.sh synthetic/*.sh pbx-agent/rtp-capture/*.sh
fi

command -v docker >/dev/null 2>&1 || {
  echo "docker with the Compose plugin is required" >&2
  exit 1
}

docker compose version >/dev/null
docker compose --env-file .env.example config --quiet

grep -q '^Requires=docker.service$' systemd/monitoring-reconcile.service
grep -q '^ConditionPathExists=/opt/monitoring/.env$' systemd/monitoring-reconcile.service
grep -q '^ExecStart=/usr/bin/docker compose up -d --remove-orphans$' \
  systemd/monitoring-reconcile.service

if git rev-parse --is-inside-work-tree >/dev/null 2>&1 && \
    git ls-files --error-unmatch .env >/dev/null 2>&1; then
  echo ".env must never be tracked by Git" >&2
  exit 1
fi

echo "Repository validation passed."
