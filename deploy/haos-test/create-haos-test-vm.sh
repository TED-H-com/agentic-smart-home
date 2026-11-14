#!/bin/bash
# Skeleton for a disposable official HAOS test VM.
# HARD RULES: refuse to run on pve3; refuse VMID 100; never reference VM 100's disks.
set -euo pipefail

VMID="${VMID:-110}"
NAME="${NAME:-haos-test}"
NODE_EXPECT="${NODE_EXPECT:-pve1}"
FORBIDDEN_NODE="pve3"
FORBIDDEN_VMID="100"

host="$(hostname -s)"
if [ "$host" = "$FORBIDDEN_NODE" ]; then
  echo "ERROR: will not create a test HAOS VM on ${FORBIDDEN_NODE} (production HA lives there)." >&2
  exit 1
fi
if [ "$VMID" = "$FORBIDDEN_VMID" ]; then
  echo "ERROR: VMID ${FORBIDDEN_VMID} is the production HAOS VM. Pick another id (default 110)." >&2
  exit 1
fi
if [ "$host" != "$NODE_EXPECT" ]; then
  echo "WARNING: recommended node is ${NODE_EXPECT}, this host is ${host}." >&2
fi
if qm status "$FORBIDDEN_VMID" >/dev/null 2>&1; then
  echo "INFO: production VM ${FORBIDDEN_VMID} exists on the cluster; this script will not touch it."
fi
if qm status "$VMID" >/dev/null 2>&1; then
  echo "ERROR: VM $VMID already exists" >&2
  qm config "$VMID"
  exit 1
fi

echo "Refusing to import automatically: place an official HAOS disk image at \$HAOS_IMG,"
echo "then import it yourself on ${host} as VM ${VMID} (${NAME})."
echo "Example (review before running):"
echo "  qm create ${VMID} --name ${NAME} --memory 2048 --cores 2 --net0 virtio,bridge=vmbr0 --scsihw virtio-scsi-single --ostype l26 --machine q35"
echo "  qm disk import ${VMID} /path/to/official-haos.qcow2 local-zfs"
echo "  # then attach the imported disk, add a serial console, and start"
echo "Do not add this VM to the production HA group of VM 100."
exit 2
