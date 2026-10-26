#!/bin/bash
# One-time setup (run with sudo): let the service user control the modem's GPS
# through ModemManager. Without this, `mmcli --location-enable-gps-unmanaged`
# fails with "PolicyKit authorization failed", and the AT-port fallback can't
# work either since ModemManager holds the AT ports — so the GNSS engine never
# turns on and no coordinates reach the server.
set -e

SERVICE_USER="${1:-q8b7}"
RULE=/etc/polkit-1/rules.d/50-roadshield-modemmanager.rules

if [[ $EUID -ne 0 ]]; then
    echo "Run with sudo: sudo $0" >&2
    exit 1
fi

cat > "$RULE" <<EOF
// RoadShield: allow the device agent to enable GPS / manage the modem.
polkit.addRule(function(action, subject) {
    if (action.id.indexOf("org.freedesktop.ModemManager1.") == 0 &&
        subject.user == "$SERVICE_USER") {
        return polkit.Result.YES;
    }
});
EOF
chmod 644 "$RULE"
usermod -aG dialout "$SERVICE_USER"
echo "Installed $RULE"

# Turn GPS on now, then restart the agent so it picks up the new modem.
MODEM=$(mmcli -L | grep -o '/Modem/[0-9]*' | head -1 | cut -d/ -f3)
if [[ -n "$MODEM" ]]; then
    mmcli -m "$MODEM" --location-enable-gps-unmanaged && echo "GPS enabled on modem $MODEM"
fi
systemctl restart roadshield-surveillance.service
echo "roadshield-surveillance restarted"
