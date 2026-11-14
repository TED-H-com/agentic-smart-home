#!/bin/bash
# Used 2026-11-14 to create castle VM 102 "butler" on pve2 (Ubuntu 26.04, 4C/8G/64G, 192.168.103.105).
# Run on the target Proxmox node as root. Requires the cloud image at $IMG with the pinned SHA-256.
# Does not touch HAOS VM 100 or mgmt VM 101.

set -euo pipefail

VMID=102
IMG=/root/cloudimg/ubuntu-26.04-server-cloudimg-amd64.img
KEYS=/root/cloudimg/butler-authorized-keys.pub
SNIPPET=/var/lib/vz/snippets/butler-user-data.yaml
IP=192.168.103.105

echo "==== preflight ===="
hostname
echo "NEXTID=$(pvesh get /cluster/nextid)"
if qm status "$VMID" >/dev/null 2>&1; then
  echo "ERROR: VM $VMID already exists" >&2
  qm config "$VMID"
  exit 1
fi
if pvesh get /cluster/resources --type vm --output-format json | grep -qE '"vmid"[[:space:]]*:[[:space:]]*102'; then
  echo "ERROR: VMID 102 already in cluster resources" >&2
  exit 1
fi
NEXT=$(pvesh get /cluster/nextid)
if [ "$NEXT" != "102" ]; then
  echo "ERROR: expected nextid 102, got $NEXT" >&2
  exit 1
fi
test -f "$IMG"
sha256sum -c <<< '5645d0318ad21f643ebb1ac4567514620e9893380b07b0201fda163baa5b7eb5  /root/cloudimg/ubuntu-26.04-server-cloudimg-amd64.img'
echo "IP check:"
ping -c 2 -W 2 "$IP" || true
ip neigh show "$IP" || true
nmap -sn -n -e vmbr0 "$IP" || true

cat > "$KEYS" << 'KEYS'
ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIIIeebOslEqvhv45zmwAYY348M/1SzjH8PZN2D11wfT9 castle4-pve-admin
ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAICGpri7FTVK/AB5ocuz4w7KQAdq5LL+Pdk6hKr37sX0a box-mgmt
KEYS

# snippets already enabled on pve2 local storage; keep mkdir idempotent
pvesm set local --content iso,vztmpl,backup,import,snippets
mkdir -p /var/lib/vz/snippets

cat > "$SNIPPET" << 'CLOUD'
#cloud-config
hostname: butler
fqdn: butler.castle.lan
manage_etc_hosts: true
timezone: America/New_York
ssh_pwauth: false
disable_root: true
users:
  - name: butler
    gecos: Home butler
    groups: [sudo, adm]
    shell: /bin/bash
    lock_passwd: true
    sudo: ALL=(ALL) NOPASSWD:ALL
    ssh_authorized_keys:
      - ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIIIeebOslEqvhv45zmwAYY348M/1SzjH8PZN2D11wfT9 castle4-pve-admin
      - ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAICGpri7FTVK/AB5ocuz4w7KQAdq5LL+Pdk6hKr37sX0a box-mgmt
package_update: true
package_upgrade: false
packages:
  - qemu-guest-agent
  - unattended-upgrades
  - git
  - curl
  - jq
  - python3
  - tmux
apt:
  conf: |
    APT::Periodic::Update-Package-Lists "1";
    APT::Periodic::Unattended-Upgrade "1";
runcmd:
  - systemctl enable --now qemu-guest-agent
  - systemctl enable --now unattended-upgrades || true
  - curl -fsSL https://tailscale.com/install.sh | sh
  - mkdir -p /etc/ssh/sshd_config.d
  - printf 'PasswordAuthentication no\nKbdInteractiveAuthentication no\nPermitRootLogin no\n' > /etc/ssh/sshd_config.d/99-disable-password.conf
  - systemctl reload ssh || systemctl reload sshd || true
final_message: "butler cloud-init done after $UPTIME s"
CLOUD

echo "==== qm create ===="
qm create "$VMID" \
  --name butler \
  --memory 8192 \
  --balloon 0 \
  --cores 4 \
  --sockets 1 \
  --cpu host \
  --net0 virtio,bridge=vmbr0 \
  --scsihw virtio-scsi-single \
  --agent enabled=1 \
  --onboot 1 \
  --ostype l26 \
  --machine q35 \
  --bios seabios \
  --boot order=scsi0 \
  --serial0 socket \
  --vga serial0 \
  --description "Home butler VM (Hermes + HA official MCP). Built 2026-11-14. Preferred node pve2."

echo "==== disk import ===="
qm disk import "$VMID" "$IMG" local-zfs

echo "==== config after import ===="
qm config "$VMID"

DISK=$(qm config "$VMID" | awk -F': ' '/^unused[0-9]+:/ {print $2; exit}')
if [ -z "$DISK" ]; then
  echo "ERROR: no unused disk after import" >&2
  qm config "$VMID"
  exit 1
fi
echo "Imported disk: $DISK"

qm set "$VMID" --scsi0 "${DISK},discard=on,iothread=1,ssd=1"
qm resize "$VMID" scsi0 64G
qm set "$VMID" --ide2 local-zfs:cloudinit
qm set "$VMID" --boot order=scsi0
qm set "$VMID" --cicustom "user=local:snippets/butler-user-data.yaml"
qm set "$VMID" --ipconfig0 ip=192.168.103.105/23,gw=192.168.103.1
qm set "$VMID" --nameserver "192.168.103.1 1.1.1.1"
qm set "$VMID" --searchdomain castle.lan
qm set "$VMID" --ciupgrade 0

echo "==== final config ===="
qm config "$VMID"
echo "==== disk size ===="
zfs list -r rpool/data || true
qemu-img info "/dev/zvol/rpool/data/vm-${VMID}-disk-0" 2>/dev/null || true

echo "==== start ===="
qm start "$VMID"
sleep 2
qm status "$VMID"
echo CREATE_OK
