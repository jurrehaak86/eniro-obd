#!/usr/bin/env python3
"""
autopi-diag — stuurt de verbindingsdiagnostiek van de dongle naar de ingest.

Waarom: de GPS-kwaliteit zit al in elk `track.pos`-record (`nsat`, `fix`), maar
van 4G weten we achteraf niets. Zonder RSRP-geschiedenis kun je een gat in de
data niet onderscheiden van slechte ontvangst, en kun je ook niet zien of de
montageplek na een paar maanden nog doet wat hij bij de meting deed.

Landt op `autopi/device/diag` via dezelfde weg als de rest.

**Meten op 4G, versturen over wifi.** Onderweg schrijft hij elke meting naar een
lokale buffer en stuurt niets; zodra de dongle op een onbemeten verbinding zit
gaat de hele buffer in één keer mee. Dat kost geen mobiele data en levert toch
volle resolutie over de rit.

Dat is beter dan niet bufferen: het script draait ook zónder verbinding door en
legt vast dát er geen signaal was (`registered: false`, geen `rsrp`). Een storing
staat dus mét meetwaarden in de reeks in plaats van als een gat.

**Wifi-status (v1.3)** gaat apart, als `device.wifi` naar `autopi/device/wifi`:
verbinding (wifi/4G/geen), netwerknaam, signaal, frequentie en linksnelheid.
Op wifi elke run, op 4G alleen bij een wissel (één record bij vertrek), zodat
Home Assistant ziet dat de dongle van het thuisnetwerk af is zonder dat het
elke tien minuten mobiele data kost.

Leest `INGEST_URL` uit dezelfde env als eniro-soc, zodat het geheime pad op één
plek staat.

    sudo ./autopi_diag.py            # eenmalig versturen
    sudo ./autopi_diag.py --dry-run  # alleen tonen
"""

import argparse
import gzip
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

QMI_DEV = os.environ.get("QMI_DEV", "/dev/cdc-wdm0")
INGEST_URL = os.environ.get("INGEST_URL", "")
HTTP_TIMEOUT = float(os.environ.get("HTTP_TIMEOUT", "30"))
USER_AGENT = os.environ.get("USER_AGENT", "autopi-diag/1.1")
METERED_IFACES = set(
    x.strip() for x in os.environ.get("METERED_IFACES", "wwan0,ppp0").split(",")
    if x.strip()
)

# Thuis elke tien minuten meten is vooral ruis in de grafiek: wat je wil weten
# gebeurt onderweg. Op een onbemeten verbinding nemen we daarom hoogstens eens
# per uur een nieuw sample. 0 = altijd meten.
UNMETERED_EVERY = int(os.environ.get("UNMETERED_EVERY", "3600"))

STATE_DIR = os.environ.get("DIAG_STATE_DIR", "/var/lib/autopi-diag")
BUFFER = os.path.join(STATE_DIR, "buffer.jsonl")
LAATSTE_BESTAND = os.path.join(STATE_DIR, "laatste-onbemeten")
VERBINDING_BESTAND = os.path.join(STATE_DIR, "verbinding")
WIFI_IFACE = os.environ.get("WIFI_IFACE", "wlan0")
WPA_CTRL_DIR = os.environ.get("WPA_CTRL_DIR", "/var/run/wpa_supplicant")
# 2000 records is bij tien minuten ruim twee weken onafgebroken rijden.
BUFFER_MAX = int(os.environ.get("BUFFER_MAX", "2000"))


def qmicli(*args, timeout=15):
    try:
        res = subprocess.run(["qmicli", "-d", QMI_DEV] + list(args),
                             capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return ""
    return res.stdout if res.returncode == 0 else ""


def getal(tekst, patroon):
    m = re.search(patroon, tekst)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def signaal():
    uit = qmicli("--nas-get-signal-info")
    if not uit:
        return {}
    return {k: v for k, v in {
        "rssi": getal(uit, r"RSSI:\s*'(-?[\d.]+)"),
        "rsrq": getal(uit, r"RSRQ:\s*'(-?[\d.]+)"),
        "rsrp": getal(uit, r"RSRP:\s*'(-?[\d.]+)"),
        "snr":  getal(uit, r"SNR:\s*'(-?[\d.]+)"),
    }.items() if v is not None}


def netwerk():
    uit = qmicli("--nas-get-serving-system")
    if not uit:
        return {}
    d = {}
    m = re.search(r"Registration state:\s*'(\w+)'", uit)
    if m:
        d["registered"] = m.group(1) == "registered"
    m = re.search(r"^\s*PS:\s*'(\w+)'", uit, re.M)
    if m:
        d["ps_attached"] = m.group(1) == "attached"
    # Radio interfaces staat boven een genummerde regel met de techniek
    m = re.search(r"Radio interfaces:.*?\[0\]:\s*'(\w+)'", uit, re.S)
    if m:
        d["rat"] = m.group(1)
    m = re.search(r"Description:\s*'([^']+)'", uit)
    if m:
        d["operator"] = m.group(1)
    m = re.search(r"Roaming status:\s*'(\w+)'", uit)
    if m:
        d["roaming"] = m.group(1) == "on"
    for veld, patroon in (("cell_id", r"3GPP cell ID:\s*'(\d+)'"),
                          ("tac", r"LTE tracking area code:\s*'(\d+)'")):
        m = re.search(patroon, uit)
        if m:
            d[veld] = int(m.group(1))
    return d


def route():
    """Welke interface draagt het verkeer, en kost die geld."""
    try:
        res = subprocess.run(["ip", "-4", "route", "show", "default"],
                             capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return {}
    beste, laagste = None, None
    for regel in res.stdout.splitlines():
        v = regel.split()
        if "dev" not in v:
            continue
        dev = v[v.index("dev") + 1]
        metric = 0
        if "metric" in v:
            try:
                metric = int(v[v.index("metric") + 1])
            except (ValueError, IndexError):
                pass
        if laagste is None or metric < laagste:
            beste, laagste = dev, metric
    if beste is None:
        return {"default_iface": None, "metered": True}
    return {"default_iface": beste, "metered": beste in METERED_IFACES}


def wpa_vraag(*commando, iface=None, timeout=3.0):
    """Stelt vragen aan wpa_supplicant over zijn control-socket.

    Eigen client in plaats van wpa_cli: wpa_cli bindt zijn antwoordsocket in
    /tmp, en met PrivateTmp in de unit kan wpa_supplicant daar niet bij (het
    antwoord komt nooit aan; gezien 18-11-2026). Een abstracte socket
    (adres begint met \0) hangt niet aan het bestandssysteem en werkt wel.
    Geeft per commando de tekst terug, of {} als er geen wpa_supplicant is.
    """
    pad = os.path.join(WPA_CTRL_DIR, iface or WIFI_IFACE)
    uit = {}
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    except OSError:
        return uit
    try:
        s.bind("\0autopi-diag-%d" % os.getpid())
        s.settimeout(timeout)
        s.connect(pad)
        for cmd in commando:
            s.send(cmd.encode())
            uit[cmd] = s.recv(4096).decode("utf-8", "replace")
    except OSError:
        return {}
    finally:
        s.close()
    return uit


def sleutels(tekst):
    """'a=1\nb=2' -> {'a': '1', 'b': '2'}"""
    d = {}
    for regel in tekst.splitlines():
        k, _, v = regel.partition("=")
        if k and _:
            d[k.strip()] = v.strip()
    return d


def int_of_none(tekst):
    try:
        return int(tekst)
    except (TypeError, ValueError):
        return None


def wifi():
    """Wifi-velden als de dongle met een netwerk verbonden is, anders {}."""
    a = wpa_vraag("STATUS", "SIGNAL_POLL")
    status = sleutels(a.get("STATUS", ""))
    if status.get("wpa_state") != "COMPLETED":
        return {}
    poll = sleutels(a.get("SIGNAL_POLL", ""))
    d = {"ssid": status.get("ssid")}
    freq = int_of_none(poll.get("FREQUENCY") or status.get("freq"))
    for veld, waarde in (("wifi_dbm", int_of_none(poll.get("RSSI"))),
                         ("freq_mhz", freq),
                         ("linkspeed_mbps", int_of_none(poll.get("LINKSPEED")))):
        if waarde is not None:
            d[veld] = waarde
    if freq:
        d["band"] = "5 GHz" if freq >= 4900 else "2,4 GHz"
    return {k: v for k, v in d.items() if v not in (None, "")}


def verbinding(rt, wf):
    """'wifi', '4G' of 'geen', afgeleid van de default route."""
    dev = rt.get("default_iface")
    if dev is None:
        return "geen"
    if dev == WIFI_IFACE:
        return "wifi"
    if dev in METERED_IFACES:
        return "4G"
    return dev


def vorige_verbinding():
    try:
        with open(VERBINDING_BESTAND) as fh:
            return fh.read().strip()
    except OSError:
        return None


def bewaar_verbinding(waarde):
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(VERBINDING_BESTAND, "w") as fh:
            fh.write(waarde + "\n")
    except OSError:
        pass


def wifi_record(ts, rt):
    """Geeft (record, verbinding). record is None als het deze run niet weg
    hoeft; verbinding is de stand om na een geslaagde post te bewaren."""
    wf = wifi()
    v = verbinding(rt, wf)
    if v != "wifi" and vorige_verbinding() == v:
        return None, v          # op 4G niets nieuws: geen data uitgeven
    rec = {"@ts": ts, "@t": "device.wifi", "verbinding": v}
    rec.update(wf)
    return rec, v


def uptime():
    try:
        with open("/proc/uptime") as fh:
            return {"uptime_s": int(float(fh.read().split()[0]))}
    except (OSError, ValueError, IndexError):
        return {}


def post(records):
    if not INGEST_URL:
        raise RuntimeError("INGEST_URL is niet gezet")
    body = gzip.compress(json.dumps(records, separators=(",", ":")).encode(), 9)
    req = urllib.request.Request(
        INGEST_URL, data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "Content-Encoding": "gzip",
                 "User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        if resp.status != 200:
            raise RuntimeError("HTTP %s" % resp.status)
    return len(body)


def recent_onbemeten_gemeten():
    """True als we op wifi zitten en er recent al een sample is bewaard."""
    if UNMETERED_EVERY <= 0:
        return False
    try:
        vorige = os.path.getmtime(LAATSTE_BESTAND)
    except OSError:
        return False
    return (time.time() - vorige) < UNMETERED_EVERY


def stip_onbemeten():
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(LAATSTE_BESTAND, "w"):
            pass
    except OSError:
        pass


def buffer_lees():
    try:
        with open(BUFFER) as fh:
            uit = []
            for regel in fh:
                regel = regel.strip()
                if regel:
                    try:
                        uit.append(json.loads(regel))
                    except ValueError:
                        pass
            return uit
    except OSError:
        return []


def buffer_schrijf(records):
    """Vervangt de buffer. Houdt de nieuwste BUFFER_MAX records."""
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        tijdelijk = BUFFER + ".tmp"
        with open(tijdelijk, "w") as fh:
            for r in records[-BUFFER_MAX:]:
                fh.write(json.dumps(r, separators=(",", ":")) + "\n")
        os.replace(tijdelijk, BUFFER)
    except OSError as exc:
        print("buffer schrijven mislukt: %s" % exc, file=sys.stderr)


def buffer_voeg_toe(record):
    buffer_schrijf(buffer_lees() + [record])


def selftest():
    """Wifi-uitlezing en de wissel-logica, zonder wpa_supplicant of netwerk."""
    import tempfile
    global wpa_vraag, STATE_DIR, VERBINDING_BESTAND
    status = ("bssid=00:00:00:00:00:00\nfreq=2462\nssid=Thuis\nid=0\n"
              "mode=station\nwpa_state=COMPLETED\n")
    poll = "RSSI=-74\nLINKSPEED=19\nNOISE=9999\nFREQUENCY=2462\n"
    antwoord = {"STATUS": status, "SIGNAL_POLL": poll}
    echt = (wpa_vraag, STATE_DIR, VERBINDING_BESTAND)
    d = tempfile.mkdtemp(prefix="autopi-diag-selftest-")
    checks = []
    try:
        STATE_DIR, VERBINDING_BESTAND = d, os.path.join(d, "verbinding")
        wpa_vraag = lambda *c, **k: dict(antwoord)          # noqa: E731
        wf = wifi()
        checks.append(("wifi: velden uit STATUS en SIGNAL_POLL",
                       wf == {"ssid": "Thuis", "wifi_dbm": -74, "freq_mhz": 2462,
                              "linkspeed_mbps": 19, "band": "2,4 GHz"}))
        checks.append(("wifi: bssid gaat niet mee", "bssid" not in json.dumps(wf)))
        thuis = {"default_iface": "wlan0", "metered": False}
        onderweg = {"default_iface": "wwan0", "metered": True}
        rec, v = wifi_record("t1", thuis)
        checks.append(("op wifi: record met verbinding wifi",
                       rec is not None and rec["verbinding"] == "wifi" and v == "wifi"))
        bewaar_verbinding(v)
        rec, v = wifi_record("t2", thuis)
        checks.append(("op wifi: elke run opnieuw", rec is not None))
        wpa_vraag = lambda *c, **k: {}                        # noqa: E731
        rec, v = wifi_record("t3", onderweg)
        checks.append(("wissel naar 4G: één record zonder wifi-velden",
                       rec == {"@ts": "t3", "@t": "device.wifi", "verbinding": "4G"}))
        bewaar_verbinding(v)
        rec, v = wifi_record("t4", onderweg)
        checks.append(("op 4G zonder wissel: niets versturen", rec is None))
        checks.append(("geen route: verbinding geen",
                       verbinding({"default_iface": None}, {}) == "geen"))
        wpa_vraag = lambda *c, **k: {"STATUS": "wpa_state=SCANNING\n"}  # noqa: E731
        checks.append(("niet verbonden: geen wifi-velden", wifi() == {}))
        checks.append(("echte wpa_vraag zonder socket: leeg, geen fout",
                       echt[0]("STATUS", iface="bestaat-niet-%d" % os.getpid()) == {}))
    finally:
        wpa_vraag, STATE_DIR, VERBINDING_BESTAND = echt
        for n in os.listdir(d):
            os.remove(os.path.join(d, n))
        os.rmdir(d)
    ok = True
    for label, geslaagd in checks:
        print("  %s %s" % ("PASS" if geslaagd else "FAIL", label))
        ok = ok and geslaagd
    print()
    print("ZELFTEST %s" % ("GESLAAGD" if ok else "MISLUKT"))
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--selftest", action="store_true",
                    help="test de wifi-uitlezing zonder netwerk")
    ap.add_argument("--dry-run", action="store_true",
                    help="alleen tonen, niets bewaren of versturen")
    ap.add_argument("--altijd", action="store_true",
                    help="negeer UNMETERED_EVERY en meet hoe dan ook")
    ap.add_argument("--status", action="store_true",
                    help="toon hoeveel records er in de buffer staan")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    if args.status:
        b = buffer_lees()
        print("%d records in de buffer" % len(b))
        if b:
            print("oudste: %s" % b[0].get("@ts"))
            print("nieuwste: %s" % b[-1].get("@ts"))
        return 0

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")
    record = {"@ts": ts, "@t": "device.diag"}
    rt = route()
    for deel in (signaal(), netwerk(), rt, uptime()):
        record.update(deel)

    if args.dry_run:
        print(json.dumps(record, indent=2, sort_keys=True))
        print(json.dumps(wifi_record(ts, rt)[0], indent=2, sort_keys=True))
        return 0

    # Wifi-status live, los van de diag-buffer: hij gaat over nú.
    wrec, v = wifi_record(ts, rt)
    if wrec is not None:
        try:
            post([wrec])
            bewaar_verbinding(v)
        except (urllib.error.URLError, OSError, RuntimeError) as exc:
            print("wifi-status versturen mislukt: %s" % exc, file=sys.stderr)

    bemeten = record.get("metered") is not False

    # Onderweg: alleen bewaren. Geen mobiele data aan diagnostiek uitgeven.
    if bemeten:
        buffer_voeg_toe(record)
        print("bemeten verbinding, in buffer (%d records)" % len(buffer_lees()))
        return 0

    # Op wifi: de buffer leegmaken. Het verse sample gaat alleen mee als we
    # thuis niet net al gemeten hebben — anders vult de oprit de grafiek.
    partij = buffer_lees()
    vers = not (recent_onbemeten_gemeten() and not args.altijd)
    if vers:
        partij.append(record)

    if not partij:
        print("niets te versturen")
        return 0

    try:
        n = post(partij)
    except (urllib.error.URLError, OSError, RuntimeError) as exc:
        buffer_schrijf(partij)          # niets kwijtraken, volgende keer opnieuw
        print("versturen mislukt (%s), %d records blijven in de buffer"
              % (exc, len(partij)), file=sys.stderr)
        return 1

    buffer_schrijf([])
    if vers:
        stip_onbemeten()
    print("verstuurd: %d record(s), %d bytes" % (len(partij), n))
    return 0

if __name__ == "__main__":
    sys.exit(main())
