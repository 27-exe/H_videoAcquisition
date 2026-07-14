#!/usr/bin/env bash
# videoAcq-hk swap setup: 4G swapfile + swappiness=10
# Run once on fresh HK install. Idempotent (re-runnable).
#
# Usage: sudo bash deploy/hk/swap-setup.sh
#
# After running, verify:
#   swapon --show        -> /swapfile 4G
#   cat /proc/sys/vm/swappiness -> 10

set -euo pipefail

SWAPFILE="${SWAPFILE:-/swapfile}"
SWAPSIZE="${SWAPSIZE:-4G}"
SWAPPINESS="${SWAPPINESS:-10}"

if [[ "$EUID" -ne 0 ]]; then
  echo "must run as root (use sudo)" >&2
  exit 1
fi

# 1) swapfile
if [[ -f "$SWAPFILE" ]]; then
  echo "swapfile $SWAPFILE already exists, skipping fallocate"
else
  echo "creating $SWAPFILE ($SWAPSIZE)..."
  fallocate -l "$SWAPSIZE" "$SWAPFILE"
  chmod 600 "$SWAPFILE"
  mkswap "$SWAPFILE"
fi

# 2) swapon (if not already)
if swapon --show | grep -q "$(basename "$SWAPFILE")"; then
  echo "$SWAPFILE already enabled, skipping swapon"
else
  swapon "$SWAPFILE"
fi

# 3) fstab entry
if grep -q "^$SWAPFILE" /etc/fstab 2>/dev/null; then
  echo "fstab already has $SWAPFILE, skipping"
else
  echo "$SWAPFILE none swap sw 0 0" >> /etc/fstab
fi

# 4) swappiness
echo "vm.swappiness=$SWAPPINESS" > /etc/sysctl.d/99-swap.conf
sysctl -p /etc/sysctl.d/99-swap.conf

# 5) verify
echo "----"
swapon --show
echo "swappiness=$(cat /proc/sys/vm/swappiness)"
echo "swapfile setup complete"
