#!/usr/bin/env sh
# Compatibility entry point for existing fspbx-hep-firewall.service installs.
set -eu
script_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
exec "$script_dir/apply-pbx-access.sh" "$@"
