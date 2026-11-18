# Installatie — van kale dongle tot data in Home Assistant

Deze handleiding neemt je mee van een AutoPi-dongle zoals hij uit de doos komt
tot de accustatus van je e-Niro live in Home Assistant, óók als de auto
onderweg is. Alles hierin is zo gedaan en getest in een Kia e-Niro uit 2019.

Reken op een middag voor het geheel: flashen, beveiligen en 4G een uur of
twee, de rest van de dongle inrichten een kwartier, Home Assistant een uur,
inbouwen een half uur.

Het inrichten van de dongle doet **`install.sh`**: je vult één bestand in
(`config.env`) en draait één script (hoofdstuk 6). Wat dat script doet staat
stap voor stap in [bijlage A](#bijlage-a--wat-installsh-doet-stap-voor-stap),
voor wie wil weten wat er verandert of het liever met de hand doet.

> **Lees eerst hoofdstuk 1 helemaal.** Daar staat wat je nodig hebt, en een
> paar dingen moet je vóóraf regelen (de SIM activeren, de image downloaden,
> thuis een MQTT-login, een WireGuard-client en een firewallregel aanmaken).

---

## Inhoud

1. [Wat je nodig hebt en thuis klaarzet](#1-wat-je-nodig-hebt-en-thuis-klaarzet)
2. [Hoe het in elkaar zit](#2-hoe-het-in-elkaar-zit)
3. [Firmware flashen](#3-firmware-flashen)
4. [Eerste boot en beveiligen](#4-eerste-boot-en-beveiligen)
5. [4G met een 1NCE-SIM](#5-4g-met-een-1nce-sim)
6. [Installeren met install.sh](#6-installeren-met-installsh)
7. [Home Assistant](#7-home-assistant)
8. [Inbouwen in de auto](#8-inbouwen-in-de-auto)
9. [Controleren](#9-controleren)
10. [Als het niet werkt](#10-als-het-niet-werkt)
11. [Naslag en doorverkopen](#11-naslag-en-doorverkopen)

- [Bijlage A — wat install.sh doet, stap voor stap](#bijlage-a--wat-installsh-doet-stap-voor-stap)
- [Bijlage B — ontvanger op een server via HTTPS](#bijlage-b--ontvanger-op-een-server-via-https)

---

## 1. Wat je nodig hebt en thuis klaarzet

### Hardware

| | Opmerking |
|---|---|
| **AutoPi-dongle met CM4** | De uitvoering met een Raspberry Pi Compute Module 4 en eMMC. Deze handleiding is geschreven voor een board met silkscreen **AutoPi 3.6.10** en een CM4 erop, zonder knop aan de zijkant maar met een 2-pins header **`CM4 Flash`** op het board. |
| **Jumper-kapje** (2,54 mm) | Voor de `CM4 Flash`-header. Meegeleverd. Een female-female dupont-draadje werkt ook. |
| **Micro-USB-kabel** | Eentje die **data** doet, niet alleen laden. Dit is de nummer-één oorzaak van "rpiboot blijft wachten". |
| **12V op de OBD-connector** | De dongle start níét op van micro-USB. Gewoon in de auto flashen kan: pin 16 van de OBD-poort staat bij de e-Niro permanent onder spanning. |
| **Data-SIM** | Een **1NCE IoT-SIM** (500 MB voor tien jaar, eenmalig zo'n tientje) is wat hier gebruikt is. Nano-formaat. Met deze opzet is ~0,06 MB per dag gemeten; de 500 MB is geen beperking. |
| **OBD-verlengkabel** | Haaks, **alle 16 pinnen doorverbonden**, zo kort als je route toelaat (30-50 cm). Zie hoofdstuk 11. |
| **Kunststof demontagespatel** | Voor het onderpaneel van het dashboard. |

### Een computer om mee te flashen

macOS of Linux is het makkelijkst. Windows kan ook, zie 3.2.

### Thuis

| | Waarvoor |
|---|---|
| **Home Assistant** met een **MQTT-broker** | De Mosquitto add-on is prima. Plain MQTT op poort 1883 volstaat: het verkeer loopt door de WireGuard-tunnel. |
| **Een WireGuard-server** | Op je router (veel routers en firewalls hebben er een ingebouwd) of een eigen WireGuard-server. De dongle maakt er een split tunnel mee, alleen naar de broker. |
| **Een vaste hostnaam voor je thuis-IP** | Bij een dynamisch IP: DDNS. Gebruik je een DNS-dienst met proxy, dan dit record op **alleen DNS** — WireGuard is UDP. |
| **Een firewall die een VPN-client kan beperken** | De dongle mag alleen bij `<BROKER_IP>:1883`, nergens anders in je netwerk. Meestal dezelfde router als de WireGuard-server. |

Een eigen server, een domein met HTTPS of een tunnel naar buiten is **niet**
nodig. Wil je toch liever geen VPN, dan kan de ontvanger ook op een eigen
server achter HTTPS draaien; zie bijlage B.

### Vooraf regelen

- [ ] **De SIM activeren** in het 1NCE-portaal. Een niet-geactiveerde SIM geeft
      een foutmelding (`option-unsubscribed`, cause #33) die er precies uitziet
      als een configuratiefout. Dat kost je anders een uur.
- [ ] **De firmware-image downloaden** (1,3 GB, zie 3.1). Thuis doen, niet in de auto.
- [ ] **rpiboot en Etcher installeren** en één keer opstarten (zie 3.2).
- [ ] **Een WireGuard-client aanmaken** op je VPN-server voor de dongle, met een
      eigen tunneladres (`/32`). Bewaar de gegenereerde `.conf`; de waarden
      gaan in `config.env` (6.2). Zie 1.3.
- [ ] **Een MQTT-login voor de dongle** aanmaken in je broker (bijvoorbeeld
      `autopi-dongle`). Wachtwoord in je wachtwoordmanager. Zie 1.2.
- [ ] **De firewall thuis** zo zetten dat de dongle alleen bij de broker kan.
      Zie 1.3.
- [ ] **Een SSH-sleutel** op je laptop, als je die nog niet hebt
      (`ssh-keygen -t ed25519`). De publieke sleutel gaat in `config.env`.

### 1.1 Wat er thuis klaar moet staan

| | Hoe je het controleert |
|---|---|
| MQTT-broker op `<BROKER_IP>:1883` | vanaf je laptop: `mosquitto_sub -h <BROKER_IP> -u <gebruiker> -P '<wachtwoord>' -t 'autopi/#' -v` maakt verbinding |
| een vast adres voor de broker | DHCP-reservering of statisch IP. Dit adres komt in de tunnelconfig. |
| WireGuard-server bereikbaar op `<VPN_HOSTNAAM>:<POORT>` (UDP) | de server draait; de hostnaam wijst naar je huidige thuis-IP |

### 1.2 MQTT-login voor de dongle

Maak in je broker een **eigen login** voor de dongle, bijvoorbeeld
`autopi-dongle`. Bij de Mosquitto add-on: een Home Assistant-gebruiker, of een
login in de add-on-configuratie. Een eigen login kun je los intrekken als de
dongle ooit weg is.

Die login moet:

- publiceren op `autopi/#`;
- **lezen** op `autopi/config/profiel` — anders komt de profielkeuze uit Home
  Assistant (7.5) nooit bij de dongle aan;
- met MQTT Discovery (`HA_DISCOVERY=1`, de standaard bij een verse
  installatie, 7.2): **publiceren** op `homeassistant/device/#` (retained) en
  **lezen** op `homeassistant/status` — anders verschijnen de sensoren niet
  vanzelf in Home Assistant.

Gebruik je ACL's, zet ze dan precies zo; zonder ACL's mag elke login alles.

### 1.3 WireGuard aan de kant thuis

1. **Client aanmaken.** Maak op je VPN-server een client voor de dongle, met
   een eigen tunneladres (`/32`, bijvoorbeeld `10.0.99.8/32`). Download de
   `.conf` die de server genereert.
2. **Hostnaam.** Zorg dat `<VPN_HOSTNAAM>` naar je thuis-IP wijst (DDNS bij een
   dynamisch IP). Met een DNS-dienst die een proxy aanbiedt: dit record op
   **alleen DNS**, niet proxied. WireGuard is UDP en gaat niet door een
   HTTP-proxy.
3. **Firewall: de dongle mag alleen bij de broker.** Op de router of firewall
   waar de VPN-clients binnenkomen:
   - één regel **toestaan**: bron = tunneladres van de dongle, TCP naar
     `<BROKER_IP>:1883`;
   - daaronder **blokkeren**: alles van dat tunneladres naar je LAN, naar de
     router zelf en naar de rest van het VPN-netwerk.

   **Let op de volgorde**, en op een standaardregel als "VPN → LAN: alles
   toestaan". Veel routers hebben die. Zonder blokregel erboven kan de dongle —
   en iedereen die de dongle in handen krijgt — dan je hele netwerk in. Staan
   er andere regels voor VPN-clients (bijvoorbeeld toegang tot een beheerpagina
   vanaf het hele VPN-netwerk), dan moeten die ónder de blokregel van de
   dongle staan.

---

## 2. Hoe het in elkaar zit

```
e-Niro BMS ──CAN 7E4/7EC──►  eniro_soc.py ─┐
cluster    ──CAN 7C6/7CE──►                │
AutoPi-firmware (GPS, 12V, events) ────────┤  gzip-JSON over HTTP, lokaal
wwan-teller (4G-verbruik per boot) ────────┤
autopi-diag (4G-kwaliteit, optioneel) ─────┘
                                           ▼
                       autopi_ingest.py  op de dongle, 127.0.0.1:8181/ingest/lokaal
                                           │  één MQTT-verbinding die openblijft
                                           ▼
                       wg0: WireGuard-split-tunnel, AllowedIPs = <BROKER_IP>/32
                                           │  via wifi of 4G
                                           ▼
                       VPN-server thuis ──► MQTT-broker ──► Home Assistant ──► ABRP
                                                  ▲
                       autopi/config/profiel ─────┘  (HA zet het profiel, de dongle leest het)
```

Alles op de dongle post naar de **ontvanger op de dongle zelf**. Die pakt de
gzip uit en zet elk record op MQTT, op het topic dat bij zijn `@tag` hoort
(`autopi/obd/ev/bms`, `autopi/track/pos`, …). De ontvanger hoeft niets van
`eniro_soc.py` te weten.

**Waarom zo?**

- **Over 4G zit de dongle achter de NAT van de provider.** Hij kan zelf naar
  buiten, maar niemand kan bij hem. De WireGuard-tunnel begint bij de dongle,
  dus dat is geen probleem: de dongle bereikt de broker thuis alsof hij in je
  netwerk zit — en alleen de broker.
- **Data.** Een record is een paar honderd bytes; een nieuwe TLS-verbinding
  kost ruwweg 5 KB. Met één openstaande MQTT-verbinding door de tunnel, en de
  profielen uit 7.5, ging het verbruik op 4G van 2-8 MB per dag naar
  **~0,06 MB per dag**.
- **De firmware stuurt gzip over HTTP**, geen MQTT. De lokale ontvanger
  vertaalt. Een Home Assistant-webhook kan geen gzip uitpakken.
- **Geen publiek endpoint.** Niets van jou staat open op internet.
- **Storing is vertraging, geen verlies.** Is de tunnel of de broker weg, dan
  geeft de ontvanger een `503`; `eniro-soc` zet de records in een wachtrij op
  schijf en de firmware houdt zijn cache vast.
- **Split tunnel.** Alleen het broker-adres gaat door de tunnel. Valt die weg,
  dan heeft de dongle gewoon nog internet.

### Wat het doet en niet doet

**Wel:** SOC (display én BMS), pakspanning, stroom, vermogen, celtemperaturen,
celafwijking, 12V-spanning, cumulatief geladen en verbruikt, laden, snelladen,
contact aan/uit, State of Health, kilometerstand, lopend verbruik over 100 km,
en via de firmware: GPS-positie. Daarnaast over de dongle zelf: online/offline,
4G-kwaliteit en 4G-verbruik per boot.

**Niet:** openen, sluiten, voorverwarmen, claxonneren. Dit leest alleen.

**Beperkingen die bij de auto horen, niet bij de software:**
- **Met het contact uit antwoordt er niemand.** De auto moet in ready staan of
  aan de lader hangen.
- **De kilometerstand alleen tijdens rijden.** De instrumentencluster is stil
  tijdens het laden. eniro-soc stuurt dan de laatst bekende stand mee uit zijn
  eigen geschiedenis (`odometer_bron: opgeslagen`).

---

## 3. Firmware flashen

We zetten een verse AutoPi Core-image op de eMMC. Daarna is de dongle
volledig lokaal te beheren, zonder AutoPi Cloud en zonder abonnement.

### 3.1 De image

**AutoPi Core v1.22.9, CM4-uitvoering.** Dit is de release die AutoPi-support
aanwees als stabiel voor deze hardware, en waarmee alles hier getest is.

- Releasepagina: https://github.com/autopi-io/autopi-core/releases/tag/v1.22.9
- Bestand: `autopi_core_CM4_021222_v1-22-9_7.0_autopi_1677672940.img.gz` (1,29 GB)
- Directe link:
  https://github.com/autopi-io/autopi-core/releases/download/v1.22.9/autopi_core_CM4_021222_v1-22-9_7.0_autopi_1677672940.img.gz

**Niet uitpakken.** Etcher flasht de `.img.gz` rechtstreeks.

> Er bestaan nieuwere releases (v1.26, v1.28-beta). Die zijn hier niet getest.
> `install.sh` en bijlage A gaan uit van de `engines.conf` van v1.22.9.

> De release-notes zeggen dat je na het flashen een "device encryption key"
> moet accepteren op my.autopi.io. **Dat is niet nodig** voor deze opzet — de
> dongle werkt volledig zonder cloudregistratie.

### 3.2 Gereedschap installeren

**macOS**

```bash
brew install rpiboot
brew install --cask balenaetcher
```

Start Etcher één keer en geef toegang tot verwijderbare volumes, anders ziet
hij de eMMC straks niet.

Liever zelf bouwen? `brew install libusb pkgconf`, dan
`git clone --depth=1 https://github.com/raspberrypi/usbboot && cd usbboot && make`.
**`pkgconf` is verplicht** — zonder faalt `make` met `'libusb.h' file not found`,
wat misleidend is want libusb staat er wel.

**Linux (Debian/Ubuntu)**

```bash
sudo apt install git build-essential pkg-config libusb-1.0-0-dev
git clone --depth=1 https://github.com/raspberrypi/usbboot
cd usbboot && make
```

Etcher is er als AppImage, of gebruik `dd` (zie 3.4).

**Windows**

De usbboot-repository (https://github.com/raspberrypi/usbboot) heeft een
Windows-installer die de driver en `rpiboot` meelevert — zie de README daar.
Daarna Balena Etcher voor Windows. Niet door de schrijver van deze handleiding
getest; het principe is hetzelfde.

### 3.3 Dongle in flash-modus

Open de behuizing. Op het board zitten twee 2-pins headers:

| Header | Wat hij doet | Nu |
|---|---|---|
| **`CM4 Flash`** (links naast de boardnaam) | Schakelt het booten van eMMC uit. De CM4 biedt zich dan via USB aan als opslagapparaat. | **Jumper erop** |
| **`FORCE 5V`** (rechtsboven) | Overbrugt de Smart Power Manager, zodat de CM4 niet in slaap kan vallen. | Optioneel tijdens het flashen, **daarna er beslist weer af** |

> **`FORCE 5V` nooit laten zitten.** AutoPi is daar expliciet over: met die
> jumper is de accubescherming overbrugd en kan de dongle je 12V-accu leegtrekken.

De volgorde is dwingend: **de jumper moet erop zitten vóórdat er spanning op
komt**, en blijven zitten zolang rpiboot draait.

1. Spanning eraf (dongle uit de OBD-poort).
2. Jumper op `CM4 Flash`.
3. Micro-USB van de dongle naar je computer. Rechtstreeks, of via een
   **gevoede** hub — een ongevoede hub geeft schrijffouten.
4. Spanning erop (dongle in de OBD-poort, of je 12V-opstelling).
5. Start rpiboot:

```bash
sudo rpiboot                 # macOS met Homebrew
sudo ./rpiboot               # Linux, in de usbboot-map
```

Verwacht: `Waiting for BCM2835/6/7/2711...` → `Sending bootcode.bin` → … →
`Second stage boot server done`.

Blijft hij hangen op `Waiting`? Dan: de jumper zat niet goed tijdens power-on
(spanning eraf, opnieuw), een andere USB-poort, of een micro-USB-kabel die
alleen laadt.

Werkt de standaardaanroep op macOS niet, probeer dan:

```bash
sudo rpiboot -d "$(brew --prefix rpiboot)"/share/rpiboot/mass-storage-gadget64
```

macOS meldt daarna **"De schijf die je hebt geplaatst is niet leesbaar"** — klik
**Negeren**. Dat is de eMMC.

### 3.4 Flashen

Zoek de eMMC op. Het is een schijf van ~16 of ~32 GB die niet van jou is.

```bash
diskutil list        # macOS, bijvoorbeeld /dev/disk4
lsblk                # Linux, bijvoorbeeld /dev/sdb
```

**Met Etcher:** *Flash from file* → de `.img.gz` → *Select target* → de eMMC.
**Controleer de grootte twee keer.** Dit is het moment waarop je per ongeluk je
eigen schijf overschrijft. Dan *Flash*. Laat de computer niet slapen
(`caffeinate -dimsu` in een tweede terminal op macOS).

**Zonder Etcher:**

```bash
# macOS - let op: rdiskN, niet diskN (tien keer sneller)
diskutil unmountDisk /dev/diskN
gunzip -c autopi_core_CM4_*.img.gz | sudo dd of=/dev/rdiskN bs=4m status=progress
sync

# Linux
gunzip -c autopi_core_CM4_*.img.gz | sudo dd of=/dev/sdX bs=4M status=progress conv=fsync
```

Het flashen zelf duurt 5-10 minuten.

### 3.5 Afronden

1. Schijf uitwerpen (`diskutil eject /dev/diskN` of `sudo eject /dev/sdX`).
2. Spanning eraf.
3. Micro-USB los.
4. **Jumper van `CM4 Flash` af.** Blijft die zitten, dan wacht de dongle bij
   elke start weer op USB en boot hij nooit van de eMMC.
5. `FORCE 5V` ook af, als je die gebruikt had.
6. Behuizing dicht.

Bewaar het jumper-kapje. Je hebt het nodig als je ooit opnieuw wilt flashen.

---

## 4. Eerste boot en beveiligen

### 4.1 Binnenkomen

1. Dongle **spanningsloos**. SIM erin: tekstzijde omhoog, afgeschuinde hoek naar
   binnen.
2. Spanning erop, wachten tot de LED constant blauw is.
3. Verbind met de eigen wifi-hotspot van de dongle. Fabrieksgegevens van een
   verse image:

| | |
|---|---|
| SSID | `AutoPi-Initial` |
| Wifi-wachtwoord | `autopi2018` |
| SSH | `pi` / `autopi2018` |
| Adres van de dongle | `192.168.4.1` |
| Webinterface | http://local.autopi.io (of http://192.168.4.1) |

```bash
ssh pi@192.168.4.1
```

**Een ethernetkabel werkt ook, en is prettiger.** Poort 22 staat op `eth0` open,
en de dongle krijgt via DHCP een adres op je netwerk. Wifi-hotspot en wifi-client
delen op de dongle één radio; met beide actief kan SSH traag worden (zie 10.1).

### 4.2 De dongle valt steeds in slaap

Na een boot zonder rijdende motor blijft de dongle standaard maar **4 minuten**
wakker en slaapt hij daarna 6 uur. Tijdens het inrichten is dat onwerkbaar.

Twee manieren om dat te voorkomen:

- **Auto in ready** (contact aan). De boordspanning gaat naar ~14,4 V en de
  dongle blijft wakker. Simpelst als je toch in de auto zit.
- **De sleep-timers wissen**, zodra je binnen bent:

```bash
sudo salt-call --local power.sleep_timer clear='*'
```

Terugzetten gebeurt vanzelf bij de volgende herstart van `salt-minion`.
`install.sh` zet de wachttijd straks structureel ruimer (`INACTIVITY_PERIOD`,
zie A.3.3).

Let ook op `battery_critical_level`: blijft de 12V-spanning 300 seconden onder
12,0 V, dan gaat de dongle in hibernation. Bij een auto die al dagen stilstaat
kan dat gebeuren. Auto in ready lost het op.

### 4.3 Direct beveiligen — dit is geen formaliteit

**De lokale API op poort 9000 heeft geen authenticatie.** Wie op de hotspot zit
kan via die API willekeurige commando's als root uitvoeren. Met het
fabriekswachtwoord `autopi2018` — dat publiek gedocumenteerd is — betekent dat:
iedereen binnen wifi-bereik van je auto heeft root.

Doe daarom als eerste:

```bash
passwd                                          # nieuw wachtwoord voor pi
sudo nano /etc/hostapd/hostapd_uap0.conf
```

In `hostapd_uap0.conf` aanpassen:

```
ssid=<EIGEN_SSID>
wpa_passphrase=<STERK_WACHTWOORD>
country_code=NL
ignore_broadcast_ssid=0
```

`<STERK_WACHTWOORD>`: minimaal 16 tekens, uit je wachtwoordmanager.
(De fabrieksconfig heeft `country_code=DK` en een ongeldige waarde voor
`ignore_broadcast_ssid`. Dit bestand wordt door geen enkele salt-state beheerd,
dus je wijziging blijft staan.)

```bash
sudo systemctl restart hostapd
systemctl is-enabled hostapd      # moet 'enabled' zijn
```

Verbind opnieuw met de nieuwe SSID. **Zet de hotspot niet uit.** Over 4G is de
dongle van buitenaf onbereikbaar; de hotspot is dan je enige weg naar binnen.

Zet straks dezelfde SSID en hetzelfde wachtwoord in `config.env`
(`HOTSPOT_SSID`, `HOTSPOT_PSK`, zie 6.2). `install.sh` controleert ze dan
alleen; staat er iets anders, dan past hij `hostapd_uap0.conf` aan en herstart
hij hostapd als allerlaatste stap.

### 4.4 Kleine ergernis: trage sudo

Elke `sudo` wacht op een DNS-timeout omdat de hostnaam niet in `/etc/hosts`
staat. Eenmalig oplossen:

```bash
grep -q "$(hostname)" /etc/hosts || echo "127.0.1.1 $(hostname)" | sudo tee -a /etc/hosts
```

---

## 5. 4G met een 1NCE-SIM

Dit hoofdstuk kostte bij de eerste installatie de meeste tijd. Het werkende
recept staat hieronder; de achtergrond staat in hoofdstuk 10.

Dit deel blijft met de hand: de APN staat in de webinterface, en het modem wil
een vaste volgorde van AT-commando's met een herstart van `qmi-manager`
ertussen. Je doet het één keer; `install.sh` raakt 4G niet aan.

### 5.1 APN in de webinterface

Open http://local.autopi.io (verbonden met de hotspot) en stel in:

| Instelling | Waarde |
|---|---|
| APN | `iot.1nce.net` |
| Gebruikersnaam / wachtwoord | leeg |
| Authenticatie | geen (PAP als het veld verplicht is) |
| PDP-type | **IPv4** — 1NCE doet geen IPv6 |

Niet `nxt20.net` invullen; dat is de APN van AutoPi's eigen SIM.

### 5.2 Het modem goed zetten

Twee dingen moeten in het modem zelf, via AT-commando's:

1. **De attach-profile op de 1NCE-APN, IPv4-only.** Een verse image heeft een
   lege APN met `ipv4-or-ipv6`. Op een IPv4-only SIM geeft dat de beruchte
   cause #33.
2. **Alleen LTE.** Anders kan het modem op een 2G-cel gaan staan die geen data
   doorgeeft (`PS: detached`), en dan reset `qmi-manager` het modem elke paar
   minuten zonder dat er ooit verbinding komt. Dat is hier gebeurd terwijl er
   drie LTE-netwerken in bereik waren.

De volgorde is dwingend; doe niets tussendoor:

```bash
sudo systemctl stop qmi-manager

# Verplicht: qmi-manager stoppen zet het modem in power save, en dan
# weigert het elk AT-commando met 'CME: operation not allowed'.
sudo qmicli -d /dev/cdc-wdm0 --dms-set-operating-mode=online
sleep 5

sudo salt-call --local modem.connection execute 'AT+CGDCONT=1,"IP","iot.1nce.net"'
sudo salt-call --local modem.connection execute 'AT+WS46=28'     # 28 = alleen LTE
sudo salt-call --local modem.connection execute 'AT+COPS=0'      # automatische netwerkkeuze
sleep 45

# Controle
sudo salt-call --local modem.connection execute 'AT+CGDCONT?'    # 1,"IP","iot.1nce.net"
sudo salt-call --local modem.connection execute 'AT+WS46?'       # 28
sudo salt-call --local modem.connection execute 'AT+COPS?'       # eerste cijfer 0, laatste 7 (= LTE)

sudo systemctl start qmi-manager
```

Alle drie de instellingen staan in het geheugen van het modem en **overleven een
reboot**. Je doet dit één keer.

**Gebruik hiervoor geen `qmicli --nas-set-system-selection-preference`.** Op dit
Telit-modem loopt dat in een time-out of wordt het bij de volgende modemreset
teruggedraaid. QMI om uit te lezen, AT om in te stellen wat moet blijven staan.

**Zet geen vaste operator** (`AT+COPS=1,...`). Dat werkt, maar breekt roaming
in het buitenland, en `qmi-manager` zet het toch terug op automatisch.

### 5.3 Controleren

Na een minuut of twee:

```bash
ip -4 addr show wwan0                                  # een 10.x-adres
ping -I wwan0 -c3 1.1.1.1                              # eerste ping traag, daarna ~40-50 ms
sudo qmicli -d /dev/cdc-wdm0 --nas-get-serving-system  # 'registered', PS 'attached', 'lte'
```

Staat wifi of ethernet ook aan, dan houden díe de default route. Test 4G daarom
altijd expliciet via `-I wwan0`.

Staat er elke 5 seconden een SMS-fout in het log, leeg dan de SMS-opslag:
`sudo salt-call --local modem.connection execute 'AT+CMGD=1,4'`.

---

## 6. Installeren met install.sh

Vanaf hier richt `install.sh` de dongle in: de ontvanger, WireGuard, wifi, de
hotspot, `engines.conf`, SSH, het journal en de software. Je vult één bestand
in en draait één script. Het script is herhaalbaar: wat al klopt blijft staan,
en een dienst wordt alleen herstart als er voor die dienst iets veranderde.

Doe vóór dit hoofdstuk: hoofdstuk 1 (thuis klaarzetten), 3, 4 en 5.

### 6.1 De repository op de dongle

Op je laptop:

```bash
git clone https://github.com/jurrehaak86/eniro-obd.git
# of: download de release-zip van GitHub en pak hem uit tot een map eniro-obd
scp -r eniro-obd pi@<DONGLE_IP>:/home/pi/
ssh pi@<DONGLE_IP>
cd ~/eniro-obd
```

Heeft de dongle al internet en staat `git` erop, dan kan `git clone` ook direct
op de dongle. Alles hierna gaat uit van `~/eniro-obd/`.

### 6.2 config.env invullen

```bash
cp config.env.example config.env
chmod 600 config.env
nano config.env
```

Elk blok in het bestand heeft uitleg. Waar je de waarden vandaan haalt:

| Blok | Velden | Waar vandaan |
|---|---|---|
| MQTT | `MQTT_HOST`, `MQTT_PASS` (en `MQTT_USER`, `MQTT_PORT`, `MQTT_PREFIX`) | het LAN-adres van je broker en de login uit 1.2 |
| WireGuard | `WG_ADDRESS`, `WG_PRIVATE_KEY`, `WG_SERVER_PUBLIC_KEY`, `WG_PRESHARED_KEY`, `WG_ENDPOINT` | de `.conf` die je VPN-server voor de dongle maakte (1.3). `AllowedIPs` laat je leeg: dat wordt `<MQTT_HOST>/32`. Een `DNS`-regel neem je niet over. |
| Hotspot | `HOTSPOT_SSID`, `HOTSPOT_PSK` | wat je in 4.3 instelde |
| Wifi | `WIFI_1_SSID`, `WIFI_1_PSK`, `WIFI_1_PRIORITY`, `WIFI_2_…` | je thuisnetwerk en eventuele andere. Geen enkel netwerk ingevuld = wifi blijft zoals hij is (bijvoorbeeld via de webinterface) |
| SSH | `SSH_PUBKEY_1`, `SSH_WIFI_SUBNET` | de regel uit `~/.ssh/id_ed25519.pub` op je laptop; het netwerk waar je laptop op zit |
| Gedrag | `BUNDEL_INTERVAL`, `GPS_INTERVAL`, `INTERVAL_CHARGING`, … | de geteste standaardwaarden; laten staan |
| Home Assistant | `HA_DISCOVERY`, `HA_DISCOVERY_PREFIX` | `1` = de dongle maakt de sensoren zelf aan in HA (7.2); laten staan bij een verse installatie. Prefix leeg = `homeassistant` |

Notatie: `KEY=waarde`, één per regel. Waarden met spaties of rare tekens tussen
`'enkele'` aanhalingstekens. Er wordt niets geëxpandeerd: `$` en `\` zijn
gewone tekens.

`config.env` bevat al je wachtwoorden en sleutels in één bestand. Het staat in
`.gitignore`, `install.sh` weigert het als het voor anderen leesbaar is, en
`scripts/wipe.sh` wist het bij doorverkopen.

### 6.3 Proefdraaien

```bash
sudo ./install.sh --dry-run
```

Er verandert niets. Eerst controleert het script `config.env` en noemt het
alles wat ontbreekt of niet klopt (een IP-adres dat geen IP-adres is, een
WireGuard-sleutel van de verkeerde lengte, een hotspot-wachtwoord van minder
dan 8 tekens). Daarna per stap, per bestand: `ongewijzigd`, `nieuw`, of
`gewijzigd` met een diff. Wachtwoorden, PSK's en sleutels staan in die diff als
`***`. Tot slot welke diensten het zou inschakelen of herstarten.

### 6.4 Installeren

```bash
sudo ./install.sh
```

| Stap | Wat | Met de hand: |
|---|---|---|
| 1 | `python3-paho-mqtt` en `wireguard-tools` uit de Raspbian-repository | A.4, A.5 |
| 2 | systeemgebruiker `autopi-ingest` | A.5 |
| 3 | het journal persistent | A.7 |
| 4 | `/etc/eniro-soc.env` | A.8 |
| 5 | `/etc/autopi-ingest.env` (600, met het MQTT-wachtwoord) | A.5 |
| 6 | `/etc/wireguard/wg0.conf` (600), `wg-reresolve` | A.4 |
| 7 | `engines.conf`: lokale ontvanger, meetfrequenties, spraak uit | A.3 |
| 8 | SSH-sleutel(s) en SSH over wifi vanaf je thuisnetwerk | A.2 |
| 9 | de scripts naar `/opt/…` en de systemd-units | A.5, A.8, A.10–A.12 |
| 10 | diensten inschakelen; herstarten wat veranderde | |
| 11 | wifi-netwerken in `/etc/salt/grains` | A.1 |
| 12 | hotspot (`hostapd_uap0.conf`) | 4.3 |
| 13 | `scripts/verify.sh` | 9.1 |

Een paar dingen om te weten:

- **Draai het via ethernet of de hotspot.** Bij stap 11 kan wifi even wegvallen,
  en verandert de hotspot, dan herstart hostapd na afloop. Het script loopt
  door als je verbinding wegvalt; de uitvoer staat ook in
  `/var/log/eniro-obd-install-<ts>.log` (zonder wachtwoorden).
- **`verify.sh` aan het eind** doet een meting uit de auto. Staat de auto niet
  in ready of aan de lader, dan meldt hij daar een fout; dat is geen
  installatiefout.
- **`engines.conf`** wordt niet op regelnummer aangepast maar op zijn plek in
  de YAML, en vóór het wegschrijven gecontroleerd: alleen de bedoelde waarden
  mogen veranderen. Na de herstart van `salt-minion` moet `test.ping` `True`
  geven; anders zet het script de oude terug en stopt het.
- **Gaat er onderweg iets mis**, dan stopt het script met een melding. Wat het
  al deed, draai je terug met `--rollback` (6.5).

Wat `install.sh` niet doet: het wachtwoord van `pi` (4.3), 4G (hoofdstuk 5),
`/etc/hosts` (4.4), de kant thuis (1.1–1.3) en Home Assistant (hoofdstuk 7).
De tests in A.4 (handshake, negatieve test) en A.5 (een testbericht) zijn de
moeite waard om één keer met de hand te doen.

### 6.5 Terugdraaien en backups

Elk bestand dat `install.sh` overschrijft, krijgt eerst een backup
`<bestand>.bak-<ts>`. De tijdstempel staat aan het eind van de uitvoer.

```bash
sudo ./install.sh --rollback 20261027-101500       # bestanden van die run terug
sudo reboot                                         # zodat alle diensten ze inlezen
sudo ./install.sh --purge-backups 20261027-101500  # backups van die run wissen
```

De backups bevatten de oude wachtwoorden. Wis ze als alles werkt.

### 6.6 Later iets wijzigen of bijwerken

Een instelling wijzigen: `config.env` aanpassen, `--dry-run`, dan zonder. Er
verandert precies dat ene bestand, en alleen de bijbehorende dienst herstart.

Een nieuwe versie:

```bash
cd ~/eniro-obd && git pull          # of de nieuwe release-zip
sudo ./install.sh --dry-run         # welke scripts en units veranderen
sudo ./install.sh
```

Lees bij elke versie de [changelog](CHANGELOG.md). Een script dat alleen in
commentaar afwijkt van de repository blijft staan; de proefdraai meldt dat.

### 6.7 Updates: bewust niet automatisch

De dongle werkt zichzelf niet bij, en dat is zo bedoeld:

- Er staan geen apt-timers aan (`systemctl list-timers 'apt*'` geeft niets), en
  draai ook geen `apt upgrade`. De basis is Raspbian buster, dat geen reguliere
  updates meer krijgt, en bijwerken raakt de drivers voor CAN, modem en de
  Smart Power Manager.
- De firmware-updates van AutoPi komen via hun cloud, en daar is de dongle niet
  aan gekoppeld.
- De bescherming zit in de bereikbaarheid: over 4G zit de dongle achter de NAT
  van de provider, de tunnel mag alleen naar de broker, en de hotspot heeft een
  eigen sterk wachtwoord (4.3).

De scripts uit deze repository werk je zelf bij, met `install.sh` (6.6). De
enige pakketten die het installeert zijn `wireguard-tools` en
`python3-paho-mqtt`.

### 6.8 Bestaande installatie (v1.0/v1.1) overzetten

Heb je de dongle met de hand ingericht volgens een eerdere versie van deze
handleiding, dan hoef je `config.env` niet over te typen:

```bash
cd ~/eniro-obd && git pull           # of de nieuwe release-zip
sudo ./install.sh --export-config    # leest de dongle uit naar ./config.env (600)
nano config.env                      # controleren
sudo ./install.sh --dry-run          # verwacht: alleen de ontvanger bijgewerkt
```

`--export-config` verandert niets op de dongle. Het leest `/etc/autopi-ingest.env`,
`/etc/eniro-soc.env`, `/etc/wireguard/wg0.conf`, de wifi-netwerken uit
`/etc/salt/grains`, de hotspot, de waarden in `engines.conf`,
`authorized_keys` en het subnet van de SSH-regel uit `/etc/iptables/rules.v4`,
en schrijft dat naar `config.env`. Wat het niet kon vinden, meldt het met
`let op:`. Een bestaande `config.env` overschrijft het niet.

Van v1.1 naar v1.2 toont de proefdraai alleen `/opt/autopi-ingest/autopi_ingest.py`
en zijn README als gewijzigd, en een herstart van `autopi-ingest`: de ontvanger
kent nu MQTT Discovery (standaard uit). Geeft de proefdraai meer wijzigingen,
lees dan de diff: dat zijn verschillen tussen jouw dongle en wat `install.sh`
zou schrijven (bijvoorbeeld een nieuwere versie van een script). Pas als je die wilt, draai je
`sudo ./install.sh`.

**MQTT Discovery blijft bij het overzetten uit.** Je bestaande installatie
gebruikt `eniro-mqtt.yaml`; `--export-config` zet daarom `HA_DISCOVERY=0` (met
een `let op:`-regel). Wil je overstappen, volg dan de stappen in 7.2 —
in die volgorde, anders staan dezelfde sensoren twee keer in Home Assistant.

---

## 7. Home Assistant

### 7.1 Kijken wat er binnenkomt

Voordat je iets definieert:

```bash
mosquitto_sub -h <BROKER_IP> -u <gebruiker> -P '<wachtwoord>' -t 'autopi/#' -v
```

Of in Home Assistant: **Instellingen → Apparaten en diensten → MQTT → Configureren
→ Luisteren naar een topic**, met `autopi/#`.

| Topic | Van |
|---|---|
| `autopi/obd/ev/bms` | eniro-soc: SOC, spanning, stroom, temperaturen, kilometerstand |
| `autopi/track/pos` | firmware: GPS |
| `autopi/obd/bat` | firmware: 12V-accu |
| `autopi/dongle/status` | ontvanger: `online` / `offline` (last-will) |
| `autopi/wwan/boot` | wwan-teller: 4G-verbruik van de vorige boot |
| `autopi/device/diag` | autopi-diag (als je die draait) |
| `autopi/system/…`, `autopi/vehicle/…` | firmware: gebeurtenissen |

Alles is **retained**: na een herstart van Home Assistant zie je meteen de
laatste waarde, ook als die van gisteren is.

### 7.2 De sensoren en de packages

Er zijn twee soorten bestanden in `homeassistant/`:

| Bestand | Wat | Nodig |
|---|---|---|
| `discovery.json` | **alle MQTT-sensoren**, onder de apparaten **Kia e-Niro** en **AutoPi dongle**, plus de locatie-tracker. De bron van waarheid | met discovery: de dongle gebruikt hem, jij doet niets |
| `eniro-mqtt.yaml` | dezelfde sensoren als YAML-package, gegenereerd uit `discovery.json` | alleen **zonder** discovery |
| `eniro-autopi.yaml` | de helpers (profiel, accucapaciteit, laatst bekend verbruik), de actieradius en de automatiseringen (profiel naar de dongle, verbruik bewaren, twee meldingen) | ja |
| `abrp.yaml` | live telemetrie naar ABRP | optioneel, 7.7 |
| `secrets.example.yaml` | voorbeeld voor je eigen `secrets.yaml` | alleen bij ABRP — **geen package** |

#### De sensoren: MQTT Discovery (standaard)

Met `HA_DISCOVERY=1` in `config.env` (zo staat het in `config.env.example`)
publiceert de ontvanger op de dongle de inhoud van `discovery.json` als twee
*device discovery*-berichten: `homeassistant/device/kia_eniro_bms/config` en
`homeassistant/device/autopi_dongle/config`, retained. De MQTT-integratie van
Home Assistant maakt dan zelf de twee apparaten en hun entiteiten aan, met
dezelfde entity-ID's als in 7.3. Er hoeft geen YAML voor de sensoren in Home
Assistant.

- **Zuinig.** Het bericht gaat alleen weg als `discovery.json` veranderde
  (de ontvanger onthoudt een hash in `/var/lib/autopi-ingest/discovery.sha256`),
  of als Home Assistant opnieuw start (HA meldt `online` op
  `homeassistant/status`). Samen ~9 KB; niet bij elke wekbeurt.
- **Voorwaarden:** MQTT Discovery staat aan in de MQTT-integratie (de
  standaard) met prefix `homeassistant` (anders `HA_DISCOVERY_PREFIX`), en de
  MQTT-login van de dongle mag op die topics (1.2).
- Controleren: **Instellingen → Apparaten en diensten → MQTT → Apparaten**:
  *Kia e-Niro* en *AutoPi dongle*. In het journal van de dongle:
  `journalctl -u autopi-ingest | grep Discovery`.

#### De sensoren: zonder discovery

Wil je de sensoren liever in YAML, zet dan `HA_DISCOVERY=0` en laad
`eniro-mqtt.yaml` als package (hieronder). **Nooit allebei:** dan bestaan
dezelfde `unique_id`'s twee keer. Home Assistant laat de tweede dan niet toe
(een fout in het log over een unique ID die al bestaat) en welke van de twee
wint, hangt af van wat het eerst laadt.

`eniro-mqtt.yaml` wordt gegenereerd; bewerk het niet met de hand. Wijzig
`discovery.json` en draai `python3 scripts/gen_ha_yaml.py`.

#### De packages laden

Zet in `configuration.yaml` (eenmalig, als je nog geen packages gebruikt):

```yaml
homeassistant:
  packages: !include_dir_named packages
```

Heb je al een `homeassistant:`-sleutel, zet de regel `packages:` daar dan
onder. Kopieer daarna de bestanden naar `/config/packages/`:

```
/config/packages/eniro-autopi.yaml
/config/packages/abrp.yaml          (alleen als je ABRP gebruikt)
/config/packages/eniro-mqtt.yaml    (alleen zonder discovery)
```

**`secrets.example.yaml` hoort níét in `packages/`.** Daaruit kopieer je alleen
een blok naar je eigen `/config/secrets.yaml` (7.7).

Herstart Home Assistant (de eerste keer is een herstart nodig, daarna volstaat
herladen).

Liever zonder packages? Dan kan het ook; de kop van elk bestand beschrijft hoe.
De helpers en de automatiseringen uit `eniro-autopi.yaml` kun je dan via de UI
aanmaken en daar ook bewerken.

#### Overstappen van eniro-mqtt.yaml naar discovery

Voor een bestaande installatie die de sensoren uit `eniro-mqtt.yaml` (of een
eigen `mqtt:`-blok) haalt. Doe het in deze volgorde:

0. **Vooraf.** Maak een back-up van Home Assistant. Geef de MQTT-login van de
   dongle de rechten uit §1.2 (publiceren op `homeassistant/device/#`, lezen op
   `homeassistant/status`). En kijk of je al eigen entiteiten hebt met een
   entity-ID die discovery voor een **nieuwe** sensor gebruikt
   (`sensor.e_niro_kilometerstand`, `sensor.e_niro_state_of_health`,
   `binary_sensor.e_niro_contact`, enz. — de volledige lijst staat in §7.3).
   Een eigen template die bijvoorbeeld de kilometerstand vasthoudt, maakt de
   nieuwe sensor anders tot `_2`. Ruim zo'n template op en zet verwijzingen
   (ABRP, automations) na de overstap om naar de discovery-sensor.
1. **Haal de MQTT-sensoren uit je YAML.** Verwijder
   `/config/packages/eniro-mqtt.yaml`, of de betreffende regels onder `mqtt:`.
   Laat `eniro-autopi.yaml` en `abrp.yaml` staan. Herlaad via
   **Ontwikkelaarshulpmiddelen → YAML → Handmatig geconfigureerde
   MQTT-entiteiten**. De entiteiten staan daarna even als *niet meer geleverd*
   in de lijst. **Verwijder ze niet**: hun registervermelding (entity-ID, je
   aanpassingen in de UI) is precies wat straks hergebruikt wordt.
2. **Zet discovery aan op de dongle.** In `config.env`: `HA_DISCOVERY=1`, dan
   `sudo ./install.sh --dry-run` (verwacht: `/etc/autopi-ingest.env` gewijzigd,
   `/opt/autopi-ingest/discovery.json` nieuw, `autopi-ingest` herstart) en
   `sudo ./install.sh`. Bij de eerstvolgende verbinding met de broker gaan de
   twee berichten weg.
3. **Home Assistant koppelt op `unique_id`.** Een MQTT-entiteit staat in het
   entiteitenregister onder platform, integratie (`mqtt`) en `unique_id`; of
   hij uit YAML of uit discovery komt, maakt voor die sleutel niet uit. Daarom
   krijgt elke sensor zijn oude registervermelding terug: dezelfde entity-ID,
   met wat je in de UI aanpaste, en de geschiedenis loopt door (die hangt aan
   de entity-ID). `default_entity_id` speelt hierbij geen rol: met een
   `unique_id` gebruikt HA die alleen de allereerste keer.
   De apparaten *Kia e-Niro* en *AutoPi dongle* hebben dezelfde
   `identifiers` als in YAML; HA hangt de entiteiten aan hetzelfde apparaat in
   het apparatenregister (gebied en naam blijven), dat er nu een
   softwareversie en de herkomst *eniro-obd* bij krijgt.
4. **Controleer** in Instellingen → Entiteiten dat er geen `_2`-varianten zijn
   ontstaan en dat de entiteiten niet meer *niet meer geleverd* zijn.

Dat discovery een YAML-entiteit met dezelfde `unique_id` zo overneemt, volgt
uit hoe het entiteitenregister werkt; de Home Assistant-documentatie beschrijft
deze overstap niet letterlijk. Maak voor de zekerheid eerst een back-up van
Home Assistant. Terug naar YAML: `HA_DISCOVERY=0`, `install.sh`, in HA de twee
apparaten verwijderen (dat haalt ook de retained discovery-berichten van de
broker) en `eniro-mqtt.yaml` weer laden.

Had je de YAML-sensoren zelf aangepast (een andere template, eenheid of
naam in YAML)? Discovery gebruikt wat in `discovery.json` staat. Neem je
aanpassingen daarin over vóór stap 2, of doe ze daarna via de UI (zie
hieronder).

#### Een sensor aanpassen

- **Naam, entity-ID, icoon, gebied, uitzetten:** gewoon in de UI van Home
  Assistant (klik op de entiteit → tandwiel). Dat blijft bewaard, ook als de
  dongle het discovery-bericht opnieuw stuurt.
- **Template, eenheid, topic, `device_class`:** in `discovery.json`. Daarna
  op de dongle `sudo ./install.sh` (dat installeert het nieuwe bestand en
  herstart de ontvanger, die het dan publiceert). Zonder discovery: daarna
  `python3 scripts/gen_ha_yaml.py` en het nieuwe `eniro-mqtt.yaml` laden.
  Let op: een andere eenheid geeft in de langetermijnstatistieken een melding
  die je in Ontwikkelaarshulpmiddelen → Statistieken oplost.
- **Een `unique_id` verander je nooit**: dan maakt Home Assistant een nieuwe
  entiteit, zonder geschiedenis.
- **Een sensor weghalen:** laat in `discovery.json` onder die `unique_id`
  alleen `"platform": "sensor"` staan (of het platform dat hij had) en werk de
  dongle bij; Home Assistant verwijdert de entiteit dan. Daarna mag het item
  er helemaal uit.
- **Een apparaat in de UI verwijderen** werkt maar tot de volgende herstart van
  Home Assistant: dan stuurt de dongle het bericht opnieuw. Wil je het echt
  kwijt, zet dan `HA_DISCOVERY=0`.

### 7.3 Sensoren en entity-ID's

Elke entiteit heeft een vaste `default_entity_id`. Bij een verse installatie
krijg je precies die ID's, en de automatiseringen en de ABRP-template rekenen
erop. De belangrijkste:

| Entity-ID | Wat |
|---|---|
| `sensor.e_niro_soc` | SOC zoals het dashboard hem toont; alle velden van het BMS-record als attributen, inclusief `@ts` |
| `sensor.e_niro_soc_bms` | SOC volgens de BMS |
| `sensor.e_niro_accuvermogen` | positief = laden |
| `sensor.e_niro_accuspanning`, `sensor.e_niro_accustroom` | |
| `sensor.e_niro_accutemperatuur`, `sensor.e_niro_accutemperatuur_max` | |
| `sensor.e_niro_12v_boordspanning`, `sensor.e_niro_celafwijking` | |
| `sensor.e_niro_totaal_geladen`, `sensor.e_niro_totaal_verbruikt` | cumulatief, kWh |
| `sensor.e_niro_kilometerstand` | gemeten tijdens rijden; daartussen de laatst bekende stand van eniro-soc (attribuut `odometer_bron` van `sensor.e_niro_soc`: `cluster` of `opgeslagen`) |
| `sensor.e_niro_verbruik_100_km` | netto, na de eerste 100 km; daartussen het laatst berekende verbruik |
| `sensor.e_niro_actieradius` | SOC × bruikbare capaciteit / verbruik (uit `eniro-autopi.yaml`); valt terug op `input_number.e_niro_verbruik_laatst_bekend` als het verbruik na een herstart nog onbekend is |
| `sensor.e_niro_state_of_health` | |
| `binary_sensor.e_niro_laadt`, `binary_sensor.e_niro_snelladen`, `binary_sensor.e_niro_contact` | *laadt* = snelladen, of meer dan 1 A de accu in met het contact uit (dezelfde definitie als eniro-soc). *laadt* en *snelladen* worden onbeschikbaar na 15 minuten zonder BMS-record: tijdens laden komt er minstens elke 5 minuten een, dus stilte betekent dat de stand onbekend is, niet dat het laden gestopt is |
| `device_tracker.e_niro_locatie` | GPS van de dongle (7.4) |
| `sensor.autopi_12v_spanning`, `sensor.autopi_12v_niveau`, `sensor.autopi_12v_laadstatus` | 12V-accu, gemeten door de dongle |
| `sensor.autopi_laatste_meting` | tijdstip van de laatste 12V-meting |
| `binary_sensor.autopi_dongle_online` | of de dongle wakker en verbonden is |
| `sensor.autopi_4g_signaal`, `sensor.autopi_4g_ruisverhouding`, `sensor.autopi_netwerk` | van autopi-diag |
| `sensor.autopi_verbinding`, `sensor.autopi_wifi_netwerk`, `sensor.autopi_wifi_signaal` | van autopi-diag (v1.3): wifi/4G/geen, netwerknaam en RSSI (dBm; frequentie, band en linksnelheid als attributen). Op wifi elke 10 minuten bijgewerkt, op 4G alleen bij de wissel |

**Controleer na het laden in Instellingen → Entiteiten of ze zo heten.**
Bestaat een ID al (bijvoorbeeld van een eerdere poging, of van een integratie
die een entiteit met dezelfde naam heeft), dan maakt Home Assistant er
stilletjes een `_2`-variant van. De automatiseringen en de ABRP-template vinden
de bedoelde sensor dan niet, en `| float(0)` stuurt zonder foutmelding een 0.
Verwijder de oude entiteit of pas de verwijzingen aan.

Twee dingen om te weten:

- **Geen `availability_topic`, met opzet.** De last-will van de ontvanger gaat
  bij elke slaap op `offline`. Als availability zou dat de SOC onbeschikbaar
  maken zodra de auto geparkeerd staat. Nu blijft de laatste waarde staan.
  - Of de dongle wakker is: `binary_sensor.autopi_dongle_online`. Netjes
    slapen = meteen `offline`; spanning abrupt weg = `offline` na ~7,5 minuut
    (1,5 × de keepalive van 300 s).
  - Hoe oud een meting is: het attribuut **`@ts`** van `sensor.e_niro_soc`. Dat
    is UTC zonder tijdzone; in een template:
    `as_timestamp(state_attr('sensor.e_niro_soc', '@ts') ~ '+00:00')`.
- **De kilometerstand wordt alleen tijdens rijden gemeten.** Daartussen
  (laden, stilstand) stuurt eniro-soc de laatst bekende stand en het laatst
  berekende verbruik mee uit zijn verbruiksgeschiedenis, met
  `odometer_bron: opgeslagen` in plaats van `cluster`. Het retained bericht
  heeft ze dus altijd, en beide sensoren hebben ook na een herstart van Home
  Assistant meteen een waarde. Alleen vóór de eerste rit met contact aan (nog
  geen geschiedenis op de dongle) staan ze op `unknown`.
- **Verbruik pas na 100 km.** Tot die tijd is `sensor.e_niro_verbruik_100_km`
  `unknown`. De actieradius rekent dan met
  `input_number.e_niro_verbruik_laatst_bekend`, die een automatisering bij elke
  geldige waarde bijwerkt; vul de helper desgewenst één keer met de hand (bv.
  150 Wh/km).

### 7.4 Locatie

De tracker `device_tracker.e_niro_locatie` hoort bij de sensoren uit 7.2.
**Controleer wel hoe de payload op `autopi/track/pos` eruitziet** (met 7.1) —
de veldnamen kunnen per firmwareversie verschillen. Op v1.22.9 staat de positie
in `loc.lat` / `loc.lon`, met `alt`, `sog` (snelheid in **km/h**, niet in
knopen — gecontroleerd op 286 metingen), `cog` (koers), `fix` en het aantal
satellieten. Heten de velden bij jou anders, pas dan de templates in
`discovery.json` aan (7.2, *Een sensor aanpassen*).

De tracker staat op `home` als de auto binnen 100 m van `zone.home` staat.

### 7.5 Profielen: dagelijks en reis

Voor dagelijkse ritjes is live data overbodig, en op 4G kost elk wakker uur
data (~0,4 MB per wakker uur zonder profielen). Met de helper
**AutoPi profiel** (`input_select.autopi_profiel`, uit `eniro-autopi.yaml`)
kies je:

| Profiel | Op 4G | Op wifi |
|---|---|---|
| **Dagelijks** (standaard) | per topic alleen de laatste stand, elke 30 minuten in één keer en bij inslapen; laadrecords, het eerste record na begin of einde van laden en `vehicle/battery/critical_level` altijd direct | alles live |
| **Reis** | alles live — voor een lange rit met ABRP | alles live |

De automatisering **AutoPi - profiel naar dongle** zet de keuze retained op
`autopi/config/profiel`. De ontvanger op de dongle leest hem bij elke
(her)verbinding en onthoudt hem op schijf, zodat hij na een boot meteen klopt.

Let op:

- **De automatisering publiceert alleen bij een wissel of bij het starten van
  Home Assistant.** Heb je de package herladen in plaats van Home Assistant te
  herstarten, wissel het profiel dan één keer heen en terug.
- **De MQTT-login van de dongle moet `autopi/config/profiel` mogen lezen**
  (1.2). Anders blijft de dongle op `PROFIEL_STANDAARD` staan.

Controleren op de dongle:

```bash
sudo journalctl -u autopi-ingest | grep Profiel   # "Profiel: dagelijks → reis" bij een wissel
sudo cat /var/lib/autopi-ingest/profiel           # het onthouden profiel (alleen met sudo leesbaar)
```

### 7.6 Meldingen in plaats van spraak

De spraakmeldingen staan uit (A.3.2); `eniro-autopi.yaml` bevat twee
automatiseringen die er een melding van maken:

| Automatisering | Topic | Wanneer |
|---|---|---|
| AutoPi - geen 4G | `autopi/system/network/wwan0/offline` | pas als het vijf minuten aanhoudt; verdwijnt als de verbinding terugkomt |
| AutoPi - update mislukt | `autopi/system/release/failed` | altijd |

Er is bewust **geen** melding voor `vehicle/battery/critical_level` (v1.0–v1.1 hadden
die wel). Dat event betekent dat de dongle zichzelf uitschakelt om de 12V-accu te
sparen: hij doet wat hij moet doen, en wordt vanzelf weer wakker op spanning of
timer. De 12V-spanning staat in `sensor.autopi_12v_spanning` als je wilt meekijken.

Ze maken een persistent notification (de bel in de zijbalk). Wil je ook een
pushmelding, voeg dan je eigen notify-actie toe; de kop van het bestand laat
zien hoe.

Omdat alles retained is, vuurt een MQTT-trigger ook op een oud bericht zodra
Home Assistant zich abonneert. Daarom vergelijken de automatiseringen het veld
`@ts` uit de payload met nu: ouder dan een uur geeft geen melding. Bouw je zelf
iets op deze topics, doe dan hetzelfde.

### 7.7 ABRP — optioneel

A Better Routeplanner kan live telemetrie ontvangen: SOC, vermogen, spanning,
accutemperatuur, laden en snelladen, snelheid, hoogte, koers, positie en
kilometerstand. ABRP gebruikt dat om zijn verbruiksmodel op jouw auto te
kalibreren. **Alle gegevens komen van de dongle**, ook de kilometerstand
(`sensor.e_niro_kilometerstand`) en de positie (`device_tracker.e_niro_locatie`);
een fabrieksintegratie of cloudkoppeling van de auto is niet nodig.

**Stap 1 — sleutels.** Je hebt een **API-key** nodig (ABRP-ontwikkelaarsportaal,
hoort bij een applicatie) en een **token** (in de ABRP-app bij je voertuig,
onder de live-data-instellingen). Twee verschillende dingen.

**Stap 2 — capaciteit.** De helper **e-Niro bruikbare capaciteit**
(`input_number.e_niro_bruikbare_capaciteit`) staat in `eniro-autopi.yaml`, op
**70 kWh**. Drie onafhankelijke metingen aan deze auto gaven ~70 kWh, niet de
64 van de fabrieksopgave. `capacity` en `soe` rekenen er allebei mee. Omdat de
helper in YAML staat met `initial: 70`, pas je een andere waarde aan in het
bestand; maak je hem via de UI, dan onthoudt hij zelf zijn waarde.

**Stap 3 — secret.** Kopieer het blok `abrp_kia_niro_telemetry_url:` uit
`secrets.example.yaml` naar je eigen `/config/secrets.yaml` en vervang
`JOUW_API_KEY` en `JOUW_TOKEN`.

Waarom de hele URL-template in `secrets.yaml` staat: key en token moeten in de
URL, en een `!secret` kan niet midden in een template. Maar Home Assistant vult
secrets in *voordat* de template wordt uitgerekend. Door de complete template
als secret op te slaan, staan je sleutels nergens in je configuratie.

**Stap 4 — package.** Kopieer `abrp.yaml` naar `/config/packages/` en herstart.
De triggers in de automatisering **ABRP - e-Niro telemetrie** zijn een voorstel;
de **eerste conditie niet**. Zonder die guard maakt `| float(0)` van een
`unavailable`-sensor een SOC van 0%, en dat gebeurt bij elke herstart van Home
Assistant. ABRP gooit dan je routeplanning om.

De template stuurt de kilometerstand alleen mee als de sensor een getal heeft,
en de positie alleen als de dongle er een heeft gepubliceerd — anders zou ABRP
0 km of een positie op 0,0 krijgen.

**Rij je met ABRP, zet dan het profiel op Reis** (7.5). In profiel dagelijks
komen de gegevens op 4G maar eens per 30 minuten binnen.

Controleren: **Ontwikkelaarshulpmiddelen → Acties →
`rest_command.update_abrp_kia_niro`** uitvoeren. In de ABRP-app moet bij je
voertuig bron "Api" en "< 1 min geleden" verschijnen, met een kilometerstand
die klopt — niet 0. Meldt ABRP `Missing telemetry: speed`, dan heeft de tracker
nog geen positie.

Eén bewuste keuze: **`est_battery_range` gaat níét mee.** ABRP rekent zelf met
snelheid, hoogteprofiel, temperatuur en wind; de schatting van de auto
voorspelt het werkelijke verbruik slecht.

> **Plak key, token of de secret-URL nooit in een chat of forum.** Een URL
> wordt daar vaak automatisch een link, en die link bevat de volledige waarde,
> ook als de zichtbare tekst ingekort is. Zo is het bij de bouw van dit project
> drie keer misgegaan.

---

## 8. Inbouwen in de auto

### 8.1 De OBD-poort

Die zit bij de e-Niro links onder het stuur, achter het onderpaneel van het
dashboard, en wijst naar beneden. De dongle is te groot om achter het
standaardklepje te blijven zitten; vandaar de verlengkabel.

**De verlengkabel:** alle 16 pinnen doorverbonden (minimaal 16 voor +12V, 4 en
5 voor massa, 6 en 14 voor CAN), haaks, en zo kort als de route toelaat. Een
lange dunne lintkabel geeft bij ~2 A merkbaar spanningsval. Een versie met
schakelaar is handig: dan kun je de dongle bij lange stilstand spanningsloos
maken zonder onder het dashboard te kruipen.

### 8.2 Het onderpaneel eraf

Uit de werkplaatshandleiding van de Niro
(https://www.kniro.net/crash_pad_lower_panel_repair_procedures-1443.html):

1. Wip het **zijkapje** los aan de linkerkant van het dashboard (zichtbaar met
   het portier open). Kunststof spatel, geen schroevendraaier.
2. Draai de schroef los die daarachter zit.
3. Trek het onderpaneel naar je toe; de rest zit op clips.
4. Koppel de connector van het tankklepje los.

Terugplaatsen in omgekeerde volgorde. Clips breken vaak bij de eerste keer;
vervang ze in plaats van ze terug te duwen.

### 8.3 Waar je van afblijft

- **De knie-airbag.** De Niro heeft er een aan bestuurderszijde, achter dit
  paneel. Leg de kabel niet over de module of door het ontvouwingsgebied, en
  klem niets tussen paneel en dwarsbalk.
- **De stuurkolom en de stuurbekrachtiging.** Achter het paneel zit de
  MDPS-regeleenheid (een lichtblauw kastje met een MOBIS-sticker) aan de
  stuurkolom, met een dikke bundel en blauwe en gele connectoren. Geen tiewraps
  om de kolom of die bundel.
- **De pedalen.** Niets dat los kan raken en eronder kan schuiven.

Volg de bestaande kabelbundel richting de zekeringkast en zet vast met
klittenband of tiewraps **aan die bundel**.

### 8.4 Plaats van de dongle

De dongle heeft interne antennes voor 4G en GPS. Een plek hoger en met meer
"zicht" naar boven geeft een snellere GPS-fix en beter 4G-signaal. Niet achter
een metalen paneel wegwerken. Met `autopi-diag` (A.12) kun je een plek een paar
dagen meten voordat je hem definitief vastzet.

---

## 9. Controleren

### 9.1 Op de dongle

Met de auto in ready of aan de lader:

```bash
sudo ~/eniro-obd/scripts/verify.sh
```

Controleert in een halve minuut, zonder iets te versturen:

- of `eniro_soc.py` er staat, of `can0` omhoog is en of de decodering klopt
  (`--selftest`);
- één echte meting uit de auto (`--once --dry-run`);
- of de diensten en timers aan staan: `eniro-soc`, `autopi-ingest`,
  `autopi-wakelock.timer`, `autopi-diag.timer`, `wwan-teller.timer`,
  `wg-quick@wg0`, `wg-reresolve.timer` (wat niet geïnstalleerd is, slaat hij
  over);
- waar `eniro-soc` en de firmware naartoe posten (lokaal of extern, zonder het
  pad te tonen);
- of de lokale ontvanger de broker bereikt (`/healthz`: `200`, of `503` als de
  tunnel weg is);
- of `wg0` er is en wanneer de laatste handshake was.

Draai je de ontvanger op een server (bijlage B), gebruik dan
`sudo ONTVANGER= ~/eniro-obd/scripts/verify.sh`.

### 9.2 De hele keten

- [ ] `sudo journalctl -u eniro-soc -f` op de dongle: `Verstuurd: … record(s)`
- [ ] `sudo journalctl -u autopi-ingest -f` op de dongle: `record(s) verwerkt`
- [ ] `sudo wg show wg0 latest-handshakes`: een recente tijdstempel
- [ ] `autopi/obd/ev/bms` verschijnt op de broker
- [ ] `binary_sensor.autopi_dongle_online` staat op aan
- [ ] De SOC in Home Assistant klopt met het dashboard van de auto (±1%)
- [ ] Wifi uit (of weg van huis): de data blijft komen over 4G, in profiel
      dagelijks gebundeld per 30 minuten
- [ ] Een laadsessie: de SOC loopt vloeiend op, minimaal elke 5 minuten een
      meting, zonder gaten van uren; na het laden gaat de dongle binnen een
      half uur slapen
- [ ] Profiel wisselen in Home Assistant: `Profiel: … → …` in het journal van
      `autopi-ingest`
- [ ] Na een slaap: `/var/log/syslog` bevat de vorige boot tot het moment van
      uitschakelen (`systemd[1]: Stopping …`), zie 10.2
- [ ] `FORCE 5V`-jumper eraf, `CM4 Flash`-jumper eraf

### 9.3 Dataverbruik

Gemeten met deze configuratie (ontvanger op de dongle, WireGuard, profiel
dagelijks): **0,02–0,11 MB per dag, gemiddeld ~0,06 MB per dag**. Een
nachtlading van zeven uur kostte 0,058 MB. Met 500 MB is het datavolume daarmee
geen beperking meer. Ter vergelijking: met de ontvanger op een server achter
HTTPS was het 2-8 MB per dag.

MQTT Discovery (7.2) voegt daar weinig aan toe: ~9 KB als `discovery.json`
verandert of Home Assistant herstart, en per verbinding een abonnement op
`homeassistant/status` van een paar tientallen bytes.

Kijk zelf mee:

```bash
sudo python3 /opt/wwan-teller/wwan_teller.py --rapport
```

Dat geeft het verbruik per dag (UTC, op de startdatum van de boot) en de
laatste 15 boots. Zie je een uitschieter, kijk dan in
`/var/lib/wwan-teller/pieken.log`: daar staan de open verbindingen van elke
periode van meer dan 2 MB in 2 minuten.

Houd het verbruik de eerste weken ook in het 1NCE-portaal in de gaten (de
grafiek daar is in MB). Ook thuis op wifi gaat er een klein beetje over 4G
(achtergrondverkeer van het modem); dat is normaal.

---

## 10. Als het niet werkt

### 10.1 Symptomen

| Symptoom | Oorzaak | Oplossing |
|---|---|---|
| rpiboot blijft op `Waiting for BCM…` | jumper niet goed tijdens power-on, of een laad-only USB-kabel | spanning eraf, jumper controleren, andere kabel, opnieuw |
| Dongle boot niet na het flashen | `CM4 Flash`-jumper zit er nog op | eraf |
| Dongle valt na een paar minuten uit | sleep-timer (4 min) of `battery_critical_level` | 4.2; auto in ready |
| Dongle gaat ~16 minuten na elke boot uit (~61 bij `period: 3600`), ook tijdens laden | de reactor-timer (`inactivity_fallback`, `period` uit `INACTIVITY_PERIOD`) | `autopi-wakelock` (A.10); controleer `journalctl -u autopi-wakelock` |
| Dongle slaapt na een lading niet meer | wakelock liet los zonder een timer terug te zetten (oude versie) | huidige `autopi_wakelock.py` installeren |
| SSH over wifi: time-out, ping werkt ook niet | geen firewallregel voor `wlan0` | A.2 |
| SSH over wifi traag, pings van ~2000 ms | hotspot en wifi-client delen één radio | ethernetkabel voor langer werk; trage toegang die werkt is beter dan de hotspot uitzetten |
| `sudo` duurt seconden | hostnaam niet in `/etc/hosts` | 4.4 |
| `salt-call` blijft hangen | wacht op een Enter na een ATTENTION-melding over de sleep timer | Enter |
| `salt-call state.sls` toont wifi-wachtwoorden | de diff van `wpa_supplicant.conf` | `--state-output=terse` (A.1); uitvoer niet delen |
| 4G: `option-unsubscribed`, cause #33 | SIM niet geactiveerd, of lege APN met IPv4v6 in de attach-profile | SIM activeren in het portaal; 5.2 |
| 4G: modem valt elke ~4 minuten van de USB-bus | modem staat op 2G (`PS: detached`), `qmi-manager` reset het steeds | 5.2, `AT+WS46=28` |
| AT-commando's geven `CME: operation not allowed` | modem in power save na het stoppen van `qmi-manager` | `qmicli -d /dev/cdc-wdm0 --dms-set-operating-mode=online` |
| `AT+CSQ` geeft `99,99` | idem — radio staat uit, niet "geen bereik" | idem |
| `qmi-manager` logt "Successfully set operator selection to automatic" maar het klopt niet | bekende onjuiste melding op dit modem | zelf controleren met `AT+COPS?` |
| `wg show`: handshake `0` | er is nog geen verkeer geweest, of Endpoint, sleutel of firewall thuis klopt niet | eerst verkeer opwekken (A.4); dan 10.4 |
| `wg0` ontbreekt na een boot | `wg-quick` startte voordat er netwerk of DNS was | `wg-reresolve.timer` brengt hem binnen 5 minuten op; `journalctl -t wg-reresolve` |
| Tunnel werkt op 4G, niet thuis op wifi | router doet geen hairpin NAT | lokale DNS-override voor `<VPN_HOSTNAAM>` (A.4) |
| Tunnel werkte, na een tijd niet meer | thuis-IP veranderd, endpoint niet opnieuw opgezocht | `wg-reresolve` (A.4); DDNS-record op alleen DNS? |
| Ontvanger geeft `503`, `/healthz` ook | broker niet bereikbaar | `sudo wg show`; broker, login, wachtwoord in `/etc/autopi-ingest.env`; de dongle probeert het later opnieuw |
| Ontvanger: `Connection refused` / not authorised | MQTT-login of ACL | 1.2 |
| Upload geeft `404` | pad in `engines.conf` of `/etc/eniro-soc.env` wijkt af van `INGEST_PATH` | alle drie gelijk maken |
| Profielwissel komt niet aan | automatisering heeft nog niet gepubliceerd, of de login mag het topic niet lezen | profiel heen en terug wisselen; ACL (1.2); 7.5 |
| eniro-soc: `OSError 100 (ENETDOWN)` | `can0` staat DOWN — de firmware brengt hem niet zelf omhoog | het script doet het zelf (`CAN_AUTO_UP=1`); staat dat uit, zet het aan |
| SOC valt midden in een lading weg tot de volgende boot | CAN-controller in bus-off, zonder automatisch herstel | 10.3 |
| eniro-soc: geen antwoord van de BMS | auto staat uit | ready of lader; met het contact uit slaapt de bus |
| `candump can0` laat niets zien | de diagnostische bus zwijgt tot je hem aanspreekt | geen fout; `--once --dry-run` is de echte test |
| AutoPi's eigen `obd.*`-commando's: `Not supported by SocketCAN interface` | die laag hoort bij een andere hardware-uitvoering | niet nodig; eniro-soc praat rechtstreeks met SocketCAN |
| SOC loopt tijdens laden in sprongen op | dongle slaapt tijdens het laden | `autopi-wakelock` (A.10) |
| Sensoren verschijnen niet met `HA_DISCOVERY=1` | login mag niet op `homeassistant/device/#` publiceren, ander discovery-prefix in HA, of de dongle was nog niet verbonden | ACL (1.2), `HA_DISCOVERY_PREFIX`, `journalctl -u autopi-ingest \| grep Discovery`; 7.2 |
| Log: unique ID bestaat al; sensoren half of dubbel | `eniro-mqtt.yaml` (of eigen YAML) en discovery tegelijk actief | één van de twee weghalen; 7.2 |
| Sensoren in HA onbeschikbaar zodra de auto stilstaat | een `availability_topic` op `autopi/dongle/status` of `autopi/status` | weghalen; zie 7.3 |
| ABRP krijgt SOC 0% of kilometerstand 0 | entity-ID met `_2`, of een sensor die `unavailable` is | 7.3; de conditie in `abrp.yaml` laten staan |
| HA-melding over iets van vorige week na een herstart | retained bericht zonder `@ts`-controle | 7.6 |
| `journalctl` toont vorige boots van maar ~30 s | bekende beperking van journald op deze dongle | kijk in `/var/log/syslog` (10.2, A.7) |
| `journalctl -b -1` laat de verkeerde boot zien | eerste kernelregels met een tijdstempel uit 2019 | kijk in `/var/log/syslog` (10.2) |
| `engines.conf` gewijzigd, salt-minion start niet | YAML-fout, meestal inspringing van de URL | backup terugzetten, A.3.5 |
| Ineens veel meer 4G-verbruik | onbekend proces | `wwan_teller.py --rapport` en `pieken.log` (9.3) |

### 10.2 Logs van een vorige boot

**Kijk voor eerdere boots in `/var/log/syslog`**, niet in `journalctl`. Op deze
dongle bewaart journald van een vorige boot alleen de eerste ~30 seconden (zie
A.7); rsyslog schrijft dezelfde regels naar platte tekst en die overleven een
slaap wél, tot en met het uitschakelen.

```bash
sudo grep -E 'eniro-soc|autopi-ingest|autopi-wakelock' /var/log/syslog | tail -40
sudo grep -E 'eniro-soc|autopi-ingest|autopi-wakelock' /var/log/syslog.1 | tail -40   # gisteren
sudo zgrep -iE 'sleep|hibernat' /var/log/syslog.2.gz | tail -20        # eergisteren
sudo grep 'Nov  3 09:4' /var/log/syslog.1                              # een tijdvenster
```

De diensten loggen onder hun eigen naam (`SyslogIdentifier` in de units), dus
`eniro-soc[…]` in plaats van `python3[…]`. De tijden in syslog staan in UTC. logrotate draait dagelijks en bewaart zeven
dagen (`syslog.1`, daarna `.2.gz` enzovoort).

Voor de **huidige** boot is `journalctl` prima:

```bash
sudo journalctl -b 0 -u eniro-soc -u autopi-ingest -u autopi-wakelock --no-pager
sudo journalctl -b 0 -k | grep -i can                 # kernelmeldingen over CAN
```

Hoe lang een boot echt duurde, zie je onafhankelijk van de logs in
`sudo python3 /opt/wwan-teller/wwan_teller.py --rapport`.

### 10.3 CAN in bus-off

Valt de SOC midden in een lading weg, terwijl de 12V laat zien dat er nog
geladen wordt, en gaat de dongle daarna slapen: kijk naar de CAN-controller.

```bash
ip -details link show can0
```

- `state ERROR-ACTIVE` — normaal.
- `state ERROR-PASSIVE` — normaal als de auto slaapt (niemand bevestigt onze
  vragen). Geen reden tot zorg.
- `state BUS-OFF` — de controller is van de bus gegaan.
- `restart-ms 0` — na een bus-off komt hij **nooit** vanzelf terug, tot iemand
  de interface down en up zet.

`eniro-soc` zet `restart-ms` (`CAN_RESTART_MS=100`) elke keer dat het `can0`
opbrengt, en brengt hem opnieuw op als hij `BUS-OFF` staat of `restart-ms 0`
heeft. In het journal staat dan:

```
can0 staat BUS-OFF — opnieuw opbrengen op 500000 bit/s, restart-ms 100
```

Met de hand herstellen kan ook:

```bash
sudo ip link set can0 down
sudo ip link set can0 up type can bitrate 500000 restart-ms 100
```

### 10.4 Geen WireGuard-handshake

Zonder `PersistentKeepalive` komt er pas een handshake als de dongle iets
verstuurt. Wek eerst verkeer op, kijk dan pas:

```bash
timeout 5 bash -c '</dev/tcp/<BROKER_IP>/1883' && echo "broker open"
sudo wg show wg0
```

Blijft `latest handshake` weg, loop dan na:

1. **Endpoint.** `getent hosts <VPN_HOSTNAAM>` op de dongle moet je huidige
   thuis-IP geven. Een proxied DNS-record geeft het adres van de proxy: zet het
   op alleen DNS.
2. **Sleutels.** De `PublicKey` in `[Peer]` is die van de server, de
   `PrivateKey` in `[Interface]` die van deze client. Een `PresharedKey` moet
   aan beide kanten gelijk zijn, of aan beide kanten ontbreken.
3. **Poort.** De UDP-poort van de server moet vanaf internet bereikbaar zijn
   (op een router met ingebouwde WireGuard-server meestal vanzelf).
4. **Thuis op wifi?** Test vanaf 4G (wifi uit). Werkt het daar wel, dan is het
   hairpin NAT (A.4).
5. **Handshake wel, broker niet bereikbaar?** Dan is het de firewall thuis
   (1.3) of `AllowedIPs` op de dongle.

`sudo ~/eniro-obd/scripts/verify.sh` doet de controles op handshake en broker
in één keer.

---

## 11. Naslag en doorverkopen

### Sleep-timers

```bash
sudo salt-call --local power.sleep_timer              # bekijken
sudo salt-call --local power.sleep_timer clear='*'    # wissen
sudo systemctl restart salt-minion                    # standaard terugzetten
```

### Geforceerd slapen, om te testen

```bash
sudo autopi power.sleep interval=120 confirm=true reason=test
```

De dongle gaat uit en is na een minuut of vier terug. Handig om het journal
(A.7), de wakelock of het herverbinden van de tunnel te testen.

### Waarom werd hij wakker, waarom ging hij slapen

```bash
sudo salt-call --local spm.query status
```

`last_trigger: up: volt_change` betekent: wakker geworden van een
spanningsverandering (laadkabel erin, contact aan).

### 4G

```bash
sudo qmicli -d /dev/cdc-wdm0 --nas-get-serving-system        # registratie, operator, RAT
sudo qmicli -d /dev/cdc-wdm0 --nas-get-signal-info           # RSRP, RSRQ, SNR
sudo journalctl -u qmi-manager -n 40 --no-pager
sudo python3 /opt/wwan-teller/wwan_teller.py --rapport       # verbruik per dag en per boot
```

Vuistregel voor RSRP: beter dan −100 dBm is goed, −100 tot −110 is werkbaar,
slechter dan −110 wordt wankel.

### WireGuard

```bash
sudo wg show wg0                         # peer, endpoint, laatste handshake, verkeer
journalctl -t wg-reresolve -n 10 --no-pager
systemctl status wg-quick@wg0 --no-pager
```

### Diensten

```bash
systemctl status eniro-soc autopi-ingest --no-pager
systemctl list-timers 'autopi-*' wwan-teller.timer wg-reresolve.timer
sudo journalctl -u eniro-soc --since '1 hour ago' --no-pager
sudo journalctl -u autopi-ingest --since '1 hour ago' --no-pager
```

### Bestanden op de dongle

| Pad | Wat | Geheim |
|---|---|---|
| `~/eniro-obd/config.env` | alle instellingen voor `install.sh` | **ja**, alle wachtwoorden en sleutels |
| `<bestand>.bak-<ts>` | backups die `install.sh` maakte voor het overschrijven | **ja**, de oude waarden |
| `/var/lib/eniro-obd-install/` | per run de lijst van wat `install.sh` veranderde (voor `--rollback`) | nee |
| `/var/log/eniro-obd-install-<ts>.log` | uitvoer van `install.sh` | nee |
| `/etc/salt/minion.d/engines.conf` | firmwareconfig, `url:` naar de ontvanger | nee (lokaal pad) |
| `/etc/salt/minion.d/engines.conf.orig` | eenmalige backup van de fabrieksversie | ja, het AutoPi-token |
| `/etc/salt/grains` | wifi-netwerken | **ja**, wifi-wachtwoorden |
| `/etc/hostapd/hostapd_uap0.conf` | de eigen hotspot | **ja**, hotspot-wachtwoord |
| `/etc/autopi-ingest.env` | ontvanger | **ja**, MQTT-wachtwoord |
| `/etc/eniro-soc.env` | eniro-soc, ook gelezen door autopi-diag | nee (bij de servervariant wel: het pad) |
| `/etc/wireguard/wg0.conf` | tunnel | **ja**, privésleutel |
| `/usr/local/sbin/wg-reresolve.sh` | endpoint opnieuw opzoeken | nee, wel je hostnaam |
| `/etc/systemd/journald.conf.d/persistent.conf` | persistent journal | nee |
| `/var/lib/eniro-soc/` | wachtrij, verbruiksgeschiedenis, `status.json` | persoonlijk (kilometerstanden) |
| `/var/lib/autopi-ingest/profiel` | het onthouden profiel | nee |
| `/opt/autopi-ingest/discovery.json` | de HA-entiteiten voor MQTT Discovery (alleen met `HA_DISCOVERY=1`) | nee |
| `/var/lib/autopi-ingest/discovery.sha256` | hash van het laatst gepubliceerde discovery-bericht | nee |
| `/var/lib/wwan-teller/` | 4G-verbruik per boot, piek-log | persoonlijk |
| `/var/lib/autopi-diag/` | diag-buffer | persoonlijk (cel-ID's) |
| `/var/log/journal/` | het persistente journal | persoonlijk (GPS, SSID's) |
| `/var/log/syslog*` | dezelfde logregels als platte tekst (rsyslog) | persoonlijk (GPS, SSID's) |

### De CAN-adressen

| Module | Verzoek → antwoord | Wat |
|---|---|---|
| BMS | `7E4` → `7EC`, service 22, PID's `0101` en `0105` | SOC, spanning, stroom, temperaturen, energie, SOH |
| Instrumentencluster | `7C6` → `7CE`, DID `B002` | kilometerstand (alleen met contact aan) |

Alle formules en meetwaarden: `docs/pids-e-niro.md`.

### Alles terugdraaien

- Eén run van `install.sh`: `sudo ~/eniro-obd/install.sh --rollback <ts>` (de
  tijdstempel staat aan het eind van de uitvoer en in de naam van de log), dan
  `sudo reboot`.
- `engines.conf`: `sudo cp /etc/salt/minion.d/engines.conf.orig /etc/salt/minion.d/engines.conf && sudo systemctl restart salt-minion`
- De diensten: `sudo systemctl disable --now eniro-soc autopi-ingest autopi-wakelock.timer autopi-diag.timer wwan-teller.timer wg-reresolve.timer wg-quick@wg0`
- Alles: opnieuw flashen (hoofdstuk 3). Dat overschrijft de hele eMMC.

### De dongle doorverkopen

`scripts/wipe.sh` haalt weg wat over jou gaat: wifi-wachtwoorden, de
WireGuard-sleutels, het MQTT-wachtwoord, je eigen endpoint, meetdata, het
journal en de rsyslog-logs, shell-geschiedenis en SSH-sleutels, en de `config.env` en
de backups van `install.sh`. Het zet de firmware-url
terug op de lokale ontvanger en geeft de hotspot een nieuw wachtwoord.

```bash
sudo ~/eniro-obd/scripts/wipe.sh            # proefdraai: laat zien wat er zou gebeuren
sudo ~/eniro-obd/scripts/wipe.sh --yes      # voert het uit - niet terug te draaien
```

Schrijf het nieuwe hotspot-wachtwoord dat het script toont op een kaartje dat
meegaat; het staat nergens anders. Daarna met de hand:

- `passwd pi` — een nieuw wachtwoord, ook op het kaartje;
- de SIM eruit — die hangt aan jouw account;
- op je VPN-server de WireGuard-client van de dongle **intrekken**;
- op je broker de MQTT-login van de dongle **intrekken**;
- rebooten en `sudo ~/eniro-obd/scripts/wipe.sh --check` draaien, met je eigen
  zoektermen in de grep die het toont. Geen uitvoer is het bewijs.

Opnieuw flashen (hoofdstuk 3) is nog grondiger. Het intrekken van de
WireGuard-client en de MQTT-login moet je dan nog steeds zelf doen.

---

## Bijlage A — wat install.sh doet, stap voor stap

Dit zijn de handmatige stappen die `install.sh` sinds v1.2 voor je doet. Ze
staan hier als naslag: om te begrijpen wat er op de dongle verandert, om één
stap met de hand over te doen, of als je `install.sh` niet wilt gebruiken.
Welke stap van het script bij welke sectie hoort, staat in 6.4.

Werk je met de hand, zet dan eerst de repository op de dongle (6.1); alles
hieronder gaat uit van `~/eniro-obd/`. Achtergrond en ontwerpkeuzes staan in de
README van elke map, met name `wireguard/README.md` en `ingest/README.md`.

Waar `install.sh` het anders doet dan hieronder:

- **Firewall (A.2):** het script zet de twee regels rechtstreeks in
  `/etc/iptables/rules.v4` (vóór de afsluitende REJECT/DROP) en voegt ze live
  toe met `iptables -I`. Het draait geen `netfilter-persistent save`, zodat
  andere, tijdelijke regels niet per ongeluk blijvend worden.
- **`engines.conf` (A.3):** niet met `sed` op regelnummers, maar op de plek in
  de YAML-structuur; vóór het wegschrijven wordt gecontroleerd dat alleen de
  bedoelde waarden veranderen.
- **Wifi (A.1):** de grains worden met de Python van salt bijgewerkt, en
  `state.sls` draait stil (`--state-output=terse --out=quiet`), zodat er geen
  wachtwoord op het scherm komt.
- **`wg-reresolve.sh` (A.4):** `PEER` en `ENDPOINT` komen uit `config.env`.

### A.1 Wifi-client

Stel je thuisnetwerk in via de webinterface (http://local.autopi.io), net als
de APN. De dongle gebruikt wifi als dat er is en valt anders terug op 4G.

> **Het wifi-wachtwoord komt in platte tekst op de dongle te staan**, in
> `/etc/salt/grains`. Iedereen die toegang tot de dongle krijgt kan het lezen.
> Heb je een apart IoT-netwerk of -VLAN, zet de dongle daar.

Geef de dongle een vast adres (DHCP-reservering op het MAC-adres van `wlan0`,
te vinden met `ip link show wlan0`).

**Meer netwerken** (een vakantieadres, een travel router) kunnen er ook bij.
Ze staan als lijst in `/etc/salt/grains` onder `wpa_supplicant` → `networks`.
Pas je die met de hand aan, pas dan de salt-state toe met beperkte uitvoer:

```bash
sudo salt-call --local state.sls network.wlan.client.config --state-output=terse
```

Zonder `--state-output=terse` toont salt de diff van `wpa_supplicant.conf`,
**mét alle wifi-wachtwoorden in platte tekst**. Deel die uitvoer niet, en ruim
een eventuele backup van de grains daarna op — die bevat ze ook.

Alle wifi telt voor de software als onbemeten: daar gaat alles live. Alleen
`wwan0`/`ppp0` (4G) telt als bemeten.

### A.2 SSH over wifi openzetten

Standaard kun je over wifi **niet** bij de dongle: de firewall staat op
`INPUT DROP` en heeft regels voor `lo`, de hotspot en `eth0`, maar helemaal
niets voor `wlan0`. Dat lijkt op een radioprobleem, maar het is de firewall.

De regels worden geladen door `netfilter-persistent` uit
`/etc/iptables/rules.v4`; salt raakt ze niet aan, dus handmatige regels blijven
staan. Ze moeten **vóór** de afsluitende REJECT en DROP:

```bash
sudo iptables -L INPUT --line-numbers -n
# Noteer het nummer N van de eerste REJECT- of DROP-regel onderaan.

# Bron = het netwerk waar je laptop op zit. 192.168.1.0/24 is een voorbeeld:
# vervang het door jouw netwerk.
sudo iptables -I INPUT N -i wlan0 -s 192.168.1.0/24 -p tcp -m state --state NEW -m tcp --dport 22 -j ACCEPT
sudo iptables -I INPUT N -i wlan0 -s 192.168.1.0/24 -p icmp -j ACCEPT
```

**Test nu vanaf je laptop** (`ssh pi@<DONGLE_IP>`, het `wlan0`-adres), en sla
pas daarna op:

```bash
sudo netfilter-persistent save
```

Ping er bewust bij: "ping werkt, ssh niet" versus "allebei niet" scheidt een
firewallprobleem van een dongle die weg is.

Zit de dongle op een apart IoT-netwerk, dan moet je router ook verkeer van je
laptop naar dat netwerk toelaten (alleen poort 22 naar de dongle is genoeg).

Voor `wg0` hoef je niets open te zetten: de bestaande regel
`-A INPUT -m state --state RELATED,ESTABLISHED -j ACCEPT` laat de antwoorden
door de tunnel binnen, en de dongle begint zelf elk gesprek.

### A.3 engines.conf aanpassen

`/etc/salt/minion.d/engines.conf` is het hart van de AutoPi-firmware. Hier
zetten we de uploads naar de ontvanger op de dongle, zetten we de
spraakmeldingen uit en maken we de meetfrequenties verstandiger.

**Eerst een backup:**

```bash
sudo cp /etc/salt/minion.d/engines.conf /etc/salt/minion.d/engines.conf.orig
```

De regelnummers hieronder gelden voor een verse v1.22.9. Elke `sed` is aan een
regelnummer én aan de verwachte inhoud gekoppeld: klopt de inhoud niet, dan
verandert er niets. Controleer daarom na elke stap.

#### A.3.1 Uploads naar de ontvanger op de dongle

Zoek de upload-URL:

```bash
grep -n "url:" /etc/salt/minion.d/engines.conf
```

Onder `cloud_manager` → `endpoint` staat een AutoPi-URL (op deze image
`https://api.symboticware.autopi.io/logbook/storage`). Vervang die met `nano`
door de lokale ontvanger:

```yaml
      url: http://127.0.0.1:8181/ingest/lokaal
```

Precies **zes spaties** inspringing (gelijk aan `auth_token` en `timeout`
eronder), één kale URL op één regel, geen aanhalingstekens of haken. Een
verkeerde inspringing breekt de hele engine-config.

`auth_token` laat je staan. De ontvanger kijkt er niet naar.

Dit pad is lokaal en dus geen geheim. Het moet wel exact gelijk zijn aan
`INGEST_PATH` in `/etc/autopi-ingest.env` (A.5) en `INGEST_URL` in
`/etc/eniro-soc.env` (A.8). Tot de ontvanger draait (A.5), mislukken
de uploads; de firmware houdt ze in zijn cache. Naar de AutoPi-cloud lukten ze
toch al niet, want de dongle is daar niet geregistreerd.

#### A.3.2 Spraakmeldingen uit

De dongle spreekt dertien gebeurtenissen hardop uit ("network offline",
"hibernate"…). Die hangen allemaal aan één reactor, `speak_events`, met één
regex. We neutraliseren de regex in plaats van het blok te verwijderen: met één
regel terug te draaien.

```bash
grep -n "regex: ^(?:system/release" /etc/salt/minion.d/engines.conf     # rond regel 214
sudo sed -i 's#regex: ^(?:system/release|vehicle/battery/critical_level.*#regex: ^disabled/speak_events$#' /etc/salt/minion.d/engines.conf
grep -n "disabled/speak_events" /etc/salt/minion.d/engines.conf         # precies één treffer
```

Niet aanraken in diezelfde lijst: `cloud_upload_on_event` en de
`modem.reset`-reactor. Zonder die laatste komt 4G na een storing niet vanzelf
terug.

De gebeurtenissen zelf komen gewoon binnen op MQTT; in Home Assistant worden er
meldingen van gemaakt (7.6).

#### A.3.3 Langer wakker na het opstarten

Standaard slaapt de dongle 4 minuten na een boot zonder rijdende motor. Dat
maakt elk onderhoud een race. Naar een kwartier (`install.sh`:
`INACTIVITY_PERIOD`, standaard 900):

```bash
grep -n "period: 240" /etc/salt/minion.d/engines.conf        # precies twee treffers
sudo sed -i 's/period: 240$/period: 900/' /etc/salt/minion.d/engines.conf
grep -n "period: 900" /etc/salt/minion.d/engines.conf
```

Tot v1.2.0 stond hier een uur (3600). Sinds de wakelock (A.10) rijden en laden
vasthoudt, telt deze timer alleen nog bij wakes waarin niets gebeurt; een uur
wakker zonder te meten belast alleen de 12V-accu. Heb je voor onderhoud meer
tijd nodig, wis dan de timers (4.2).

Dit zijn de twee inactiviteitsreactors (`inactivity_fallback` na wekken op
spanning, `inactivity_after_sleep` na wekken op de timer). Ze zetten bij elke
boot een sleep timer van een kwartier (na 60 s vertraging), met daarna 6 uur slapen. Gewist worden ze
alleen bij het event `vehicle/engine/running`, en **dat event komt op deze auto
niet**. Gevolg zonder verdere maatregelen: elke boot duurt ~16 minuten, ook
midden in een rit of een laadsessie. Tijdens het rijden wekt de spanning de
dongle meteen weer, tijdens AC-laden niet (de boordspanning staat dan op
~13,0 V).

Dat lost `autopi-wakelock` (A.10) op: die wist de timers zolang de auto rijdt of
laadt, en zet bij loslaten zelf een korte timer terug. De `engine_stopped`-timer
(15 minuten) laat je ook staan.

#### A.3.4 Meetfrequenties

| Regel | Wat | Van | Naar | Waarom |
|---|---|---|---|---|
| 64 | `gnss_manager` → `poll_logger` `interval` | 60 | **60 laten staan** | elke minuut een positie is genoeg voor de tracker en ABRP; het verbruik wordt berekend uit kilometerstand en energietellers, niet uit GPS |
| 402 | `spm_manager` → `_battery_checker` `interval` | 5 | **30** | 75% van alle records kwam hiervandaan |
| 474 | `motion_event_trigger` → `shake_g_threshold` | 0.01 | **0.05** | 0,01 g reageert op wind en langsrijdend verkeer |

```bash
F=/etc/salt/minion.d/engines.conf
sudo sed -n '64p;402p;474p' $F       # eerst kijken: klopt het met de tabel?

sudo sed -i '402s/interval: 5$/interval: 30/' $F
sudo sed -i '474s/shake_g_threshold: 0.01$/shake_g_threshold: 0.05/' $F

sudo sed -n '64p;402p;474p' $F       # en controleren
```

Staat regel 64 op een andere waarde dan 60 (bijvoorbeeld 30 van een eerdere
poging), zet hem dan terug:
`sudo sed -i '64s/interval: [0-9]*$/interval: 60/' $F`.

**Laat staan:**

- `cloud_manager` → upload `interval` (regel ~511, 300 s). De uploads gaan naar
  de ontvanger op de dongle en kosten geen 4G; wat er over 4G gaat, bepaalt de
  ontvanger (profielen, 7.5).
- `battery_critical_level` (regel ~417): **12,0 V over 300 s**. Blijft de
  boordspanning zo lang onder die drempel, dan gaat de dongle in hibernation.
  Dat is de bescherming van je startaccu; de wakelock raakt hem niet aan.
- Alles onder `crypto_manager`.

#### A.3.5 Toepassen en valideren

De systeem-Python op de dongle heeft geen YAML-module; salt zelf is de controle:

```bash
sudo systemctl restart salt-minion
sleep 10
sudo salt-call --local test.ping     # moet True geven
```

Geen `True`? Terug naar de backup
(`sudo cp /etc/salt/minion.d/engines.conf.orig /etc/salt/minion.d/engines.conf`)
en opnieuw, één wijziging per keer.

`salt-minion` herstart traag. En `salt-call` kan na een ATTENTION-melding over
de sleep timer op een Enter wachten — druk dan gewoon op Enter.

### A.4 WireGuard op de dongle

`wireguard-tools` komt uit de Raspbian-buster-repository. Die versie is oud,
maar dat maakt niet uit: het protocol zit in de kernel (5.10 op de dongle);
`wg` en `wg-quick` zijn alleen het configuratiegereedschap.

```bash
cd ~/eniro-obd/wireguard
sudo apt-get update && sudo apt-get install -y wireguard-tools

sudo install -m 600 -o root -g root wg0.conf.example /etc/wireguard/wg0.conf
sudo nano /etc/wireguard/wg0.conf
```

Neem in `/etc/wireguard/wg0.conf` de waarden over uit de `.conf` van je
VPN-server, met twee verschillen:

| | In de `.conf` van je server | Op de dongle |
|---|---|---|
| `AllowedIPs` | vaak `0.0.0.0/0` | **`<BROKER_IP>/32`** — alleen de broker door de tunnel |
| `DNS` | vaak aanwezig | **weg** — de resolver thuis is door de tunnel niet bereikbaar, en `wg-quick` heeft er `resolvconf` voor nodig, dat niet op de image staat |

Geen `PersistentKeepalive`: de dongle begint zelf elk gesprek. De placeholders
staan uitgelegd bovenin het voorbeeldbestand.

```bash
sudo wg-quick up wg0
sudo wg show                     # interface en peer zichtbaar
```

Dan `wg-reresolve`. WireGuard zoekt de hostnaam van de server alleen op bij het
opstarten; verandert je thuis-IP daarna, dan blijft hij naar het oude adres
sturen. Dit script zoekt hem elke 5 minuten opnieuw op, en brengt `wg0` alsnog
op als dat bij het booten mislukte (nog geen netwerk of DNS).

```bash
sudo install -m 755 wg-reresolve.sh /usr/local/sbin/wg-reresolve.sh
sudo nano /usr/local/sbin/wg-reresolve.sh    # PEER en ENDPOINT invullen, zie de kop
sudo install -m 644 wg-reresolve.service wg-reresolve.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable wg-quick@wg0
sudo systemctl enable --now wg-reresolve.timer
```

**Testen.** Zonder keepalive komt er pas een handshake als er verkeer is; wek
dat eerst op:

```bash
timeout 5 bash -c '</dev/tcp/<BROKER_IP>/1883' && echo "broker open"
sudo wg show wg0 latest-handshakes          # een tijdstempel, niet 0
getent hosts <VPN_HOSTNAAM>                 # op de dongle staat geen dig
journalctl -t wg-reresolve -n 10 --no-pager
```

**Negatieve test (aanbevolen).** Zet tijdelijk een tweede adres in `AllowedIPs`
— bijvoorbeeld de beheerpagina van je router — en controleer dat dat **dicht**
is (`timeout 5 bash -c '</dev/tcp/<ROUTER_IP>/443'` moet falen). Zo weet je
dat de firewall uit 1.3 doet wat hij moet. Daarna terug naar alleen
`<BROKER_IP>/32` en `sudo wg-quick down wg0 && sudo wg-quick up wg0`.

**Thuis op wifi** werkt de tunnel meestal ook: de router buigt verkeer naar zijn
eigen publieke IP terug naar de VPN-server (hairpin NAT, of NAT-loopback). Dat
is router-afhankelijk. Werkt het bij jou niet, dan wacht de dongle thuis tot
hij op 4G zit, of los je het op met een lokale DNS-override voor
`<VPN_HOSTNAAM>` naar het LAN-adres van de VPN-server. Test het: met de dongle
op je wifi moet de handshake hierboven ook lukken.

### A.5 De ontvanger installeren

Op de dongle is `python3-paho-mqtt` uit apt genoeg (versie 1.4.0); een venv is
niet nodig.

```bash
cd ~/eniro-obd/ingest
sudo apt-get install -y python3-paho-mqtt

sudo useradd --system --home /var/lib/autopi-ingest --shell /usr/sbin/nologin autopi-ingest
sudo install -d /opt/autopi-ingest
sudo install -m 755 autopi_ingest.py /opt/autopi-ingest/autopi_ingest.py
sudo install -m 644 README.md /opt/autopi-ingest/README.md
# alleen met MQTT Discovery (HA_DISCOVERY=1, 7.2):
sudo install -m 644 ../homeassistant/discovery.json /opt/autopi-ingest/discovery.json

sudo install -m 600 -o root -g root autopi-ingest-dongle.env.example /etc/autopi-ingest.env
sudo nano /etc/autopi-ingest.env
```

In `/etc/autopi-ingest.env` hoeven maar twee regels ingevuld:

| Variabele | Waarde |
|---|---|
| `MQTT_HOST` | `<BROKER_IP>`, hetzelfde adres als in `AllowedIPs` (staat er als `VUL_HIER_HET_BROKER_IP_IN`) |
| `MQTT_PASS` | het wachtwoord van de login uit 1.2 (staat er als `VUL_HIER_HET_WACHTWOORD_IN`) |

Heet je login anders dan `autopi-dongle`, pas dan ook `MQTT_USER` aan. De rest
zijn de geteste instellingen, met toelichting in het bestand:

| Variabele | Waarde | Waarom |
|---|---|---|
| `LISTEN_HOST` / `LISTEN_PORT` | `127.0.0.1` / `8181` | alleen lokaal bereikbaar |
| `INGEST_PATH` | `/ingest/lokaal` | lokaal, dus geen geheim; gelijk aan de url in `engines.conf` |
| `MQTT_CLIENT_ID` | `autopi-ingest-dongle` | een eigen ID; twee clients met hetzelfde ID gooien elkaar bij de broker eruit |
| `MQTT_KEEPALIVE` | `300` | elke keepalive gaat over 4G |
| `STATUS_TOPIC` | `autopi/dongle/status` | last-will: `offline` bij elke slaap; in HA de sensor "dongle online" |
| `RAW_DIR` | leeg | geen ruwe payloads op de eMMC |
| `PROFIEL_TOPIC` | `autopi/config/profiel` | de profielkeuze uit HA (7.5) |
| `PROFIEL_STANDAARD` | `dagelijks` | zolang er nog niets uit HA is binnengekomen |
| `BUNDEL_INTERVAL` | `1800` | in profiel dagelijks op 4G: elke 30 minuten de laatste stand per topic |
| `BUNDEL_UITZONDERINGEN` | `vehicle/battery/critical_level` | gaat altijd direct door |
| `METERED_IFACES` | `wwan0,ppp0` | default route over een van deze = bemeten |

De dienst. Op de dongle heet hij gewoon `autopi-ingest.service`; de `-dongle`
in de bestandsnaam is alleen om hem te onderscheiden van de servervariant. Hij
start na `wg-quick@wg0`.

```bash
sudo install -m 644 autopi-ingest-dongle.service /etc/systemd/system/autopi-ingest.service
sudo systemctl daemon-reload
sudo systemctl enable --now autopi-ingest
sudo journalctl -u autopi-ingest -n 20 --no-pager
```

Verwacht `MQTT verbonden met <BROKER_IP>:1883` en een regel die met
`Profiel: dagelijks` begint.

**Testen zonder firmware** (op de dongle):

```bash
printf '[{"@ts":"2026-01-01T00:00:00","@tag":"test/hallo","@t":"test","ok":1}]' \
  | gzip \
  | curl -s -w '\n%{http_code}\n' -X POST --data-binary @- \
      -H 'Content-Type: application/json' -H 'Content-Encoding: gzip' \
      http://127.0.0.1:8181/ingest/lokaal

curl -s http://127.0.0.1:8181/healthz ; echo
```

Verwacht `{"ok":true,"records":1}` en `200`. Op de broker verschijnt
`autopi/test/hallo`. Een `503` betekent: ontvanger draait, broker niet
bereikbaar — kijk eerst naar `sudo wg show`.

Het testbericht staat daarna retained op de broker. Weghalen:
`mosquitto_pub -h <BROKER_IP> -u <gebruiker> -P '<wachtwoord>' -t autopi/test/hallo -r -n`.

### A.6 De firmware laten posten

De firmware-url staat sinds A.3.1 al op de lokale ontvanger. Herstart salt en
forceer een upload:

```bash
sudo systemctl restart salt-minion
sudo salt-call --local cloud.upload
sudo journalctl -u autopi-ingest -n 5 --no-pager    # "... record(s) verwerkt (... bytes)"
```

Op de broker verschijnen nu onder meer `autopi/track/pos` (GPS, zodra er een
fix is), `autopi/obd/bat` (12V) en gebeurtenissen onder `autopi/system/…`.
`eniro-soc` volgt in A.8; die post standaard al naar dezelfde lokale
ontvanger.

### A.7 Het journal en de slaap

Elke keer slapen is voor de dongle een volledige shutdown. Standaard staat het
journal in het geheugen. `install.sh` zet het persistent, met een maximum:

```bash
sudo install -d /etc/systemd/journald.conf.d
printf '[Journal]\nStorage=persistent\nSystemMaxUse=100M\nSyncIntervalSec=30s\n' \
  | sudo tee /etc/systemd/journald.conf.d/persistent.conf
sudo install -d /var/log/journal
sudo systemctl restart systemd-journald
```

**Bekende beperking.** Ook zo bewaart journald van een vorige boot alleen de
eerste ~30 seconden. Bij elke start meldt het
`system.journal corrupted or uncleanly shut down, renaming and replacing`, en in
het gearchiveerde bestand staat alleen het begin van die boot. Verkleinen
(van 300 naar 100 MB) en `SyncIntervalSec` veranderen daar niets aan; de
oorzaak is niet gevonden. Het is ook niet nodig: **rsyslog** (standaard
aanwezig) schrijft dezelfde regels naar `/var/log/syslog`, en die overleven een
slaap volledig, tot en met de `Stopping …`-regels van het uitschakelen. Zie
10.2. Het persistente journal blijft staan omdat het een crash vlak na het
opstarten wél vastlegt.

### A.8 eniro-soc — het hoofdscript

Leest de BMS uit (en tijdens het rijden de kilometerstand; daarbuiten gaat de
laatst bekende stand mee) en post naar de ontvanger op de dongle. Past zijn tempo aan op wat de auto doet:

| Situatie | Meten | Versturen naar de ontvanger |
|---|---|---|
| Snelladen | elke 60 s | direct |
| Rijden | elke 60 s | gebundeld per 300 s; op wifi direct |
| Laden op 4G | elke 300 s | direct, en minimaal elke 5 minuten een record |
| Laden op wifi | elke 60 s | bij elke 0,5% en minimaal elke 5 minuten |
| Stilstaand | elke 1800 s | gebundeld, hartslag elk uur |
| Bus antwoordt niet | elke 300 s; de eerste 180 s na een boot elke 15 s | — |

De eerste meting na het wakker worden, en elke wissel van toestand (stil →
laden, laden → rijden), gaat direct de deur uit. Mislukt het versturen, dan
gaat het record in een wachtrij op schijf (`/var/lib/eniro-soc/spool`), die bij
de volgende geslaagde post eerst wordt verstuurd — zo blijft de volgorde in
Home Assistant kloppen.

Wat er daarna over 4G gaat, bepaalt de ontvanger (profielen, 7.5).
Laadrecords gaan ook in profiel dagelijks altijd direct door.

```bash
cd ~/eniro-obd/eniro-soc
python3 eniro_soc.py --selftest       # decodering testen - moet alles OK geven

sudo install -d /opt/eniro-soc
sudo install -m 755 eniro_soc.py /opt/eniro-soc/eniro_soc.py
sudo install -m 644 README.md /opt/eniro-soc/README.md
sudo install -m 600 eniro-soc.env.example /etc/eniro-soc.env
sudo nano /etc/eniro-soc.env
```

In `/etc/eniro-soc.env` hoeft niets aangepast. Controleer alleen:

```
INGEST_URL=http://127.0.0.1:8181/ingest/lokaal
```

Dezelfde URL als in `engines.conf`. Alle andere waarden zijn de geteste
instellingen; de toelichting staat in het bestand zelf. Twee om te kennen:

- `CAN_AUTO_UP=1` — de firmware brengt `can0` na een boot niet zelf omhoog;
  het script doet het.
- `CAN_RESTART_MS=100` — zonder `restart-ms` komt de CAN-controller na een
  bus-off nooit meer terug, en valt de data midden in een lading weg tot de
  volgende boot. Het script zet dit elke keer dat het `can0` opbrengt, en
  brengt hem opnieuw op als hij `BUS-OFF` staat. Zie 10.3.

**Zet de auto in ready** (of hang hem aan de lader) en doe één proefmeting die
niets verstuurt:

```bash
sudo python3 /opt/eniro-soc/eniro_soc.py --once --dry-run
```

Je ziet SOC, spanning, stroom en de rest. Dan de dienst:

```bash
sudo install -m 644 eniro-soc.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now eniro-soc
sudo journalctl -u eniro-soc -f
```

Verwacht binnen een minuut iets als:

```
INFO  SOC 36.0% (BMS 35.0%) · 356.8 V · -8.7 A · laden · onbemeten · volgende over 60s
INFO  Verstuurd: 1 record(s), 313 bytes (...)
```

En in `sudo journalctl -u autopi-ingest -n 5`: `1 record(s) verwerkt`.

### A.9 Wakker worden

Wakker **worden** doet de dongle zelf: de Smart Power Manager reageert op een
spanningsverandering (laadkabel erin, contact aan). Wat hij als reden zag:
`sudo salt-call --local spm.query status` (`last_trigger: up: volt_change`).
Het **wakker blijven** is het probleem; dat is A.10.

### A.10 autopi-wakelock — wakker blijven tijdens rijden en laden

Zonder dit gaat de dongle tijdens het laden gewoon slapen — precies wanneer je
data wil. Twee oorzaken: de reactor-timer uit A.3.3 (elke boot eindigt na
~16 minuten), en de `engine_stopped`-timer van 15 minuten.

Elke vijf minuten beslist het script:

| Situatie | Besluit |
|---|---|
| `eniro-soc` meldt `laden`, `snelladen` of `rijden` (status jonger dan 15 min) | wakker houden: alle sleep timers wissen |
| boordspanning ≥ 13,5 V | wakker houden |
| anders, terwijl hij vasthield | loslaten **en** zelf een timer van 15 minuten zetten (daarna 6 uur slapen) |
| anders | niets |

De status komt uit `/var/lib/eniro-soc/status.json`, die `eniro-soc` na elke
uitlezing schrijft. Op spanning alleen gaat het niet: tijdens AC-laden staat de
12V op ~13,0 V, onder de drempel. Het zelf terugzetten van een timer bij
loslaten is nodig omdat de uurtimer van de reactor pas bij de volgende boot
terugkomt; zonder dat zou de dongle na een lading nooit meer slapen.

Het kan je accu niet leegtrekken: `battery_critical_level` blijft ongemoeid.
Vasthouden op spanning stopt na 16 uur (`MAX_WAKE_HOURS`), vasthouden op
laadstatus na 48 uur (`MAX_WAKE_HOURS_LADEN`, voor langzaam laden aan een
gewoon stopcontact). Kan het script niets lezen, dan doet het niets.

```bash
cd ~/eniro-obd/wakelock
sudo install -d /opt/autopi-wakelock
sudo install -m 755 autopi_wakelock.py /opt/autopi-wakelock/autopi_wakelock.py
sudo install -m 644 README.md /opt/autopi-wakelock/README.md
sudo install -m 644 autopi-wakelock.service autopi-wakelock.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo python3 /opt/autopi-wakelock/autopi_wakelock.py --dry-run
sudo systemctl enable --now autopi-wakelock.timer
```

Na de eerste lading controleren: `journalctl -u autopi-wakelock` toont dan
regels met `sleep timers gewist` en aan het eind `wakeslot losgelaten`.

### A.11 wwan-teller — 4G-verbruik per boot

Legt per boot vast hoeveel data er over `wwan0` ging, en stuurt dat bij de
volgende boot als record naar `autopi/wwan/boot`. Groeit het verbruik meer dan
2 MB tussen twee metingen (elke 2 minuten), dan komen de open verbindingen in
een piek-log — het spoor naar de veroorzaker als er ooit een uitschieter is.
De tellers tellen al het IP-verkeer op de interface, inclusief de
WireGuard-overhead; dat is ook wat de provider rekent.

```bash
cd ~/eniro-obd/wwan-teller
sudo install -d /opt/wwan-teller
sudo install -m 755 wwan_teller.py /opt/wwan-teller/wwan_teller.py
sudo install -m 644 README.md /opt/wwan-teller/README.md
sudo install -m 644 wwan-teller.service wwan-teller.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now wwan-teller.timer
sudo python3 /opt/wwan-teller/wwan_teller.py --snapshot && sudo cat /var/lib/wwan-teller/huidig.json
```

Overzicht per dag en per boot: `sudo python3 /opt/wwan-teller/wwan_teller.py --rapport`
(zie 9.3).

### A.12 autopi-diag — optioneel

Meet elke tien minuten de 4G-kwaliteit (signaal, operator, cel) en stuurt dat
naar `autopi/device/diag`. Onderweg buffert hij, thuis op wifi dumpt hij de
hele buffer. Handig om een slechte montageplek of een wegvallend modem aan te
tonen. Leest `INGEST_URL` uit dezelfde `/etc/eniro-soc.env`.

```bash
cd ~/eniro-obd/diag
sudo install -d /opt/autopi-diag
sudo install -m 755 autopi_diag.py /opt/autopi-diag/autopi_diag.py
sudo install -m 644 README.md /opt/autopi-diag/README.md
sudo install -m 644 autopi-diag.service autopi-diag.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo python3 /opt/autopi-diag/autopi_diag.py --dry-run
sudo systemctl enable --now autopi-diag.timer
```

---

## Bijlage B — ontvanger op een server via HTTPS

Wil je geen VPN, dan kan de ontvanger ook op een eigen server draaien (een
kleine container of VM), bereikbaar via HTTPS — bijvoorbeeld via een tunnel of
reverse proxy met een geldig certificaat. De dongle post dan rechtstreeks naar
`https://autopi.voorbeeld.nl/ingest/VUL_HIER_JE_HEX_IN`.

De verschillen in het kort:

| | Ontvanger op de dongle (standaard) | Ontvanger op een server |
|---|---|---|
| Bestanden | `autopi-ingest-dongle.service`, `autopi-ingest-dongle.env.example` | `autopi-ingest.service`, `autopi-ingest.env.example` |
| Geheim | MQTT-wachtwoord | geheim pad (`openssl rand -hex 16`) + MQTT-wachtwoord |
| Publiek endpoint | nee | ja; afschermen met WAF- en rate-limitregels |
| 4G-verbruik | ~0,06 MB/dag (gemeten, met profielen) | 2-8 MB/dag: een TLS-handshake per upload, geen profielen |
| WireGuard | nodig | niet nodig |

Wat er dan anders gaat in deze handleiding:

- **`install.sh` richt deze variant niet in.** Werk met de hand (bijlage A)
  en sla 1.2, 1.3 en A.4–A.6 over. Installatie, afscherming (WAF-regels, rate limit,
  níét filteren op User-Agent) en tests staan in `ingest/README.md`, onder
  "Installatie op een eigen server".
- **A.3.1:** de `url:` in `engines.conf` wordt je HTTPS-adres met het geheime pad.
- **A.8:** `INGEST_URL` in `/etc/eniro-soc.env` idem (de regel staat er al als
  commentaar). Dat bestand is daarmee gevoelig.
- **A.11:** `wwan-teller` post standaard lokaal; zet de url met
  `sudo systemctl edit wwan-teller` → `Environment=INGEST_URL=…`.
- **7.5:** profielen bestaan in deze variant niet; het profiel-blok in
  `homeassistant/eniro-autopi.yaml` mag je weglaten.
- **7.3:** de status van de servervariant staat op `autopi/status`, niet op
  `autopi/dongle/status`.
- **9.1:** draai `verify.sh` met `ONTVANGER=` (leeg), dan slaat hij de
  controles op de lokale ontvanger over.
