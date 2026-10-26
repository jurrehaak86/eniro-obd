# autopi-diag

Stuurt elke tien minuten de verbindingsdiagnostiek van de dongle naar de ingest.
Landt op **`autopi/device/diag`**.

## Waarom

De GPS-kwaliteit zit al in elk `track.pos`-record (`nsat`, `nsat_glonass`,
`fix`). Van 4G wisten we achteraf niets: zonder RSRP-geschiedenis kun je een gat
in de data niet onderscheiden van slechte ontvangst, en kun je niet zien of de
montageplek over een paar maanden nog doet wat hij bij de meting deed.

## Wat het verstuurt

```json
{
  "@ts": "2026-09-19T12:00:00.000000", "@t": "device.diag",
  "rssi": -70, "rsrq": -12, "rsrp": -98, "snr": 6.4,
  "rat": "lte", "operator": "Odido", "registered": true,
  "ps_attached": true, "roaming": true,
  "cell_id": 5839728, "tac": 299,
  "default_iface": "wlan0", "metered": false,
  "uptime_s": 6866
}
```

Ontbrekende velden worden weggelaten in plaats van op nul gezet — slaapt het
modem, dan komt er gewoon een korter record.

## Meten op 4G, versturen over wifi

Onderweg schrijft hij elke meting naar een lokale buffer en stuurt **niets**.
Zodra de dongle op een onbemeten verbinding zit gaat de hele buffer in één keer
mee, samen met het verse sample. Kosten voor de databundel: nul.

Dat is bovendien beter dan niet bufferen. Het script draait ook zónder
verbinding door en legt vast dát er geen signaal was — `registered: false`, geen
`rsrp`. Een storing komt dus mét meetwaarden in de reeks te staan in plaats van
als een gat waarvan je de oorzaak moet raden.

Buffer: `/var/lib/autopi-diag/buffer.jsonl`, maximaal `BUFFER_MAX` records
(standaard 2000 — bij tien minuten ruim twee weken onafgebroken rijden). Bij een
mislukte verzending blijft alles staan en gaat het de volgende keer opnieuw mee.

```bash
sudo python3 /opt/autopi-diag/autopi_diag.py --status
```

## Thuis hoogstens eens per uur

Elke tien minuten de ontvangst meten op de eigen oprit maakt de grafiek vooral
vol; wat je wil weten gebeurt onderweg. Op een onbemeten verbinding wordt daarom
hoogstens eens per uur een **nieuw** sample genomen.

Let op het onderscheid: die drempel geldt voor het *meten*, niet voor het
*dumpen*. Kom je thuis met een volle buffer, dan gaat die meteen mee.

Instelbaar met `UNMETERED_EVERY` (seconden, 0 = altijd meten). Forceren met
`--altijd`.

## Installatie

```bash
sudo install -d /opt/autopi-diag
sudo install -m 755 autopi_diag.py /opt/autopi-diag/autopi_diag.py
sudo install -m 644 README.md /opt/autopi-diag/README.md
sudo install -m 644 autopi-diag.service /etc/systemd/system/
sudo install -m 644 autopi-diag.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo python3 /opt/autopi-diag/autopi_diag.py --dry-run   # eerst kijken
sudo python3 /opt/autopi-diag/autopi_diag.py --status    # buffer inzien
sudo systemctl enable --now autopi-diag.timer
systemctl list-timers autopi-diag.timer
```

Het geheime ingest-pad staat **niet** in een eigen env: de service leest
`/etc/eniro-soc.env`, zodat dat pad op één plek blijft staan en één rotatie
volstaat.

Draait als root omdat `qmicli` bij `/dev/cdc-wdm0` moet.

## Interpretatie

| Waarde | Comfortabel | Werkbaar | Problematisch |
|---|---|---|---|
| RSRP | > -90 dBm | -90 tot -105 | < -110 |
| RSRQ | > -10 dB | -10 tot -15 | < -20 |
| SNR | > 13 dB | 0 tot 13 | < 0 |

RSRP is de maat die telt. SNR beperkt de snelheid, niet de betrouwbaarheid — bij
een paar kilobyte per vijf minuten is een lage SNR onbelangrijk.

`cell_id` en `tac` zijn handig om ontvangstproblemen aan een plek te koppelen:
zie je op één cel structureel slechte waarden, dan ligt het aan die mast en niet
aan de dongle.
