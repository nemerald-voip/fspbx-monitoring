#!/usr/bin/env sh
set -eu

project_dir=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
test_root=$(mktemp -d)
cleanup() {
  rm -rf -- "$test_root"
}
trap cleanup EXIT HUP INT TERM

source_dir=$test_root/source
install_dir=$test_root/deployment
unit_dir=$test_root/systemd
fake_bin=$test_root/bin
tracked_files=$test_root/tracked-files
os_release_file=$test_root/os-release

install -d -m 0755 "$source_dir" "$install_dir" "$unit_dir" "$fake_bin"
git -C "$project_dir" ls-files >"$tracked_files"
(
  cd "$project_dir"
  tar --verbatim-files-from --no-unquote -cf - -T "$tracked_files"
) | (
  cd "$source_dir"
  tar -xf -
)

cat >"$fake_bin/id" <<'EOF'
#!/usr/bin/env sh
if [ "${1:-}" = -u ]; then
  echo 0
else
  exec /usr/bin/id "$@"
fi
EOF
cat >"$fake_bin/ps" <<'EOF'
#!/usr/bin/env sh
echo systemd
EOF
cat >"$fake_bin/systemctl" <<'EOF'
#!/usr/bin/env sh
exit 0
EOF
cat >"$fake_bin/docker" <<'EOF'
#!/usr/bin/env sh
exit 0
EOF
chmod 0755 "$fake_bin/id" "$fake_bin/ps" "$fake_bin/systemctl" \
  "$fake_bin/docker"
printf 'ID=debian\nVERSION_CODENAME=bookworm\n' >"$os_release_file"

install -d -m 0755 "$source_dir/prometheus/targets" \
  "$source_dir/alertmanager" "$source_dir/backups" "$source_dir/data" \
  "$source_dir/docs/__pycache__"
printf '%s\n' source-secret >"$source_dir/.env"
printf '%s\n' source-target >"$source_dir/prometheus/targets/source.yml"
printf '%s\n' source-alert >"$source_dir/alertmanager/alertmanager.local.yml"
printf '%s\n' source-backup >"$source_dir/backups/source.txt"
printf '%s\n' source-data >"$source_dir/data/source.txt"
printf '%s\n' source-cache >"$source_dir/docs/__pycache__/source.pyc"
printf '%s\n' source-capture >"$source_dir/source.pcap"

install -d -m 0755 "$install_dir/prometheus/targets" "$install_dir/alertmanager"
printf '%s\n' preserved-secret >"$install_dir/.env"
chmod 0600 "$install_dir/.env"
printf '%s\n' preserved-target >"$install_dir/prometheus/targets/local.yml"
printf '%s\n' preserved-alert >"$install_dir/alertmanager/alertmanager.local.yml"
printf '%s\n' preserved-user-file >"$install_dir/operator-notes.txt"

run_installer() {
  PATH="$fake_bin:$PATH" \
    MONITORING_OS_RELEASE_FILE="$os_release_file" \
    MONITORING_SYSTEMD_UNIT_DIR="$unit_dir" \
    "$source_dir/install.sh" \
      --source-dir "$source_dir" \
      --install-dir "$install_dir" \
      --skip-docker --no-secrets --no-start >/dev/null
}

run_installer

[ "$(cat "$install_dir/.env")" = preserved-secret ]
[ "$(stat -c '%a' "$install_dir/.env")" = 600 ]
[ "$(cat "$install_dir/prometheus/targets/local.yml")" = preserved-target ]
[ "$(cat "$install_dir/alertmanager/alertmanager.local.yml")" = preserved-alert ]
[ "$(cat "$install_dir/operator-notes.txt")" = preserved-user-file ]
[ ! -e "$install_dir/prometheus/targets/source.yml" ]
[ ! -e "$install_dir/backups/source.txt" ]
[ ! -e "$install_dir/data/source.txt" ]
[ ! -e "$install_dir/docs/__pycache__/source.pyc" ]
[ ! -e "$install_dir/source.pcap" ]
[ -s "$install_dir/.installed-files" ]
grep -Fq 'source=local:' "$install_dir/.installed-version"
grep -Fq 'revision=unknown' "$install_dir/.installed-version"
grep -Fq "$install_dir" "$unit_dir/monitoring-reconcile.service"

rm -f -- "$source_dir/SECURITY.md"
run_installer

[ ! -e "$install_dir/SECURITY.md" ]
[ "$(cat "$install_dir/.env")" = preserved-secret ]
[ "$(cat "$install_dir/prometheus/targets/local.yml")" = preserved-target ]
[ "$(cat "$install_dir/operator-notes.txt")" = preserved-user-file ]

echo "Installer update test passed."
