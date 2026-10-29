#!/usr/bin/env bash
# Optional hard firewall rule (needs root): drop and log every packet that a given
# user sends to anything but loopback. Run Clara as that user and the kernel
# refuses egress regardless of Landlock, Python or model behaviour, UDP and ICMP
# included. This is the belt to Landlock's braces.
#
#   sudo scripts/nft-clara-egress.sh install clara-user [192.168.178.0/24]   # allow LAN for the phone app
#   sudo scripts/nft-clara-egress.sh status
#   sudo scripts/nft-clara-egress.sh remove
#
# Logged drops appear in `journalctl -k -g clara-egress` (or dmesg).
set -eu
ACTION="${1:-status}"
USER_NAME="${2:-}"
LAN="${3:-}"
case "$ACTION" in
  install)
    [ -n "$USER_NAME" ] || { echo "usage: $0 install USER [LAN_CIDR]" >&2; exit 2; }
    id "$USER_NAME" >/dev/null
    nft list table inet clara >/dev/null 2>&1 && nft delete table inet clara
    nft add table inet clara
    nft add chain inet clara output '{ type filter hook output priority 0; policy accept; }'
    nft add rule inet clara output meta skuid "$USER_NAME" oif lo accept
    if [ -n "$LAN" ]; then
      nft add rule inet clara output meta skuid "$USER_NAME" ip daddr "$LAN" accept
    fi
    nft add rule inet clara output meta skuid "$USER_NAME" counter log prefix '"clara-egress "' drop
    echo "installed: user $USER_NAME may only talk to loopback${LAN:+ and $LAN}; everything else is dropped and logged"
    ;;
  remove)
    nft delete table inet clara && echo "removed"
    ;;
  status)
    nft list table inet clara 2>/dev/null || echo "no clara table installed"
    ;;
  *) echo "usage: $0 install USER [LAN_CIDR] | status | remove" >&2; exit 2 ;;
esac
