# Changelog

## v1.1.0 — 2026-10-26

Betrouwbaarheid rond het einde van laden en rare antwoorden van de auto. Dank
aan een gebruiker voor de review.

### Opgelost

- **Einde van laden kwam niet door.** Stopte een AC-lading zonder dat de SOC of
  de statusbits veranderden (bijvoorbeeld van −2 A naar 0 A), dan verstuurde
  `eniro-soc` dat niet. Begin én einde van laden gaan nu altijd, en direct, weg.
  Het script gebruikt daarvoor overal één definitie van "laden": snelladen, of
  meer dan 1 A de accu in met het contact uit.
- **Ontvanger hield het einde van laden vast op 4G.** In profiel *dagelijks*
  bleef het eerste record na het laden tot 30 minuten in de bundel. Dat gaat nu
  live door. Ook kan een ouder, gebundeld record niet meer ná een nieuwer
  record gepubliceerd worden (de SOC in Home Assistant sprong dan terug).
- **"e-Niro laadt" telde remmen als laden.** De sensor gebruikt nu dezelfde
  definitie als het script, dus met het contact uit.
- **"e-Niro laadt" en "e-Niro snelladen" bleven aan staan** als de auto stil
  viel tijdens het laden. Ze worden nu onbeschikbaar na 15 minuten zonder
  meting (tijdens laden komt er minstens elke 5 minuten een).
- **Crash op een onverwacht antwoord van de auto.** Een leeg CAN-frame of een
  weigering van de BMS (negatief UDS-antwoord) liet `eniro-soc` crashen; de
  metingen die nog wachtten gingen dan verloren. Zulke antwoorden worden nu
  herkend en behandeld als een stille bus, het antwoord wordt gecontroleerd
  vóór het decoderen, en een onverwachte fout laat de dienst niet meer vallen.
- `--selftest` van `eniro-soc` test nu ook deze gevallen; de ontvanger heeft
  een eigen `--selftest` (zonder broker).

### Bijwerken

Vervang op de dongle `eniro_soc.py` en `autopi_ingest.py` en herstart beide
diensten. Laad in Home Assistant ook `homeassistant/eniro-mqtt.yaml` opnieuw
(kopiëren en *MQTT-entiteiten* herladen, of HA herstarten). Geen nieuwe
instellingen in de `.env`-bestanden.

### Bekend, nog niet opgelost

- Het versturen van de wachtrij heeft geen tijdslimiet; na lang offline zijn kan
  één ronde lang duren.
- Het wegschrijven naar de wachtrij is niet atomair en meldt niet of het
  gelukt is; bij een schrijffout kunnen metingen verloren gaan.
- Na een meetgat met meer dan 1 kWh regeneratie wordt die stap als laden
  gezien, waardoor het verbruik over 100 km iets te hoog uitvalt.

## v1.0.0 — 2026-10-23

Eerste publieke versie. Getest in een Kia e-Niro uit 2019.

- `eniro-soc`: SOC, pakspanning en -stroom, celtemperaturen, SOH, energie in/uit,
  kilometerstand en verbruik over 100 km uit de BMS (7E4) en de cluster (7C6).
- Ontvanger (`ingest/`) op de dongle zelf, met één MQTT-verbinding door een
  WireGuard-split-tunnel; profielen *dagelijks* en *reis* voor zuinig 4G-gebruik
  (~0,06 MB/dag gemeten). Ontvanger op een eigen server via HTTPS blijft als
  alternatief.
- `wakelock/`: houdt de dongle wakker tijdens laden en rijden.
- `wwan-teller/`, `diag/`: 4G-verbruik en verbindingsdiagnostiek.
- Home Assistant-packages: sensoren, profiel-helper, meldingen, actieradius, ABRP.
