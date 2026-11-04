# Changelog

## v1.2.0 — 2026-11-04

Eén bestand invullen en één script draaien, in plaats van tientallen commando's
met je eigen waarden.

### Nieuw

- **`install.sh` met `config.env`.** Alle eigen waarden (MQTT, WireGuard,
  hotspot, wifi-netwerken, SSH-sleutel en thuissubnet, gedrag) staan in één
  `config.env`, met uitleg per blok in `config.env.example`. Het script
  controleert dat bestand eerst (rechten 600, verplichte velden, IP-adressen,
  poorten, WireGuard-sleutels, wachtwoordlengtes) en doet dan idempotent wat
  hoofdstuk 6–9 van de v1.1-handleiding met de hand deden. Wat al klopt blijft
  staan; een dienst herstart alleen als er voor die dienst iets veranderde.
- **`--dry-run`** toont per bestand `ongewijzigd` of een diff, met
  wachtwoorden, PSK's en sleutels gemaskeerd.
- **`--export-config`** leest een bestaande dongle uit naar een ingevulde
  `config.env`, zodat een installatie van v1.0/v1.1 zonder overtypen overgaat.
- **Backups en terugdraaien.** Elk overschreven bestand krijgt eerst een
  backup `<bestand>.bak-<ts>`; `--rollback <ts>` zet een run terug,
  `--purge-backups <ts>` wist de backups (die bevatten oude wachtwoorden).
- **`engines.conf`** wordt niet meer op regelnummer aangepast maar op zijn plek
  in de YAML, met een controle vooraf dat alleen de bedoelde waarden
  veranderen, en een `test.ping` na de herstart van `salt-minion` (bij een
  fout gaat de oude terug).
- Testbaar zonder dongle: `ROOT=<map>` en `INSTALL_TEST=1`.
- **MQTT Discovery.** De sensoren verschijnen vanzelf in Home Assistant, onder
  de apparaten *Kia e-Niro* en *AutoPi dongle*, met dezelfde `unique_id`'s en
  entity-ID's als in `eniro-mqtt.yaml`. De ontvanger op de dongle publiceert
  daarvoor `homeassistant/discovery.json` als twee device-discoveryberichten
  (retained, QoS 1), maar alleen als de definitie veranderde (hash in
  `/var/lib/autopi-ingest/discovery.sha256`) of als Home Assistant opnieuw
  start (`online` op `homeassistant/status`). Samen ~9 KB. Instelbaar met
  `HA_DISCOVERY` en `HA_DISCOVERY_PREFIX` in `config.env`; standaard aan bij
  een verse installatie, uit in de code en bij `--export-config`.
- **`homeassistant/discovery.json`** is nu de enige bron van waarheid voor de
  MQTT-entiteiten, met uitleg per sensor. **`scripts/gen_ha_yaml.py`** maakt er
  `eniro-mqtt.yaml` van voor wie geen discovery wil (`--check` controleert of
  het bestand nog klopt).

### Gewijzigd

- **`INSTALLATIE.md`** opnieuw ingedeeld: §1 bevat nu ook de kant thuis
  (MQTT-login, WireGuard-client, firewall), §6 is de installatie met
  `install.sh` (inclusief overzetten van een bestaande installatie), en de
  handmatige stappen staan als naslag in bijlage A. De ontvanger op een
  server staat in bijlage B. Hoofdstukken 10–14 heten nu 7–11.
- **`scripts/wipe.sh`** wist ook `config.env`, de backups van `install.sh`
  (`*.bak-<datum>-<tijd>`), `engines.conf.orig` (met het AutoPi-token) en de
  logs van `install.sh`.
- `.gitignore`: ook `config.env*` (behalve het voorbeeld).
- **`homeassistant/eniro-mqtt.yaml`** is gegenereerd. Inhoudelijk gelijk aan
  v1.1.0 (zelfde entiteiten en opties, gecontroleerd na het inlezen), alleen
  de opmaak, de ankernamen en de commentaarkop zijn anders.
- De MQTT-login van de dongle moet met discovery ook op
  `homeassistant/device/#` publiceren en `homeassistant/status` lezen (§1.2).

### Opgelost

- **Kilometerstand en verbruik staan niet meer op 'onbekend' na een herstart
  van Home Assistant.** MQTT-sensoren herstellen hun state niet, en het
  retained bericht had tijdens laden of stilstand geen kilometerstand en geen
  verbruik; de template die de vorige waarde vasthield, had na een herstart
  niets om vast te houden. eniro-soc stuurt nu in elk BMS-record de laatst
  bekende kilometerstand en het laatst berekende verbruik mee uit zijn
  verbruiksgeschiedenis, met `odometer_bron: "opgeslagen"` (of `"cluster"` bij
  een verse meting; zichtbaar als attribuut van de SOC-sensor). Een opgeslagen
  stand wordt alleen gelezen: hij komt niet als punt in de geschiedenis, telt
  niet mee voor het verbruik en lokt geen extra verzending uit. De templates in
  `discovery.json` zijn daardoor weer eenvoudig, zonder `states(entity_id)`:
  ontbreekt het veld, dan `unknown`. Werk de dongle bij vóór of samen met Home
  Assistant; met een oude eniro-soc staan beide sensoren tijdens laden op
  `unknown`.
- **`verify.sh`** meldde "de BMS antwoordde" ook als de BMS niet antwoordde (de
  exitcode van de pijp naar `sed` telde). Nu: OK, `--` bij auto uit, of FOUT.
  `eniro_soc.py --once` geeft bij een stille bus een korte melding en exitcode 3
  in plaats van een traceback.

### Gewijzigd (logs)

- **Logs van een vorige boot staan in `/var/log/syslog`**, niet in
  `journalctl`. Op deze dongle bewaart journald van een vorige boot alleen de
  eerste ~30 seconden; rsyslog schrijft alles naar platte tekst, en dat
  overleeft een slaap volledig. INSTALLATIE 10.2 en A.7 zijn daarop aangepast.
- Het persistente journal maximaal **100 MB** (was 300 MB).
- De units hebben een **`SyslogIdentifier`**: in `/var/log/syslog` staan de
  regels als `eniro-soc[…]`, `autopi-ingest[…]` enz. in plaats van `python3[…]`,
  zodat je er op kunt zoeken. `install.sh` past de units aan en herstart de
  diensten één keer.
- **`wipe.sh`** maakt nu ook de actieve rsyslog-bestanden leeg
  (`/var/log/syslog`, `messages`, `*.log`). Daarin stond net zoveel als in het
  journal, en ze bleven bij v1.1 staan.

### Gewijzigd (Home Assistant)

- **Entiteitnamen zonder voorvoegsel**: `Accutemperatuur` in plaats van
  `e-Niro accutemperatuur`. Home Assistant zet de apparaatnaam er zelf voor,
  dus je ziet *Kia e-Niro Accutemperatuur* in plaats van *Kia e-Niro e-Niro
  accutemperatuur*. Entity-ID's veranderen niet.
- **Automatisering "AutoPi - accu kritiek" vervallen.** Het event
  `vehicle/battery/critical_level` betekent dat de dongle zichzelf uitschakelt
  om de 12V-accu te sparen; hij doet dan wat hij moet doen en wordt vanzelf weer
  wakker. Een melding waar je niets mee hoeft. Heb je hem uit v1.0/v1.1 in je
  Home Assistant staan, dan kun je hem verwijderen. Het topic gaat nog steeds
  direct door (ook in profiel Dagelijks), dus wie toch iets wil, kan er zelf op
  triggeren.

### Bijwerken vanaf v1.1

`git pull`, dan `sudo ./install.sh --export-config` en
`sudo ./install.sh --dry-run` (INSTALLATIE §6.8). Alleen de ontvanger verandert
(`/opt/autopi-ingest/autopi_ingest.py` en zijn README, herstart van
`autopi-ingest`); de rest hoort `ongewijzigd` te zijn. Discovery blijft uit
(`HA_DISCOVERY=0`), dus in Home Assistant verandert niets tot je zelf
overstapt. Laad wel `eniro-autopi.yaml` opnieuw (de accu-kritiek-melding is
eruit).

### Overstappen naar MQTT Discovery (bestaande installatie)

Optioneel, en in één keer te doen. Uitgebreid in INSTALLATIE §7.2.

1. **Maak een back-up van Home Assistant.** De overstap leunt op hoe het
   entiteitenregister werkt; dat is betrouwbaar, maar niet letterlijk zo
   gedocumenteerd.
2. **Controleer op dubbele entity-ID's.** Discovery maakt negen sensoren die in
   v1.1 niet bestonden (o.a. `sensor.e_niro_kilometerstand`,
   `sensor.e_niro_state_of_health`, `binary_sensor.e_niro_contact`). Heb je zelf
   al een entiteit met zo'n ID (bijvoorbeeld een template die de kilometerstand
   vasthoudt), dan krijgt de nieuwe er `_2` achter. Ruim zo'n eigen template
   eerst op en zet verwijzingen (ABRP, automations) daarna om.
3. **MQTT-rechten**: de login van de dongle moet ook publiceren op
   `homeassistant/device/#` en lezen op `homeassistant/status`.
4. **Haal de MQTT-sensoren uit je YAML** (het blok uit `eniro-mqtt.yaml`) en
   herlaad *Handmatig geconfigureerde MQTT-entiteiten*. Verwijder de
   entiteiten zelf **niet** in de UI: dan gaat de koppeling met je historie
   verloren.
5. **Zet `HA_DISCOVERY=1`** in `config.env` en draai `sudo ./install.sh`.
6. **Controleer**: de bestaande sensoren zijn terug met hun oude entity-ID's,
   aanpassingen en historie (Home Assistant koppelt op `unique_id`), met de
   nieuwe namen, plus de negen nieuwe. Op de apparaatpagina staat de versie van
   de dongle.

Nooit YAML en discovery tegelijk aan: dubbele `unique_id`'s, en Home Assistant
weigert de tweede.

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
