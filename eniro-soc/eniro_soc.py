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

De kilometerstand komt uit de instrumentencluster en die antwoordt alleen met
het contact aan. In records zonder cluster-antwoord (laden, stilstand) gaan de
laatst bekende kilometerstand en het laatst berekende verbruik mee uit de
verbruiksgeschiedenis, met `odometer_bron: "opgeslagen"` (anders "cluster").
Zo staan die sensoren in Home Assistant ook na een herstart meteen goed. Een
opgeslagen stand wordt alleen gelezen: hij komt nooit als nieuw punt in de
geschiedenis en telt niet mee voor de verbruiksberekening of voor ODO_DELTA.

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
from urllib.parse import urlparse

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
# v1.3 (H): de drempel groeit mee met de afstand van de stap. Normaal ligt er
# een kilometer tussen twee punten, maar na een meetgat (dongle sliep, cluster
# stil) kan een stap tientallen kilometers beslaan, en dan haalt gewone
# regeneratie de 1 kWh wel. Die telde tot v1.2 als laden en viel weg uit het
# netto verbruik. Per kilometer rekenen we met hoogstens de afdaling hierboven.
REGEN_MAX_KWH_PER_KM = float(os.environ.get("REGEN_MAX_KWH_PER_KM", "0.3"))
# Bovengrens voor die drempel. Zonder grens telde na een gat van 200 km een
# laadsessie van 30 kWh zonder laadvlag als regeneratie (drempel 60 kWh), en
# dan is het verbruik weer veel te gunstig, de fout van 28-09. Regeneratie
# over een lang gat haalt 5 kWh zelden; een laadsessie bijna altijd.
LAAD_DREMPEL_MAX_KWH = float(os.environ.get("LAAD_DREMPEL_MAX_KWH", "5.0"))
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
USER_AGENT = os.environ.get("USER_AGENT", "eniro-soc/1.3")

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
# v1.3 (E): hoogstens zoveel seconden per cyclus aan de wachtrij besteden, en
# bij het afsluiten nog minder: systemd geeft 90 s voor het SIGKILL wordt.
SPOOL_FLUSH_BUDGET = int(os.environ.get("SPOOL_FLUSH_BUDGET", "120"))
SPOOL_FLUSH_BUDGET_STOP = int(os.environ.get("SPOOL_FLUSH_BUDGET_STOP", "20"))
# Staat er iets in de wachtrij en is de ontvanger lokaal (127.0.0.1), probeer
# dan elke zoveel seconden opnieuw in plaats van pas bij de volgende meting.
# Na een boot ging het eerste record anders pas ~5 minuten later mee: de
# ontvanger start net na eniro-soc. 0 = uit. Een ontvanger elders (HTTPS)
# wordt niet zo vaak geprobeerd: elke poging kost dan een TLS-handshake.
SPOOL_RETRY = int(os.environ.get("SPOOL_RETRY", "15"))
# HTTP-timeout voor die extra pogingen en voor het afsluiten. De ontvanger is
# lokaal; antwoordt hij niet binnen vijf seconden, dan is hij er niet.
RETRY_TIMEOUT = float(os.environ.get("RETRY_TIMEOUT", "5"))

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


def ingest_lokaal(url=None):
    """True als de ontvanger op de dongle zelf draait."""
    try:
        host = urlparse(url if url is not None else INGEST_URL).hostname or ""
    except ValueError:
        return False
    return host in ("127.0.0.1", "localhost", "::1")


INGEST_LOKAAL = ingest_lokaal()


def config_fouten(ingest_nodig=True):
    """Controleert de instellingen bij het opstarten (v1.3, I).

    Geeft een lijst foutmeldingen; leeg = in orde. Een verkeerde waarde gaf
    tot v1.2 pas later een raadselachtige fout, of een lus die nooit sliep.
    """
    fouten = []
    if ingest_nodig:
        if not INGEST_URL:
            fouten.append("INGEST_URL is niet gezet")
        elif urlparse(INGEST_URL).scheme not in ("http", "https"):
            fouten.append("INGEST_URL moet met http:// of https:// beginnen")
    for naam in ("INTERVAL_DC", "INTERVAL_DRIVING", "INTERVAL_CHARGING",
                 "INTERVAL_IDLE", "INTERVAL_NO_BUS", "INTERVAL_UNMETERED",
                 "INTERVAL_NO_BUS_START", "MAX_SILENCE", "MAX_SILENCE_LADEN",
                 "OUTBOX_MAX", "SPOOL_MAX", "CAN_BITRATE"):
        if globals()[naam] <= 0:
            fouten.append("%s moet groter dan 0 zijn (nu %s)" % (naam, globals()[naam]))
    for naam in ("UPLOAD_WAIT", "UPLOAD_WAIT_DC", "SPOOL_FLUSH_BUDGET",
                 "SPOOL_FLUSH_BUDGET_STOP", "SPOOL_RETRY", "STARTUP_WINDOW",
                 "CAN_RESTART_MS"):
        if globals()[naam] < 0:
            fouten.append("%s mag niet negatief zijn (nu %s)" % (naam, globals()[naam]))
    for naam in ("SOC_DELTA", "ODO_DELTA", "LAAD_DREMPEL_KWH", "HTTP_TIMEOUT",
                 "CAN_TIMEOUT", "VERBRUIK_VENSTER_KM"):
        if globals()[naam] <= 0:
            fouten.append("%s moet groter dan 0 zijn (nu %s)" % (naam, globals()[naam]))
    if REGEN_MAX_KWH_PER_KM < 0:
        fouten.append("REGEN_MAX_KWH_PER_KM mag niet negatief zijn")
    if VERBRUIK_BEWAAR_KM < VERBRUIK_VENSTER_KM:
        fouten.append("VERBRUIK_BEWAAR_KM moet minstens VERBRUIK_VENSTER_KM zijn")
    if not (ODO_MIN < ODO_MAX):
        fouten.append("ODO_MIN moet kleiner zijn dan ODO_MAX")
    return fouten

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

    while True:
        first = _recv(sock, rsp, deadline)
        # Een frame zonder data (DLC 0) is geen antwoord. Vóór v1.1 gaf dit
        # een IndexError die de hele dienst liet crashen.
        if not first:
            raise BusError("leeg frame (DLC 0) van %03X" % rsp)
        pci = first[0] >> 4

        if pci != 0:
            break

        # single frame
        length = first[0] & 0x0F
        if length == 0 or length > len(first) - 1:
            raise BusError("ongeldige single-frame-lengte in %s" % first.hex())
        antwoord = first[1 : 1 + length]

        # Negatief UDS-antwoord: 7F <service> <NRC>. NRC 0x78 betekent
        # 'responsePending': het echte antwoord komt nog, dus doorwachten tot
        # de deadline. Elke andere NRC is een weigering.
        if antwoord[0] == 0x7F:
            nrc = antwoord[2] if len(antwoord) >= 3 else None
            if nrc == 0x78:
                log.debug("%03X: responsePending, wacht op het antwoord", rsp)
                continue
            raise BusError("negatief antwoord van %03X op %s: NRC %s" % (
                rsp, service_pid.hex().upper(),
                "onbekend" if nrc is None else "0x%02X" % nrc))
        return antwoord

    if pci != 1 or len(first) < 2:  # verwacht een first frame
        raise BusError("onverwacht PCI-type 0x%X in %s" % (pci, first.hex()))

    total = ((first[0] & 0x0F) << 8) | first[1]
    data = bytearray(first[2:8])

    # Flow control: ga door, geen blokgrootte, geen wachttijd
    _send(sock, req, b"\x30\x00\x00")

    expect = 0x21
    while len(data) < total:
        cf = _recv(sock, rsp, deadline)
        if not cf:
            raise BusError("leeg vervolgframe (DLC 0) van %03X" % rsp)
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
# byte NA de echo `62 01 0X`. Zie docs/pids-e-niro.md voor de
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


# Minimale lengte van het antwoord, echo meegeteld: de hoogste letter die
# decode_0101 leest is 'bg' (byte 59 na de echo), bij decode_0105 'af' (32).
MIN_LEN_0101 = 3 + 59
MIN_LEN_0105 = 3 + 32


def check_antwoord(antwoord, did, minimaal):
    """Controleert echo (62 + DID) en lengte vóór het decoderen.

    Een antwoord op een ander verzoek, of een afgekapt antwoord, zou anders
    stilletjes onzin opleveren of een KeyError geven.
    """
    echo = b"\x62" + did
    if antwoord[:3] != echo:
        raise BusError("verwachtte echo %s, kreeg %s" % (echo.hex(), antwoord[:3].hex()))
    if len(antwoord) < minimaal:
        raise BusError("antwoord op %s te kort: %d bytes, minimaal %d"
                       % (did.hex().upper(), len(antwoord), minimaal))
    return antwoord


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
    # docs/pids-e-niro.md.


def read_odometer(sock):
    """Kilometerstand uit de cluster, of None als die slaapt.

    Bewust niet-fataal: met het contact uit antwoordt 7C6 niet, en dat is de
    normale situatie tijdens laden. Een ontbrekende kilometerstand mag de
    BMS-uitlezing niet meeslepen.
    """
    try:
        antwoord = isotp_query(sock, b"\x22\xB0\x02", timeout=1.0,
                               req=ODO_REQ, rsp=ODO_RSP)
        check_antwoord(antwoord, b"\xB0\x02", 3 + ODO_OFFSET + 3)
    except (BusError, OSError):
        return None
    data = antwoord[3:]            # echo 62 B0 02 eraf
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


def atomair_schrijf(pad, tekst, fsync=False):
    """Schrijft via pad.tmp + os.replace: een lezer of een stroomonderbreking
    ziet altijd het oude of het nieuwe bestand, nooit een half. Gooit OSError."""
    os.makedirs(os.path.dirname(pad) or ".", exist_ok=True)
    with open(pad + ".tmp", "w") as fh:
        fh.write(tekst)
        if fsync:
            fh.flush()
            os.fsync(fh.fileno())
    os.replace(pad + ".tmp", pad)


def _verbruik_schrijf(punten):
    try:
        atomair_schrijf(VERBRUIK_BESTAND, "".join(
            json.dumps(p, separators=(",", ":")) + "\n" for p in punten))
        return True
    except OSError as exc:
        log.warning("verbruiksgeschiedenis schrijven mislukt: %s", exc)
        return False


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
    try:
        atomair_schrijf(STATUS_BESTAND, json.dumps(data))
    except OSError as exc:
        log.warning("Status schrijven mislukt: %s", exc)


def laadt(values):
    """De enige definitie van 'de auto laadt', overal in dit script gebruikt.

    Snelladen, of meer dan 1 A de accu in met het contact uit. Zonder die
    laatste voorwaarde telt elke regeneratieve rem tijdens het rijden als
    laden. De ontvanger en de HA-sensor 'e-Niro laadt' gebruiken dezelfde
    definitie.
    """
    return bool(values.get("dc_charging")) or (
        values.get("hv_current", 0.0) < -1.0 and not values.get("ignition"))


def markeer_laden(values):
    """Onthoudt dat er geladen is sinds het laatste kilometerpunt.

    Een bestand in plaats van een variabele, zodat het een herstart of een
    slaapperiode van de dongle overleeft: laden gebeurt juist terwijl de auto
    stilstaat, en tussen twee kilometerpunten kan de dongle uren slapen.
    """
    if not laadt(values):
        return
    try:
        if not os.path.exists(LAADVLAG_BESTAND):
            os.makedirs(os.path.dirname(LAADVLAG_BESTAND), exist_ok=True)
            with open(LAADVLAG_BESTAND, "w") as fh:
                fh.write(now_ts() + "\n")
    except OSError as exc:
        log.warning("laadvlag zetten mislukt: %s", exc)


def laad_drempel(d_km):
    """Meer dan dit (kWh) de accu in over d_km kilometer kan geen regeneratie zijn."""
    try:
        d_km = max(float(d_km), 0.0)
    except (TypeError, ValueError):
        d_km = 0.0
    return max(LAAD_DREMPEL_KWH,
               min(d_km * REGEN_MAX_KWH_PER_KM, LAAD_DREMPEL_MAX_KWH))


def _rij_stap(vorig, uit, inn, geladen, km=None):
    """Netto rij-energie (kWh) tussen het vorige punt en nu.

    km is de kilometerstand van het nieuwe punt; zonder km (oude aanroep)
    geldt de vaste drempel."""
    d_uit = uit - vorig["uit"]
    d_in = inn - vorig["in"]
    d_km = (km - vorig["km"]) if km is not None and "km" in vorig else 0.0
    if geladen or d_in > laad_drempel(d_km):
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
        p["rij"] = round(vorig["rij"] + _rij_stap(vorig, p["uit"], p["in"], False,
                                                    p["km"]), 3)
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
            rij = punten[-1]["rij"] + _rij_stap(punten[-1], uit, inn, geladen, km)
        else:
            geladen, rij = False, 0.0
        punten.append({"km": km, "uit": uit, "in": inn, "rij": round(rij, 3)})
        punten = [p for p in punten if km - p["km"] <= VERBRUIK_BEWAAR_KM]
        # Laadvlag alleen weg als het punt echt op schijf staat (v1.3, F).
        # Anders is de laadsessie bij de volgende poging niet meer te zien.
        if _verbruik_schrijf(punten) and geladen:
            try:
                os.remove(LAADVLAG_BESTAND)
            except OSError:
                pass
    elif herberekend:
        _verbruik_schrijf(punten)

    return _venster_uit(punten)


def _venster_uit(punten):
    """Het venster over een lijst punten, met het laatste punt als 'nu'.

    Rekent alleen; leest en schrijft niets. Gedeeld door verbruik_venster()
    (echte meting) en opgeslagen_stand() (geen cluster-antwoord).
    """
    if not punten:
        return None, None
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


def opgeslagen_stand():
    """Laatst bekende kilometerstand en verbruik uit de geschiedenis.

    Voor records zonder antwoord van de cluster (laden, stilstand, contact
    uit). Zonder dit staan kilometerstand en verbruik in Home Assistant na een
    herstart op 'unknown' tot de volgende rit: MQTT-sensoren herstellen hun
    state niet, en het retained bericht had die velden dan niet.

    Alleen lezen. Er wordt niets geschreven: geen nieuw punt, geen
    herberekening op schijf, de laadvlag blijft staan. De opgeslagen stand is
    het laatste punt zelf, dus er valt ook niets toe te voegen. Ontbreekt de
    geschiedenis of is hij onleesbaar, dan een lege dict: velden weglaten.
    """
    try:
        # Oude punten zonder 'rij' alleen in het geheugen aanvullen; het
        # wegschrijven doet verbruik_venster() bij de volgende echte meting.
        punten, _ = _verbruik_herbereken(_verbruik_lees())
        if not punten:
            return {}
        km = punten[-1]["km"]
        if isinstance(km, bool) or not isinstance(km, (int, float)):
            raise ValueError("kilometerstand %r is geen getal" % (km,))
        uit = {"odometer_km": km, "odometer_bron": "opgeslagen"}
        wh, venster = _venster_uit(punten)
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        log.warning("verbruiksgeschiedenis onleesbaar, geen opgeslagen stand: %s", exc)
        return {}
    if wh is not None:
        uit["wh_per_km_100"] = wh
        uit["verbruik_venster_km"] = venster
    return uit


def read_bms(sock):
    m1 = as_map(check_antwoord(isotp_query(sock, b"\x22\x01\x01"),
                               b"\x01\x01", MIN_LEN_0101))
    m5 = as_map(check_antwoord(isotp_query(sock, b"\x22\x01\x05"),
                               b"\x01\x05", MIN_LEN_0105))
    out = {}
    out.update(decode_0101(m1))
    out.update(decode_0105(m5))
    markeer_laden(out)
    km = read_odometer(sock)
    if km is not None:
        out["odometer_km"] = km
        out["odometer_bron"] = "cluster"
        wh, venster = verbruik_venster(out)
        if wh is not None:
            out["wh_per_km_100"] = wh
            out["verbruik_venster_km"] = venster
    else:
        # Cluster stil: laatst bekende waarden meesturen, alleen gelezen.
        out.update(opgeslagen_stand())
    return out


# -------------------------------------------------------------------- versturen


def now_ts():
    # Zelfde vorm als de AutoPi zelf gebruikt: UTC, geen tijdzone-achtervoegsel
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")


def make_record(values):
    rec = {"@ts": now_ts(), "@t": "obd.ev.bms"}
    rec.update(values)
    return rec


def post(records, timeout=None):
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
    with urllib.request.urlopen(req, timeout=timeout or HTTP_TIMEOUT) as resp:
        if resp.status != 200:
            raise RuntimeError("HTTP %s" % resp.status)
    return len(body)


def spool_write(records):
    """Zet records in de wachtrij. True als ze veilig op schijf staan.

    Atomair (v1.3, F): eerst een .tmp, dan os.replace. Een stroomonderbreking
    halverwege laat zo geen half bestand achter dat spool_flush() als
    onleesbaar weggooit. Bij False houdt de aanroeper de records zelf vast.
    """
    try:
        name = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%f") + ".json"
        atomair_schrijf(os.path.join(SPOOL_DIR, name), json.dumps(records), fsync=True)
        files = spool_bestanden()
        for old in files[:-SPOOL_MAX]:
            os.remove(os.path.join(SPOOL_DIR, old))
        log.info("In wachtrij gezet (%d bestanden)", min(len(files), SPOOL_MAX))
        return True
    except OSError as exc:
        log.error("Wegschrijven naar wachtrij mislukt: %s", exc)
        return False


def spool_bestanden():
    """De wachtrijbestanden, oudste eerst. Halve .tmp-bestanden tellen niet."""
    try:
        return sorted(n for n in os.listdir(SPOOL_DIR) if n.endswith(".json"))
    except OSError:
        return []


def spool_flush(budget=None, timeout=None):
    """Verstuurt de wachtrij, oudste eerst. True als hij daarna leeg is.

    Hoogstens `budget` seconden (v1.3, E; standaard SPOOL_FLUSH_BUDGET). Een
    volle wachtrij over een traag 4G-signaal hield anders de meetlus
    minutenlang vast. Wat overblijft gaat de volgende cyclus mee.
    """
    if not os.path.isdir(SPOOL_DIR):
        return True
    budget = SPOOL_FLUSH_BUDGET if budget is None else budget
    eind = time.monotonic() + budget
    try:
        alles = sorted(os.listdir(SPOOL_DIR))
    except OSError:
        return True
    # Een .tmp is een schrijfactie die door stroomverlies niet afkwam; dit
    # proces schrijft niet tegelijk, dus wat er nu staat is een wees.
    for n in alles:
        if n.endswith(".tmp"):
            try:
                os.remove(os.path.join(SPOOL_DIR, n))
            except OSError:
                pass
    for name in (n for n in alles if n.endswith(".json")):
        if time.monotonic() >= eind:
            log.info("Wachtrij: tijdsbudget van %ss op, rest volgt later", budget)
            return False
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
            post(records, timeout=timeout)
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

    # Laden alleen met het contact uit, zie laadt().
    if laadt(values):
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


def _gemeten_km(values):
    """Kilometerstand als die nu van de cluster kwam, anders None."""
    if values.get("odometer_bron") == "opgeslagen":
        return None
    return values.get("odometer_km")


def worth_sending(values, last_sent, last_time):
    """Alleen versturen bij verandering, of na MAX_SILENCE. Spaart data.

    last_time komt van time.monotonic() (v1.3, I). De dongle start met de klok
    op 1970 en springt pas na de tijdsync naar nu; met time.time() gaf dat
    direct na het wekken een onterechte 'hartslag' en een lege uitbox.
    """
    if last_sent is None:
        return True, "eerste meting"
    # Begin én einde van laden. Vóór v1.1 ging het einde van een AC-lading
    # (stroom −2 → 0 A, SOC en statusbits gelijk) nooit weg.
    if laadt(values) != laadt(last_sent):
        return True, "laadstatus veranderd"
    stil = time.monotonic() - last_time
    if stil >= MAX_SILENCE:
        return True, "hartslag"
    if laadt(values) and stil >= MAX_SILENCE_LADEN - 5:
        return True, "hartslag laden"
    for key in ("soc_display", "soc_bms"):
        if abs(values.get(key, 0) - last_sent.get(key, 0)) >= SOC_DELTA:
            return True, "%s veranderd" % key
    for key in ("dc_charging", "ignition", "main_relay"):
        if values.get(key) != last_sent.get(key):
            return True, "%s veranderd" % key
    # Afstand als aanleiding: tijdens het rijden levert dit een meetpunt per
    # ODO_DELTA kilometer, en stilstaand niets. Nodig voor de verbruikscurve,
    # waarvoor we energie per afgelegde kilometer willen weten. Alleen een
    # kilometerstand van de cluster telt; een opgeslagen stand is geen nieuwe
    # meting en mag geen verzending uitlokken.
    nu, toen = _gemeten_km(values), _gemeten_km(last_sent)
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
    outbox, outbox_since = [], None       # outbox_since: time.monotonic()
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
            naar_wachtrij()
            return
        try:
            grootte = post(outbox)
            log.info("Verstuurd: %d record(s), %d bytes (%s)", aantal, grootte, reden)
            outbox, outbox_since = [], None
        except (urllib.error.URLError, OSError, RuntimeError) as exc:
            log.warning("Versturen mislukt (%s), %d record(s) in wachtrij", exc, aantal)
            naar_wachtrij()

    def naar_wachtrij():
        """Uitbox naar schijf; alleen leeg als dat lukte (v1.3, F)."""
        nonlocal outbox, outbox_since
        if spool_write(outbox):
            outbox, outbox_since = [], None
        elif len(outbox) > OUTBOX_MAX * 4:
            # Schijf kapot of vol: niet eindeloos geheugen vullen.
            log.error("Wachtrij onschrijfbaar, oudste %d record(s) weggegooid",
                      len(outbox) - OUTBOX_MAX * 4)
            outbox = outbox[-OUTBOX_MAX * 4:]

    def slaap(seconden):
        """Wacht, maar stopt binnen twee seconden na SIGTERM.

        Een kale time.sleep() hervat na een signaal gewoon. Met de auto uit
        wachtte het script daardoor 300 s aan een stuk; systemd gaf het na 90 s
        op en schoot het af met SIGKILL - en dan ging de uitbox verloren,
        precies bij het inslapen van de dongle. Vastgesteld 28-09-2026.
        """
        eind = time.monotonic() + seconden
        opnieuw = time.monotonic() + SPOOL_RETRY
        while not stop["now"]:
            rest = eind - time.monotonic()
            if rest <= 0:
                break
            time.sleep(min(2.0, rest))
            # v1.3: wachtrij niet laten liggen tot de volgende meting. Alleen
            # bij een lokale ontvanger; daar kost een poging niets.
            # Niet bij een stopverzoek, en met een korte timeout: slaap() belooft
            # binnen een paar seconden te stoppen na SIGTERM.
            if (not dry_run and SPOOL_RETRY > 0 and INGEST_LOKAAL
                    and not stop["now"] and time.monotonic() >= opnieuw):
                opnieuw = time.monotonic() + SPOOL_RETRY
                if spool_bestanden() and spool_flush(budget=SPOOL_RETRY,
                                                     timeout=RETRY_TIMEOUT):
                    log.info("Wachtrij leeg (opnieuw geprobeerd tijdens het wachten)")

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
        except Exception:  # noqa: BLE001 - laatste vangnet
            # Onverwacht antwoord of een bug in het decoderen: niet crashen,
            # want dan gaat de uitbox verloren. Behandelen als een stille bus;
            # bus_stil() verstuurt wat er nog in de uitbox staat.
            log.exception("Onverwachte fout bij het uitlezen, behandeld als stille bus")
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
        # Laadwissel: direct versturen, net als tijdens laden.
        laadwissel = last_sent is not None and laadt(values) != laadt(last_sent)

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
                if outbox_since is None:
                    outbox_since = time.monotonic()
                last_sent, last_time = values, time.monotonic()
                log.debug("In uitbox (%s, %d wachtend)", reason, len(outbox))
            else:
                log.debug("Niet in uitbox: %s", reason)

            if outbox and (
                wachttijd <= 0
                or nieuw
                or laadwissel
                or time.monotonic() - outbox_since >= wachttijd
                or len(outbox) >= OUTBOX_MAX
            ):
                leeg_outbox("uitbox vol" if len(outbox) >= OUTBOX_MAX else why)
            elif not outbox:
                spool_flush()

        slaap(interval)

    # Bij afsluiten niets weggooien. Eerst de uitbox naar schijf (v1.3, E): dat
    # is binnen een seconde klaar, terwijl versturen over een traag signaal
    # langer kan duren dan systemd wacht, en dan ging de uitbox verloren. Daarna
    # met een kort budget proberen de wachtrij nog weg te krijgen.
    if not dry_run:
        if outbox:
            naar_wachtrij()
        if spool_bestanden():
            spool_flush(budget=SPOOL_FLUSH_BUDGET_STOP, timeout=RETRY_TIMEOUT)


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


class _NepSocket:
    """Speelt vooraf opgegeven CAN-frames af als antwoord; voor de zelftest."""

    def __init__(self, frames):
        self.frames = list(frames)

    def send(self, data):
        pass

    def settimeout(self, t):
        pass

    def recv(self, n):
        if not self.frames:
            time.sleep(0.05)
            raise socket.timeout()
        frame = self.frames.pop(0)
        data, dlc = frame[0], frame[1]
        can_id = frame[2] if len(frame) > 2 else RSP_ID
        return struct.pack(CAN_FRAME_FMT, can_id, dlc, data.ljust(8, b"\x00"))


def _geeft_buserror(functie, *args, **kwargs):
    try:
        functie(*args, **kwargs)
    except BusError:
        return True
    except Exception as exc:  # noqa: BLE001
        print("  onverwacht: %s: %s" % (type(exc).__name__, exc))
        return False
    return False


def _robuustheid_selftest():
    """Regressietests voor v1.1: rare bus-antwoorden (G) en einde laden (A)."""
    h = bytes.fromhex
    leeg = (b"", 0)
    weigering = (h("037F223100000000"), 8)        # requestOutOfRange
    wacht = (h("037F227800000000"), 8)            # responsePending
    goed = (h("0462B00201000000"), 8)
    checks = [
        ("G: leeg frame geeft BusError",
         _geeft_buserror(isotp_query, _NepSocket([leeg]), b"\x22\x01\x01")),
        ("G: negatief antwoord (7F) geeft BusError",
         _geeft_buserror(isotp_query, _NepSocket([weigering]), b"\x22\x01\x01")),
        ("G: responsePending, daarna het echte antwoord",
         isotp_query(_NepSocket([wacht, goed]), b"\x22\xB0\x02") == h("62B00201")),
        ("G: responsePending zonder vervolg geeft BusError",
         _geeft_buserror(isotp_query, _NepSocket([wacht]), b"\x22\x01\x01",
                         timeout=0.3)),
        ("G: read_bms met 7F geeft BusError, geen KeyError",
         _geeft_buserror(read_bms, _NepSocket([weigering, weigering]))),
        ("G: read_bms met leeg frame geeft BusError",
         _geeft_buserror(read_bms, _NepSocket([leeg, leeg]))),
        ("G: read_bms met verkeerde echo geeft BusError",
         _geeft_buserror(read_bms, _NepSocket([(h("0462010500000000"), 8)]))),
        ("G: read_bms met te kort antwoord geeft BusError",
         _geeft_buserror(read_bms, _NepSocket([(h("0462010100000000"), 8)]))),
    ]

    basis = dict(soc_display=80.0, soc_bms=79.5, dc_charging=False,
                 ignition=False, main_relay=True)
    laden = dict(basis, hv_current=-2.0)
    gestopt = dict(basis, hv_current=0.0)
    vorige = time.monotonic() - 60
    checks += [
        ("A: −2 A → 0 A, SOC gelijk: versturen",
         worth_sending(gestopt, laden, vorige)[0]),
        ("A: 0 A → −2 A, SOC gelijk: versturen",
         worth_sending(laden, gestopt, vorige)[0]),
        ("A: regeneratie tijdens rijden is geen laden",
         not laadt(dict(basis, ignition=True, hv_current=-20.0))),
        ("A: na het laden weer het idle-interval",
         interval_for(gestopt, True)[2] == "idle"),
    ]
    return checks


def _bms_frames():
    """De BMS-antwoorden uit de zelftest als frames voor _NepSocket."""
    return [(bytes.fromhex(f), 8)
            for f in (SELFTEST_0101 + " " + SELFTEST_0105).split()]


def _odo_frames(km):
    """Een cluster-antwoord (7CE) op 22 B0 02 met deze kilometerstand."""
    data = b"\x62\xB0\x02" + bytes(ODO_OFFSET) + km.to_bytes(3, "big")
    ff = bytes([0x10 | (len(data) >> 8), len(data) & 0xFF]) + data[:6]
    uit = [(ff, 8, ODO_RSP)]
    rest, volg = data[6:], 0x21
    while rest:
        uit.append((bytes([volg]) + rest[:7], 8, ODO_RSP))
        rest, volg = rest[7:], 0x20 | ((volg + 1) & 0x0F)
    return uit


def _opgeslagen_selftest():
    """v1.2: laatst bekende kilometerstand en verbruik in records zonder
    cluster-antwoord, zonder de verbruiksgeschiedenis te raken."""
    import shutil
    import tempfile
    global VERBRUIK_BESTAND, LAADVLAG_BESTAND, _laatste_odo
    bewaard = (VERBRUIK_BESTAND, LAADVLAG_BESTAND, _laatste_odo)
    checks = []

    def bouw(d):
        """120 km rijden met 150 Wh/km netto, zoals _verbruik_selftest."""
        global VERBRUIK_BESTAND, LAADVLAG_BESTAND
        VERBRUIK_BESTAND = os.path.join(d, "verbruik.jsonl")
        LAADVLAG_BESTAND = os.path.join(d, "geladen-sinds-punt")
        km, uit, inn = 180000, 30000.0, 30500.0
        for stap in range(121):
            if stap:
                km, uit, inn = km + 1, uit + 0.17, inn + 0.02
            verbruik_venster({"odometer_km": km,
                              "cum_energy_used_kwh": round(uit, 3),
                              "cum_energy_charged_kwh": round(inn, 3)})
        return km, uit, inn

    def lees(pad):
        try:
            with open(pad, "rb") as fh:
                return fh.read()
        except OSError:
            return None

    mappen = []
    try:
        # Twee identieke geschiedenissen: 'a' krijgt records zonder cluster
        # ertussen, 'b' niet. Daarna moet een echte meting in beide hetzelfde
        # opleveren.
        a, b = (tempfile.mkdtemp(prefix="eniro-selftest-") for _ in range(2))
        mappen += [a, b]
        km, uit, inn = bouw(b)
        bouw(a)
        venster_voor = verbruik_venster({"odometer_km": km,
                                         "cum_energy_used_kwh": round(uit, 3),
                                         "cum_energy_charged_kwh": round(inn, 3)})
        with open(LAADVLAG_BESTAND, "w") as fh:     # er is net geladen
            fh.write("selftest\n")
        voor = lees(VERBRUIK_BESTAND)
        vlag_voor = lees(LAADVLAG_BESTAND)

        _laatste_odo = None
        rec1 = read_bms(_NepSocket(_bms_frames()))   # cluster antwoordt niet
        rec2 = read_bms(_NepSocket(_bms_frames()))
        checks += [
            ("v1.2 a: zonder cluster odometer_km = laatste punt",
             rec1.get("odometer_km") == km),
            ("v1.2 a: odometer_bron = opgeslagen",
             rec1.get("odometer_bron") == "opgeslagen"),
            ("v1.2 a: verbruik uit de geschiedenis mee",
             rec1.get("wh_per_km_100") == venster_voor[0]
             and rec1.get("verbruik_venster_km") == venster_voor[1]
             and 147 <= rec1["wh_per_km_100"] <= 153),
            ("v1.2 b: verbruik.jsonl byte-gelijk na opgeslagen records",
             voor is not None and lees(VERBRUIK_BESTAND) == voor),
            ("v1.2 b: laadvlag ongemoeid, geen .tmp achtergebleven",
             lees(LAADVLAG_BESTAND) == vlag_voor
             and not os.path.exists(VERBRUIK_BESTAND + ".tmp")),
        ]

        # c: een echte meting met een hogere kilometerstand, in a (met de
        # opgeslagen records ertussen) en in b (zonder), moet gelijk uitpakken.
        volgende = {"odometer_km": km + 1,
                    "cum_energy_used_kwh": round(uit + 0.17, 3),
                    "cum_energy_charged_kwh": round(inn + 0.02, 3)}
        uitkomst_a = verbruik_venster(dict(volgende))
        bestand_a, vlag_a = lees(VERBRUIK_BESTAND), lees(LAADVLAG_BESTAND)
        VERBRUIK_BESTAND = os.path.join(b, "verbruik.jsonl")
        LAADVLAG_BESTAND = os.path.join(b, "geladen-sinds-punt")
        with open(LAADVLAG_BESTAND, "w") as fh:
            fh.write("selftest\n")
        uitkomst_b = verbruik_venster(dict(volgende))
        checks += [
            ("v1.2 c: echte meting daarna: zelfde venster als zonder opgeslagen records",
             uitkomst_a == uitkomst_b and uitkomst_a[0] is not None),
            ("v1.2 c: zelfde geschiedenis, punt erbij, laadvlag opgeruimd",
             bestand_a == lees(VERBRUIK_BESTAND)
             and bestand_a.count(b"\n") == voor.count(b"\n") + 1
             and vlag_a is None and lees(LAADVLAG_BESTAND) is None),
        ]

        # c: hetzelfde via read_bms met een echt cluster-antwoord
        VERBRUIK_BESTAND = os.path.join(a, "verbruik.jsonl")
        LAADVLAG_BESTAND = os.path.join(a, "geladen-sinds-punt")
        regels = lees(VERBRUIK_BESTAND).count(b"\n")
        _laatste_odo = None
        rec3 = read_bms(_NepSocket(_bms_frames() + _odo_frames(km + 2)))
        laatste = json.loads(lees(VERBRUIK_BESTAND).splitlines()[-1])
        checks.append(
            ("v1.2 c: read_bms met cluster: bron cluster, punt erbij",
             rec3.get("odometer_km") == km + 2
             and rec3.get("odometer_bron") == "cluster"
             and lees(VERBRUIK_BESTAND).count(b"\n") == regels + 1
             and laatste["km"] == km + 2))

        # d: geen, lege of kapotte geschiedenis
        d = tempfile.mkdtemp(prefix="eniro-selftest-")
        mappen.append(d)
        VERBRUIK_BESTAND = os.path.join(d, "verbruik.jsonl")
        LAADVLAG_BESTAND = os.path.join(d, "geladen-sinds-punt")
        velden = ("odometer_km", "odometer_bron", "wh_per_km_100",
                  "verbruik_venster_km")
        goed = True
        for inhoud in (None, "", "geen json\n", '{"km": "x"}\n', "[1, 2]\n"):
            if inhoud is not None:
                with open(VERBRUIK_BESTAND, "w") as fh:
                    fh.write(inhoud)
            try:
                rec = read_bms(_NepSocket(_bms_frames()))
            except Exception as exc:  # noqa: BLE001
                print("  onverwacht bij %r: %s: %s" % (inhoud, type(exc).__name__, exc))
                goed = False
                continue
            goed = goed and not any(v in rec for v in velden) \
                and lees(VERBRUIK_BESTAND) == (None if inhoud is None
                                               else inhoud.encode())
        checks.append(("v1.2 d: geen/lege/kapotte geschiedenis: velden weg, geen fout",
                       goed))

        # Kilometerstand maar nog geen 100 km: wel km, geen verbruik
        with open(VERBRUIK_BESTAND, "w") as fh:
            fh.write('{"km":180000,"uit":1.0,"in":1.0,"rij":0.0}\n')
        rec = read_bms(_NepSocket(_bms_frames()))
        checks.append(("v1.2 d: korte geschiedenis: km mee, verbruik weg",
                       rec.get("odometer_km") == 180000
                       and "wh_per_km_100" not in rec
                       and "verbruik_venster_km" not in rec))
    finally:
        VERBRUIK_BESTAND, LAADVLAG_BESTAND, _laatste_odo = bewaard
        for d in mappen:
            shutil.rmtree(d, ignore_errors=True)

    # e: een ongewijzigde opgeslagen stand mag geen verzending uitlokken
    basis = dict(soc_display=80.0, soc_bms=79.5, dc_charging=False,
                 ignition=False, main_relay=True, hv_current=0.0)
    vorige = time.monotonic() - 60
    opgeslagen = dict(basis, odometer_km=180120, odometer_bron="opgeslagen")
    gemeten = dict(basis, odometer_km=180120, odometer_bron="cluster")
    checks += [
        ("v1.2 e: opgeslagen km ongewijzigd: niet versturen",
         not worth_sending(opgeslagen, opgeslagen, vorige)[0]),
        ("v1.2 e: cluster -> opgeslagen, zelfde km: niet versturen",
         not worth_sending(opgeslagen, gemeten, vorige)[0]),
        ("v1.2 e: opgeslagen km telt niet als gereden",
         not worth_sending(dict(opgeslagen, odometer_km=180100), gemeten, vorige)[0]
         and not worth_sending(gemeten, dict(opgeslagen, odometer_km=180100),
                               vorige)[0]),
        ("v1.2 e: cluster +1 km: wel versturen (als voorheen)",
         worth_sending(dict(gemeten, odometer_km=180121), gemeten, vorige)[0]),
    ]
    return checks


def _v13_selftest():
    """v1.3: drempel per afstand (H), atomaire wachtrij (F), tijdsbudget (E),
    configcontrole en monotone klok (I)."""
    import shutil
    import tempfile
    global SPOOL_DIR, INGEST_URL, post
    checks = []

    # H: 30 km na een meetgat met 2 kWh regeneratie is rijden, geen laden
    vorig = {"km": 180000, "uit": 100.0, "in": 50.0, "rij": 0.0}
    gat = _rij_stap(vorig, 106.0, 52.0, False, 180030)
    checks.append(("v1.3 H: 30 km meetgat met 2 kWh regen telt als regeneratie",
                   abs(gat - 4.0) < 1e-6))
    laad = _rij_stap(vorig, 100.2, 60.0, False, 180001)
    checks.append(("v1.3 H: 10 kWh erin over 1 km blijft laden",
                   abs(laad - 0.2) < 1e-6))
    lang = _rij_stap(vorig, 101.0, 80.0, False, 180200)
    checks.append(("v1.3 H: 30 kWh laden na 200 km gat blijft laden (bovengrens)",
                   abs(lang - 1.0) < 1e-6))
    checks.append(("v1.3 H: zonder km de vaste drempel (oude aanroep)",
                   abs(_rij_stap(vorig, 106.0, 52.0, False) - 6.0) < 1e-6))

    bewaard = (SPOOL_DIR, INGEST_URL, post)
    d = tempfile.mkdtemp(prefix="eniro-v13-")
    try:
        SPOOL_DIR = os.path.join(d, "spool")
        ok = spool_write([{"a": 1}])
        bestanden = os.listdir(SPOOL_DIR)
        checks.append(("v1.3 F: wachtrij schrijft atomair (geen .tmp over)",
                       ok and len(bestanden) == 1 and bestanden[0].endswith(".json")))
        open(os.path.join(SPOOL_DIR, "half.json.tmp"), "w").close()
        checks.append(("v1.3 F: half .tmp-bestand telt niet als wachtrij",
                       len(spool_bestanden()) == 1))
        post = lambda records, timeout=None: 1    # noqa: E731
        spool_flush()
        checks.append(("v1.3 F: flush ruimt het halve .tmp-bestand op",
                       os.listdir(SPOOL_DIR) == []))

        # spool_write mislukt -> False (map is een bestand)
        SPOOL_DIR = os.path.join(d, "geen-map")
        open(SPOOL_DIR, "w").close()
        checks.append(("v1.3 F: mislukt schrijven geeft False", spool_write([{}]) is False))

        # E: tijdsbudget; elke post duurt 0,3 s, budget 0,5 s -> stopt na 2
        SPOOL_DIR = os.path.join(d, "budget")
        for i in range(5):
            spool_write([{"i": i}])
            time.sleep(0.002)
        verstuurd = []

        def trage_post(records, timeout=None):
            time.sleep(0.3)
            verstuurd.append(records[0]["i"])
            return 1
        post = trage_post
        leeg = spool_flush(budget=0.5)
        checks.append(("v1.3 E: tijdsbudget op -> False, rest blijft staan",
                       leeg is False and verstuurd == [0, 1]
                       and len(spool_bestanden()) == 3))
        leeg = spool_flush(budget=10)
        checks.append(("v1.3 E: volgende keer de rest, oudste eerst",
                       leeg is True and verstuurd == [0, 1, 2, 3, 4]))
    finally:
        SPOOL_DIR, INGEST_URL, post = bewaard
        shutil.rmtree(d, ignore_errors=True)

    # I: configcontrole en lokale ontvanger
    INGEST_URL = "http://127.0.0.1:8181/ingest/x"
    try:
        checks.append(("v1.3 I: standaardconfig zonder fouten", config_fouten() == []))
        INGEST_URL = ""
        checks.append(("v1.3 I: ontbrekende INGEST_URL gemeld",
                       any("INGEST_URL" in f for f in config_fouten())))
        checks.append(("v1.3 I: dry-run zonder INGEST_URL mag",
                       config_fouten(ingest_nodig=False) == []))
        INGEST_URL = "ftp://x/y"
        checks.append(("v1.3 I: verkeerd schema gemeld",
                       any("http" in f for f in config_fouten())))
    finally:
        INGEST_URL = bewaard[1]
    checks.append(("v1.3: ontvanger 127.0.0.1 is lokaal, https elders niet",
                   ingest_lokaal("http://127.0.0.1:8181/x")
                   and not ingest_lokaal("https://voorbeeld.nl/x")))

    # I: hartslag op de monotone klok, niet op de wandklok
    basis = dict(soc_display=80.0, soc_bms=79.5, dc_charging=False,
                 ignition=False, main_relay=True, hv_current=0.0)
    checks.append(("v1.3 I: klok springt vooruit, geen onterechte hartslag",
                   not worth_sending(basis, basis, time.monotonic() - 10)[0]))
    return checks


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

    checks.append(("Echo en lengte 220101/220105 kloppen",
                   not _geeft_buserror(check_antwoord, _assemble(SELFTEST_0101),
                                       b"\x01\x01", MIN_LEN_0101)
                   and not _geeft_buserror(check_antwoord, _assemble(SELFTEST_0105),
                                           b"\x01\x05", MIN_LEN_0105)))
    checks += _robuustheid_selftest()
    checks += _opgeslagen_selftest()
    checks += _v13_selftest()

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

    fouten = config_fouten(ingest_nodig=not args.dry_run)
    if fouten:
        for f in fouten:
            log.error("Configuratie: %s", f)
        return 2

    if args.once:
        sock = open_can(CAN_IFACE)
        try:
            values = read_bms(sock)
        except (BusError, OSError) as exc:
            # OSError: met de auto in slaap loopt de CAN-zendbuffer vol
            # (ENOBUFS, er bevestigt niemand). Hetzelfde als geen antwoord.
            print("Geen antwoord van de BMS (%s) - auto uit?" % exc, file=sys.stderr)
            return 3
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
