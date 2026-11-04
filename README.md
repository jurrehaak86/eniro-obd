# eniro-obd

De accustatus van een Kia e-Niro (2019, eerste generatie) uitlezen over de
OBD-poort, zonder Kia Connect en zonder abonnement. De data gaat naar je eigen
Home Assistant.

> **English summary.** Reads the traction battery of a first-generation Kia
> e-Niro over OBD-II (HKMC UDS, 7E4/7EC) from an AutoPi dongle and publishes
> SOC, pack voltage, current, cell temperatures, cumulative energy and the
> odometer to Home Assistant over MQTT, through a WireGuard split tunnel.
> Documentation and code comments are in Dutch.

---

## Waarom

De connected diensten van de eerste generatie e-Niro houden op. Daarmee
verdwijnt het remote uitlezen van accupercentage, laadstatus en locatie. De auto
weet het allemaal nog prima — je moet het er alleen zelf uit halen.

## Wat het doet

| | |
|---|---|
| **Wel** | SOC (BMS en display), pakspanning en -stroom, celtemperaturen, min/max celspanning, State of Health, cumulatieve energie in en uit, laden ja/nee, snelladen ja/nee, contact aan/uit, kilometerstand, lopend gemiddeld verbruik over 100 km, GPS-positie |
| **Niet** | openen, sluiten, voorverwarmen, claxonneren. Dit leest alleen. |

## Wat het niet kan, en waarom

- **Met het contact uit antwoordt er niemand.** De diagnostische CAN-bus achter
  de OBD-poort slaapt dan, en `can0` staat DOWN. De auto moet in ready staan of
  aan de lader hangen.
- **De kilometerstand alleen tijdens rijden.** Die komt uit de
  instrumentencluster, en die is stil zolang er niets weer te geven valt — dus
  ook tijdens het laden. Daartussen stuurt eniro-soc de laatst bekende stand
  mee (`odometer_bron: opgeslagen`).
- **De bus zwijgt tot je hem aanspreekt.** Een `candump` die niets laat zien
  bewijst niet dat er niets is. Bij HKMC hangt de poort achter een gateway
  zonder broadcast.

## Wat je nodig hebt

- Een **AutoPi**-dongle (Raspberry Pi CM4 met CAN-controller en 4G-modem), of
  gelijkwaardige hardware met SocketCAN op de OBD-poort
- Een **data-SIM**. Gemeten verbruik over 4G met deze opzet: **0,02–0,11 MB per
  dag, gemiddeld ~0,06 MB**. Een 1NCE IoT-SIM (500 MB voor tien jaar, eenmalig
  zo'n tientje) is daarmee ruim voldoende
- **Home Assistant** met een MQTT-broker
- Een **WireGuard-server** thuis (op je router of los) en een firewall die de
  dongle alleen bij de broker laat. Wie geen VPN wil, kan in plaats daarvan een
  ontvanger op een eigen server achter HTTPS draaien — zie `ingest/README.md`

## Hoe het in elkaar zit

```
e-Niro BMS ──CAN 7E4/7EC──► eniro_soc.py ─┐
AutoPi-firmware (GPS, 12V) ───────────────┤  HTTP, lokaal op de dongle
wwan-teller, autopi-diag ─────────────────┘
                                          ▼
                         autopi_ingest.py (127.0.0.1:8181, op de dongle)
                                          │  één MQTT-verbinding die openblijft
                                          ▼
                         WireGuard, alleen <BROKER_IP>/32 ──► broker thuis ──► Home Assistant
```

Alles op de dongle post naar een kleine ontvanger op de dongle zelf. Die zet de
records op MQTT, over één verbinding die openblijft, door een WireGuard-tunnel
naar huis. Over 4G zit de dongle achter carrier-NAT; omdat de tunnel bij de
dongle begint, is dat geen probleem. Het is een split tunnel: alleen het
broker-adres gaat erdoor, en de firewall thuis laat de dongle nergens anders
toe.

Waarom niet rechtstreeks: de AutoPi-firmware stuurt gzip over HTTP, geen MQTT.
En waarom niet naar een HTTPS-endpoint op internet, zoals eerst: elke upload is
dan een nieuwe TLS-verbinding van ~5 KB voor een paar honderd bytes aan
metingen. Met de ontvanger op de dongle en de profielen (dagelijks: op 4G elke
30 minuten de laatste stand; reis: alles live; laden altijd minimaal elke
5 minuten) ging het verbruik van 2-8 MB naar ~0,06 MB per dag.

Is de tunnel of de broker weg, dan geeft de ontvanger een 503: `eniro-soc`
spoolt naar schijf en de firmware houdt zijn cache vast. Storing is vertraging,
geen verlies.

## De onderdelen

| Map | Wat |
|---|---|
| `eniro-soc/` | het hoofdscript: leest de BMS uit en verstuurt |
| `ingest/` | de ontvanger: pakt uit en zet op MQTT; standaard op de dongle, als alternatief op een server |
| `wireguard/` | split tunnel naar alleen de broker, met `wg-reresolve` voor een dynamisch thuis-IP |
| `wakelock/` | houdt de dongle wakker zolang de auto rijdt of laadt |
| `wwan-teller/` | legt per boot het 4G-verbruik vast, en de open verbindingen bij een piek |
| `diag/` | 4G- en routediagnostiek, buffert op 4G en dumpt op wifi (optioneel) |
| `docs/pids-e-niro.md` | alle gedecodeerde PID's met meetwaarden en formules |
| `install.sh` + `config.env.example` | richt de dongle in vanuit één ingevuld bestand; met `--dry-run`, `--export-config` en `--rollback` |
| `scripts/install_helper.py` | hulpje van `install.sh`: valideert de config en bouwt de bestanden op |
| `scripts/verify.sh` | controleert in een halve minuut of de keten werkt, inclusief tunnel en broker |
| `scripts/wipe.sh` | haalt persoonlijke sporen van een dongle voordat hij van eigenaar wisselt |
| `homeassistant/discovery.json` | alle MQTT-sensoren en de locatie-tracker, onder twee apparaten; de ontvanger publiceert dit als MQTT Discovery, zodat ze vanzelf in Home Assistant verschijnen |
| `homeassistant/eniro-mqtt.yaml` | dezelfde sensoren als YAML-package, voor wie geen discovery wil; gegenereerd met `scripts/gen_ha_yaml.py` |
| `homeassistant/eniro-autopi.yaml` | helpers (profiel, accucapaciteit, laatst bekend verbruik), actieradius en automatiseringen (profiel naar de dongle, verbruik bewaren, meldingen), als package |
| `homeassistant/abrp.yaml` + `secrets.example.yaml` | live telemetrie naar ABRP, volledig van de dongle |
| `INSTALLATIE.md` | de volledige handleiding |

Elke map heeft een eigen README met de installatie en de redenering.

## Installatie

De volledige handleiding staat in **[`INSTALLATIE.md`](INSTALLATIE.md)**: van een
kale dongle tot data in Home Assistant, inclusief de valkuilen. Reken op een
middag. In het kort:

1. **Vooraf regelen** (§1): data-SIM activeren, AutoPi-image downloaden, en
   thuis: een eigen MQTT-login voor de dongle, een WireGuard-client en een
   firewallregel die de dongle alleen bij de broker laat.
2. **Flashen en beveiligen** (§3–4): image op de eMMC, wachtwoord, eigen
   hotspot-wachtwoord.
3. **4G** (§5): APN en modem, eenmalig met de hand.
4. **Installeren** (§6): de repository op de dongle, één bestand invullen, één
   script draaien:

   ```bash
   scp -r eniro-obd pi@<DONGLE_IP>:/home/pi/    # of git clone / de release-zip
   ssh pi@<DONGLE_IP>
   cd ~/eniro-obd
   cp config.env.example config.env && chmod 600 config.env
   nano config.env                 # MQTT, WireGuard, hotspot, wifi, SSH-sleutel
   sudo ./install.sh --dry-run     # laat zien wat er zou veranderen
   sudo ./install.sh
   ```

   `install.sh` richt de ontvanger, WireGuard, wifi, de hotspot,
   `engines.conf`, SSH, het journal en de diensten in, maakt van elk bestand
   dat het overschrijft een backup, en sluit af met `scripts/verify.sh`.
   Wat het precies doet, stap voor stap, staat in bijlage A van de
   handleiding.
5. **Home Assistant** (§7): de sensoren verschijnen vanzelf (MQTT Discovery,
   `HA_DISCOVERY=1`); `eniro-autopi.yaml` en eventueel `abrp.yaml` laad je als
   package.
6. **Inbouwen en controleren** (§8–9).

Snel kijken of de decodering werkt, zonder auto:

```bash
git clone https://github.com/jurrehaak86/eniro-obd.git
cd eniro-obd/eniro-soc
python3 eniro_soc.py --selftest
```

### Bestaande installatie (v1.0/v1.1) overzetten

Met de hand ingericht volgens een eerdere versie? Dan leest `install.sh` je
huidige instellingen uit, zodat je niets hoeft over te typen:

```bash
cd ~/eniro-obd && git pull           # of de nieuwe release-zip
sudo ./install.sh --export-config    # schrijft ./config.env (600)
nano config.env                      # controleren
sudo ./install.sh --dry-run          # verwacht: alleen de ontvanger bijgewerkt
```

Zie §6.8 van de handleiding. MQTT Discovery blijft daarbij uit
(`HA_DISCOVERY=0`), want je hebt de sensoren al in YAML. Overstappen kan
daarna, in de volgorde van §7.2: eerst de sensoren uit YAML halen, dan
discovery aanzetten. Home Assistant koppelt de entiteiten op `unique_id`, dus
entity-ID's en geschiedenis blijven.

### Bijwerken

Wat er per versie veranderd is staat in [`CHANGELOG.md`](CHANGELOG.md). Een
nieuwe versie zet je zo op de dongle:

```bash
cd ~/eniro-obd && git pull          # of download de release-zip
sudo ./install.sh --dry-run         # welke scripts en instellingen veranderen
sudo ./install.sh                   # installeert en herstart alleen wat veranderde
```

Lees bij elke versie wel de changelog: soms verandert er ook iets in de Home
Assistant-bestanden.

### Vragen en fouten

Issues en pull requests zijn welkom. Dit is een hobbyproject: reacties zijn
een gunst, geen dienst.

## Dingen die tijd kostten

Voor wie hetzelfde pad loopt — dit is waar het misging:

1. **`can0` komt na een reboot niet vanzelf omhoog**, ook niet met een levende
   bus. Een raw socket geeft dan `OSError 100 (ENETDOWN)`, wat er precies
   uitziet als een stille bus. Het script brengt de interface zelf omhoog en
   leest de IFF_UP-vlag uit `/sys/class/net/<if>/flags` — níét `operstate`, want
   CAN-interfaces melden daar `unknown` terwijl ze prima werken.
2. **En als hij omhoog is: `restart-ms 0`.** Dan komt de CAN-controller na een
   bus-off nooit meer terug, en valt de data midden in een lading weg tot de
   volgende boot. Het script zet `restart-ms 100` en brengt een interface in
   `BUS-OFF` opnieuw op.
3. **AutoPi's eigen `obd.*`-laag werkt niet op dit board** (`Not supported by
   SocketCAN interface`). Die hoort bij de seriële STN-chip van de
   retail-AutoPi. Gebruik `can-utils`.
4. **Elke boot duurde 61 minuten.** Een reactor in de firmware zet bij elke boot
   een sleep timer van een uur, die alleen wordt gewist door een
   motor-draait-event dat deze auto nooit stuurt. Tijdens laden ging de dongle
   dus gewoon slapen. `wakelock/` wist de timers zolang de BMS laden of rijden
   meldt, en zet bij loslaten zelf een korte timer terug. Op de 12V-spanning
   alleen gaat het niet: tijdens AC-laden staat die op ~13,0 V, net als in rust.
   De eerste versie keek daarnaar, en filterde de timers bovendien weg op hun
   naam; hij deed weken niets.
5. **Het journal haalde de eMMC niet.** Slapen is een volledige shutdown. Met
   alleen `Storage=persistent` bleven van elke boot ~30 seconden over: de Smart
   Power Manager haalt de stroom eraf voordat journald schrijft. Pas met
   `SyncIntervalSec=30s` is achteraf debuggen mogelijk.
6. **(Servervariant) Cloudflare geeft 403 op `Python-urllib/3.x`.** Zet een
   eigen `User-Agent`.

## Openstaand

- Verbruik tegen snelheid modelleren. Twee keer geprobeerd, twee keer een
  geloofwaardig maar fout antwoord opgeleverd; zie `docs/pids-e-niro.md`.

Rechtstreeks naar MQTT publiceren stond hier eerder als grootste
vereenvoudiging. Dat is in feite gerealiseerd: de ontvanger draait op de dongle
en praat zelf MQTT door de tunnel. Een publiek endpoint is niet meer nodig.

## Hoe dit gemaakt is

Dit is gebouwd samen met Claude (Anthropic). Het decoderen van de PID's, de
datastrategie en het debuggen van het modem zijn in die samenwerking ontstaan.
De code is getest in een echte auto; de meetwaarden in de documentatie zijn
echte metingen, geen voorbeelden.

## Waarschuwing

Dit praat met de diagnostische bus van een auto. Het stelt alleen leesvragen en
schrijft niets, maar je bent zelf verantwoordelijk voor wat je in je OBD-poort
steekt en voor wat er daarna gebeurt — met je auto, je garantie en je
verzekering. Geen enkele garantie, zie `LICENSE`.

Niet gelieerd aan Kia, Hyundai of AutoPi.
