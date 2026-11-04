#!/usr/bin/env python3
"""gen_ha_yaml.py - maakt homeassistant/eniro-mqtt.yaml uit homeassistant/discovery.json.

discovery.json is de enige bron van waarheid voor de MQTT-entiteiten. Met MQTT
Discovery (HA_DISCOVERY=1 op de dongle) publiceert de ontvanger dat bestand
zelf en heb je eniro-mqtt.yaml niet nodig. Wie liever YAML gebruikt, laadt het
bestand dat dit script maakt.

    python3 scripts/gen_ha_yaml.py            # schrijft homeassistant/eniro-mqtt.yaml
    python3 scripts/gen_ha_yaml.py --check    # exit 1 als het bestand niet meer
                                              # klopt met discovery.json
    python3 scripts/gen_ha_yaml.py --stdout   # alleen tonen

Alleen de standaardbibliotheek; geen PyYAML nodig.
"""

import json
import os
import re
import sys
import textwrap

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BRON = os.path.join(REPO, "homeassistant", "discovery.json")
DOEL = os.path.join(REPO, "homeassistant", "eniro-mqtt.yaml")
PLATFORMS = ("sensor", "binary_sensor", "device_tracker")
BREEDTE = 78
# Zonder aanhalingstekens alleen wat YAML zeker als tekst leest.
KAAL = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")
GERESERVEERD = {"y", "n", "yes", "no", "true", "false", "on", "off", "null"}

KOP = """\
# Home Assistant - MQTT-entiteiten voor de e-Niro en de AutoPi-dongle
#
# GEGENEREERD uit discovery.json door scripts/gen_ha_yaml.py - niet met de hand
# bewerken. Iets aanpassen? Wijzig homeassistant/discovery.json en draai
#   python3 scripts/gen_ha_yaml.py
#
# ALLEEN NODIG ZONDER MQTT DISCOVERY. Staat op de dongle HA_DISCOVERY=1, dan
# maakt de ontvanger deze entiteiten zelf aan in Home Assistant; laad dit
# bestand dan NIET (zelfde unique_id's twee keer = fouten in het log en
# entiteiten die niet laden). Zie INSTALLATIE, hoofdstuk Home Assistant.
#
# LADEN - kies een van de twee:
#   a) Als package (aanbevolen). Zet in configuration.yaml:
#        homeassistant:
#          packages: !include_dir_named packages
#      en kopieer dit bestand naar /config/packages/eniro-mqtt.yaml. Laat de
#      regel `mqtt:` hieronder dan staan.
#   b) Zonder packages: plak alles vanaf de regel `sensor:` onder je bestaande
#      `mqtt:`-sleutel in configuration.yaml (een tweede `mqtt:` geeft een fout).
# Daarna: Ontwikkelaarshulpmiddelen > YAML > "Handmatig geconfigureerde
# MQTT-entiteiten" herladen (de eerste keer een herstart).
#
# Topics: hier staat het standaardprefix autopi/. Gebruik je op de dongle een
# ander MQTT_PREFIX of STATUS_TOPIC, pas ze dan in discovery.json aan en
# genereer opnieuw (met discovery doet de ontvanger dat zelf).
#
# Het device-blok staat per apparaat een keer voluit (&anker) en de rest
# verwijst ernaar (*anker). Ankers werken alleen binnen een bestand: knip je
# dit bestand op, zet dan overal een volledig device-blok.
#
# Uit discovery.json:
"""


def commentaar(tekst, inspring):
    regels = tekst if isinstance(tekst, list) else [tekst]
    uit = []
    for i, alinea in enumerate(regels):
        if i:
            uit.append(" " * inspring + "#")
        uit.extend(textwrap.wrap(alinea, BREEDTE - inspring - 2,
                                 initial_indent=" " * inspring + "# ",
                                 subsequent_indent=" " * inspring + "# ",
                                 break_long_words=False, break_on_hyphens=False))
    return uit


def scalar(sleutel, waarde, inspring):
    """Eén 'sleutel: waarde'-regel (of blok) in YAML, zonder PyYAML."""
    pre = " " * inspring + sleutel + ":"
    if isinstance(waarde, bool):
        return [pre + (" true" if waarde else " false")]
    if isinstance(waarde, (int, float)):
        return [pre + " " + json.dumps(waarde)]
    if isinstance(waarde, list):
        return [pre + " " + json.dumps(waarde, ensure_ascii=False)]
    if not isinstance(waarde, str):
        raise SystemExit("%s: type %s wordt niet ondersteund" % (sleutel, type(waarde).__name__))
    regels = waarde.split("\n")
    blok_ok = (waarde and not waarde[0].isspace() and all(r == r.rstrip() for r in regels)
               and all(r for r in regels))
    if "\n" in waarde and blok_ok:
        # Letterlijk blok: elke regel blijft precies zoals hij is.
        return [pre + " |-"] + [" " * (inspring + 2) + r for r in regels]
    if blok_ok and (len(pre) + len(waarde) > BREEDTE - 4 or waarde.startswith("{%")):
        # Gevouwen blok op één regel: dezelfde tekenreeks, leesbaarder.
        return [pre + " >-", " " * (inspring + 2) + waarde]
    if KAAL.match(waarde) and waarde.lower() not in GERESERVEERD:
        return [pre + " " + waarde]
    return [pre + " " + json.dumps(waarde, ensure_ascii=False)]


def genereer(defs):
    uit = KOP.rstrip("\n").split("\n")
    for alinea in defs.get("_comment", []):
        uit.append("#")
        uit.extend(commentaar(alinea, 0))
    uit += ["", "mqtt:"]
    ankers = set()
    for plat in PLATFORMS:
        blok = []
        for did, dev in defs["devices"].items():
            # Zonder unique_id = een verwijdermarkering voor discovery; in YAML weglaten.
            comps = [(cid, c) for cid, c in dev["components"].items()
                     if c.get("platform") == plat and c.get("unique_id")]
            if not comps:
                continue
            kop = " " + dev["device"]["name"]
            blok.append("    # " + "-" * (BREEDTE - 6 - len(kop)) + kop)
            for cid, c in comps:
                if "_comment" in c:
                    blok.extend(commentaar(c["_comment"], 4))
                eerste = True
                for k, v in c.items():
                    if k == "platform" or k.startswith("_"):
                        continue
                    regels = scalar(k, v, 6)
                    if eerste:
                        regels[0] = "    - " + regels[0].lstrip()
                        eerste = False
                    blok.extend(regels)
                if did in ankers:
                    blok.append("      device: *%s" % did)
                else:
                    ankers.add(did)
                    blok.append("      device: &%s" % did)
                    for k, v in dev["device"].items():
                        blok.extend(scalar(k, v, 8))
                blok.append("")
        if blok:
            uit.append("  %s:" % plat)
            uit.extend(blok)
    while uit[-1] == "":
        uit.pop()
    return "\n".join(uit) + "\n"


def laad(pad=BRON):
    with open(pad, encoding="utf-8") as fh:
        return json.load(fh)


def main(argv):
    tekst = genereer(laad())
    if "--stdout" in argv:
        sys.stdout.write(tekst)
        return 0
    if "--check" in argv:
        try:
            with open(DOEL, encoding="utf-8") as fh:
                huidig = fh.read()
        except OSError:
            huidig = None
        if huidig != tekst:
            print("eniro-mqtt.yaml loopt achter op discovery.json - draai scripts/gen_ha_yaml.py")
            return 1
        print("eniro-mqtt.yaml klopt met discovery.json")
        return 0
    with open(DOEL, "w", encoding="utf-8") as fh:
        fh.write(tekst)
    print("geschreven: %s" % os.path.relpath(DOEL))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
