#!/usr/bin/env sh
set -eu

project_dir=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
cd "$project_dir"

for required in README.md AGENTS.md SECURITY.md \
  docs/GETTING_STARTED.md docs/ARCHITECTURE.md docs/OPERATIONS.md \
  docs/SYNTHETIC_SIP.md pbx-agent/README.md alertmanager/README.md; do
  if [ ! -s "$required" ]; then
    echo "Missing or empty documentation file: $required" >&2
    exit 1
  fi
done

if grep -RInE 'OWNER/REPOSITORY|GITHUB_OWNER' \
    README.md AGENTS.md docs pbx-agent alertmanager; then
  echo "Documentation still contains a repository placeholder" >&2
  exit 1
fi

find . -type f -name '*.md' -not -path './.git/*' -print | \
  while IFS= read -r document; do
    links=$(grep -oE '\]\([^)]+\)' "$document" || true)
    printf '%s\n' "$links" | sed -e 's/^](//' -e 's/)$//' | \
      while IFS= read -r target; do
        case "$target" in
          ''|'#'*|http://*|https://*|mailto:*) continue ;;
        esac
        target=${target%%#*}
        target=${target%%\?*}
        case "$target" in
          /*) continue ;;
        esac
        resolved=$(dirname "$document")/$target
        if [ ! -e "$resolved" ]; then
          echo "Broken local link in $document: $target" >&2
          exit 1
        fi
      done
  done

echo "Documentation validation passed."
