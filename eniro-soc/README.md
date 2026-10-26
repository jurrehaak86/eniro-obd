# eniro-soc

Leest de BMS van de Kia e-Niro over CAN en stuurt het resultaat naar het
ingest-endpoint — dezelfde weg die de AutoPi zelf al gebruikt.

```
e-Niro BMS ──CAN 7E4/7EC──► eniro_soc.py (op de dongle)
                              └─ gzip JSON over HTTP
                                  └─ 127.0.0.1:8181 (ontvanger op de dongle) → WireGuard → MQTT → HA
```

Standaard draait de ontvanger op de dongle zelf (zie `../ingest/` en
`../wireguard/`). Draai je de ontvanger liever op een eigen server achter
HTTPS, dan verandert hier alleen `INGEST_URL`.

## Waarom via het ingest-endpoint en niet rechtstreeks naar MQTT

Dan werkt het thuis **en** onderweg. Over 4G zit het apparaat achter de NAT van
1NCE en is de MQTT-broker van buitenaf onbereikbaar. De ontvanger op de dongle
is altijd bereikbaar (localhost) en regelt zelf de weg naar de broker, door de
WireGuard-tunnel.
De records krijgen dezelfde vorm als die van de AutoPi zelf (`@ts` + `@t`), dus
ze landen automatisch op **`autopi/obd/ev/bms`** zonder dat de ontvanger iets
van dit script hoeft te weten.

## Testen zonder auto

```bash
./eniro_soc.py --selftest
```

Decodeert de frames die de auto op 02-09-2026 daadwerkelijk stuurde en
controleert elf uitkomsten tegen onafhankelijke bronnen: dashboard-SOC,
de 12V-meting van `obd.battery`, celspanningen, buitentemperatuur en het
levenslange verbruik. **Doe dit vóór installatie** — dan weet je dat het
decodeergedeelte klopt en hoef je in de auto alleen nog de CAN-kant te
verifiëren.

Eenmalig echt uitlezen, niets versturen:

```bash
sudo ./eniro_soc.py --once --dry-run
```

## Installatie op de AutoPi

```bash
sudo install -d /opt/eniro-soc
sudo install -m 755 eniro_soc.py /opt/eniro-soc/eniro_soc.py
sudo install -m 600 eniro-soc.env.example /etc/eniro-soc.env
sudo nano /etc/eniro-soc.env          # INGEST_URL controleren (standaard de lokale ontvanger)
sudo install -m 644 eniro-soc.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now eniro-soc
sudo journalctl -u eniro-soc -f
```

## Wat het doet

Elke cyclus vraagt het `22 01 01` en `22 01 05` op, decodeert, en beslist twee
dingen:

**Hoe lang tot de volgende meting** — zie de tabel hieronder. Alles instelbaar
in de env.

**Of het verstuurd moet worden** — alleen als de SOC met ≥ 0,5 % verandert of
een statusbit omklapt, en anders elk uur een hartslag.

**Wanneer het de deur uit gaat** — meten en versturen zijn losgekoppeld. Een
record is ~313 bytes, maar een TLS-verbinding kost ruwweg 5 KB: de kosten zitten
in het *aantal* uploads, niet in de hoeveelheid data. Records wachten daarom in
een uitbox en gaan samen in één POST.

| Situatie | Meten | Uitbox | Kosten op 4G |
|---|---|---|---|
| Snelladen (DC) | 60 s | direct | ~150 KB per sessie, ~10×/jaar = **1,5 MB/jaar** |
| Rijden | 60 s | 300 s | ~40 KB/dag = **15 MB/jaar** (zonder bundelen: 82 MB) |
| AC-laden onbemeten | 60 s | direct | nul — gaat over wifi |
| AC-laden bemeten | 600 s | 300 s | verwaarloosbaar |
| Idle | 1800 s | 300 s | verwaarloosbaar |
| Bus antwoordt niet | 300 s | — | nul |

Snelladen verstuurt bewust direct: tien sessies per jaar kost 1,5 MB, en dat is
precies het moment waarop je bij de laadpaal live naar de curve staat te kijken.

**Bemeten of niet** wordt bepaald met `ip -4 route show default`: gaat het
verkeer over een interface uit `METERED_IFACES` (standaard `wwan0`), dan doet het
script zuinig. Bewust geen DNS-lookup van de endpoint — dat zou elke cyclus
verkeer kosten op precies de verbinding die we sparen. Kan de route niet bepaald
worden, dan geldt bemeten: falen doen we zuinig.

Bij afsluiten wordt de uitbox alsnog verstuurd, of anders naar de wachtrij
geschreven. Zonder dat verlies je bij elke herstart tot `UPLOAD_WAIT` seconden
aan metingen.

Mislukt het versturen (geen bereik), dan gaat het record in een lokale
wachtrij onder `/var/lib/eniro-soc/spool` en wordt het bij de volgende
geslaagde verbinding meegestuurd. Maximaal 500 bestanden, daarna vallen de
oudste eraf.

## ISO-TP

Zelf geïmplementeerd op een raw CAN-socket: verzoek, first frame lezen, flow
control `30 00 00` sturen, vervolgframes samenvoegen. Dat is een handvol regels
en het scheelt de afhankelijkheid van de `can-isotp`-kernelmodule.

De service draait als root omdat een raw CAN-socket `CAP_NET_RAW` vereist;
verder staat alles dicht wat dicht kan.

## Wat er bewust niet in zit

**Max regen en max vermogen.** Die geven op dit voertuig beide 170 kW, wat
onmogelijk is — het hoort 0-98 kW te zijn. Zie `../docs/pids-e-niro.md`.

**Celspanningen** (`220102`-`0104`). Alle 98 cellen staan op exact 3,68 V met
nul afwijking, dus per cel loggen levert nu niets op. `cell_v_deviation` uit
`220105` is de samenvatting die je wél wil zien: loopt die op, dan is er iets
aan de hand met het pak.

## Home Assistant

Zodra er data op `autopi/obd/ev/bms` staat, in `configuration.yaml` onder
`mqtt: sensor:`:

```yaml
    - name: "e-Niro SOC"
      unique_id: eniro_soc_display
      state_topic: "autopi/obd/ev/bms"
      value_template: "{{ value_json.soc_display }}"
      unit_of_measurement: "%"
      device_class: battery
      state_class: measurement

    - name: "e-Niro accuspanning"
      unique_id: eniro_hv_voltage
      state_topic: "autopi/obd/ev/bms"
      value_template: "{{ value_json.hv_voltage }}"
      unit_of_measurement: "V"
      device_class: voltage
      state_class: measurement

    - name: "e-Niro accutemperatuur"
      unique_id: eniro_batt_temp
      state_topic: "autopi/obd/ev/bms"
      value_template: "{{ value_json.batt_temp_min }}"
      unit_of_measurement: "°C"
      device_class: temperature
      state_class: measurement

    - name: "e-Niro totaal geladen"
      unique_id: eniro_cum_energy_charged
      state_topic: "autopi/obd/ev/bms"
      value_template: "{{ value_json.cum_energy_charged_kwh }}"
      unit_of_measurement: "kWh"
      device_class: energy
      state_class: total_increasing
```

Bewust **geen** `availability_topic`. Met de ontvanger op de dongle staat de
last-will op `autopi/dongle/status` en die gaat bij elke slaap op `offline`;
als availability zou dat de SOC onbeschikbaar maken zodra de auto stilstaat.
De waarden zijn retained, dus de laatste meting blijft staan. Gebruik
`autopi/dongle/status` als aparte "dongle online"-sensor, en voor "hoe oud is
deze meting" `value_json['@ts']` met `+00:00` erachter.

## Getest

**In productie sinds 15-09-2026.** Draait als systemd-dienst op de dongle; de
vijf sensoren staan in HA onder het apparaat *Kia e-Niro*.

- `--selftest` slaagt op alle elf controles (02-09-2026)
- CAN-kant met de hand geverifieerd: `cansend` + flow control geeft de volledige
  respons van 62 respectievelijk 46 bytes
- **Levende bus tijdens laden** (15-09-2026): twee uitlezingen 71 minuten uit
  elkaar, onderling consistent op drie onafhankelijke tellerparen — +3,0 % SOC
  tegen +6,0 Ah op een pakket van ~180 Ah, en +2,1 kWh bij 356 V = 5,9 Ah
- Eerste record: 313 bytes. Ruim binnen het budget van ~137 KB/dag
- De wachtrij werkte bij de eerste echte storing: het record van 13:51 ging
  alsnog mee toen de volgende post slaagde

### Twee dingen die bij de installatie naar boven kwamen

**`can0` staat DOWN na een reboot** — de AutoPi brengt hem niet omhoog, ook niet
met een levende bus. Het script doet dat nu zelf (`ensure_can_up()`), instelbaar
met `CAN_AUTO_UP`.

**(Servervariant achter Cloudflare) HTTP 403 op de standaard `Python-urllib/3.x`.** De ontvanger
kent die statuscode niet eens; de blokkade zit ervoor. Opgelost met een eigen
`User-Agent` (`eniro-soc/1.0`, instelbaar via `USER_AGENT`) — meteen ook handig
om dit verkeer te herkennen bij het schrijven van WAF-regels.

## Wijzigingen 06-10-2026

Aanleiding: tijdens het thuisladen viel de SOC na een uur stil, en de eerste
meting na het wakker worden kwam pas na ~10 minuten binnen.

- **Status voor de wakelock.** Na elke uitlezing schrijft het script
  `/var/lib/eniro-soc/status.json` (`laden`, `snelladen`, `rijden`, `idle` of
  `geen bus`). `autopi-wakelock` houdt de dongle daarop wakker; de 12V-spanning
  alleen herkent AC-laden niet (~13,0 V).
- **Eerste meting direct.** Na het wakker worden en bij elke wissel van
  toestand (idle → laden, laden → rijden) gaat het record meteen de deur uit,
  ook op 4G. Daarna weer gebundeld per `UPLOAD_WAIT`.
- **Snel opnieuw proberen na boot.** Antwoordt de BMS niet, dan de eerste
  `STARTUP_WINDOW` (180 s) elke 15 s opnieuw in plaats van na 300 s.
- **Volgorde.** Eerst de wachtrij, dan de uitbox. Lukt de wachtrij niet, dan
  sluit de uitbox achteraan aan. Ook met de auto in slaap wordt de wachtrij
  verstuurd.
- **Regeneratie is geen laden.** `laden` alleen met het contact uit, net als
  `markeer_laden()`. Op wifi wordt ook tijdens rijden direct verstuurd.

Getest met een nagebootste bus en klok: eerste post bij thuisladen na 30 s
met twee stille BMS-uitvragen (was 600 s), op 4G na 0 s (was 600 s); bij
een mislukte upload onderweg blijft de volgorde intact (was door elkaar).
Zelftest 14/14.

## Wijziging 13-10-2026 — CAN herstelt na bus-off

`ip -details link show can0` gaf op 13-10 `can state ERROR-PASSIVE restart-ms 0`.
Met `restart-ms 0` komt de controller na een bus-off nooit meer terug tot
iemand de interface down/up zet; het script ziet dan alleen nog "geen bus".
Vermoedelijk de oorzaak van de twee keer dat de SoC midden in een lading
wegviel.

`ensure_can_up()` brengt can0 nu ook opnieuw op als hij `BUS-OFF` staat of
`restart-ms 0` heeft, en zet altijd `restart-ms` (standaard 100, `CAN_RESTART_MS`).
`ERROR-PASSIVE` alleen is normaal als de auto slaapt (geen ACK op onze vragen)
en is geen reden. Getest met nagebootste `ip`-uitvoer: 5/5; zelftest 14/14.

## Wijziging 18-10-2026 — laden minimaal elke 5 minuten

- Tijdens laden gaat er minimaal elke `MAX_SILENCE_LADEN` (300 s) een record
  uit, ook als de SoC niet 0,5 % veranderde. Bij laden aan een stopcontact
  (~1,5 %/uur) kwam er anders maar één per 20 min.
- Laden op 4G: meten elke 300 s (`INTERVAL_CHARGING`, was 600) en direct
  versturen (was `UPLOAD_WAIT`). Kost weinig: de post gaat naar de ontvanger op
  de dongle, die laadrecords ook in profiel 'dagelijks' live doorzet.
- Op wifi: meten elke 60 s, versturen bij elke 0,5 % en minimaal elke 5 min.
