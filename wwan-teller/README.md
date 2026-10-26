# wwan-teller

Legt per boot vast hoeveel data er over 4G (`wwan0`) ging. Gebouwd 19-10-2026
na de onverklaarde 73 MB van 14-10.

- Timer elke 2 min → `--snapshot`: stand van deze boot naar
  `/var/lib/wwan-teller/huidig.json`.
- Nieuwe boot gezien → vorige boot als regel in `boots.jsonl` én als record
  `@t: wwan.boot` naar de ontvanger (MQTT `autopi/wwan/boot`, retained).
- Meer dan 2 MB tussen twee snapshots → open verbindingen (`ss -tunp`) in
  `pieken.log`. Dat is het spoor naar de veroorzaker.
- `--rapport`: MB per dag en de laatste 15 boots.

Wat ontbreekt: het staartje na de laatste snapshot (≤ 2 min) voor het slapen.
De tellers tellen IP-verkeer op de interface, inclusief WireGuard-overhead —
dat is ook wat 1NCE rekent.

Het record gaat naar `INGEST_URL` (standaard `http://127.0.0.1:8181/ingest/lokaal`,
de ontvanger op de dongle). Draai je de servervariant van de ontvanger, zet
dan een `Environment=INGEST_URL=...` in de unit (bijv. met
`systemctl edit wwan-teller`). Overige instellingen (`WWAN_IFACE`, `PIEK_BYTES`,
`WWAN_TELLER_DIR`) staan met hun standaard bovenin `wwan_teller.py`.

## Installatie

```bash
sudo install -d /opt/wwan-teller
sudo install -m 755 wwan_teller.py /opt/wwan-teller/wwan_teller.py
sudo install -m 644 README.md /opt/wwan-teller/README.md
sudo install -m 644 wwan-teller.service wwan-teller.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now wwan-teller.timer
sudo python3 /opt/wwan-teller/wwan_teller.py --snapshot && sudo cat /var/lib/wwan-teller/huidig.json
```

Getest met nagebootste tellers en boot-ID's: afsluiten van een boot, piek-log,
ontbrekende `wwan0`, rapport.
