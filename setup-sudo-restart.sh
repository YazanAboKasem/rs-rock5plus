#!/bin/bash
# One-time setup (run with sudo): let the service user run the dashboard's
# "Restart Service" and "Reboot" buttons. camera-control.py runs these via
# `sudo -n`; without this rule sudo asks for a password, which a systemd
# service can't give it, so both buttons silently do nothing.
set -e

SERVICE_USER="${1:-${SUDO_USER:?pass the service user, e.g. sudo $0 q8b7}}"
RULE=/etc/sudoers.d/roadshield-restart

if [[ $EUID -ne 0 ]]; then
    echo "Run with sudo: sudo $0" >&2
    exit 1
fi

TMP=$(mktemp)
cat > "$TMP" <<EOF
$SERVICE_USER ALL=(root) NOPASSWD: /usr/bin/systemctl restart roadshield-surveillance.service
$SERVICE_USER ALL=(root) NOPASSWD: /usr/sbin/reboot
EOF
visudo -cf "$TMP"
install -m 440 -o root -g root "$TMP" "$RULE"
rm -f "$TMP"
echo "Installed $RULE"
