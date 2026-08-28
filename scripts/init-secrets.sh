#!/usr/bin/env sh
set -eu

project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
env_file="$project_dir/.env"
template="$project_dir/.env.example"

if [ -e "$env_file" ]; then
  echo "Refusing to overwrite $env_file" >&2
  exit 1
fi

command -v openssl >/dev/null 2>&1 || {
  echo "openssl is required" >&2
  exit 1
}

umask 077
homer_password=$(openssl rand -hex 16)
grafana_password=$(openssl rand -hex 16)
homer_hash=$(printf '%s' "$homer_password" | openssl dgst -sha256 -r | awk '{print $1}')
flight_token=$(openssl rand -hex 32)
jwt_secret=$(openssl rand -hex 48)

cp "$template" "$env_file"
sed -i \
  -e "s/^HOMER_ADMIN_PASSWORD=.*/HOMER_ADMIN_PASSWORD=$homer_password/" \
  -e "s/^HOMER_ADMIN_PASSWORD_HASH=.*/HOMER_ADMIN_PASSWORD_HASH=$homer_hash/" \
  -e "s/^HOMER_FLIGHT_TOKEN=.*/HOMER_FLIGHT_TOKEN=$flight_token/" \
  -e "s/^HOMER_JWT_SECRET=.*/HOMER_JWT_SECRET=$jwt_secret/" \
  -e "s/^GRAFANA_ADMIN_PASSWORD=.*/GRAFANA_ADMIN_PASSWORD=$grafana_password/" \
  "$env_file"
chmod 600 "$env_file"

echo "Created $env_file (mode 0600)."
echo "HOMER admin password:  $homer_password"
echo "Grafana admin password: $grafana_password"
echo "Store these passwords in your password manager; they are also in .env."
