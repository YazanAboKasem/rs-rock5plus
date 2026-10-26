#!/usr/bin/env python3
"""
One-time GPS configuration for a Quectel EC25-style modem (run with sudo).

A new modem can ship with NMEA output routed away from the USB NMEA port
(if01) or with GPS auto-start off, so even with the GNSS engine enabled the
agent reads nothing. This prints the current GPS config, then sets (saved in
the modem's NVRAM, so it survives reboots and doesn't depend on ModemManager):
  - outport  = usbnmea   (NMEA sentences on the USB NMEA port)
  - nmeasrc  = 1         (NMEA sentences available)
  - gpsnmeatype = 31     (GGA/RMC/GSV/GSA/VTG)
  - autogps  = 1         (engine starts by itself on every power-up)
  - AT+QGPS=1            (start the engine now)

ModemManager keeps the AT ports open and reads the modem's replies itself,
so it is stopped while the commands run and started again afterwards (the
internet connection drops for ~30s and reconnects automatically).
"""
import glob
import os
import subprocess
import sys
import time

import serial

if os.geteuid() != 0:
    sys.exit("Run with sudo: sudo python3 fix-modem-gps.py")

ports = sorted(glob.glob("/dev/serial/by-id/usb-Quectel_*-if02-port0"))
if not ports:
    sys.exit("No Quectel AT port found")
port = ports[0]
print("AT port:", port, flush=True)


def at(ser, cmd, wait=3.0):
    ser.reset_input_buffer()
    ser.write((cmd + "\r\n").encode())
    resp = ""
    deadline = time.time() + wait
    while time.time() < deadline:
        resp += ser.read(ser.in_waiting or 1).decode(errors="ignore")
        if "OK" in resp or "ERROR" in resp:
            break
    resp = resp.strip()
    shown = resp.replace("\r", "").replace("\n", " | ") or "(no reply)"
    print(f"> {cmd}\n  {shown}", flush=True)
    return resp


def configure():
    with serial.Serial(port, 115200, timeout=1) as ser:
        print("\n--- current config ---", flush=True)
        at(ser, "ATE0")
        at(ser, "AT+QGPS?")
        at(ser, 'AT+QGPSCFG="outport"')
        at(ser, 'AT+QGPSCFG="nmeasrc"')
        at(ser, 'AT+QGPSCFG="gpsnmeatype"')
        at(ser, 'AT+QGPSCFG="autogps"')

        print("\n--- applying ---", flush=True)
        at(ser, 'AT+QGPSCFG="outport","usbnmea"')
        at(ser, 'AT+QGPSCFG="nmeasrc",1')
        at(ser, 'AT+QGPSCFG="gpsnmeatype",31')
        at(ser, 'AT+QGPSCFG="autogps",1')
        at(ser, "AT+QGPS=1")  # "+CME ERROR: 504" = already running, fine

        print("\n--- after ---", flush=True)
        at(ser, "AT+QGPS?")
        at(ser, 'AT+QGPSCFG="outport"')
        at(ser, "AT+QGPSLOC=2")  # "+CME ERROR: 516" = no fix yet

    nmea = sorted(glob.glob("/dev/serial/by-id/usb-Quectel_*-if01-port0"))
    if nmea:
        print("\n--- NMEA sample from", nmea[0], "(5s) ---", flush=True)
        with serial.Serial(nmea[0], 115200, timeout=5) as s:
            print(s.read(600).decode(errors="ignore") or "(no data)", flush=True)


print("Stopping ModemManager (internet drops briefly)...", flush=True)
subprocess.run(["systemctl", "stop", "ModemManager"], check=False)
time.sleep(2)
try:
    configure()
finally:
    print("\nStarting ModemManager again...", flush=True)
    subprocess.run(["systemctl", "start", "ModemManager"], check=False)
    # The agent re-enables GPS through ModemManager when it reopens the port;
    # restart it so that happens once the modem is back.
    time.sleep(20)
    subprocess.run(["systemctl", "restart", "roadshield-surveillance.service"], check=False)
    print("Done. Internet should be back within ~30s.", flush=True)
