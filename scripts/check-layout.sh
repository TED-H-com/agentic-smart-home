#!/bin/bash
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$root"
required=(
  README.md
  docs/ARCHIVE.md
  docs/DEPLOY.md
  docs/TEST-HA.md
  docs/CONNECT-REAL-HA.md
  deploy/butler-vm/create-butler-vm.sh
  deploy/butler-vm/butler-user-data.yaml
  deploy/butler-vm/setup-replication-ha.sh
  deploy/butler-vm/hosts.pin
  deploy/butler-vm/ssh-config.snippet
  deploy/haos-test/create-haos-test-vm.sh
  hermes/SOUL.md
  hermes/config.template.yaml
  hermes/prompts/system.md
  hermes/skills/household-safety/SKILL.md
  hermes/skills/ha-official-mcp/SKILL.md
  ha-mcp/README.md
  ha-mcp/mcp.example.json
)
missing=0
for f in "${required[@]}"; do
  if [ ! -e "$f" ]; then
    echo "missing $f" >&2
    missing=1
  fi
done
# Old appliance paths must not return on this mainline
for f in host/controller host/compose.yaml controller runner bridge custom_components web/package.json; do
  if [ -e "$f" ]; then
    echo "old appliance path should not be on this branch: $f" >&2
    missing=1
  fi
done
# Safety guards in the test-VM script
if ! grep -q 'FORBIDDEN_NODE="pve3"' deploy/haos-test/create-haos-test-vm.sh; then
  echo "haos-test script must refuse pve3" >&2
  missing=1
fi
if ! grep -q 'FORBIDDEN_VMID="100"' deploy/haos-test/create-haos-test-vm.sh; then
  echo "haos-test script must refuse VM 100" >&2
  missing=1
fi
if [ "$missing" -ne 0 ]; then
  exit 1
fi
echo "layout: ok"
