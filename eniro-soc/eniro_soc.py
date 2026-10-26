#!/usr/bin/env python3
"""
eniro-soc — leest de BMS van de Kia e-Niro over CAN en stuurt het resultaat naar
het ingest-endpoint, dezelfde weg die de AutoPi zelf al gebruikt.

Waarom via het ingest-endpoint en niet rechtstreeks naar MQTT: dan werkt het
zowel thuis als onderweg. Over 4G zit het apparaat achter de NAT van 1NCE, en
de MQTT-broker is niet vanaf internet bereikbaar. De ontvanger (autopi-ingest)
draait standaard op de dongle zelf, op 127.0.0.1:8181, en houdt via een
WireGuard-tunnel één MQTT-verbinding open naar de broker. Alternatief: een
ontvanger op een eigen server achter HTTPS. Voor dit script is dat alleen een
andere INGEST_URL.

De records krijgen dezelfde vorm als die van de AutoPi zelf (`@ts` + `@t`), dus
ze landen automatisch op `autopi/obd/ev/bms` zonder dat de ontvanger iets hoeft
te weten van dit script.

ISO-TP wordt hier zelf geïmplementeerd op een raw CAN-socket. Dat is een
handvol regels en het scheelt de afhankelijkheid van de `can-isotp`-kernelmodule.

Zelftest zonder auto:
    ./eniro_soc.py --selftest

Eenmalig uitlezen en printen, niets versturen:
    ./eniro_soc.py --once --dry-run
"""

import argparse
import gzip
import json
import logging
import os
import signal
import socket
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

# ---------------------------------------------------------------- configuratie

CAN_IFACE = os.environ.get("CAN_IFACE", "can0")
REQ_ID = int(os.environ.get("REQ_ID", "0x7E4"), 16)
RSP_ID = int(os.environ.get("RSP_ID", "0x7EC"), 16)
CAN_TIMEOUT = float(os.environ.get("CAN_TIMEOUT", "1.0"))

# De kilometerstand zit in de instrumentencluster, niet in de BMS. Gevonden
# 16-09-2026: verzoek 7C6, antwoord 7CE, DID B002, drie bytes big-endian op
# positie 6 van de data, in hele kilometers.
#
# De cluster antwoordt ALLEEN met het contact aan. Tijdens laden is hij stil —
# er valt niets weer te geven. Dat is geen probleem: afstand heb je nodig terwijl
# je rijdt, en dan is hij er.
ODO_REQ = int(os.environ.get("ODO_REQ", "0x7C6"), 16)
ODO_RSP = int(os.environ.get("ODO_RSP", "0x7CE"), 16)
ODO_OFFSET = int(os.environ.get("ODO_OFFSET", "6"))

# Plausibiliteitsgrenzen. Een kilometerstand loopt alleen op, en niet met
# sprongen van honderden kilometers tussen twee metingen van een minuut.
# Lopend gemiddeld verbruik, zoals het dashboard van de auto het ook doet.
#
# Waarom juist honderd kilometer: de kilometerteller telt in hele kilometers en
# de energieteller in tienden van kilowattuur. Op een traject van tien kilometer
# is dat tien procent fout, en die fout koppelt aan de gemeten snelheid — vandaar
# dat eerdere analyses een e-Niro lieten zien die zuiniger werd naarmate hij
# harder reed. Over honderd kilometer is diezelfde fout één procent.
VERBRUIK_VENSTER_KM = float(os.environ.get("VERBRUIK_VENSTER_KM", "100"))
VERBRUIK_BEWAAR_KM = float(os.environ.get("VERBRUIK_BEWAAR_KM", "200"))
VERBRUIK_BESTAND = os.environ.get(
    "VERBRUIK_BESTAND", "/var/lib/eniro-soc/verbruik.jsonl")

# De BMS-teller cum_energy_charged_kwh telt ALLE energie die de accu in gaat:
# regeneratie, maar ook elke laadsessie aan de paal. Het netto verbruik mag
# alleen regeneratie aftrekken. Een stap tussen twee kilometerpunten waarin
# geladen is, telt daarom alleen het bruto verbruik.
#
# "Geladen" herkennen we op twee manieren: het script heeft tussen de punten
# zelf een laadsessie gezien (LAADVLAG_BESTAND), of er ging meer dan
# LAAD_DREMPEL_KWH de accu in. Regeneratie haalt dat per stap nooit: een
# afdaling van tien procent over een kilometer levert zo'n 0,3 kWh.
#
# Vastgesteld 28-09-2026: zonder deze correctie gaf het venster 79-87 Wh/km
# en een actieradius van ~500 km waar Kia 262 zei. Elke laadsessie binnen de
# honderd kilometer werd als regeneratie van het verbruik afgetrokken.
LAAD_DREMPEL_KWH = float(os.environ.get("LAAD_DREMPEL_KWH", "1.0"))
LAADVLAG_BESTAND = os.environ.get(
    "LAADVLAG_BESTAND",
    os.path.join(os.path.dirname(VERBRUIK_BESTAND), "geladen-sinds-punt"))

ODO_MIN = int(os.environ.get("ODO_MIN", "1000"))
ODO_MAX = int(os.environ.get("ODO_MAX", "1000000"))
ODO_SPRONG = int(os.environ.get("ODO_SPRONG", "500"))
_laatste_odo = None

# De AutoPi brengt can0 NIET uit zichzelf omhoog — gemeten na een reboot op
# 15-09-2026: obd_manager laat de interface DOWN staan. Zonder dit zou dit
# script onbemand nooit iets lezen en alleen ENETDOWN opleveren.
CAN_BITRATE = int(os.environ.get("CAN_BITRATE", "500000"))
CAN_AUTO_UP = os.environ.get("CAN_AUTO_UP", "1").lower() not in ("0", "false", "no")
# Na een bus-off herstelt de controller alleen vanzelf als restart-ms > 0.
# Met 0 (wat 'ip link set up' zonder optie geeft) blijft can0 voorgoed dood tot
# iemand hem down/up zet — vermoedelijk de oorzaak van de twee keer dat de SoC
# op 09-10-2026 midden in een lading wegviel. Gezien 13-10: restart-ms 0.
CAN_RESTART_MS = int(os.environ.get("CAN_RESTART_MS", "100"))

INGEST_URL = os.environ.get("INGEST_URL", "")
HTTP_TIMEOUT = float(os.environ.get("HTTP_TIMEOUT", "30"))
USER_AGENT = os.environ.get("USER_AGENT", "eniro-soc/1.0")

# Pollintervallen in seconden, per voertuigstatus.
INTERVAL_DC = int(os.environ.get("INTERVAL_DC", "60"))
INTERVAL_DRIVING = int(os.environ.get("INTERVAL_DRIVING", "60"))
INTERVAL_CHARGING = int(os.environ.get("INTERVAL_CHARGING", "300"))
INTERVAL_IDLE = int(os.environ.get("INTERVAL_IDLE", "1800"))
INTERVAL_NO_BUS = int(os.environ.get("INTERVAL_NO_BUS", "300"))

# Op een onbemeten verbinding (wifi, ethernet) kost data niets, dus mag alles
# sneller. AC-laden gebeurt vrijwel altijd thuis; dan is dit het interval.
INTERVAL_UNMETERED = int(os.environ.get("INTERVAL_UNMETERED", "60"))

# Meten en versturen zijn losgekoppeld. Een record is ~313 bytes, maar een
# TLS-verbinding kost ruwweg 5 KB — de kosten zitten in het aantal uploads, niet
# in de hoeveelheid data. Records wachten daarom UPLOAD_WAIT seconden in een
# uitbox en gaan dan samen in één POST. Rijden kost daarmee ~15 MB/jaar in
# plaats van ~82 MB.
#
# Snelladen is de uitzondering: 0 betekent direct versturen. Dat is ~10 sessies
# per jaar (~1,5 MB) en juist het moment waarop je live naar de curve kijkt.
UPLOAD_WAIT = int(os.environ.get("UPLOAD_WAIT", "300"))
UPLOAD_WAIT_DC = int(os.environ.get("UPLOAD_WAIT_DC", "0"))
OUTBOX_MAX = int(os.environ.get("OUTBOX_MAX", "60"))

# Interfaces waarover data geld kost. Bij twijfel gaan we uit van bemeten.
METERED_IFACES = set(
    x.strip() for x in os.environ.get("METERED_IFACES", "wwan0,ppp0").split(",")
    if x.strip()
)

# Alleen versturen als er iets verandert, of na dit aantal seconden alsnog.
# Beschermt de databundel: 500 MB voor 10 jaar is ~137 KB per dag.
SOC_DELTA = float(os.environ.get("SOC_DELTA", "0.5"))
ODO_DELTA = float(os.environ.get("ODO_DELTA", "1"))
MAX_SILENCE = int(os.environ.get("MAX_SILENCE", "3600"))
# Tijdens laden minimaal elke zoveel seconden een record, ook zonder 0,5 %
# verandering: de laadautomation in HA wil een verse SoC (18-10-2026).
MAX_SILENCE_LADEN = int(os.environ.get("MAX_SILENCE_LADEN", "300"))

SPOOL_DIR = os.environ.get("SPOOL_DIR", "/var/lib/eniro-soc/spool")
SPOOL_MAX = int(os.environ.get("SPOOL_MAX", "500"))

# Status voor autopi-wakelock. Die houdt de dongle wakker zolang dit bestand
# vers is en 'laden', 'snelladen' of 'rijden' zegt. Op de 12V-spanning alleen
# kan dat niet: tijdens AC-laden staat die op ~13,0 V, onder de 13,5 V waarmee
# rijden herkend wordt (gemeten 05-10-2026).
STATUS_BESTAND = os.environ.get("STATUS_BESTAND", "/var/lib/eniro-soc/status.json")

# Net na het opstarten antwoordt de BMS soms nog niet. Dan niet INTERVAL_NO_BUS
# wachten maar kort, STARTUP_WINDOW seconden lang. Kort gehouden: vragen aan
# een slapende auto kunnen zijn gateway wekken.
INTERVAL_NO_BUS_START = int(os.environ.get("INTERVAL_NO_BUS_START", "15"))
STARTUP_WINDOW = int(os.environ.get("STARTUP_WINDOW", "180"))

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)-7s %(message)s",
)
log = logging.getLogger("eniro-soc")

# ------------------------------------------------------------------- ISO-TP

CAN_FRAME_FMT = "<IB3x8s"
CAN_RAW = 1


class BusError(Exception):
    """Geen of ongeldig antwoord van de bus."""


def _iface_is_up(iface):
    """Leest de IFF_UP-vlag uit sysfs. Betrouwbaarder dan operstate, want
    CAN-interfaces melden daar 'unknown' terwijl ze prima werken."""
    try:
        with open("/sys/class/net/%s/flags" % iface) as fh:
            return bool(int(fh.read().strip(), 16) & 0x1)
    except OSError:
        return False


def _can_details(iface):
    """(state, restart_ms) uit 'ip -details link show', of (None, None)."""
    try:
        res = subprocess.run(["ip", "-details", "link", "show", iface],
                             capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None, None
    velden = res.stdout.split()
    state = restart = None
    if "state" in velden:
        # 'can state ERROR-PASSIVE restart-ms 0' — de tweede 'state' is de CAN-state
        for i, w in enumerate(velden[:-1]):
            if w == "can" and velden[i + 1] == "state" and i + 2 < len(velden):
                state = velden[i + 2]
    if "restart-ms" in velden:
        try:
            restart = int(velden[velden.index("restart-ms") + 1])
        except (ValueError, IndexError):
            pass
    return state, restart


def ensure_can_up(iface, bitrate=CAN_BITRATE):
    """Brengt de CAN-interface omhoog als die down staat.

    Nodig omdat de AutoPi dit niet doet: na een reboot staat can0 DOWN, ook
    met een levende bus. Een raw socket op een down interface geeft dan
    OSError 100 (ENETDOWN), wat eruitziet als een stille bus maar het niet is.
    """
    reden = None
    if not _iface_is_up(iface):
        reden = "staat down"
    else:
        state, restart = _can_details(iface)
        if state == "BUS-OFF":
            reden = "staat BUS-OFF"
        elif CAN_RESTART_MS > 0 and restart == 0:
            reden = "heeft restart-ms 0 (herstelt niet na bus-off)"
    if reden is None:
        return
    if not CAN_AUTO_UP:
        raise BusError("%s %s en CAN_AUTO_UP staat uit" % (iface, reden))

    log.warning("%s %s — opnieuw opbrengen op %d bit/s, restart-ms %d",
                iface, reden, bitrate, CAN_RESTART_MS)
    subprocess.run(["ip", "link", "set", iface, "down"],
                   capture_output=True, text=True, timeout=10)
    cmd = ["ip", "link", "set", iface, "up", "type", "can", "bitrate", str(bitrate)]
    if CAN_RESTART_MS > 0:
        cmd += ["restart-ms", str(CAN_RESTART_MS)]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    if res.returncode != 0:
        raise BusError("kon %s niet omhoog brengen: %s"
                       % (iface, (res.stderr or res.stdout).strip()))
    time.sleep(0.2)
    if not _iface_is_up(iface):
        raise BusError("%s bleef down na het omhoog brengen" % iface)


def open_can(iface):
    ensure_can_up(iface)
    sock = socket.socket(socket.AF_CAN, socket.SOCK_RAW, CAN_RAW)
    try:
        sock.bind((iface,))
    except OSError as exc:
        sock.close()
        if exc.errno == 100:   # ENETDOWN
            raise BusError("%s is down; draait dit als root?" % iface)
        raise
    sock.settimeout(CAN_TIMEOUT)
    return sock


def _send(sock, can_id, payload):
    data = bytes(payload).ljust(8, b"\x00")[:8]
    sock.send(struct.pack(CAN_FRAME_FMT, can_id, 8, data))


def _recv(sock, want_id, deadline):
    """Wacht op een frame met het gevraagde ID; negeert alles anders."""
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        sock.settimeout(max(0.05, min(CAN_TIMEOUT, remaining)))
        try:
            frame = sock.recv(16)
        except socket.timeout:
            continue
        can_id, length, data = struct.unpack(CAN_FRAME_FMT, frame)
        can_id &= 0x1FFFFFFF
        if can_id == want_id:
            return data[:length]
    raise BusError("geen frame van %03X binnen de tijd" % want_id)


def isotp_query(sock, service_pid, timeout=2.0, req=None, rsp=None):
    """
    Stuurt een UDS-verzoek en geeft de volledige respons terug.

    `service_pid` is bijvoorbeeld b'\\x22\\x01\\x01'. De teruggegeven bytes
    beginnen met de echo (`62 01 01`); de aanroeper snijdt die er zelf af.
    """
    req = REQ_ID if req is None else req
    rsp = RSP_ID if rsp is None else rsp
    deadline = time.monotonic() + timeout
    _send(sock, req, bytes([len(service_pid)]) + service_pid)

    first = _recv(sock, rsp, deadline)
    pci = first[0] >> 4

    if pci == 0:  # single frame
        length = first[0] & 0x0F
        return first[1 : 1 + length]

    if pci != 1:  # verwacht een first frame
        raise BusError("onverwacht PCI-type 0x%X in %s" % (pci, first.hex()))

    total = ((first[0] & 0x0F) << 8) | first[1]
    data = bytearray(first[2:8])

    # Flow control: ga door, geen blokgrootte, geen wachttijd
    _send(sock, req, b"\x30\x00\x00")

    expect = 0x21
    while len(data) < total:
        cf = _recv(sock, rsp, deadline)
        if cf[0] != expect:
            raise BusError(
                "verwachtte volgnummer %02X, kreeg %02X" % (expect, cf[0])
            )
        data += cf[1:8]
        expect = 0x20 | ((expect + 1) & 0x0F)

    return bytes(data[:total])


# ------------------------------------------------------------------ decoderen
#
# De letters komen uit de HKMC-EV-tabellen (JejuSoul) en beginnen bij de eerste
# byte NA de echo `62 01 0X`. Zie autopi/eniro-pid-decoder.md voor de
# verificatie tegen dashboard, 12V-meting en celspanningen.


def _letters(n):
    out = []
    for i in range(n):
        if i < 26:
            out.append(chr(97 + i))
        else:
            j = i - 26
            out.append(chr(97 + j // 26) + chr(97 + j % 26))
    return out


def as_map(response, echo_len=3):
    """Zet een respons om in een dict met Torque-letters als sleutel."""
    data = response[echo_len:]
    keys = _letters(len(data))
    return {keys[i]: data[i] for i in range(len(data))}


def _u16(m, hi, lo):
    return (m[hi] << 8) | m[lo]


def _u32(m, b3, b2, b1, b0):
    return (m[b3] << 24) | (m[b2] << 16) | (m[b1] << 8) | m[b0]


def _s8(v):
    return v - 256 if v > 127 else v


def decode_0101(m):
    current = ((_s8(m["k"]) * 256) + m["l"]) / 10.0
    voltage = _u16(m, "m", "n") / 10.0
    return {
        "soc_bms": m["e"] / 2.0,
        "aux_voltage": round(m["ad"] * 0.1, 2),
        "hv_voltage": round(voltage, 1),
        "hv_current": round(current, 1),
        "hv_power": round(current * voltage / 1000.0, 3),
        "batt_temp_max": _s8(m["o"]),
        "batt_temp_min": _s8(m["p"]),
        "batt_temp_modules": [_s8(m[x]) for x in ("q", "r", "s", "t")],
        "batt_inlet_temp": _s8(m["w"]),
        "isolation_kohm": _u16(m, "bf", "bg"),
        "main_relay": bool(m["j"] & 0x01),
        "dc_charging": bool(m["j"] & 0x80),
        "ignition": bool(m["ay"] & 0x04),
        "cum_charge_ah": _u32(m, "ae", "af", "ag", "ah") / 10.0,
        "cum_discharge_ah": _u32(m, "ai", "aj", "ak", "al") / 10.0,
        "cum_energy_charged_kwh": _u32(m, "am", "an", "ao", "ap") / 10.0,
        "cum_energy_used_kwh": _u32(m, "aq", "ar", "as", "at") / 10.0,
    }


def decode_0105(m):
    return {
        "soc_display": m["af"] / 2.0,
        "soh": _u16(m, "z", "aa") / 10.0,
        "cell_v_deviation": round(m["u"] / 50.0, 2),
        "heater_temp": _s8(m["x"]),
    }
    # Bewust NIET meegenomen: "max regen" en "max vermogen" uit 220101.
    # Die geven op dit voertuig beide 170 kW, wat onmogelijk is. Zie
    # eniro-pid-decoder.md.


def read_odometer(sock):
    """Kilometerstand uit de cluster, of None als die slaapt.

    Bewust niet-fataal: met het contact uit antwoordt 7C6 niet, en dat is de
    normale situatie tijdens laden. Een ontbrekende kilometerstand mag de
    BMS-uitlezing niet meeslepen.
    """
    try:
        antwoord = isotp_query(sock, b"\x22\xB0\x02", timeout=1.0,
                               req=ODO_REQ, rsp=ODO_RSP)
    except (BusError, OSError):
        return None
    data = antwoord[3:]            # echo 62 B0 02 eraf
    if len(data) < ODO_OFFSET + 3:
        return None
    km = int.from_bytes(data[ODO_OFFSET:ODO_OFFSET + 3], "big")

    # Onzin weigeren in plaats van doorgeven. Op 16-09-2026 landde er een
    # 6.832.744 in Home Assistant; waar die vandaan kwam is onbekend, maar een
    # kilometerstand van bijna zeven miljoen hoort het script nooit te versturen.
    global _laatste_odo
    if not ODO_MIN <= km <= ODO_MAX:
        log.warning("Onwaarschijnlijke kilometerstand %d genegeerd", km)
        return None
    if _laatste_odo is not None and not 0 <= km - _laatste_odo <= ODO_SPRONG:
        log.warning("Kilometerstand sprong van %d naar %d, genegeerd",
                    _laatste_odo, km)
        return None
    _laatste_odo = km
    return km


def _verbruik_lees():
    try:
        with open(VERBRUIK_BESTAND) as fh:
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


def _verbruik_schrijf(punten):
    try:
        os.makedirs(os.path.dirname(VERBRUIK_BESTAND), exist_ok=True)
        tijdelijk = VERBRUIK_BESTAND + ".tmp"
        with open(tijdelijk, "w") as fh:
            for p in punten:
                fh.write(json.dumps(p, separators=(",", ":")) + "\n")
        os.replace(tijdelijk, VERBRUIK_BESTAND)
    except OSError as exc:
        log.warning("verbruiksgeschiedenis schrijven mislukt: %s", exc)


def schrijf_status(toestand, values=None):
    """Legt vast wat de auto doet, voor autopi-wakelock.

    Atomair via een tijdelijk bestand, zodat de wakelock nooit een half
    geschreven bestand leest.
    """
    data = {"ts": round(time.time()), "toestand": toestand}
    if values:
        data["soc"] = values.get("soc_display")
        data["hv_current"] = values.get("hv_current")
        data["ignition"] = values.get("ignition")
    tijdelijk = STATUS_BESTAND + ".tmp"
    try:
        os.makedirs(os.path.dirname(STATUS_BESTAND), exist_ok=True)
        with open(tijdelijk, "w") as fh:
            json.dump(data, fh)
        os.replace(tijdelijk, STATUS_BESTAND)
    except OSError as exc:
        log.warning("Status schrijven mislukt: %s", exc)


def markeer_laden(values):
    """Onthoudt dat er geladen is sinds het laatste kilometerpunt.

    Een bestand in plaats van een variabele, zodat het een herstart of een
    slaapperiode van de dongle overleeft: laden gebeurt juist terwijl de auto
    stilstaat, en tussen twee kilometerpunten kan de dongle uren slapen.
    """
    laadt = values.get("dc_charging") or (
        values.get("hv_current", 0.0) < -1.0 and not values.get("ignition"))
    if not laadt:
        return
    try:
        if not os.path.exists(LAADVLAG_BESTAND):
            os.makedirs(os.path.dirname(LAADVLAG_BESTAND), exist_ok=True)
            with open(LAADVLAG_BESTAND, "w") as fh:
                fh.write(now_ts() + "\n")
    except OSError as exc:
        log.warning("laadvlag zetten mislukt: %s", exc)


def _rij_stap(vorig, uit, inn, geladen):
    """Netto rij-energie (kWh) tussen het vorige punt en nu."""
    d_uit = uit - vorig["uit"]
    d_in = inn - vorig["in"]
    if geladen or d_in > LAAD_DREMPEL_KWH:
        # Laadsessie ertussen: de geladen energie is geen regeneratie. Het
        # beetje regen van de laatste meters voor en na het parkeren laten we
        # vallen; dat is hooguit enkele Wh.
        return max(d_uit, 0.0)
    return d_uit - d_in


def _verbruik_herbereken(punten):
    """Vult 'rij' in voor punten van voor 28-09-2026, die het veld missen.

    Zonder laadvlaggeschiedenis, dus alleen op de drempel. Daarmee klopt het
    venster meteen, in plaats van pas na honderd nieuwe kilometers.
    """
    if not punten or all("rij" in p for p in punten):
        return punten, False
    punten[0]["rij"] = 0.0
    for vorig, p in zip(punten, punten[1:]):
        p["rij"] = round(vorig["rij"] + _rij_stap(vorig, p["uit"], p["in"], False), 3)
    return punten, True


def verbruik_venster(values):
    """Gemiddeld netto verbruik over de laatste VERBRUIK_VENSTER_KM kilometer.

    Netto: regeneratie gaat eraf, net zoals het dashboard van de auto doet.
    Laadsessies niet - zie LAAD_DREMPEL_KWH. Elk punt draagt een oplopende
    teller 'rij' met de netto energie die sinds het oudste punt aan rijden is
    besteed; het venster is dan een simpel verschil van twee tellers.

    Geeft (Wh/km, werkelijk gebruikt venster in km) of (None, None) zolang er
    nog niet genoeg kilometers achter de rug zijn.
    """
    km = values.get("odometer_km")
    uit = values.get("cum_energy_used_kwh")
    inn = values.get("cum_energy_charged_kwh")
    if km is None or uit is None or inn is None:
        return None, None

    punten, herberekend = _verbruik_herbereken(_verbruik_lees())
    if herberekend:
        log.info("verbruiksgeschiedenis herberekend zonder laadsessies (%d punten)",
                 len(punten))

    # Alleen bewaren als de teller echt verder staat; anders groeit het bestand
    # vol met identieke punten terwijl de auto stilstaat.
    if not punten or km > punten[-1]["km"]:
        if punten:
            geladen = os.path.exists(LAADVLAG_BESTAND)
            rij = punten[-1]["rij"] + _rij_stap(punten[-1], uit, inn, geladen)
        else:
            geladen, rij = False, 0.0
        punten.append({"km": km, "uit": uit, "in": inn, "rij": round(rij, 3)})
        punten = [p for p in punten if km - p["km"] <= VERBRUIK_BEWAAR_KM]
        _verbruik_schrijf(punten)
        if geladen:
            try:
                os.remove(LAADVLAG_BESTAND)
            except OSError:
                pass
    elif herberekend:
        _verbruik_schrijf(punten)

    # Het jongste punt dat ver genoeg terug ligt: venster zo dicht mogelijk
    # bij VERBRUIK_VENSTER_KM in plaats van veel ruimer.
    nu = punten[-1]
    kandidaten = [p for p in punten if nu["km"] - p["km"] >= VERBRUIK_VENSTER_KM]
    if not kandidaten:
        return None, None
    toen = kandidaten[-1]

    dkm = nu["km"] - toen["km"]
    netto = nu["rij"] - toen["rij"]
    if dkm <= 0 or netto <= 0:
        return None, None
    return round(netto * 1000 / dkm, 1), round(dkm, 1)


def read_bms(sock):
    m1 = as_map(isotp_query(sock, b"\x22\x01\x01"))
    m5 = as_map(isotp_query(sock, b"\x22\x01\x05"))
    out = {}
    out.update(decode_0101(m1))
    out.update(decode_0105(m5))
    markeer_laden(out)
    km = read_odometer(sock)
    if km is not None:
        out["odometer_km"] = km
        wh, venster = verbruik_venster(out)
        if wh is not None:
            out["wh_per_km_100"] = wh
            out["verbruik_venster_km"] = venster
    return out


# -------------------------------------------------------------------- versturen


def now_ts():
    # Zelfde vorm als de AutoPi zelf gebruikt: UTC, geen tijdzone-achtervoegsel
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")


def make_record(values):
    rec = {"@ts": now_ts(), "@t": "obd.ev.bms"}
    rec.update(values)
    return rec


def post(records):
    if not INGEST_URL:
        raise RuntimeError("INGEST_URL is niet gezet")
    body = gzip.compress(
        json.dumps(records, separators=(",", ":")).encode("utf-8"), 9
    )
    req = urllib.request.Request(
        INGEST_URL,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Content-Encoding": "gzip",
            # Zonder eigen User-Agent stuurt urllib "Python-urllib/3.x", en dat
            # blokkeert Cloudflare (HTTP 403) voordat het verzoek de ontvanger
            # bereikt. Vastgesteld 15-09-2026. Een herkenbare naam maakt het ook
            # makkelijker terug te vinden in de Cloudflare-logs.
            "User-Agent": USER_AGENT,
        },
    )
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        if resp.status != 200:
            raise RuntimeError("HTTP %s" % resp.status)
    return len(body)


def spool_write(records):
    try:
        os.makedirs(SPOOL_DIR, exist_ok=True)
        name = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%f") + ".json"
        with open(os.path.join(SPOOL_DIR, name), "w") as fh:
            json.dump(records, fh)
        files = sorted(os.listdir(SPOOL_DIR))
        for old in files[:-SPOOL_MAX]:
            os.remove(os.path.join(SPOOL_DIR, old))
        log.info("In wachtrij gezet (%d bestanden)", min(len(files), SPOOL_MAX))
    except OSError as exc:
        log.error("Wegschrijven naar wachtrij mislukt: %s", exc)


def spool_flush():
    """Verstuurt de wachtrij, oudste eerst. True als hij daarna leeg is."""
    if not os.path.isdir(SPOOL_DIR):
        return True
    for name in sorted(os.listdir(SPOOL_DIR)):
        path = os.path.join(SPOOL_DIR, name)
        try:
            with open(path) as fh:
                records = json.load(fh)
        except (OSError, ValueError) as exc:
            log.warning("Onleesbaar wachtrijbestand %s weggegooid: %s", name, exc)
            try:
                os.remove(path)
            except OSError:
                pass
            continue
        try:
            post(records)
        except (urllib.error.URLError, OSError, RuntimeError) as exc:
            log.debug("Wachtrij nog niet te versturen: %s", exc)
            return False  # netwerk is weg, later opnieuw
        os.remove(path)
        log.info("Wachtrijbestand %s verstuurd", name)
    return True


# ------------------------------------------------------------------------ loop


def default_iface():
    """De interface van de default route met de laagste metric, of None.

    Bewust geen DNS-lookup van de endpoint: dat zou elke cyclus verkeer kosten
    op precies de verbinding die we proberen te sparen.
    """
    try:
        res = subprocess.run(["ip", "-4", "route", "show", "default"],
                             capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("Kan default route niet bepalen: %s", exc)
        return None

    beste, laagste = None, None
    for regel in res.stdout.splitlines():
        velden = regel.split()
        if "dev" not in velden:
            continue
        dev = velden[velden.index("dev") + 1]
        metric = 0
        if "metric" in velden:
            try:
                metric = int(velden[velden.index("metric") + 1])
            except (ValueError, IndexError):
                metric = 0
        if laagste is None or metric < laagste:
            beste, laagste = dev, metric
    return beste


def is_metered():
    """True als het verkeer over een verbinding gaat die geld kost.

    Faalt veilig: kunnen we het niet vaststellen, dan doen we zuinig.
    """
    dev = default_iface()
    if dev is None:
        return True
    return dev in METERED_IFACES


def interval_for(values, metered=True):
    """Geeft (pollinterval, uitbox-wachttijd, omschrijving)."""
    if values.get("dc_charging"):
        return INTERVAL_DC, UPLOAD_WAIT_DC, "snelladen"

    # Laden alleen met het contact uit, net als in markeer_laden(). Anders
    # telt elke regeneratieve rem tijdens het rijden als laden.
    current = values.get("hv_current", 0.0)
    if current < -1.0 and not values.get("ignition"):
        if not metered:
            return INTERVAL_UNMETERED, 0, "laden"
        # Direct door: de ontvanger staat op de dongle zelf (geen TLS-handshake
        # meer per upload), en in 'dagelijks' laat hij laadrecords live door.
        return INTERVAL_CHARGING, 0, "laden"

    if values.get("ignition"):
        # Op wifi kost versturen niets: dan ook tijdens rijden direct.
        return INTERVAL_DRIVING, UPLOAD_WAIT if metered else 0, "rijden"

    if not metered:
        return INTERVAL_UNMETERED, 0, "idle"
    return INTERVAL_IDLE, UPLOAD_WAIT, "idle"


def laadt(values):
    return bool(values.get("dc_charging")) or (
        values.get("hv_current", 0.0) < -1.0 and not values.get("ignition"))


def worth_sending(values, last_sent, last_time):
    """Alleen versturen bij verandering, of na MAX_SILENCE. Spaart data."""
    if last_sent is None:
        return True, "eerste meting"
    if time.time() - last_time >= MAX_SILENCE:
        return True, "hartslag"
    if laadt(values) and time.time() - last_time >= MAX_SILENCE_LADEN - 5:
        return True, "hartslag laden"
    for key in ("soc_display", "soc_bms"):
        if abs(values.get(key, 0) - last_sent.get(key, 0)) >= SOC_DELTA:
            return True, "%s veranderd" % key
    for key in ("dc_charging", "ignition", "main_relay"):
        if values.get(key) != last_sent.get(key):
            return True, "%s veranderd" % key
    # Afstand als aanleiding: tijdens het rijden levert dit een meetpunt per
    # ODO_DELTA kilometer, en stilstaand niets. Nodig voor de verbruikscurve,
    # waarvoor we energie per afgelegde kilometer willen weten.
    nu, toen = values.get("odometer_km"), last_sent.get("odometer_km")
    if nu is not None and toen is not None and abs(nu - toen) >= ODO_DELTA:
        return True, "%.0f km gereden" % abs(nu - toen)
    return False, "niets veranderd"


def run_loop(dry_run=False):
    stop = {"now": False}

    def handler(signum, frame):
        log.info("Signaal %s ontvangen, afsluiten", signum)
        stop["now"] = True

    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)

    last_sent, last_time = None, 0.0
    outbox, outbox_since = [], 0.0
    vorige_toestand = None
    gestart = time.monotonic()

    def leeg_outbox(reden):
        """Verstuurt de uitbox in één POST; bij mislukking naar de wachtrij."""
        nonlocal outbox, outbox_since
        if not outbox:
            return
        aantal = len(outbox)
        # Eerst de wachtrij, dan de uitbox. Andersom komen oudere metingen na
        # nieuwere binnen en zet HA de SOC terug (gezien 05-10-2026). Lukt de
        # wachtrij niet, dan sluit de uitbox achteraan aan.
        if not spool_flush():
            log.warning("Wachtrij nog niet weg, %d record(s) erachter gezet", aantal)
            spool_write(outbox)
            outbox, outbox_since = [], 0.0
            return
        try:
            grootte = post(outbox)
            log.info("Verstuurd: %d record(s), %d bytes (%s)", aantal, grootte, reden)
            outbox, outbox_since = [], 0.0
        except (urllib.error.URLError, OSError, RuntimeError) as exc:
            log.warning("Versturen mislukt (%s), %d record(s) in wachtrij", exc, aantal)
            spool_write(outbox)
            outbox, outbox_since = [], 0.0

    def slaap(seconden):
        """Wacht, maar stopt binnen twee seconden na SIGTERM.

        Een kale time.sleep() hervat na een signaal gewoon. Met de auto uit
        wachtte het script daardoor 300 s aan een stuk; systemd gaf het na 90 s
        op en schoot het af met SIGKILL - en dan ging de uitbox verloren,
        precies bij het inslapen van de dongle. Vastgesteld 28-09-2026.
        """
        eind = time.monotonic() + seconden
        while not stop["now"]:
            rest = eind - time.monotonic()
            if rest <= 0:
                break
            time.sleep(min(2.0, rest))

    def bus_stil():
        """De auto is net stil gevallen: verstuur wat er nog wacht.

        Anders blijven de laatste metingen van een rit of laadsessie in het
        geheugen staan tot de auto weer wakker wordt - of gaan ze verloren als
        de dongle intussen uitschakelt.
        """
        nonlocal vorige_toestand
        if not dry_run:
            schrijf_status("geen bus")
            if outbox:
                leeg_outbox("bus stil")
            else:
                # Ook met de auto in slaap: de dongle is wakker en heeft
                # verbinding, dus weg met wat er nog wacht.
                spool_flush()
        vorige_toestand = None
        if time.monotonic() - gestart < STARTUP_WINDOW:
            slaap(INTERVAL_NO_BUS_START)
        else:
            slaap(INTERVAL_NO_BUS)

    while not stop["now"]:
        try:
            sock = open_can(CAN_IFACE)
        except (OSError, BusError) as exc:
            log.error("Kan %s niet openen: %s", CAN_IFACE, exc)
            bus_stil()
            continue

        try:
            values = read_bms(sock)
        except BusError as exc:
            log.info("Geen antwoord van de BMS (%s) — voertuig slaapt?", exc)
            sock.close()
            bus_stil()
            continue
        except OSError as exc:
            log.error("CAN-fout: %s", exc)
            sock.close()
            bus_stil()
            continue
        finally:
            try:
                sock.close()
            except OSError:
                pass

        metered = is_metered()
        interval, wachttijd, why = interval_for(values, metered)
        send, reason = worth_sending(values, last_sent, last_time)

        log.info(
            "SOC %.1f%% (BMS %.1f%%) · %.1f V · %.1f A · %s · %s · volgende over %ds",
            values["soc_display"], values["soc_bms"], values["hv_voltage"],
            values["hv_current"], why,
            "bemeten" if metered else "onbemeten", interval,
        )

        # Begint er iets nieuws (eerste meting na het wakker worden, start van
        # laden of rijden), dan meteen versturen in plaats van na UPLOAD_WAIT.
        # De laadautomation in HA wil de SOC zodra de stekker erin zit.
        nieuw = why != vorige_toestand
        vorige_toestand = why

        if dry_run:
            print(json.dumps(make_record(values), indent=2))
        else:
            schrijf_status(why, values)
            if send:
                outbox.append(make_record(values))
                if not outbox_since:
                    outbox_since = time.time()
                last_sent, last_time = values, time.time()
                log.debug("In uitbox (%s, %d wachtend)", reason, len(outbox))
            else:
                log.debug("Niet in uitbox: %s", reason)

            if outbox and (
                wachttijd <= 0
                or nieuw
                or time.time() - outbox_since >= wachttijd
                or len(outbox) >= OUTBOX_MAX
            ):
                leeg_outbox("uitbox vol" if len(outbox) >= OUTBOX_MAX else why)
            elif not outbox:
                spool_flush()

        slaap(interval)

    # Bij afsluiten niets weggooien: wat in de uitbox staat gaat alsnog mee,
    # en anders naar de wachtrij. Zonder dit verlies je bij elke herstart tot
    # UPLOAD_WAIT seconden aan metingen.
    if outbox and not dry_run:
        leeg_outbox("afsluiten")


# --------------------------------------------------------------------- zelftest

# De frames die de auto op 02-09-2026 om 17:08 daadwerkelijk stuurde, met het
# dashboard op 48-49% SOC. Hiermee is het decodeergedeelte te testen zonder auto.
SELFTEST_0101 = (
    "103E620101FFF7E7 21FF5F4268426803 2200140E28151415 23151415000017B9 "
    "240CB85800009000 250E21F9000E20E5 2600055415000531 27A902769BA80D01 "
    "286A0000000003E8"
)
SELFTEST_0105 = (
    "102E620105003FFF 2190000000000000 2200000000000042 2368426800015017 "
    "240003E82F03E801 25610000B8B80000 261500000000AAAA"
)


def _assemble(frames):
    parts = frames.split()
    first = bytes.fromhex(parts[0])
    total = ((first[0] & 0x0F) << 8) | first[1]
    data = bytearray(first[2:])
    for p in parts[1:]:
        data += bytes.fromhex(p)[1:]
    return bytes(data[:total])


def _verbruik_selftest():
    """Rijdt 150 km na met 150 Wh/km netto, met een laadsessie van 10 kWh
    halverwege het venster. Het venster moet 150 Wh/km blijven zeggen; de
    versie van voor 28-09 kwam hier op ~50 uit."""
    import shutil
    import tempfile
    global VERBRUIK_BESTAND, LAADVLAG_BESTAND
    bewaard = (VERBRUIK_BESTAND, LAADVLAG_BESTAND)
    uitkomsten = {}
    try:
        for scenario in ("laadvlag", "drempel", "herberekend"):
            d = tempfile.mkdtemp(prefix="eniro-selftest-")
            VERBRUIK_BESTAND = os.path.join(d, "verbruik.jsonl")
            LAADVLAG_BESTAND = os.path.join(d, "geladen-sinds-punt")
            km, uit, inn = 180000, 30000.0, 30500.0
            oud_formaat = []
            wh = None
            for stap in range(151):
                if stap == 60:                      # geparkeerd en geladen
                    inn += 10.0
                    uit += 0.05
                    if scenario == "laadvlag":
                        markeer_laden({"hv_current": -20.0, "ignition": False})
                if stap:
                    km += 1
                    uit += 0.17                    # bruto
                    inn += 0.02                    # regeneratie
                values = {"odometer_km": km, "cum_energy_used_kwh": round(uit, 3),
                          "cum_energy_charged_kwh": round(inn, 3)}
                if scenario == "herberekend" and stap < 150:
                    # geschiedenis zoals de oude versie hem wegschreef: zonder 'rij'
                    oud_formaat.append({"km": km, "uit": values["cum_energy_used_kwh"],
                                        "in": values["cum_energy_charged_kwh"]})
                    continue
                if scenario == "herberekend":
                    _verbruik_schrijf(oud_formaat)
                wh, _ = verbruik_venster(values)
            uitkomsten[scenario] = wh
            shutil.rmtree(d, ignore_errors=True)
    finally:
        VERBRUIK_BESTAND, LAADVLAG_BESTAND = bewaard
    return uitkomsten


def selftest():
    m1 = as_map(_assemble(SELFTEST_0101))
    m5 = as_map(_assemble(SELFTEST_0105))
    values = {}
    values.update(decode_0101(m1))
    values.update(decode_0105(m5))

    print(json.dumps(values, indent=2, sort_keys=True))
    print()

    checks = [
        ("SOC display tussen 48 en 49",  47.9 <= values["soc_display"] <= 49.1),
        ("SOC BMS tussen 47 en 49",      46.9 <= values["soc_bms"] <= 49.1),
        ("Aux 12V rond 14,4",            14.3 <= values["aux_voltage"] <= 14.5),
        ("HV-spanning rond 362",         360 <= values["hv_voltage"] <= 365),
        ("Accutemp 20 of 21",            values["batt_temp_min"] in (20, 21)),
        ("Isolatie 1000 kOhm",           values["isolation_kohm"] == 1000),
        ("Hoofdrelais dicht",            values["main_relay"] is True),
        ("Niet aan het snelladen",       values["dc_charging"] is False),
        ("Contact aan",                  values["ignition"] is True),
        ("Celafwijking 0,00 V",          values["cell_v_deviation"] == 0.0),
        ("Levenslang ~194 Wh/km",
         190 <= values["cum_energy_charged_kwh"] * 1000 / 179542 <= 198),
    ]

    verbruik = _verbruik_selftest()
    print("verbruiksvenster met laadsessie erin: %s" % json.dumps(verbruik))
    print()
    for scenario, label in (("laadvlag", "laadsessie gezien"),
                            ("drempel", "laadsessie niet gezien, drempel"),
                            ("herberekend", "oude geschiedenis herberekend")):
        wh = verbruik.get(scenario)
        checks.append(("Verbruik 150 Wh/km, %s" % label,
                       wh is not None and 147 <= wh <= 153))

    ok = True
    for label, passed in checks:
        print("  %s %s" % ("PASS" if passed else "FAIL", label))
        ok = ok and passed
    print()
    print("ZELFTEST %s" % ("GESLAAGD" if ok else "MISLUKT"))
    return 0 if ok else 1


# ------------------------------------------------------------------------ main


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--selftest", action="store_true",
                    help="decodeer de opgeslagen frames en controleer de uitkomsten")
    ap.add_argument("--once", action="store_true",
                    help="één keer uitlezen en stoppen")
    ap.add_argument("--dry-run", action="store_true",
                    help="niets versturen, alleen printen")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    if args.once:
        sock = open_can(CAN_IFACE)
        try:
            values = read_bms(sock)
        finally:
            sock.close()
        record = make_record(values)
        if args.dry_run:
            print(json.dumps(record, indent=2, sort_keys=True))
        else:
            print("verstuurd, %d bytes" % post([record]))
        return 0

    run_loop(dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
