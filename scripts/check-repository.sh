#!/usr/bin/env sh
set -eu

project_dir=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
cd "$project_dir"

for script in install.sh scripts/*.sh ops/*.sh synthetic/*.sh \
  pbx-agent/rtp-capture/*.sh pbx-agent/rtcp-quality/*.sh; do
  sh -n "$script"
done

./install.sh --help >/dev/null
./pbx-agent/rtcp-quality/install.sh --help >/dev/null
python3 pbx-agent/rtcp-quality/test_freeswitch_rtcp_to_hep.py
./scripts/check-docs.sh

if command -v shellcheck >/dev/null 2>&1; then
  shellcheck install.sh scripts/*.sh ops/*.sh synthetic/*.sh pbx-agent/rtp-capture/*.sh
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
grep -q '^ExecStart=/opt/monitoring/ops/apply-hep-firewall.sh$' \
  systemd/fspbx-hep-firewall.service
rtcp_unit=pbx-agent/rtcp-quality/freeswitch-rtcp-to-hep.service
grep -q '^ExecStart=/usr/bin/python3 /usr/local/sbin/freeswitch-rtcp-to-hep --config /etc/freeswitch-rtcp-to-hep/config.json$' \
  "$rtcp_unit"
grep -q '^DynamicUser=true$' "$rtcp_unit"
grep -q '^LoadCredential=esl_password:/etc/freeswitch-rtcp-to-hep/esl-password$' \
  "$rtcp_unit"
grep -q '^CPUWeight=10$' "$rtcp_unit"
grep -q '^MemoryMax=96M$' "$rtcp_unit"
grep -q '^MemorySwapMax=0$' "$rtcp_unit"
grep -q '^TasksMax=16$' "$rtcp_unit"
grep -q '^OOMScoreAdjust=500$' "$rtcp_unit"
grep -q '^CapabilityBoundingSet=$' "$rtcp_unit"
grep -q '^RestrictAddressFamilies=AF_INET AF_INET6$' "$rtcp_unit"
grep -q '"esl_host": "127.0.0.1"' \
  pbx-agent/rtcp-quality/freeswitch-rtcp-to-hep.json.template
grep -q '"hep_host": "@@MONITORING_HOST@@"' \
  pbx-agent/rtcp-quality/freeswitch-rtcp-to-hep.json.template
grep -q 'install -d -o root -g root -m 0755 "$config_dir"' \
  pbx-agent/rtcp-quality/install.sh
test ! -e pbx-agent/rtcp-quality/heplify-rtcp.service
test ! -e pbx-agent/rtcp-quality/heplify.json.template
test ! -e pbx-agent/rtcp-quality/verify-bpf.sh

if git rev-parse --is-inside-work-tree >/dev/null 2>&1 && \
    git ls-files --error-unmatch .env >/dev/null 2>&1; then
  echo ".env must never be tracked by Git" >&2
  exit 1
fi

echo "Repository validation passed."
