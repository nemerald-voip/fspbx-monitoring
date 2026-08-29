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
grep -q '^ExecStart=/usr/local/sbin/heplify -config /etc/heplify-rtcp/heplify.json$' \
  pbx-agent/rtcp-quality/heplify-rtcp.service
grep -q '^ExecStartPost=+/usr/local/libexec/verify-heplify-rtcp-bpf$' \
  pbx-agent/rtcp-quality/heplify-rtcp.service
grep -q '^CPUWeight=10$' pbx-agent/rtcp-quality/heplify-rtcp.service
grep -q '^MemoryMax=512M$' pbx-agent/rtcp-quality/heplify-rtcp.service
grep -q '^MemorySwapMax=0$' pbx-agent/rtcp-quality/heplify-rtcp.service
grep -q '^TasksMax=128$' pbx-agent/rtcp-quality/heplify-rtcp.service
grep -q '^OOMScoreAdjust=500$' pbx-agent/rtcp-quality/heplify-rtcp.service
grep -q '"bpf_filter": ".*portrange @@SIP_START@@-@@SIP_END@@' \
  pbx-agent/rtcp-quality/heplify.json.template
grep -q '"bpf_filter": "ip and udp and portrange @@RTP_START@@-@@RTP_END@@' \
  pbx-agent/rtcp-quality/heplify.json.template
grep -q '"write_file": ""' pbx-agent/rtcp-quality/heplify.json.template
grep -q '"enable": false' pbx-agent/rtcp-quality/heplify.json.template

if git rev-parse --is-inside-work-tree >/dev/null 2>&1 && \
    git ls-files --error-unmatch .env >/dev/null 2>&1; then
  echo ".env must never be tracked by Git" >&2
  exit 1
fi

echo "Repository validation passed."
