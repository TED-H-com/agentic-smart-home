#!/bin/bash
# Fail if the tree looks like it contains credentials. Public keys and placeholders are allowed.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$root"

failed=0
# Private key blocks or raw token-looking filenames that should never be committed
while IFS= read -r -d '' f; do
  case "$f" in
    .git/*) continue ;;
  esac
  base="$(basename "$f")"
  if [[ "$base" == "id_rsa" || "$base" == "id_ed25519" || "$base" == *.pem ]]; then
    echo "forbidden filename: $f" >&2
    failed=1
  fi
  if grep -q --binary-files=without-match -E 'BEGIN (OPENSSH |RSA |EC )?PRIVATE KEY' "$f" 2>/dev/null; then
    echo "private key material: $f" >&2
    failed=1
  fi
  if grep -q --binary-files=without-match -E 'ya29\.[A-Za-z0-9_-]{20,}' "$f" 2>/dev/null; then
    echo "oauth-token-like content: $f" >&2
    failed=1
  fi
done < <(find . -type f ! -path './.git/*' -print0)

if [ "$failed" -ne 0 ]; then
  exit 1
fi
echo "no-secrets: ok"
