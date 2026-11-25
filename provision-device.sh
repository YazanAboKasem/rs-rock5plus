#!/bin/bash
# =============================================================================
# RoadShield — one-time device provisioning (run once per device with sudo):
#
#     cd ~/rs-rock5plus && sudo ./provision-device.sh
#
# Safe to re-run. After this the device never needs a password again:
#   1. sudoers: the service user may restart the service, reboot and repair
#      the modem GPS (one file, same on every device). The GPS script is
#      copied to a root-owned path first: a sudo rule on a file the service
#      user can edit would hand that user full root.
#   2. polkit: the service user may control the modem through ModemManager.
#   3. The control-room server's key is in ~/.ssh/authorized_keys, so the
#      dashboard's Access Terminal always lets it in.
#   4. The modem's GPS is configured to start by itself on every power-up.
# =============================================================================
set -euo pipefail

SERVICE_USER="${1:-${SUDO_USER:?run with sudo from the service user, or pass the user: sudo $0 q8b7}}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
USER_HOME="$(getent passwd "$SERVICE_USER" | cut -d: -f6)"
SERVER_KEY="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIF4wgvraZuiPP9kGK84sLveNgOmep50UYMIdAYEFOIba claude@controlroom.roadshield.ae"

if [[ $EUID -ne 0 ]]; then
    echo "Run with sudo: sudo $0" >&2
    exit 1
fi

# ── 1. sudoers ───────────────────────────────────────────────────────────────
install -d -m 755 -o root -g root /usr/local/lib/roadshield
install -m 755 -o root -g root "$SCRIPT_DIR/fix-modem-gps.py" /usr/local/lib/roadshield/fix-modem-gps.py
TMP=$(mktemp)
cat > "$TMP" <<EOF
# RoadShield device agent — installed by provision-device.sh
$SERVICE_USER ALL=(root) NOPASSWD: /usr/bin/systemctl restart roadshield-surveillance.service
$SERVICE_USER ALL=(root) NOPASSWD: /usr/sbin/reboot
$SERVICE_USER ALL=(root) NOPASSWD: /usr/bin/python3 /usr/local/lib/roadshield/fix-modem-gps.py
EOF
visudo -cf "$TMP" >/dev/null
install -m 440 -o root -g root "$TMP" /etc/sudoers.d/roadshield
rm -f "$TMP"
# Older per-feature files (some name another device's user after cloning).
rm -f /etc/sudoers.d/roadshield-restart /etc/sudoers.d/roadshield-gps
echo "✓ sudoers: /etc/sudoers.d/roadshield"

# ── 2. polkit + serial access ────────────────────────────────────────────────
cat > /etc/polkit-1/rules.d/50-roadshield-modemmanager.rules <<EOF
// RoadShield: allow the device agent to enable GPS / manage the modem.
polkit.addRule(function(action, subject) {
    if (action.id.indexOf("org.freedesktop.ModemManager1.") == 0 &&
        subject.user == "$SERVICE_USER") {
        return polkit.Result.YES;
    }
});
EOF
chmod 644 /etc/polkit-1/rules.d/50-roadshield-modemmanager.rules
usermod -aG dialout "$SERVICE_USER"
echo "✓ polkit + dialout"

# ── 3. control-room server key ───────────────────────────────────────────────
install -d -m 700 -o "$SERVICE_USER" -g "$(id -gn "$SERVICE_USER")" "$USER_HOME/.ssh"
AK="$USER_HOME/.ssh/authorized_keys"
touch "$AK"
grep -qF "$SERVER_KEY" "$AK" || echo "$SERVER_KEY" >> "$AK"
chown "$SERVICE_USER:$(id -gn "$SERVICE_USER")" "$AK"
chmod 600 "$AK"
echo "✓ server key in $AK"

# ── 3b. image libraries for data_collector.py (road vanishing point) ────────
# NEEDRESTART_MODE=l: list only — needrestart would otherwise restart sshd /
# services mid-install and cut the session running this script.
if ! python3 -c "import cv2, numpy" 2>/dev/null; then
    echo "Installing python3-opencv (large download over cellular)..."
    DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l apt-get install -y -q python3-opencv python3-numpy >/tmp/provision-apt.log 2>&1 \
        && echo "✓ python3-opencv" || echo "! python3-opencv install failed, see /tmp/provision-apt.log"
fi

# ── 4. modem GPS (saved in the modem; restarts the service when done) ────────
if ls /dev/serial/by-id/usb-Quectel_*-if02-port0 >/dev/null 2>&1; then
    echo "Configuring modem GPS (internet drops ~30s)..."
    /usr/bin/python3 /usr/local/lib/roadshield/fix-modem-gps.py
else
    echo "- no Quectel modem found, GPS step skipped"
    systemctl restart roadshield-surveillance.service
fi
echo "✓ provisioning done for $SERVICE_USER"
