#!/usr/bin/env python3
"""
wwan-teller — houdt per boot bij hoeveel data er over 4G (wwan0) ging.

Aanleiding: op 14-10-2026 verbruikte de dongle 73 MB op één dag, terwijl een
normale dag 2-8 MB is. Achteraf viel niet meer te zien waardoor. Deze teller
maakt elke volgende uitschieter per boot zichtbaar, met de verbindingen die op
dat moment openstonden.

Hoe het werkt (een timer roept --snapshot elke 2 minuten aan):
- De tellers in /sys/class/net/wwan0/statistics beginnen bij elke boot op 0.
- Elke snapshot schrijft de stand van deze boot weg in STATE.
- Is de boot-ID in STATE anders dan de huidige, dan is er een boot voorbij:
  die gaat als één regel in LOG (JSON per regel) en als record naar de
  ontvanger op de dongle, zodat HA hem ziet (topic autopi/wwan/boot).
- Groeit het verbruik tussen twee snapshots harder dan PIEK_BYTES, dan komen
  de open verbindingen (ss -tunp) erbij in PIEK_LOG. Dat is het spoor dat bij
  de 73 MB ontbrak.

    wwan_teller.py --snapshot     # door de timer
    wwan_teller.py --rapport      # overzicht per dag en per boot
"""

import argparse
import gzip
import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone

IFACE = os.environ.get("WWAN_IFACE", "wwan0")
SYS_NET = os.environ.get("SYS_NET", "/sys/class/net")
BOOT_ID_BESTAND = os.environ.get("BOOT_ID_BESTAND", "/proc/sys/kernel/random/boot_id")
UPTIME_BESTAND = os.environ.get("UPTIME_BESTAND", "/proc/uptime")
DIR = os.environ.get("WWAN_TELLER_DIR", "/var/lib/wwan-teller")
STATE = os.path.join(DIR, "huidig.json")
LOG = os.path.join(DIR, "boots.jsonl")
PIEK_LOG = os.path.join(DIR, "pieken.log")
PIEK_BYTES = int(os.environ.get("PIEK_BYTES", str(2 * 1024 * 1024)))  # per snapshot
INGEST_URL = os.environ.get("INGEST_URL", "http://127.0.0.1:8181/ingest/lokaal")
LOG_MAX_REGELS = int(os.environ.get("LOG_MAX_REGELS", "5000"))


def lees(pad, standaard=None):
    try:
        with open(pad) as fh:
            return fh.read().strip()
    except OSError:
        return standaard


def tellers():
    """(rx, tx) in bytes sinds de boot; (0, 0) als wwan0 er niet is."""
    basis = os.path.join(SYS_NET, IFACE, "statistics")
    rx = lees(os.path.join(basis, "rx_bytes"))
    tx = lees(os.path.join(basis, "tx_bytes"))
    try:
        return int(rx), int(tx)
    except (TypeError, ValueError):
        return 0, 0


def nu_iso(ts=None):
    return datetime.fromtimestamp(ts if ts is not None else time.time(),
                                  timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def schrijf_json(pad, data):
    tijdelijk = pad + ".tmp"
    with open(tijdelijk, "w") as fh:
        json.dump(data, fh)
    os.replace(tijdelijk, pad)


def voeg_toe(pad, regel):
    with open(pad, "a") as fh:
        fh.write(regel + "\n")


def kort_log_in():
    try:
        with open(LOG) as fh:
            regels = fh.readlines()
    except OSError:
        return
    if len(regels) > LOG_MAX_REGELS:
        with open(LOG, "w") as fh:
            fh.writelines(regels[-LOG_MAX_REGELS:])


def post_record(record):
    """Naar de ontvanger op de dongle. Mislukt het, dan staat het nog in LOG."""
    body = gzip.compress(json.dumps([record]).encode("utf-8"))
    req = urllib.request.Request(
        INGEST_URL, data=body, method="POST",
        headers={"Content-Type": "application/json", "Content-Encoding": "gzip",
                 "User-Agent": "wwan-teller/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception as exc:  # noqa: BLE001
        print("versturen naar ontvanger mislukt: %s" % exc, file=sys.stderr)
        return False


def verbindingen():
    try:
        res = subprocess.run(["ss", "-tunp"], capture_output=True, text=True, timeout=10)
        return res.stdout
    except (OSError, subprocess.SubprocessError) as exc:
        return "ss mislukt: %s" % exc


def snapshot():
    os.makedirs(DIR, exist_ok=True)
    boot = lees(BOOT_ID_BESTAND, "onbekend")
    try:
        uptime = float(lees(UPTIME_BESTAND, "0").split()[0])
    except (ValueError, IndexError):
        uptime = 0.0
    nu = time.time()
    rx, tx = tellers()

    vorige = None
    try:
        with open(STATE) as fh:
            vorige = json.load(fh)
    except (OSError, ValueError):
        pass

    if vorige and vorige.get("boot") != boot:
        # Een boot is voorbij: vastleggen. De laatste snapshot is hooguit
        # 2 minuten oud, dus er ontbreekt hooguit het staartje voor het slapen.
        afgerond = {
            "@t": "wwan.boot", "@ts": nu_iso(vorige["eind"]).rstrip("Z"),
            "boot": vorige["boot"], "start": nu_iso(vorige["start"]),
            "eind": nu_iso(vorige["eind"]), "rx_bytes": vorige["rx"],
            "tx_bytes": vorige["tx"], "mb": round((vorige["rx"] + vorige["tx"]) / 1e6, 3),
        }
        voeg_toe(LOG, json.dumps(afgerond))
        kort_log_in()
        print("vorige boot %s: %.3f MB over %s" % (vorige["boot"][:8], afgerond["mb"], IFACE))
        post_record(afgerond)
        vorige = None

    if vorige:
        delta = (rx + tx) - (vorige["rx"] + vorige["tx"])
        if delta > PIEK_BYTES:
            voeg_toe(PIEK_LOG, "=== %s  +%.2f MB in %.0f s over %s\n%s" % (
                nu_iso(nu), delta / 1e6, nu - vorige["eind"], IFACE, verbindingen()))
            print("piek: +%.2f MB sinds vorige snapshot, verbindingen gelogd" % (delta / 1e6))

    schrijf_json(STATE, {
        "boot": boot, "start": (vorige or {}).get("start", nu - uptime),
        "eind": nu, "rx": rx, "tx": tx,
    })
    return 0


def rapport():
    rijen = []
    try:
        with open(LOG) as fh:
            for regel in fh:
                try:
                    rijen.append(json.loads(regel))
                except ValueError:
                    pass
    except OSError:
        pass
    try:
        with open(STATE) as fh:
            h = json.load(fh)
        rijen.append({"start": nu_iso(h["start"]), "eind": nu_iso(h["eind"]) + " (nu)",
                      "mb": round((h["rx"] + h["tx"]) / 1e6, 3), "boot": h["boot"]})
    except (OSError, ValueError, KeyError):
        pass
    per_dag = {}
    for r in rijen:
        per_dag[r["start"][:10]] = per_dag.get(r["start"][:10], 0) + r["mb"]
    print("Per dag (UTC, op startdatum van de boot):")
    for dag in sorted(per_dag):
        print("  %s  %8.2f MB" % (dag, per_dag[dag]))
    print("\nLaatste 15 boots:")
    for r in rijen[-15:]:
        print("  %s → %s  %8.3f MB  %s" % (r["start"], r["eind"], r["mb"], r["boot"][:8]))
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--snapshot", action="store_true")
    ap.add_argument("--rapport", action="store_true")
    a = ap.parse_args()
    if a.rapport:
        return rapport()
    return snapshot()


if __name__ == "__main__":
    sys.exit(main())
