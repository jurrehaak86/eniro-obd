#!/usr/bin/env python3
"""
autopi-ingest — ontvangt de uploads van de AutoPi-dongle en zet ze op MQTT.

De dongle POST een gzip-gecomprimeerde JSON-array naar een geheim pad. Elk
record heeft de vorm:

    {"@ts": "...", "@tag": "vehicle/battery/discharging",
     "@t": "event.vehicle.battery", "level": 80}

Per record wordt het hele record als JSON gepubliceerd op
    <MQTT_PREFIX>/<@tag>            (retained)
en op
    <MQTT_PREFIX>/_last             (retained, laatste record ongeacht tag)

Niet elk record heeft een `@tag`. De 12V-metingen van de SPM komen alleen met
`@t`, bijvoorbeeld:

    {"state": "discharging", "voltage": 12.68, "@t": "obd.bat", "level": 100}

Ontbreekt `@tag`, dan valt de topic terug op `@t` met punten als
scheidingsteken -- `obd.bat` wordt `<MQTT_PREFIX>/obd/bat`. Alleen als beide
ontbreken komt het op `_untagged`.

Pas als MQTT de publicatie bevestigt gaat er een 200 terug. Faalt het, dan
krijgt de dongle een 503 en biedt hij het later opnieuw aan — zijn eigen
cache doet tot 30 retries. Zo raken we geen data kwijt.

Profielen (sinds 18-10-2026, alleen zinvol op de dongle zelf):

    reis       alles gaat live door, zoals altijd
    dagelijks  op een bemeten verbinding (4G) wordt per topic alleen de laatste
               waarde bewaard en elke BUNDEL_INTERVAL seconden (30 min) in één
               keer gepubliceerd. Op wifi gaat alles live.

Het profiel komt retained binnen op PROFIEL_TOPIC (gezet door een automation in
HA) en wordt op schijf onthouden, zodat het na een boot meteen klopt.

MQTT Discovery (sinds v1.2, HA_DISCOVERY=1): de ontvanger publiceert de
entiteiten uit discovery.json als twee device-discoveryberichten
(<HA_DISCOVERY_PREFIX>/device/<apparaat>/config, retained). Zuinig: alleen als
de definitie veranderd is (hash op schijf), en opnieuw als Home Assistant
'online' meldt op <HA_DISCOVERY_PREFIX>/status (herstart van HA).

Configuratie via omgevingsvariabelen, zie autopi-ingest.env.example.
"""

import gzip
import hashlib
import json
import logging
import os
import re
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def _stub_paho():
    """Nep-paho voor --selftest: geen broker en geen paho nodig."""
    import types

    class _Info:
        rc = 0

        def wait_for_publish(self, timeout=None):
            pass

        def is_published(self):
            return True

    class _Client:
        def __init__(self, *a, **k):
            self.gepubliceerd = []
            self.vlaggen = []          # (topic, qos, retain)
            self.geabonneerd = []

        def __getattr__(self, naam):
            return lambda *a, **k: None

        def subscribe(self, topic, qos=0):
            self.geabonneerd.append(topic)
            return (0, 1)

        def publish(self, topic, payload=None, qos=0, retain=False):
            self.gepubliceerd.append((topic, payload))
            self.vlaggen.append((topic, qos, retain))
            return _Info()

    client = types.ModuleType("paho.mqtt.client")
    client.Client = _Client
    client.CallbackAPIVersion = types.SimpleNamespace(VERSION2=2)
    client.MQTT_ERR_SUCCESS = 0
    for naam in ("paho", "paho.mqtt"):
        sys.modules[naam] = types.ModuleType(naam)
    sys.modules["paho.mqtt.client"] = client
    sys.modules["paho.mqtt"].client = client
    sys.modules["paho"].mqtt = sys.modules["paho.mqtt"]


SELFTEST = "--selftest" in sys.argv[1:]
if SELFTEST:
    _stub_paho()

import paho.mqtt.client as mqtt  # noqa: E402

# ---------------------------------------------------------------- configuratie

LISTEN_HOST = os.environ.get("LISTEN_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "8181"))

# Het geheime pad waarop de dongle post, bijv. "/ingest/<32-hex>"
INGEST_PATH = os.environ.get("INGEST_PATH", "").rstrip("/")

MQTT_HOST = os.environ.get("MQTT_HOST", "127.0.0.1")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
MQTT_USER = os.environ.get("MQTT_USER") or None
MQTT_PASS = os.environ.get("MQTT_PASS") or None
MQTT_PREFIX = os.environ.get("MQTT_PREFIX", "autopi").strip("/")
MQTT_QOS = int(os.environ.get("MQTT_QOS", "1"))
PUBLISH_TIMEOUT = float(os.environ.get("PUBLISH_TIMEOUT", "10"))

# Ruwe payloads wegschrijven voor debug; leeg = uit
RAW_DIR = os.environ.get("RAW_DIR", "").strip()
RAW_KEEP = int(os.environ.get("RAW_KEEP", "200"))

MAX_BODY = int(os.environ.get("MAX_BODY", str(8 * 1024 * 1024)))

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)-7s %(message)s",
)
log = logging.getLogger("autopi-ingest")


def mask_path(path):
    """Toont genoeg om een pad te herkennen, te weinig om het te gebruiken.

    Het ingest-pad is de enige authenticatie op een endpoint dat vanaf het hele
    internet bereikbaar is. Een geheim in een logregel komt mee in elke
    journal-dump, elke backup en elke supportvraag — dus loggen we het nooit
    voluit. Op 15-09-2026 lekte het pad precies zo weg.
    """
    if not path:
        return "(leeg)"
    staart = path.rstrip("/").rsplit("/", 1)[-1]
    if len(staart) <= 6:
        return "/ingest/***"
    return "/ingest/%s***%s" % (staart[:3], staart[-2:])

if not INGEST_PATH and not SELFTEST:
    log.error("INGEST_PATH is niet gezet. Zonder geheim pad start ik niet.")
    sys.exit(1)

# Instelbaar zodat dezelfde ontvanger ook op de dongle zelf kan draaien. De
# standaardwaarden zijn die van de servervariant (ontvanger achter HTTPS).
#
# MQTT_CLIENT_ID: twee clients met hetzelfde ID gooien elkaar bij de broker
#   er steeds uit. Draai je beide varianten, geef ze elk een eigen ID.
# MQTT_KEEPALIVE: op de dongle gaat elke keepalive over 4G. 300 s in plaats
#   van 60 scheelt ruwweg 10 KB per wakker uur.
# STATUS_TOPIC: de last-will. Op de dongle vuurt die elke keer dat hij gaat
#   slapen; op autopi/status zou dat alle HA-sensoren met dat
#   availability_topic onbeschikbaar maken. Vandaar een eigen topic.
MQTT_CLIENT_ID = os.environ.get("MQTT_CLIENT_ID", "autopi-ingest")
MQTT_KEEPALIVE = int(os.environ.get("MQTT_KEEPALIVE", "60"))
STATUS_TOPIC = os.environ.get("STATUS_TOPIC", f"{MQTT_PREFIX}/status")

# Profielen. PROFIEL_TOPIC leeg = functie uit (zo blijft de servervariant gelijk).
PROFIEL_TOPIC = os.environ.get("PROFIEL_TOPIC", "").strip()
PROFIEL_STANDAARD = os.environ.get("PROFIEL_STANDAARD", "dagelijks").strip().lower()
PROFIEL_BESTAND = os.environ.get(
    "PROFIEL_BESTAND", "/var/lib/autopi-ingest/profiel")
BUNDEL_INTERVAL = int(os.environ.get("BUNDEL_INTERVAL", "1800"))
# Topics (staart na het prefix) die ook in 'dagelijks' direct doorgaan.
BUNDEL_UITZONDERINGEN = [
    x.strip() for x in os.environ.get(
        "BUNDEL_UITZONDERINGEN", "vehicle/battery/critical_level").split(",")
    if x.strip()]
METERED_IFACES = set(
    x.strip() for x in os.environ.get("METERED_IFACES", "wwan0,ppp0").split(",")
    if x.strip())

# MQTT Discovery (v1.2). Standaard UIT in de code, zodat een bestaande
# installatie niets merkt; install.sh zet HA_DISCOVERY=1 bij een verse
# installatie. Zie homeassistant/discovery.json en INSTALLATIE (Home Assistant).
VERSIE = "1.2.0"
PROJECT_URL = "https://github.com/jurrehaak86/eniro-obd"
HA_DISCOVERY = os.environ.get("HA_DISCOVERY", "0").strip() == "1"
HA_DISCOVERY_PREFIX = (os.environ.get("HA_DISCOVERY_PREFIX", "").strip().strip("/")
                       or "homeassistant")


def _standaard_discovery_bestand():
    """Naast het script (/opt/autopi-ingest/discovery.json, zo installeert
    install.sh hem); anders de plek in de repository."""
    hier = os.path.dirname(os.path.abspath(__file__))
    for pad in (os.path.join(hier, "discovery.json"),
                os.path.join(hier, "..", "homeassistant", "discovery.json")):
        if os.path.exists(pad):
            return os.path.normpath(pad)
    return os.path.join(hier, "discovery.json")


DISCOVERY_FILE = (os.environ.get("DISCOVERY_FILE", "").strip()
                  or _standaard_discovery_bestand())
DISCOVERY_HASH_BESTAND = os.environ.get(
    "DISCOVERY_HASH_BESTAND", "/var/lib/autopi-ingest/discovery.sha256")

# ------------------------------------------------------------------ mqtt-client


class Publisher:
    """Houdt één MQTT-verbinding open en publiceert met bevestiging."""

    def __init__(self):
        try:  # paho-mqtt 2.x
            self.client = mqtt.Client(
                mqtt.CallbackAPIVersion.VERSION2, client_id=MQTT_CLIENT_ID
            )
        except AttributeError:  # paho-mqtt 1.x
            self.client = mqtt.Client(client_id=MQTT_CLIENT_ID)

        if MQTT_USER:
            self.client.username_pw_set(MQTT_USER, MQTT_PASS)

        self.client.will_set(STATUS_TOPIC, "offline", qos=1, retain=True)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message
        self.client.reconnect_delay_set(min_delay=1, max_delay=60)
        self.connected = threading.Event()

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        ok = (reason_code == 0) or (getattr(reason_code, "is_failure", True) is False)
        if ok:
            log.info("MQTT verbonden met %s:%s", MQTT_HOST, MQTT_PORT)
            client.publish(STATUS_TOPIC, "online", qos=1, retain=True)
            if PROFIEL_TOPIC:
                client.subscribe(PROFIEL_TOPIC, qos=1)
            if HA_DISCOVERY:
                client.subscribe(birth_topic(), qos=1)
            self.connected.set()
            if HA_DISCOVERY:
                # Niet in deze callback publiceren en wachten: dat blokkeert
                # de netwerklus van paho, die de bevestiging moet ontvangen.
                achtergrond(discovery.sync, "verbonden")
        else:
            log.error("MQTT-verbinding geweigerd: %s", reason_code)
            self.connected.clear()

    def _on_message(self, client, userdata, msg):
        if PROFIEL_TOPIC and msg.topic == PROFIEL_TOPIC:
            profiel.zet(msg.payload.decode("utf-8", "replace"))
        elif HA_DISCOVERY and msg.topic == birth_topic():
            # Alleen een live 'online' (HA is net gestart). Een retained
            # 'online' krijgen we bij elke verbinding opnieuw; daarop
            # publiceren zou elke wekbeurt kilobytes 4G kosten.
            tekst = msg.payload.decode("utf-8", "replace").strip()
            if tekst == "online" and not getattr(msg, "retain", False):
                achtergrond(discovery.sync, "Home Assistant online", True)

    def _on_disconnect(self, client, userdata, *args):
        log.warning("MQTT-verbinding verbroken, paho probeert opnieuw")
        self.connected.clear()

    def start(self):
        self.client.connect_async(MQTT_HOST, MQTT_PORT, keepalive=MQTT_KEEPALIVE)
        self.client.loop_start()

    def stop(self):
        try:
            self.client.publish(STATUS_TOPIC, "offline", qos=1, retain=True)
            time.sleep(0.2)
        finally:
            self.client.loop_stop()
            self.client.disconnect()

    def publish(self, topic, payload, retain=True, qos=None):
        """Publiceert en wacht op bevestiging. Geeft True/False terug."""
        qos = MQTT_QOS if qos is None else qos
        if not self.connected.wait(timeout=PUBLISH_TIMEOUT):
            log.error("Geen MQTT-verbinding, publicatie naar %s overgeslagen", topic)
            return False
        info = self.client.publish(topic, payload, qos=qos, retain=retain)
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            log.error("Publicatie naar %s mislukt: rc=%s", topic, info.rc)
            return False
        if qos > 0:
            try:
                try:
                    info.wait_for_publish(timeout=PUBLISH_TIMEOUT)
                except TypeError:
                    # paho-mqtt < 1.6 (Debian buster, dus de dongle) kent geen
                    # timeout-argument. Zelf wachten, anders hangt een request
                    # voor altijd als de broker niet bevestigt.
                    deadline = time.monotonic() + PUBLISH_TIMEOUT
                    while not info.is_published() and time.monotonic() < deadline:
                        time.sleep(0.05)
            except (ValueError, RuntimeError) as exc:
                log.error("Wachten op bevestiging %s mislukt: %s", topic, exc)
                return False
            if not info.is_published():
                log.error("Publicatie naar %s niet bevestigd binnen timeout", topic)
                return False
        return True


publisher = Publisher()


def achtergrond(fn, *args):
    threading.Thread(target=fn, args=args, daemon=True).start()

# --------------------------------------------------------------- mqtt discovery

STANDAARD_PREFIX = "autopi/"
STANDAARD_STATUS = "autopi/dongle/status"
PLATFORMS = ("sensor", "binary_sensor", "device_tracker")
# Alleen de afkortingen die de HA-documentatie voor device discovery noemt;
# de opties van de entiteiten gaan voluit (HA leest beide).
DEV_AFKORTING = {"identifiers": "ids", "manufacturer": "mf", "model": "mdl",
                 "sw_version": "sw", "name": "name"}


def birth_topic():
    return "%s/status" % HA_DISCOVERY_PREFIX


def discovery_topic(apparaat):
    return "%s/device/%s/config" % (HA_DISCOVERY_PREFIX, apparaat)


def eigen_topic(topic):
    """discovery.json noemt het standaardprefix; dit is wat wij publiceren."""
    if topic == STANDAARD_STATUS:
        return STATUS_TOPIC
    if topic.startswith(STANDAARD_PREFIX):
        return "%s/%s" % (MQTT_PREFIX, topic[len(STANDAARD_PREFIX):])
    return topic


def laad_discovery(pad=None):
    with open(pad or DISCOVERY_FILE, encoding="utf-8") as fh:
        return json.load(fh)


def discovery_berichten(defs):
    """discovery.json -> [(topic, payload)], één device-discoverybericht per
    apparaat. Velden die met _ beginnen (uitleg) gaan niet mee."""
    uit = []
    for apparaat, d in defs["devices"].items():
        if not re.match(r"^[A-Za-z0-9_-]+$", apparaat):
            raise ValueError("apparaat-id %r: alleen letters, cijfers, _ en -" % apparaat)
        dev = dict((DEV_AFKORTING.get(k, k), v) for k, v in d["device"].items()
                   if not k.startswith("_"))
        dev["sw"] = VERSIE
        cmps = {}
        for cid, c in d["components"].items():
            comp = {}
            for k, v in c.items():
                if k.startswith("_"):
                    continue
                if k == "platform":
                    k = "p"
                elif k.endswith("_topic"):
                    v = eigen_topic(v)
                comp[k] = v
            if comp.get("p") not in PLATFORMS:
                raise ValueError("%s: platform moet een van %s zijn" % (cid, ", ".join(PLATFORMS)))
            # Alleen "platform" = verwijderen: HA haalt de entiteit dan weg.
            if len(comp) > 1 and not comp.get("unique_id"):
                raise ValueError("%s: unique_id is verplicht" % cid)
            cmps[cid] = comp
        bericht = {"dev": dev,
                   "o": {"name": "eniro-obd", "sw": VERSIE, "url": PROJECT_URL},
                   "cmps": cmps}
        uit.append((discovery_topic(apparaat),
                    json.dumps(bericht, separators=(",", ":"), ensure_ascii=False)))
    return uit


def berichten_hash(berichten):
    h = hashlib.sha256()
    for topic, payload in berichten:
        h.update(topic.encode("utf-8") + b"\0" + payload.encode("utf-8") + b"\0")
    return h.hexdigest()


class Discovery:
    """Publiceert discovery.json, maar alleen als het nodig is."""

    def __init__(self):
        self.lock = threading.Lock()

    def _gelezen_hash(self):
        try:
            with open(DISCOVERY_HASH_BESTAND) as fh:
                return fh.read().strip()
        except OSError:
            return None

    def _bewaar_hash(self, waarde):
        try:
            os.makedirs(os.path.dirname(DISCOVERY_HASH_BESTAND), exist_ok=True)
            with open(DISCOVERY_HASH_BESTAND, "w") as fh:
                fh.write(waarde + "\n")
        except OSError as exc:
            log.warning("Discovery-hash onthouden mislukt: %s", exc)

    def sync(self, reden, forceer=False):
        """Geeft het aantal gepubliceerde berichten terug (0 = niets nodig of
        uit), of None bij een fout."""
        if not HA_DISCOVERY:
            return 0
        with self.lock:
            try:
                berichten = discovery_berichten(laad_discovery())
            except (OSError, ValueError, KeyError, AttributeError) as exc:
                log.error("Discovery: %s niet bruikbaar: %s", DISCOVERY_FILE, exc)
                return None
            nieuw = berichten_hash(berichten)
            if not forceer and nieuw == self._gelezen_hash():
                log.debug("Discovery ongewijzigd (%s), niets verstuurd", reden)
                return 0
            for topic, payload in berichten:
                if not publisher.publish(topic, payload, retain=True, qos=1):
                    log.warning("Discovery naar %s mislukt; volgende verbinding opnieuw", topic)
                    return None
            self._bewaar_hash(nieuw)
            log.info("Discovery gepubliceerd (%s): %d bericht(en), %d bytes", reden,
                     len(berichten), sum(len(p.encode("utf-8")) for _, p in berichten))
            return len(berichten)


discovery = Discovery()

# -------------------------------------------------------------------- verwerken


def topic_for(record):
    """Bepaalt de topic-staart voor een record.

    Voorkeur voor `@tag`. Ontbreekt die, dan `@t` met punten omgezet naar
    slashes -- zo landt `obd.bat` op `obd/bat` in plaats van op een hoop
    met alle andere naamloze records.
    """
    tag = record.get("@tag")
    if not tag:
        tag = str(record.get("@t") or "").replace(".", "/")
    return sanitize_tag(tag)


def sanitize_tag(tag):
    """Maakt van een tag een veilig MQTT-topic-fragment."""
    if not tag:
        return "_untagged"
    parts = []
    for part in str(tag).split("/"):
        part = part.strip()
        # MQTT-jokers en niet-printbare tekens eruit
        part = "".join(c for c in part if c.isprintable() and c not in "+#")
        if part:
            parts.append(part)
    return "/".join(parts) or "_untagged"


def store_raw(body):
    if not RAW_DIR:
        return
    try:
        os.makedirs(RAW_DIR, exist_ok=True)
        name = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%f") + ".bin"
        with open(os.path.join(RAW_DIR, name), "wb") as fh:
            fh.write(body)
        files = sorted(os.listdir(RAW_DIR))
        for old in files[:-RAW_KEEP]:
            os.remove(os.path.join(RAW_DIR, old))
    except OSError as exc:
        log.warning("Ruwe payload wegschrijven mislukt: %s", exc)


def decode_body(body, content_encoding):
    """Pakt uit als het gzip is — herkend aan de header of aan de magic bytes."""
    is_gzip = "gzip" in (content_encoding or "").lower() or body[:2] == b"\x1f\x8b"
    if is_gzip:
        body = gzip.decompress(body)
    return body.decode("utf-8")


# --------------------------------------------------------------------- profielen


def normaliseer_profiel(tekst):
    """'Reis', 'reis (live)', 'REIS' → 'reis'; al het andere → 'dagelijks'."""
    return "reis" if "reis" in str(tekst).strip().lower() else "dagelijks"


class Profiel:
    def __init__(self):
        self.naam = normaliseer_profiel(PROFIEL_STANDAARD)
        try:
            with open(PROFIEL_BESTAND) as fh:
                self.naam = normaliseer_profiel(fh.read())
        except OSError:
            pass

    def zet(self, tekst):
        nieuw = normaliseer_profiel(tekst)
        if nieuw != self.naam:
            log.info("Profiel: %s → %s", self.naam, nieuw)
        self.naam = nieuw
        try:
            os.makedirs(os.path.dirname(PROFIEL_BESTAND), exist_ok=True)
            with open(PROFIEL_BESTAND, "w") as fh:
                fh.write(nieuw)
        except OSError as exc:
            log.warning("Profiel onthouden mislukt: %s", exc)


profiel = Profiel()


def default_iface(route_bestand="/proc/net/route"):
    """Interface van de default route met de laagste metric, of None.

    Uit /proc in plaats van 'ip route': de dienst mag geen netlink-sockets
    openen (RestrictAddressFamilies).
    """
    beste, laagste = None, None
    try:
        with open(route_bestand) as fh:
            next(fh)
            for regel in fh:
                v = regel.split()
                if len(v) < 7 or v[1] != "00000000":
                    continue
                metric = int(v[6])
                if laagste is None or metric < laagste:
                    beste, laagste = v[0], metric
    except (OSError, ValueError, StopIteration):
        return None
    return beste


def bundelen():
    """True als records nu gebundeld moeten worden in plaats van live."""
    if not PROFIEL_TOPIC or profiel.naam == "reis":
        return False
    dev = default_iface()
    # Onbekend = bemeten: falen doen we zuinig, net als eniro-soc.
    return dev is None or dev in METERED_IFACES


class Bundel:
    """Laatste record per topic, tot het tijd is om te versturen."""

    def __init__(self):
        self.lock = threading.Lock()
        self.laatste = {}          # topic → payload, in volgorde van binnenkomst
        self.sinds = None

    def voeg_toe(self, topic, payload):
        with self.lock:
            self.laatste.pop(topic, None)   # opnieuw achteraan: volgorde blijft kloppen
            self.laatste[topic] = payload
            if self.sinds is None:
                self.sinds = time.monotonic()

    def vergeet(self, topic):
        """Gooit een wachtende waarde weg; er gaat net iets nieuwers live.

        Anders publiceert de volgende bundel een ouder record ná het nieuwere
        en springt bijvoorbeeld de SoC in HA terug.
        """
        with self.lock:
            self.laatste.pop(topic, None)
            if not self.laatste:
                self.sinds = None

    def moet_weg(self):
        with self.lock:
            if not self.laatste:
                return False
            return (not bundelen()
                    or time.monotonic() - self.sinds >= BUNDEL_INTERVAL)

    def verstuur(self, reden):
        with self.lock:
            items = list(self.laatste.items())
        if not items:
            return True
        ok_topics = []
        for topic, payload in items:
            if not publisher.publish(topic, payload):
                break
            ok_topics.append(topic)
        if ok_topics:
            publisher.publish(f"{MQTT_PREFIX}/_last", dict(items)[ok_topics[-1]])
        with self.lock:
            for t in ok_topics:
                # alleen weghalen als er intussen niets nieuwers voor kwam
                if self.laatste.get(t) == dict(items)[t]:
                    del self.laatste[t]
            self.sinds = time.monotonic() if self.laatste else None
        log.info("Bundel verstuurd (%s): %d van %d topic(s)",
                 reden, len(ok_topics), len(items))
        return len(ok_topics) == len(items)


bundel = Bundel()


def laadt(record):
    """Zelfde definitie als laadt() in eniro_soc.py: snelladen, of meer dan
    1 A de accu in met het contact uit."""
    try:
        stroom = float(record.get("hv_current", 0))
    except (TypeError, ValueError):
        stroom = 0.0
    return bool(record.get("dc_charging")) or (
        stroom < -1.0 and not record.get("ignition"))


def is_bms(record):
    return record.get("@t") == "obd.ev.bms"


# Laadstatus van het laatst live gepubliceerde BMS-record; None = nog niets
# sinds de start, en dan geldt er ook geen wissel (zuinig, zoals v1.0).
# Alleen bijgewerkt na een gelukte publicatie, zodat een herhaalde upload na
# een 503 opnieuw live gaat.
laatste_bms_laadt = None


def is_laadrecord(record):
    """BMS-record van eniro-soc terwijl de auto laadt, of het eerste record
    na een laadwissel (begin of einde van laden).

    Die gaan ook in 'dagelijks' op 4G direct door: de laadautomation in HA wil
    tijdens het laden een verse SoC (eniro-soc stuurt er dan één per 5 min),
    en het einde van laden mag niet een half uur in de bundel blijven hangen.
    """
    if not is_bms(record):
        return False
    if laadt(record):
        return True
    return laatste_bms_laadt is not None and laatste_bms_laadt != laadt(record)


def bundel_lus(stop):
    while not stop.wait(30):
        try:
            if bundel.moet_weg():
                bundel.verstuur("interval" if bundelen() else "live")
        except Exception as exc:  # noqa: BLE001
            log.warning("Bundel versturen mislukt: %s", exc)


def handle_payload(body, content_encoding):
    """Verwerkt één upload. Geeft (aantal_records, ok) terug."""
    store_raw(body)
    text = decode_body(body, content_encoding)
    data = json.loads(text)
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        raise ValueError("verwachtte een JSON-array of -object, kreeg %s" % type(data))

    global laatste_bms_laadt
    ok = True
    gebundeld = bundelen()
    if not gebundeld and bundel.laatste:
        # Net terug op wifi of naar 'reis': eerst wat nog wacht, dan live.
        bundel.verstuur("live")
    for record in data:
        if not isinstance(record, dict):
            log.warning("Record overgeslagen, geen object: %r", record)
            continue
        staart = topic_for(record)
        topic = f"{MQTT_PREFIX}/{staart}"
        payload = json.dumps(record, separators=(",", ":"), ensure_ascii=False)
        if (gebundeld and not is_laadrecord(record)
                and not any(staart.startswith(u) for u in BUNDEL_UITZONDERINGEN)):
            bundel.voeg_toe(topic, payload)
            continue
        # Wat nog voor dit topic in de bundel wacht is ouder: weg ermee.
        bundel.vergeet(topic)
        gelukt = publisher.publish(topic, payload)
        if gelukt and is_bms(record):
            laatste_bms_laadt = laadt(record)
        ok = gelukt and ok
        ok = publisher.publish(f"{MQTT_PREFIX}/_last", payload) and ok
        log.debug("→ %s %s", topic, payload)
    return len(data), ok


# ------------------------------------------------------------------ http-server


class Handler(BaseHTTPRequestHandler):
    server_version = "autopi-ingest"
    sys_version = ""

    def _reply(self, code, obj):
        payload = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        # Alleen een health-endpoint; alles anders bestaat niet.
        if self.path == "/healthz":
            healthy = publisher.connected.is_set()
            self._reply(200 if healthy else 503, {"mqtt": healthy})
        else:
            self._reply(404, {"error": "not found"})

    def do_POST(self):
        if self.path.rstrip("/") != INGEST_PATH:
            log.warning("Onbekend pad van %s: %s",
                        self.client_address[0], mask_path(self.path))
            self._reply(404, {"error": "not found"})
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._reply(400, {"error": "bad content-length"})
            return

        if length <= 0:
            self._reply(400, {"error": "empty body"})
            return
        if length > MAX_BODY:
            self._reply(413, {"error": "body too large"})
            return

        body = self.rfile.read(length)

        try:
            count, ok = handle_payload(body, self.headers.get("Content-Encoding"))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            # Onleesbare payload: 400, want opnieuw aanbieden lost niets op.
            log.error("Payload onverwerkbaar: %s", exc)
            self._reply(400, {"error": "unprocessable payload"})
            return
        except Exception as exc:  # noqa: BLE001 - laatste vangnet
            log.exception("Onverwachte fout: %s", exc)
            self._reply(503, {"error": "internal error"})
            return

        if ok:
            log.info("%d record(s) verwerkt (%d bytes)", count, length)
            self._reply(200, {"ok": True, "records": count})
        else:
            # MQTT deed het niet: 503, dan biedt de dongle het later opnieuw aan.
            log.error("MQTT-publicatie mislukt, 503 terug zodat de dongle retryt")
            self._reply(503, {"error": "mqtt unavailable"})

    do_PUT = do_POST

    def log_message(self, fmt, *args):
        log.debug("%s %s", self.client_address[0], fmt % args)


def selftest():
    """Speelt een rit, een laadsessie en het einde ervan af op 4G in profiel
    'dagelijks', met een nep-broker. Controleert dat het einde van laden live
    gaat en dat er nooit een ouder BMS-record na een nieuwer komt."""
    global PROFIEL_TOPIC, default_iface
    PROFIEL_TOPIC = "selftest/profiel"
    profiel.naam = "dagelijks"
    default_iface = lambda *a: "wwan0"          # noqa: E731 - 4G
    publisher.connected.set()
    log.setLevel(logging.WARNING)
    gepubliceerd = publisher.client.gepubliceerd
    bms_topic = f"{MQTT_PREFIX}/obd/ev/bms"

    def post(ts, stroom, contact):
        rec = {"@t": "obd.ev.bms", "@ts": ts, "hv_current": stroom,
               "ignition": contact, "dc_charging": False, "soc_display": 50}
        handle_payload(json.dumps([rec]).encode(), None)

    def bms_ts():
        uit = [json.loads(p)["@ts"] for t, p in gepubliceerd if t == bms_topic]
        del gepubliceerd[:]
        return uit

    checks = []
    post("10:00", 30.0, True)                  # rijden: gebundeld
    checks.append(("rit wordt gebundeld", bms_ts() == []))
    post("10:05", -7.0, False)                 # laden begint
    checks.append(("begin laden gaat live", bms_ts() == ["10:05"]))
    checks.append(("oudere rit-waarde uit de bundel gehaald",
                   bms_topic not in bundel.laatste))
    post("10:10", -7.0, False)
    checks.append(("tijdens laden live", bms_ts() == ["10:10"]))
    post("10:33", 0.0, False)                  # laden gestopt, SoC gelijk
    checks.append(("einde laden gaat live", bms_ts() == ["10:33"]))
    post("10:40", 0.0, False)                  # stilstaan: weer bundelen
    checks.append(("daarna weer gebundeld", bms_ts() == []))
    bundel.verstuur("afsluiten")
    checks.append(("bundel geeft alleen het nieuwste", bms_ts() == ["10:40"]))
    checks.append(("laaddefinitie: regeneratie is geen laden",
                   not laadt({"hv_current": -20.0, "ignition": True})))

    del gepubliceerd[:]
    checks.extend(selftest_discovery())

    ok = True
    for label, geslaagd in checks:
        if geslaagd is None:
            print("  SKIP %s" % label)
            continue
        print("  %s %s" % ("PASS" if geslaagd else "FAIL", label))
        ok = ok and geslaagd
    print()
    print("ZELFTEST %s" % ("GESLAAGD" if ok else "MISLUKT"))
    return 0 if ok else 1


def selftest_discovery():
    """MQTT Discovery: berichten opbouwen uit discovery.json, en publiceren
    alleen als het nodig is. Met de nep-broker en een tijdelijke hash."""
    import tempfile
    import types
    import zlib
    global HA_DISCOVERY, DISCOVERY_FILE, DISCOVERY_HASH_BESTAND, achtergrond
    global MQTT_PREFIX, STATUS_TOPIC, MQTT_HOST, MQTT_USER, MQTT_PASS, INGEST_PATH
    checks = []
    if not os.path.exists(DISCOVERY_FILE):
        return [("discovery: %s niet gevonden (HA_DISCOVERY=0-installatie)"
                 % DISCOVERY_FILE, None)]
    gepubliceerd = publisher.client.gepubliceerd
    bewaard = (HA_DISCOVERY, DISCOVERY_FILE, DISCOVERY_HASH_BESTAND, achtergrond,
               MQTT_PREFIX, STATUS_TOPIC, MQTT_HOST, MQTT_USER, MQTT_PASS, INGEST_PATH)
    tmp = tempfile.mkdtemp(prefix="autopi-ingest-selftest-")
    try:
        achtergrond = lambda fn, *a: fn(*a)          # noqa: E731 - synchroon
        DISCOVERY_HASH_BESTAND = os.path.join(tmp, "discovery.sha256")
        # Geheimen die nooit in een bericht mogen komen.
        MQTT_HOST, MQTT_USER = "192.0.2.77", "selftest-gebruiker"
        MQTT_PASS, INGEST_PATH = "selftest-wachtwoord-xyz", "/ingest/selftest-pad-xyz"
        MQTT_PREFIX, STATUS_TOPIC = "autopi", "autopi/dongle/status"
        defs = laad_discovery()

        def disc():
            uit = [t for t, _ in gepubliceerd if t.startswith(HA_DISCOVERY_PREFIX + "/device/")]
            del gepubliceerd[:]
            return uit

        # 1. Het bericht zelf
        berichten = discovery_berichten(defs)
        checks.append(("discovery: één bericht per apparaat, onder <prefix>/device/<id>/config",
                       [t for t, _ in berichten] == [discovery_topic(a) for a in defs["devices"]]
                       and all(re.match(r"^homeassistant/device/[A-Za-z0-9_-]+/config$", t)
                               for t, _ in berichten)))
        geladen = [json.loads(p) for _, p in berichten]
        checks.append(("discovery: geldige JSON met dev, o en cmps",
                       all(set(b) == {"dev", "o", "cmps"} for b in geladen)))
        checks.append(("discovery: dev.sw en o.sw = versie %s, o.url gezet" % VERSIE,
                       all(b["dev"].get("sw") == VERSIE and b["o"].get("sw") == VERSIE
                           and b["o"].get("name") and b["o"].get("url", "").startswith("https://")
                           for b in geladen)))
        checks.append(("discovery: dev heeft ids en name",
                       all(b["dev"].get("ids") and b["dev"].get("name") for b in geladen)))
        comps = [(cid, c) for b in geladen for cid, c in b["cmps"].items()]
        uids = [c.get("unique_id") for _, c in comps]
        checks.append(("discovery: elke entiteit heeft p en unique_id (%d stuks)" % len(comps),
                       all(c.get("p") in PLATFORMS and c.get("unique_id") for _, c in comps)))
        checks.append(("discovery: unique_id's uniek en gelijk aan de component-id",
                       len(set(uids)) == len(uids) and all(cid == c["unique_id"] for cid, c in comps)))
        checks.append(("discovery: default_entity_id past bij het platform",
                       all(c.get("default_entity_id", c["p"] + ".").startswith(c["p"] + ".")
                           for _, c in comps)))
        checks.append(("discovery: geen uitlegvelden (_comment) in het bericht",
                       not any(k.startswith("_") for _, c in comps for k in c)))
        # Topics moeten bestaan in wat de ontvanger publiceert voor de
        # records van eniro-soc, de firmware en diag, plus de last-will.
        bekend = set("%s/%s" % (MQTT_PREFIX, topic_for(r)) for r in (
            {"@t": "obd.ev.bms"},        # eniro-soc
            {"@t": "obd.bat"},           # firmware: 12V
            {"@t": "track.pos"},         # firmware: GPS
            {"@t": "device.diag"}))      # diag/
        bekend.add(STATUS_TOPIC)
        gebruikt = set(v for _, c in comps for k, v in c.items() if k.endswith("_topic"))
        checks.append(("discovery: alle topics komen voor in wat er gepubliceerd wordt",
                       gebruikt <= bekend and len(gebruikt) == 5))
        alles = "".join(p for _, p in berichten)
        checks.append(("discovery: geen host, gebruiker, wachtwoord of ingest-pad in het bericht",
                       not any(g in alles for g in (MQTT_HOST, MQTT_USER, MQTT_PASS, INGEST_PATH))))
        MQTT_PREFIX, STATUS_TOPIC = "auto2", "elders/status"
        anders = "".join(p for _, p in discovery_berichten(defs))
        checks.append(("discovery: MQTT_PREFIX en STATUS_TOPIC worden overgenomen",
                       '"auto2/obd/ev/bms"' in anders and '"elders/status"' in anders
                       and '"autopi/' not in anders))
        MQTT_PREFIX, STATUS_TOPIC = "autopi", "autopi/dongle/status"
        groottes = [len(p.encode("utf-8")) for _, p in berichten]
        gz = sum(len(zlib.compress(p.encode("utf-8"), 9)) for _, p in berichten)
        print("  INFO discovery: %d berichten, %s bytes (samen %d; gecomprimeerd ~%d)"
              % (len(groottes), "+".join(str(g) for g in groottes), sum(groottes), gz))

        # 2. Zuinig publiceren
        HA_DISCOVERY = False
        del gepubliceerd[:]
        r = discovery.sync("uit")
        checks.append(("discovery: HA_DISCOVERY=0 publiceert niets", r == 0 and disc() == []))
        publisher.client.geabonneerd = []
        publisher._on_connect(publisher.client, None, {}, 0)
        checks.append(("discovery: HA_DISCOVERY=0 abonneert niet op de birth",
                       birth_topic() not in publisher.client.geabonneerd and disc() == []))
        HA_DISCOVERY = True
        publisher.client.vlaggen = []
        publisher._on_connect(publisher.client, None, {}, 0)
        vlag = [(q, r) for t, q, r in publisher.client.vlaggen if t.startswith(HA_DISCOVERY_PREFIX)]
        checks.append(("discovery: eerste verbinding publiceert beide berichten, retained, QoS 1",
                       disc() == [t for t, _ in berichten] and vlag == [(1, True)] * len(berichten)
                       and os.path.exists(DISCOVERY_HASH_BESTAND)))
        checks.append(("discovery: abonneert op %s" % birth_topic(),
                       birth_topic() in publisher.client.geabonneerd))
        publisher._on_connect(publisher.client, None, {}, 0)
        checks.append(("discovery: tweede verbinding publiceert niets", disc() == []))
        bericht = lambda tekst, retain=False: types.SimpleNamespace(   # noqa: E731
            topic=birth_topic(), payload=tekst.encode(), retain=retain)
        publisher._on_message(publisher.client, None, bericht("online"))
        checks.append(("discovery: HA meldt 'online' -> opnieuw gepubliceerd",
                       disc() == [t for t, _ in berichten]))
        publisher._on_message(publisher.client, None, bericht("online", retain=True))
        publisher._on_message(publisher.client, None, bericht("offline"))
        checks.append(("discovery: retained 'online' of 'offline' -> niets", disc() == []))
        gewijzigd = json.loads(json.dumps(defs))
        eerste = next(iter(gewijzigd["devices"].values()))
        next(iter(eerste["components"].values()))["unit_of_measurement"] = "x"
        DISCOVERY_FILE = os.path.join(tmp, "discovery.json")
        with open(DISCOVERY_FILE, "w", encoding="utf-8") as fh:
            json.dump(gewijzigd, fh)
        publisher._on_connect(publisher.client, None, {}, 0)
        checks.append(("discovery: gewijzigde definitie -> opnieuw gepubliceerd",
                       len(disc()) == len(berichten)))
        publisher._on_connect(publisher.client, None, {}, 0)
        checks.append(("discovery: daarna weer niets", disc() == []))
        with open(DISCOVERY_FILE, "w", encoding="utf-8") as fh:
            fh.write("{kapot")
        niveau = log.level
        log.setLevel(logging.CRITICAL)             # de verwachte foutmelding niet tonen
        r = discovery.sync("kapot")
        log.setLevel(niveau)
        checks.append(("discovery: kapotte discovery.json -> fout, niets gepubliceerd",
                       r is None and disc() == []))
    finally:
        (HA_DISCOVERY, DISCOVERY_FILE, DISCOVERY_HASH_BESTAND, achtergrond,
         MQTT_PREFIX, STATUS_TOPIC, MQTT_HOST, MQTT_USER, MQTT_PASS, INGEST_PATH) = bewaard
        for naam in os.listdir(tmp):
            os.remove(os.path.join(tmp, naam))
        os.rmdir(tmp)
    return checks


def main():
    if SELFTEST:
        sys.exit(selftest())
    publisher.start()
    server = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)
    server.daemon_threads = True

    def shutdown(signum, frame):
        log.info("Signaal %s ontvangen, afsluiten", signum)
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    log.info("Luistert op %s:%s, pad %s", LISTEN_HOST, LISTEN_PORT,
             mask_path(INGEST_PATH))
    stop = threading.Event()
    if PROFIEL_TOPIC:
        log.info("Profiel: %s (topic %s, bundel elke %d s op %s)", profiel.naam,
                 PROFIEL_TOPIC, BUNDEL_INTERVAL, ",".join(sorted(METERED_IFACES)))
        threading.Thread(target=bundel_lus, args=(stop,), daemon=True).start()
    try:
        server.serve_forever()
    finally:
        stop.set()
        # Bij inslapen: de laatste stand nog meegeven, ook in 'dagelijks'.
        try:
            bundel.verstuur("afsluiten")
        except Exception as exc:  # noqa: BLE001
            log.warning("Bundel bij afsluiten mislukt: %s", exc)
        server.server_close()
        publisher.stop()
        log.info("Gestopt")


if __name__ == "__main__":
    main()
