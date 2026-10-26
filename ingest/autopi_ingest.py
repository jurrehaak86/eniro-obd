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

Configuratie via omgevingsvariabelen, zie autopi-ingest.env.example.
"""

import gzip
import json
import logging
import os
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import paho.mqtt.client as mqtt

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

if not INGEST_PATH:
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
            self.connected.set()
        else:
            log.error("MQTT-verbinding geweigerd: %s", reason_code)
            self.connected.clear()

    def _on_message(self, client, userdata, msg):
        if PROFIEL_TOPIC and msg.topic == PROFIEL_TOPIC:
            profiel.zet(msg.payload.decode("utf-8", "replace"))

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

    def publish(self, topic, payload, retain=True):
        """Publiceert en wacht op bevestiging. Geeft True/False terug."""
        if not self.connected.wait(timeout=PUBLISH_TIMEOUT):
            log.error("Geen MQTT-verbinding, publicatie naar %s overgeslagen", topic)
            return False
        info = self.client.publish(topic, payload, qos=MQTT_QOS, retain=retain)
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            log.error("Publicatie naar %s mislukt: rc=%s", topic, info.rc)
            return False
        if MQTT_QOS > 0:
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


def is_laadrecord(record):
    """BMS-record van eniro-soc terwijl de auto laadt.

    Die gaan ook in 'dagelijks' op 4G direct door: de laadautomation in HA wil
    tijdens het laden een verse SoC. eniro-soc stuurt er dan één per 5 min.
    """
    if record.get("@t") != "obd.ev.bms":
        return False
    try:
        stroom = float(record.get("hv_current", 0))
    except (TypeError, ValueError):
        stroom = 0.0
    return bool(record.get("dc_charging")) or (
        stroom < -1.0 and not record.get("ignition"))


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
        ok = publisher.publish(topic, payload) and ok
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


def main():
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
