#!/usr/bin/env sh
set -eu

project_dir=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
cd "$project_dir"

if [ ! -f .env ]; then
  echo "Missing .env; run ./scripts/init-secrets.sh" >&2
  exit 1
fi

if grep -Eq '(^|=)(replace-me|replace-me-with-sha256)($|[[:space:]])' .env; then
  echo ".env still contains placeholder credentials" >&2
  exit 1
fi

command -v docker >/dev/null 2>&1 || {
  echo "docker with the Compose plugin is required" >&2
  exit 1
}

docker compose version >/dev/null
docker compose config --quiet
echo "Docker Compose configuration is valid."
