# autopi-wakelock

Houdt de dongle wakker zolang de auto rijdt of laadt. Elke vijf minuten een controle.

> **Herzien 06-10-2026** — zie onderaan. De eerste versie werkte in de praktijk
> niet: laden werd niet herkend en de uurtimer van de reactor bleef staan.

## Het probleem

Gemeten op 19-09-2026 aan de BMS-records van één laadsessie:

| Van | Tot | Duur | SOC erbij |
|---|---|---|---|
| 09:30 | 12:35 | 185 min | **+7,0%** |
| 13:31 | 00:50 | 679 min | **+9,0%** |

Buiten die gaten kwam er netjes elke vier tot zeven minuten een meting binnen
(de 0,5%-verzenddrempel). Binnen de gaten stéég de SOC, dus er werd geladen en
de bus leefde — de dongle sliep. **Het grootste deel van elke laadcurve ontbrak.**

Voor de capaciteitsmeting maakte dat niet uit, want die tellers zijn cumulatief.
Voor de laadcurve wel.

## De redenering

De slaapstand bestaat om de **12V-startaccu** te sparen. Tijdens rijden en laden
draait de DC-DC-omvormer en staat de boordspanning op ~14,6 V: dan wordt die accu
juist bijgeladen en is er geen reden om te slapen.

Dus: zolang de spanning boven `WAKE_VOLTAGE` (13,5 V) staat, wist het script de
sleep timers. Zakt de spanning, dan laat het los en valt de dongle gewoon in
slaap.

## Waarom dit de accu niet leeg kan trekken

Het vangnet blijft staan. `battery_critical_level` (12,0 V over 300 s in
`engines.conf` onder `obd_manager`) is **geen sleep timer** maar een aparte
reactor, en die hibernateert alsnog als de spanning wegzakt. Dit script raakt hem
niet aan.

Extra rem: na `MAX_WAKE_HOURS` (standaard 16, zie hieronder) aaneengesloten wakker houden stopt
het script daarmee, voor het geval de spanningsmeting ooit blijft hangen.

En bij twijfel doet het niets: kan de spanning niet gelezen worden, dan mag de
dongle slapen. Falen doen we aan de veilige kant.

## Waarom elke vijf minuten

De kortste sleep timer in `engines.conf` is 15 minuten (`engine_stopped`). Tien
zou te krap zijn.

Het script wist alleen als er daadwerkelijk een timer staat, zodat er niet elke
vijf minuten een `sleep_timer/cleared`-event de keten in gaat.

## Installatie

```bash
sudo install -d /opt/autopi-wakelock
sudo install -m 755 autopi_wakelock.py /opt/autopi-wakelock/autopi_wakelock.py
sudo install -m 644 README.md /opt/autopi-wakelock/README.md
sudo install -m 644 autopi-wakelock.service /etc/systemd/system/
sudo install -m 644 autopi-wakelock.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo python3 /opt/autopi-wakelock/autopi_wakelock.py --dry-run
sudo systemctl enable --now autopi-wakelock.timer
```

## Kosten

Hij blijft wakker tijdens een laadsessie in plaats van er twintig minuten van te
zien. Thuis op wifi kost dat niets. Onderweg bij snelladen is het tien sessies
per jaar; `eniro-soc` stuurt daar sowieso al elke minuut.

## Herzien 06-10-2026

**Wat er mis was.** Gemeten aan de journals van 04 t/m 06-10:

1. Elke boot zet de reactor in `engines.conf` een sleep timer van een uur
   (`inactivity_fallback` of `inactivity_after_sleep`, `period: 3600`). Alle
   boots duurden 61-63 minuten, ook midden in een rit. Het script meldde elke
   keer "geen sleep timer actief" en wiste dus niets. Oorzaak: de timers heten
   `_sleep_timer/<naam>` en het filter `not k.startswith("_")` gooide ze
   allemaal weg. `salt-call --local` zelf werkt wél op de live timers
   (geverifieerd 06-10 tegen de API op :9000 en `autopi power.sleep_timer`).
2. Tijdens AC-laden staat de 12V op ~13,0 V. Onder `WAKE_VOLTAGE` 13,5, dus
   ook als het wissen had gewerkt, had het laden niet geholpen.

**Wat het nu doet:**

| Situatie | Besluit |
|---|---|
| eniro-soc-status `laden`/`snelladen`/`rijden`, jonger dan 15 min | wakker houden |
| 12V >= 13,5 V | wakker houden |
| anders, en we hielden vast | loslaten **en** een timer van 15 min zetten (`wakelock_released`, daarna 6 uur slapen) |
| anders | niets |

- Wakker houden = **altijd** `power.sleep_timer clear=*`, wat het uitlezen ook
  zegt. Het uitlezen staat alleen nog in het log, om fout 1 te begrijpen.
- Bij loslaten zelf een timer terugzetten is nodig: wij hebben de uurtimer van
  de reactor gewist, en die komt pas bij de volgende boot terug. Zonder dit
  slaapt de dongle na een lading nooit meer.
- `MAX_WAKE_HOURS` naar 16 (volle AC-lading ~9-10 uur). Daarna één keer de
  timer terugzetten en stoppen met wissen.
- Getest met nagebootste salt-aanroepen: 8/8 scenario's.

## Wijziging 18-10-2026 — langzaam laden

Op 15/16-10 laadde de auto op een vakantieadres aan een gewoon stopcontact,
~1,5 % per uur. De wakelock hield 16,5 uur vast en liet toen los op `MAX_WAKE_HOURS` (16):
laatste SoC 56,5 % om 09:56, dongle uit om 10:10, en de auto laadde door tot
~72 %. Nu geldt de 16-uursgrens alleen voor vasthouden op spanning; vasthouden
op BMS-status `laden`/`snelladen` mag tot `MAX_WAKE_HOURS_LADEN` (48). Tests 10/10.
