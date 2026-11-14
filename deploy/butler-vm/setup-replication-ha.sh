#!/bin/bash
# Apply ZFS replication and the HA rule used for butler VM 102 (source pve2).
# Run as root on the current source node (pve2). Does not migrate or reboot.
# Does not modify VM 100 or VM 101.
set -euo pipefail

VMID="${VMID:-102}"
SOURCE="${SOURCE:-pve2}"
REPL_A="${REPL_A:-pve1}"
REPL_B="${REPL_B:-pve4}"

echo "host=$(hostname)"
if [ "$(hostname -s)" != "$SOURCE" ]; then
  echo "WARNING: expected to run on $SOURCE; this host is $(hostname -s)" >&2
fi

pvesr create-local-job "${VMID}-0" "$REPL_A" --schedule '*/15' --comment "butler $SOURCE -> $REPL_A"
pvesr create-local-job "${VMID}-1" "$REPL_B" --schedule '*/15' --comment "butler $SOURCE -> $REPL_B"

ha-manager add "vm:${VMID}" --state started --max_relocate 2 --max_restart 3 --auto-rebalance 0 --comment 'Butler VM'

pvesh create /cluster/ha/rules \
  --rule butler-nodes \
  --type node-affinity \
  --affinity positive \
  --strict 1 \
  --nodes "${SOURCE}:2,${REPL_A}:1,${REPL_B}:1" \
  --resources "vm:${VMID}" \
  --comment "butler prefers ${SOURCE}; failover only to replica nodes ${REPL_A},${REPL_B}"

pvesr list
ha-manager status
echo "Note: ${REPL_B} may not have enough RAM to run mgmt 8G + butler 8G at once. ${REPL_A} is the practical failover."
