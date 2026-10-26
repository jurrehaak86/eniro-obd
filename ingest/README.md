# autopi-ingest

Ontvangt de uploads van de AutoPi-dongle (firmware én `eniro-soc`), pakt de
gzip uit en zet elk record op MQTT. Home Assistant leest daar vervolgens
sensoren uit.

Er zijn twee manieren om hem te draaien. **De standaard is op de dongle zelf.**

```
Standaard (dongle):
  firmware + eniro-soc ──HTTP──► 127.0.0.1:8181 autopi-ingest ──MQTT door WireGuard──► broker ──► HA

Alternatief (server):
  firmware + eniro-soc ──HTTPS──► reverse proxy / tunnel ──► autopi-ingest (server) ──MQTT──► broker ──► HA
```

| | Dongle (standaard) | Server (alternatief) |
|---|---|---|
| Waar | op de AutoPi, `127.0.0.1:8181` | eigen LXC/VM/server, achter HTTPS |
| Bestanden | `autopi-ingest-dongle.service`, `autopi-ingest-dongle.env.example` | `autopi-ingest.service`, `autopi-ingest.env.example` |
| Naar de broker | één openstaande MQTT-verbinding door de WireGuard-tunnel (`../wireguard/`) | MQTT op je eigen LAN |
| Geheim | MQTT-wachtwoord (pad is lokaal, geen geheim) | geheim ingest-pad + MQTT-wachtwoord |
| 4G-verbruik | laag: één verbinding, profielen mogelijk | hoger: een TLS-handshake per upload |
| Publiek endpoint nodig | nee | ja (tunnel of port-forward, afschermen) |

Waarom op de dongle: een upload is een paar honderd bytes, maar elke nieuwe
TLS-verbinding kost ruwweg 5 KB. Met de ontvanger op de dongle gaat alles over
één MQTT-verbinding die openblijft, en is er geen publiek endpoint meer nodig.
Is de broker onbereikbaar (tunnel weg, geen bereik), dan geeft de lokale
ontvanger een 503: `eniro-soc` spoolt en de firmware houdt zijn cache vast.
Storing = vertraging, geen verlies.

Waarom een aparte dienst en niet een HA-webhook: de dongle stuurt een
**gzip-gecomprimeerde** body en een HA-automation kan niet uitpakken. Zie `../docs/pids-e-niro.md`.

## Ontwerpkeuzes

- **Pas 200 na bevestigde MQTT-publicatie.** Faalt MQTT, dan gaat er een 503
  terug en biedt de dongle het later opnieuw aan — zijn cache doet tot 30
  retries (`cloud_cache.max_retry` in `/etc/salt/minion`). Een HA-webhook geeft
  altijd 200 en zou data stil laten verdwijnen.
- **Onleesbare payload geeft 400, niet 503.** Opnieuw aanbieden lost een corrupt
  bericht niet op; dan blijft het eeuwig in de wachtrij hangen.
- **Eén topic per `@tag`, retained.** Nieuwe tags verschijnen vanzelf zonder
  codewijziging. Het hele record gaat als JSON mee, zodat HA er met een
  `value_template` uit kan pikken wat het nodig heeft.
- **Geen geheimen in de code.** Alles via `/etc/autopi-ingest.env`.

## Installatie op de dongle (standaard)

Eerst de WireGuard-tunnel naar je broker opzetten: zie `../wireguard/README.md`.
Test dat de broker door de tunnel bereikbaar is voordat je hier verdergaat.

Op de dongle (Raspbian buster) is `python3-paho-mqtt` uit apt genoeg (1.4.0);
een venv is niet nodig.

```bash
sudo apt-get update && sudo apt-get install -y python3-paho-mqtt

sudo useradd --system --home /var/lib/autopi-ingest --shell /usr/sbin/nologin autopi-ingest
sudo install -d /opt/autopi-ingest
sudo install -m 755 autopi_ingest.py /opt/autopi-ingest/autopi_ingest.py
sudo install -m 644 README.md /opt/autopi-ingest/README.md

sudo install -m 600 -o root -g root autopi-ingest-dongle.env.example /etc/autopi-ingest.env
sudo nano /etc/autopi-ingest.env      # MQTT_HOST (= <BROKER_IP>) en MQTT_PASS invullen

sudo install -m 644 autopi-ingest-dongle.service /etc/systemd/system/autopi-ingest.service
sudo systemctl daemon-reload
sudo systemctl enable --now autopi-ingest
sudo journalctl -u autopi-ingest -n 20 --no-pager
```

Let op: de unit heet op de dongle gewoon `autopi-ingest.service`; de
`-dongle` in de bestandsnaam is alleen om de twee varianten uit elkaar te
houden. Hij start na `wg-quick@wg0`.

Belangrijke instellingen in `/etc/autopi-ingest.env` (de rest staat in het
voorbeeldbestand):

| Variabele | Waarde op de dongle | Waarom |
|---|---|---|
| `LISTEN_HOST` | `127.0.0.1` | alleen lokaal bereikbaar |
| `INGEST_PATH` | `/ingest/lokaal` | lokaal, dus geen geheim |
| `MQTT_HOST` | het IP van je broker (`<BROKER_IP>`) | door de tunnel |
| `MQTT_CLIENT_ID` | `autopi-ingest-dongle` | eigen ID, anders gooien twee ontvangers elkaar eruit |
| `MQTT_KEEPALIVE` | `300` | elke keepalive gaat over 4G |
| `STATUS_TOPIC` | `autopi/dongle/status` | last-will vuurt bij elke slaap; niet op `autopi/status` |
| `RAW_DIR` | leeg | geen ruwe payloads op de eMMC |
| `PROFIEL_TOPIC` | `autopi/config/profiel` | zie "Profielen" hieronder |

Daarna de twee afzenders naar de lokale ontvanger laten wijzen:

- `eniro-soc`: in `/etc/eniro-soc.env` `INGEST_URL=http://127.0.0.1:8181/ingest/lokaal`
  (dat is de standaard in `../eniro-soc/eniro-soc.env.example`).
- Firmware: in `/etc/salt/minion.d/engines.conf`, onder `cloud_manager.endpoint`:

```yaml
      url: http://127.0.0.1:8181/ingest/lokaal
```

Zes spaties indentatie, één kale URL. Daarna:

```bash
python -c "import yaml; yaml.safe_load(open('/etc/salt/minion.d/engines.conf')); print('YAML OK')"
sudo systemctl restart salt-minion eniro-soc
sudo salt-call --local cloud.upload
sudo journalctl -u autopi-ingest -n 5 --no-pager    # "... record(s) verwerkt"
```

`salt-minion` herstart traag, en `salt-call` kan na een ATTENTION-melding over
de sleep timer op een Enter wachten.

## Installatie op een eigen server (alternatief)

Container of VM: Debian 12, 512 MB RAM, 1 vCPU, 4 GB disk is ruim. Unprivileged.
Geef hem een vast IP of een DHCP-reservering.

```bash
apt update && apt install -y python3 python3-venv python3-pip

useradd --system --home /var/lib/autopi-ingest --shell /usr/sbin/nologin autopi
install -d -o autopi -g autopi /var/lib/autopi-ingest /var/lib/autopi-ingest/raw
install -d /opt/autopi-ingest

# autopi_ingest.py hierheen kopieren, dan:
python3 -m venv /opt/autopi-ingest/venv
/opt/autopi-ingest/venv/bin/pip install --upgrade pip paho-mqtt

install -m 600 -o root -g root autopi-ingest.env.example /etc/autopi-ingest.env
nano /etc/autopi-ingest.env        # pad en MQTT-wachtwoord invullen

install -m 644 autopi-ingest.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now autopi-ingest
systemctl status autopi-ingest --no-pager
```

Op de dongle wijzen `INGEST_URL` in `/etc/eniro-soc.env` en de `url:` in
`engines.conf` dan naar `https://autopi.voorbeeld.nl/ingest/VUL_HIER_JE_HEX_IN`
(je eigen domein en het geheime pad uit `INGEST_PATH`). Genereer het pad met
`openssl rand -hex 16`. Profielen staan op de server uit (geen `PROFIEL_TOPIC`).

### Publiek endpoint afschermen

Voeg in je tunnel of reverse proxy een route toe die het ingest-pad naar deze
container stuurt, bijvoorbeeld `autopi.voorbeeld.nl/ingest/*` →
`http://<ip-van-de-server>:8181`.

Zet het af — het is een publiek endpoint. Drie regels in Cloudflare, met de
expressies hieronder als uitgangspunt:

1. **WAF custom rule**: blokkeer alles op `/ingest/` dat niet POST of PUT is.
2. **WAF custom rule**: blokkeer alles op deze host buiten `/ingest/`.
3. **Rate limiting**: 50 requests per 10 seconden per IP op `/ingest/`. Free kan
   geen langer venster. Niet lager: `spool_flush()` stuurt één POST per
   wachtrijbestand en `SPOOL_MAX` is 500, dus na een storing komt er een burst.

Het geheime pad (32 hex = 128 bits entropie) blijft de primaire bescherming.
**Filter niet op User-Agent** — de salt cloud-returner gebruikt een andere dan
`eniro-soc/1.0` en zou er stil door breken.

## Testen zonder de dongle

Op de machine waar de ontvanger draait (pad = `INGEST_PATH`):

```bash
printf '[{"@ts":"2026-01-01T00:00:00","@tag":"vehicle/battery/discharging","@t":"event.vehicle.battery","level":80}]' \
  | gzip \
  | curl -s -w '\n%{http_code}\n' -X POST --data-binary @- \
      -H 'Content-Type: application/json' -H 'Content-Encoding: gzip' \
      http://127.0.0.1:8181/ingest/lokaal

curl -s http://127.0.0.1:8181/healthz ; echo
```

Verwacht: `{"ok":true,"records":1}` en een `200`. Een `503` betekent: broker
niet bereikbaar (op de dongle: kijk eerst naar `sudo wg show`). Meekijken op de
broker:

```bash
mosquitto_sub -h <broker> -u <gebruiker> -P '<wachtwoord>' -t 'autopi/#' -v
```

## MQTT

Plain MQTT op poort 1883, bijvoorbeeld de Mosquitto add-on van Home Assistant.
Maak bij voorkeur een **eigen login** voor de dongle (bijv. `autopi-dongle`),
zodat je hem los kunt intrekken als de dongle ooit weg is. Die login moet op
`autopi/#` kunnen publiceren en, voor de profielen, `autopi/config/profiel`
kunnen lezen.

Topics:

| Topic | Wat |
|---|---|
| `autopi/<@tag>` of `autopi/<@t met / i.p.v. .>` | elk record, retained |
| `autopi/_last` | laatste record ongeacht tag, retained |
| `autopi/obd/ev/bms` | de BMS-records van `eniro-soc` |
| `autopi/wwan/boot` | 4G-verbruik per boot (`../wwan-teller/`) |
| `autopi/dongle/status` | `online`/`offline` van de ontvanger op de dongle (last-will) |
| `autopi/status` | idem voor de servervariant |
| `autopi/config/profiel` | door HA gezet: `dagelijks` of `reis` (retained) |

## Home Assistant

De 12V-accu als voorbeeldsensor. In `configuration.yaml`:

```yaml
mqtt:
  sensor:
    - name: "AutoPi 12V accu"
      unique_id: autopi_12v_level
      state_topic: "autopi/vehicle/battery/discharging"
      value_template: "{{ value_json.level }}"
      unit_of_measurement: "%"
      device_class: battery
      state_class: measurement
  binary_sensor:
    - name: "AutoPi dongle online"
      unique_id: autopi_dongle_online
      state_topic: "autopi/dongle/status"
      payload_on: "online"
      payload_off: "offline"
      device_class: connectivity
```

Let op: dit is de **12V-accu**, niet de tractiebatterij. De SOC van de auto komt
van `eniro-soc`, en daarvoor moet de auto wakker zijn.

**Geen `availability_topic` op de sensoren** bij de dongle-variant. De
last-will op `autopi/dongle/status` gaat bij elke slaap op `offline`; als
availability zou dat alle sensoren onbeschikbaar maken zodra de auto
stilstaat. De waarden zijn retained, en `@ts` in elk record geeft de leeftijd.
Gebruik "dongle online" als gezondheidssignaal.

## Profielen: dagelijks en reis — 18-10-2026

Alleen op de dongle. Voor dagelijkse ritjes is live data overbodig, en op 4G
kost elk wakker uur data (gemeten ~0,4 MB per wakker uur zonder profielen).

- **HA**: een helper (bijv. een `input_select` met *Dagelijks* / *Reis*) en een
  automation die de keuze **retained** op `autopi/config/profiel` zet. Let op:
  een nieuwe automation publiceert pas bij een wissel of een HA-herstart.
- **Dongle**: de ontvanger abonneert zich op `PROFIEL_TOPIC` en onthoudt het
  in `/var/lib/autopi-ingest/profiel`, zodat het na een boot meteen klopt.
  Alles met "reis" erin = reis, de rest = dagelijks. Nog niets ontvangen =
  `PROFIEL_STANDAARD` (`dagelijks`).
- **reis**: alles live.
- **dagelijks op 4G**: per topic alleen het laatste record, elke
  `BUNDEL_INTERVAL` (1800 s) in één keer, en bij inslapen.
  `BUNDEL_UITZONDERINGEN` (standaard `vehicle/battery/critical_level`) en
  BMS-records van `eniro-soc` tijdens laden gaan altijd direct.
- **wifi**: altijd live; wat nog in de bundel zat gaat eerst.
- Bemeten = default route via een interface uit `METERED_IFACES`
  (`wwan0,ppp0`), gelezen uit `/proc/net/route` (de dienst mag geen netlink).
  Onbekend = bemeten.
- Op de servervariant ontbreekt `PROFIEL_TOPIC` → functie uit, gedrag ongewijzigd.

Controleren: `sudo journalctl -u autopi-ingest | grep Profiel` toont bijv.
`Profiel: dagelijks (topic autopi/config/profiel, bundel elke 1800 s op ppp0,wwan0)`.
Het bestand `/var/lib/autopi-ingest/profiel` is van de dienstgebruiker
(`sudo cat`).

Getest met nagebootste MQTT en route-tabel: 12/12. Resultaat in de praktijk:
van 2-8 MB per dag naar ~0,06 MB per dag over 4G.

## Aandachtspunten

- Elke tag krijgt zijn eigen topic. Kijk met `mosquitto_sub -t 'autopi/#' -v`
  wat er langskomt voordat je sensoren definieert — de tagnamen zijn niet
  gedocumenteerd en verschillen per firmware.
- `RAW_DIR` (servervariant) bewaart de laatste 200 ruwe payloads. Handig bij
  het ontdekken van nieuwe tags; zet het uit als je het niet nodig hebt.
- Servervariant: de dienst luistert op alle interfaces. Beperk dat in de
  firewall tot de tunnel of reverse proxy.
- `battery_critical_level` in `engines.conf` staat standaard op 12,0 V over
  300 s. De 12V-metingen die hier binnenkomen helpen om te onderbouwen of je
  die drempel wilt aanpassen. Laat het een week lopen voordat je dat doet.
- paho-mqtt 1.4 (apt, buster) kent geen `timeout` in `wait_for_publish()`;
  de code vangt dat af. Getest met 1.4.0 tegen een echte Mosquitto.

## Getest (02-09-2026)

End-to-end getest tegen een echte Mosquitto, met de payload die de dongle
werkelijk verstuurde:

| Scenario | Verwacht | Resultaat |
|---|---|---|
| gzip body **mét** `Content-Encoding: gzip` | 200, 2 records | ✔ |
| gzip body **zonder** die header (magic-byte-detectie) | 200, 2 records | ✔ |
| platte JSON zonder gzip | 200, 1 record | ✔ |
| los JSON-object in plaats van array | 200, 1 record | ✔ |
| verkeerd pad | 404 | ✔ |
| onleesbare body | 400 | ✔ |
| lege body | 400 | ✔ |
| `GET` op het ingest-pad | 404 | ✔ |
| tag met MQTT-jokers `a/+/b/#/c` | gesaneerd naar `autopi/a/b/c` | ✔ |
| **broker plat** tijdens upload | 503, geen dataverlies | ✔ |
| `/healthz` met broker plat | 503 | ✔ |
| broker terug, zelfde upload opnieuw | 200 | ✔ |
| retained `autopi/_last` en `autopi/status` | aanwezig | ✔ |

De 503-bij-broker-plat is de belangrijkste: de dongle houdt het bericht dan
vast en biedt het later opnieuw aan. Automatisch herstel na terugkomst van de
broker werkt (paho reconnect, ~5 s).

