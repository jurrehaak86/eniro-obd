#!/usr/bin/env python3
"""
autopi-wakelock — houdt de dongle wakker zolang de auto rijdt of laadt.

Het probleem, gemeten op 19-09-2026: de dongle hibernateert midden in een
laadsessie. Gaten van 185 en 679 minuten waarin de SOC met 7 respectievelijk 9
procent steeg zonder dat er één meting binnenkwam.

Herzien 06-10-2026. Twee fouten in de eerste versie:

1. **Hij herkende laden niet.** De versie van 19-09 keek alleen naar de
   12V-spanning (>= 13,5 V). Tijdens AC-laden houdt de e-Niro de 12V op
   ~13,0 V (gemeten 05-10-2026), dus de wakelock liet los zodra de lading
   begon. Nu telt ook de status die eniro-soc uit de BMS haalt: zegt die
   'laden', 'snelladen' of 'rijden', en is hij vers, dan blijft de dongle wakker.

2. **Hij zag de timers niet.** Elke boot zet de reactor in engines.conf een
   sleep timer van een uur (`inactivity_fallback`, `inactivity_after_sleep`).
   power.sleep_timer geeft die terug als `_sleep_timer/<naam>`, en het oude
   filter gooide alles weg wat met `_` begint. Uitkomst: elke vijf minuten
   "geen sleep timer actief", en na een uur toch uit, ook midden in een rit.
   Nu telt elke sleutel, en wist het script altijd zolang het wakker moet
   houden. `salt-call --local` werkt wél op de live timers (getest 06-10:
   toevoegen via --local, zichtbaar via de API op :9000 en `autopi`).

**Het vangnet blijft staan.** `battery_critical_level` (12,0 V over 300 s) is
geen sleep timer maar een aparte reactor, en die hibernateert alsnog als de
spanning wegzakt. Dit script raakt hem niet aan.

Extra rem: na MAX_WAKE_HOURS aaneengesloten wakker houden stopt het script
daarmee.

    sudo ./autopi_wakelock.py            # eenmalig controleren
    sudo ./autopi_wakelock.py --dry-run  # alleen tonen wat het zou doen
"""

import argparse
import json
import os
import subprocess
import sys
import time

WAKE_VOLTAGE = float(os.environ.get("WAKE_VOLTAGE", "13.5"))
# Rem voor wakker houden op alleen de 12V-spanning: als die meting ooit blijft
# hangen, niet eindeloos wakker blijven.
MAX_WAKE_HOURS = float(os.environ.get("MAX_WAKE_HOURS", "16"))
# Laden volgens de BMS mag veel langer: aan een gewoon stopcontact (~1,5 %/uur,
# gemeten 15/16-10-2026) duurt een lading ruim een etmaal, en de 16 uur hierboven
# kapte die af. Tijdens laden houdt de auto zelf de 12V op peil.
MAX_WAKE_HOURS_LADEN = float(os.environ.get("MAX_WAKE_HOURS_LADEN", "48"))
STATE_DIR = os.environ.get("WAKELOCK_STATE_DIR", "/var/lib/autopi-wakelock")
SINDS = os.path.join(STATE_DIR, "wakker-sinds")

# Geschreven door eniro-soc na elke BMS-uitlezing.
STATUS_BESTAND = os.environ.get("STATUS_BESTAND", "/var/lib/eniro-soc/status.json")
# Bij laden op 4G meet eniro-soc elke 600 s; ruim daarboven blijven.
STATUS_MAX_AGE = int(os.environ.get("STATUS_MAX_AGE", "900"))
ACTIEF = {"laden", "snelladen", "rijden"}

# Bij het loslaten zetten we zelf een sleep timer terug. We hebben die van de
# reactor (het uur na boot) gewist, en anders slaapt de dongle nooit meer:
# de reactor zet hem pas bij de volgende boot opnieuw. Parameters zoals de
# engine_stopped-reactor in engines.conf: 15 min wakker, daarna 6 uur slapen.
RELEASE_PERIOD = int(os.environ.get("RELEASE_PERIOD", "900"))
RELEASE_INTERVAL = int(os.environ.get("RELEASE_INTERVAL", "21600"))


def salt(*args, timeout=30):
    """salt-call --local met JSON-uitvoer. Geeft het 'local'-veld of None."""
    try:
        res = subprocess.run(
            ["salt-call", "--local", "--out=json"] + list(args),
            # Bij toevoegen/wissen wacht salt-call op een Enter na de
            # ATTENTION-melding. Lege stdin, dan gaat hij meteen door.
            stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        print("salt-call mislukt: %s" % exc, file=sys.stderr)
        return None
    if res.returncode != 0:
        return None
    # salt drukt soms een waarschuwing vóór de JSON; pak vanaf de eerste accolade
    tekst = res.stdout
    i = tekst.find("{")
    if i < 0:
        return None
    try:
        return json.loads(tekst[i:]).get("local")
    except ValueError:
        return None


def spanning():
    d = salt("obd.battery")
    if not isinstance(d, dict):
        return None
    for sleutel in ("voltage", "value"):
        if sleutel in d:
            try:
                return float(d[sleutel])
            except (TypeError, ValueError):
                pass
    return None


def bms_status():
    """(toestand, leeftijd in s) uit eniro-soc, of (None, None)."""
    try:
        with open(STATUS_BESTAND) as fh:
            d = json.load(fh)
        return d.get("toestand"), time.time() - float(d["ts"])
    except (OSError, ValueError, KeyError, TypeError):
        return None, None


def timers():
    """Ruwe uitvoer van power.sleep_timer, alleen voor het log."""
    d = salt("power.sleep_timer")
    if isinstance(d, dict):
        return sorted(d)
    return d


def wis_timers():
    return salt("power.sleep_timer", "clear=*") is not None


def zet_timer_terug():
    return salt("power.sleep_timer", "add=wakelock_released",
                "period=%d" % RELEASE_PERIOD, "interval=%d" % RELEASE_INTERVAL,
                "reason=wakelock_released") is not None


def laat_los(reden, dry_run):
    """Stopt met wakker houden en zorgt dat de dongle weer kan inslapen."""
    if dry_run:
        print("%s — zou loslaten en een timer van %d s zetten" % (reden, RELEASE_PERIOD))
        return 0
    if zet_timer_terug():
        los()
        print("%s — wakeslot losgelaten, slaapt over %d s" % (reden, RELEASE_PERIOD))
        return 0
    # Timer niet gezet: SINDS laten staan, dan proberen we het over vijf minuten opnieuw.
    print("%s — loslaten: timer terugzetten mislukt" % reden, file=sys.stderr)
    return 1


def wakker_sinds():
    try:
        return os.path.getmtime(SINDS)
    except OSError:
        return None


def stip():
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        if not os.path.exists(SINDS):
            open(SINDS, "w").close()
    except OSError:
        pass


def los():
    for pad in (SINDS, SINDS + ".max"):
        try:
            os.remove(pad)
        except OSError:
            pass


def beslis(v, toestand, leeftijd):
    """Geeft (wakker houden?, reden). Los van salt, zodat het te testen is."""
    if toestand in ACTIEF and leeftijd is not None and 0 <= leeftijd <= STATUS_MAX_AGE:
        return True, "BMS: %s (%ds oud)" % (toestand, leeftijd)
    if v is not None and v >= WAKE_VOLTAGE:
        return True, "%.2f V" % v
    if v is None:
        # Geen spanning en geen verse BMS-status: laten slapen is de veilige kant.
        return False, "spanning onbekend, BMS: %s" % (toestand or "geen status")
    return False, "%.2f V, BMS: %s" % (v, toestand or "geen status")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="alleen tonen, niets wissen")
    args = ap.parse_args()

    v = spanning()
    toestand, leeftijd = bms_status()
    houden, reden = beslis(v, toestand, leeftijd)

    if not houden:
        if wakker_sinds():
            return laat_los(reden, args.dry_run)
        print("%s — mag slapen" % reden)
        return 0

    laadt = toestand in ("laden", "snelladen") and reden.startswith("BMS")
    grens = MAX_WAKE_HOURS_LADEN if laadt else MAX_WAKE_HOURS
    sinds = wakker_sinds()
    if sinds and (time.time() - sinds) > grens * 3600:
        # Één keer de timer terugzetten; daarna niets meer doen tot het
        # wakeslot via de normale weg loskomt.
        uren = (time.time() - sinds) / 3600
        if not os.path.exists(SINDS + ".max"):
            if not args.dry_run and zet_timer_terug():
                open(SINDS + ".max", "w").close()
            print("%s, maar al %.1f uur wakker gehouden — timer teruggezet"
                  % (reden, uren), file=sys.stderr)
        return 0

    gezien = timers()
    if args.dry_run:
        print("%s — zou timers wissen (uitgelezen: %s)" % (reden, gezien))
        return 0

    # Altijd wissen, ook als het uitlezen niets oplevert: dat uitlezen gaf
    # tot 06-10 steeds "niets" terwijl de uurtimer er wel stond.
    if wis_timers():
        stip()
        print("%s — sleep timers gewist (uitgelezen: %s)" % (reden, gezien))
        return 0
    print("%s — wissen mislukt" % reden, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
