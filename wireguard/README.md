# wireguard

Een WireGuard-tunnel van de dongle naar je thuisnetwerk, alleen voor de
MQTT-broker. Daarover houdt de ontvanger op de dongle (`../ingest/`) één
MQTT-verbinding open. Geen publiek endpoint, geen TLS-handshake per upload.

```
dongle ── wg0 (alleen <BROKER_IP>/32) ──► VPN-server thuis ──► broker:1883
   └─ al het andere verkeer gaat gewoon buiten de tunnel om
```

| Bestand | Waarheen | Wat |
|---|---|---|
| `wg0.conf.example` | `/etc/wireguard/wg0.conf` (600) | tunnelconfig, split tunnel |
| `wg-reresolve.sh` | `/usr/local/sbin/wg-reresolve.sh` (755) | brengt wg0 op als hij ontbreekt, zoekt de endpoint opnieuw op |
| `wg-reresolve.service` / `.timer` | `/etc/systemd/system/` | draait het script elke 5 min |

## Waarom zo

- **Split tunnel** (`AllowedIPs = <BROKER_IP>/32`). Valt de tunnel weg, dan
  heeft de dongle gewoon nog internet; alleen de MQTT-verbinding wacht. De
  ontvanger geeft dan een 503, `eniro-soc` spoolt en de firmware houdt zijn
  cache vast: vertraging, geen verlies.
- **Geen DNS-regel.** Een resolver thuis is door de tunnel niet bereikbaar, en
  `wg-quick` heeft voor een DNS-regel `resolvconf` nodig, dat niet op de
  AutoPi-image staat.
- **Geen PersistentKeepalive.** De dongle begint zelf elk gesprek. De eerste
  handshake komt pas na het eerste verkeer, dat is normaal.
- **`wg-reresolve`**: WireGuard resolvet de endpoint-hostname alleen bij het
  opstarten. Met een dynamisch thuis-IP (DDNS) zoekt dit script hem elke
  5 minuten opnieuw op, en brengt het `wg0` alsnog op als dat bij het booten
  mislukte (geen netwerk of DNS). `Restart=` op oneshot-units kan niet in de
  systemd van deze image.
- **Oude `wireguard-tools` is geen probleem.** Het protocol zit in de kernel
  (5.10 op de dongle); `wg`/`wg-quick` zijn alleen het configuratiegereedschap.

## Aan de serverkant (thuis)

1. Maak op je VPN-server (router, firewall of eigen WireGuard-server) een
   client aan voor de dongle, met een eigen tunneladres (`/32`).
2. Zorg dat de endpoint-hostname naar je thuis-IP wijst (DDNS). Gebruik je een
   DNS-dienst met proxy, zet dit record dan op **alleen DNS** (niet proxied):
   WireGuard is UDP en gaat niet door een HTTP-proxy.
3. **Firewall: laat de dongle alleen bij de broker.** Eén regel *toestaan*:
   bron = tunneladres van de dongle, TCP naar `<BROKER_IP>:1883`. Daaronder
   *blokkeren*: alles van dat tunneladres naar je LAN, de router zelf en de
   rest van het VPN-netwerk. Let op de volgorde, en op een eventuele
   standaardregel "VPN → LAN alles toestaan": zonder blokregel kan de dongle
   dan je hele netwerk in.

## Installatie op de dongle

```bash
sudo apt-get update && sudo apt-get install -y wireguard-tools   # uit de Raspbian-buster-repo

sudo install -m 600 -o root -g root wg0.conf.example /etc/wireguard/wg0.conf
sudo nano /etc/wireguard/wg0.conf            # placeholders invullen, DNS-regel weg
sudo wg-quick up wg0
sudo wg show                                 # interface + peer zichtbaar

sudo install -m 755 wg-reresolve.sh /usr/local/sbin/wg-reresolve.sh
sudo nano /usr/local/sbin/wg-reresolve.sh    # PEER en ENDPOINT invullen
sudo install -m 644 wg-reresolve.service wg-reresolve.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable wg-quick@wg0
sudo systemctl enable --now wg-reresolve.timer
```

De dongle-firewall hoeft niet aangepast te worden: de standaardregel
`-A INPUT -m state --state RELATED,ESTABLISHED -j ACCEPT` geldt ook voor `wg0`.

## Testen

```bash
# Verkeer naar de broker opwekken en dan naar de handshake kijken
timeout 5 bash -c '</dev/tcp/<BROKER_IP>/1883' && echo "broker open"
sudo wg show wg0 latest-handshakes          # tijdstempel, niet 0
getent hosts <VPN_HOSTNAAM>                 # op de dongle staat geen dig
journalctl -t wg-reresolve -n 10 --no-pager
```

Negatieve test (aanbevolen): zet tijdelijk een extra adres in `AllowedIPs`
(bijv. de webinterface van je router) en controleer dat dat **dicht** is. Zo
weet je dat de firewall aan de serverkant doet wat hij moet. Daarna terug naar
alleen `<BROKER_IP>/32`.

Thuis op wifi werkt de tunnel meestal ook: de router buigt verkeer naar zijn
eigen publieke IP terug naar de VPN-server (hairpin/NAT-loopback). Werkt dat
bij jouw router niet, dan valt de dongle thuis terug op wachten tot hij op 4G
zit, of los je het op met een lokale DNS-override voor de endpoint-hostname.

## Bij verkoop of overdracht

`/etc/wireguard/wg0.conf` bevat de privésleutel. Verwijder hem (zie
`../scripts/wipe.sh`) én trek de client in op je VPN-server.
