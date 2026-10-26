# Installatie — van kale dongle tot data in Home Assistant

Deze handleiding neemt je mee van een AutoPi-dongle zoals hij uit de doos komt
tot de accustatus van je e-Niro live in Home Assistant, óók als de auto
onderweg is. Alles hierin is zo gedaan en getest in een Kia e-Niro uit 2019.

Reken op een middag voor het geheel: flashen en de dongle inrichten een uur of
twee, WireGuard en Home Assistant een uur, inbouwen een half uur.

> **Lees eerst hoofdstuk 1 helemaal.** Daar staat wat je nodig hebt, en een
> paar dingen moet je vóóraf regelen (de SIM activeren, de image downloaden,
> een WireGuard-client aanmaken).

---

## Inhoud

1. [Wat je nodig hebt](#1-wat-je-nodig-hebt)
2. [Hoe het in elkaar zit](#2-hoe-het-in-elkaar-zit)
3. [Firmware flashen](#3-firmware-flashen)
4. [Eerste boot en beveiligen](#4-eerste-boot-en-beveiligen)
5. [4G met een 1NCE-SIM](#5-4g-met-een-1nce-sim)
6. [Wifi thuis en SSH over wifi](#6-wifi-thuis-en-ssh-over-wifi)
7. [engines.conf aanpassen](#7-enginesconf-aanpassen)
8. [De ontvanger op de dongle en WireGuard](#8-de-ontvanger-op-de-dongle-en-wireguard)
9. [De software op de dongle](#9-de-software-op-de-dongle)
10. [Home Assistant](#10-home-assistant)
11. [Inbouwen in de auto](#11-inbouwen-in-de-auto)
12. [Controleren](#12-controleren)
13. [Als het niet werkt](#13-als-het-niet-werkt)
14. [Naslag en doorverkopen](#14-naslag-en-doorverkopen)

---

## 1. Wat je nodig hebt

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
server achter HTTPS draaien; zie 8.7.

### Vooraf regelen

- [ ] **De SIM activeren** in het 1NCE-portaal. Een niet-geactiveerde SIM geeft
      een foutmelding (`option-unsubscribed`, cause #33) die er precies uitziet
      als een configuratiefout. Dat kost je anders een uur.
- [ ] **De firmware-image downloaden** (1,3 GB, zie 3.1). Thuis doen, niet in de auto.
- [ ] **rpiboot en Etcher installeren** en één keer opstarten (zie 3.2).
- [ ] **Een WireGuard-client aanmaken** op je VPN-server voor de dongle, met een
      eigen tunneladres (`/32`). Bewaar de gegenereerde `.conf`; die heb je in
      8.4 nodig. Zie 8.3.
- [ ] **Een MQTT-login voor de dongle** aanmaken in je broker (bijvoorbeeld
      `autopi-dongle`). Wachtwoord in je wachtwoordmanager. Zie 8.2.

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
  profielen uit 10.5, ging het verbruik op 4G van 2-8 MB per dag naar
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
  tijdens het laden.

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
> Alle regelnummers en standaardwaarden in hoofdstuk 7 gaan uit van v1.22.9.

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
delen op de dongle één radio; met beide actief kan SSH traag worden (zie 13.1).

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

Terugzetten gebeurt vanzelf bij de volgende herstart van `salt-minion`. In
hoofdstuk 7 zetten we de wachttijd structureel ruimer.

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

### 4.4 Kleine ergernis: trage sudo

Elke `sudo` wacht op een DNS-timeout omdat de hostnaam niet in `/etc/hosts`
staat. Eenmalig oplossen:

```bash
grep -q "$(hostname)" /etc/hosts || echo "127.0.1.1 $(hostname)" | sudo tee -a /etc/hosts
```

---

## 5. 4G met een 1NCE-SIM

Dit hoofdstuk kostte bij de eerste installatie de meeste tijd. Het werkende
recept staat hieronder; de achtergrond staat in hoofdstuk 13.

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

## 6. Wifi thuis en SSH over wifi

### 6.1 Wifi-client

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

### 6.2 SSH over wifi openzetten

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

---

## 7. engines.conf aanpassen

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

### 7.1 Uploads naar de ontvanger op de dongle

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
`INGEST_PATH` in `/etc/autopi-ingest.env` (8.5) en `INGEST_URL` in
`/etc/eniro-soc.env` (9.2). Tot de ontvanger in hoofdstuk 8 draait, mislukken
de uploads; de firmware houdt ze in zijn cache. Naar de AutoPi-cloud lukten ze
toch al niet, want de dongle is daar niet geregistreerd.

### 7.2 Spraakmeldingen uit

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
meldingen van gemaakt (10.6).

### 7.3 Langer wakker na het opstarten

Standaard slaapt de dongle 4 minuten na een boot zonder rijdende motor. Dat
maakt elk onderhoud een race. Naar een uur:

```bash
grep -n "period: 240" /etc/salt/minion.d/engines.conf        # precies twee treffers
sudo sed -i 's/period: 240$/period: 3600/' /etc/salt/minion.d/engines.conf
grep -n "period: 3600" /etc/salt/minion.d/engines.conf
```

Dit zijn de twee inactiviteitsreactors (`inactivity_fallback` na wekken op
spanning, `inactivity_after_sleep` na wekken op de timer). Ze zetten bij elke
boot een sleep timer van een uur, met daarna 6 uur slapen. Gewist worden ze
alleen bij het event `vehicle/engine/running`, en **dat event komt op deze auto
niet**. Gevolg zonder verdere maatregelen: elke boot duurt ~61 minuten, ook
midden in een rit of een laadsessie. Tijdens het rijden wekt de spanning de
dongle meteen weer, tijdens AC-laden niet (de boordspanning staat dan op
~13,0 V).

Dat lost `autopi-wakelock` (9.4) op: die wist de timers zolang de auto rijdt of
laadt, en zet bij loslaten zelf een korte timer terug. De `engine_stopped`-timer
(15 minuten) laat je ook staan.

### 7.4 Meetfrequenties

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
  ontvanger (profielen, 10.5).
- `battery_critical_level` (regel ~417): **12,0 V over 300 s**. Blijft de
  boordspanning zo lang onder die drempel, dan gaat de dongle in hibernation.
  Dat is de bescherming van je startaccu; de wakelock raakt hem niet aan.
- Alles onder `crypto_manager`.

### 7.5 Toepassen en valideren

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

---

## 8. De ontvanger op de dongle en WireGuard

In dit hoofdstuk komt de weg naar je broker te liggen: eerst de kant thuis
(MQTT-login, WireGuard-client, firewall), dan de tunnel op de dongle, dan de
ontvanger. Test elke stap voordat je aan de volgende begint.

Achtergrond en ontwerpkeuzes: `wireguard/README.md` en `ingest/README.md`.

Kopieer eerst de hele repository naar de dongle; alle volgende hoofdstukken
gaan uit van `~/eniro-obd/`:

```bash
ssh pi@<DONGLE_IP> mkdir -p eniro-obd
scp -r eniro-soc ingest wireguard wakelock diag wwan-teller scripts pi@<DONGLE_IP>:/home/pi/eniro-obd/
ssh pi@<DONGLE_IP>
```

### 8.1 Wat er thuis klaar moet staan

| | Hoe je het controleert |
|---|---|
| MQTT-broker op `<BROKER_IP>:1883` | vanaf je laptop: `mosquitto_sub -h <BROKER_IP> -u <gebruiker> -P '<wachtwoord>' -t 'autopi/#' -v` maakt verbinding |
| een vast adres voor de broker | DHCP-reservering of statisch IP. Dit adres komt in de tunnelconfig. |
| WireGuard-server bereikbaar op `<VPN_HOSTNAAM>:<POORT>` (UDP) | de server draait; de hostnaam wijst naar je huidige thuis-IP |

### 8.2 MQTT-login voor de dongle

Maak in je broker een **eigen login** voor de dongle, bijvoorbeeld
`autopi-dongle`. Bij de Mosquitto add-on: een Home Assistant-gebruiker, of een
login in de add-on-configuratie. Een eigen login kun je los intrekken als de
dongle ooit weg is.

Die login moet:

- publiceren op `autopi/#`;
- **lezen** op `autopi/config/profiel` — anders komt de profielkeuze uit Home
  Assistant (10.5) nooit bij de dongle aan.

Gebruik je ACL's, zet ze dan precies zo; zonder ACL's mag elke login alles.

### 8.3 WireGuard aan de kant thuis

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

### 8.4 WireGuard op de dongle

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
dat de firewall uit 8.3 doet wat hij moet. Daarna terug naar alleen
`<BROKER_IP>/32` en `sudo wg-quick down wg0 && sudo wg-quick up wg0`.

**Thuis op wifi** werkt de tunnel meestal ook: de router buigt verkeer naar zijn
eigen publieke IP terug naar de VPN-server (hairpin NAT, of NAT-loopback). Dat
is router-afhankelijk. Werkt het bij jou niet, dan wacht de dongle thuis tot
hij op 4G zit, of los je het op met een lokale DNS-override voor
`<VPN_HOSTNAAM>` naar het LAN-adres van de VPN-server. Test het: met de dongle
op je wifi moet de handshake hierboven ook lukken.

### 8.5 De ontvanger installeren

Op de dongle is `python3-paho-mqtt` uit apt genoeg (versie 1.4.0); een venv is
niet nodig.

```bash
cd ~/eniro-obd/ingest
sudo apt-get install -y python3-paho-mqtt

sudo useradd --system --home /var/lib/autopi-ingest --shell /usr/sbin/nologin autopi-ingest
sudo install -d /opt/autopi-ingest
sudo install -m 755 autopi_ingest.py /opt/autopi-ingest/autopi_ingest.py
sudo install -m 644 README.md /opt/autopi-ingest/README.md

sudo install -m 600 -o root -g root autopi-ingest-dongle.env.example /etc/autopi-ingest.env
sudo nano /etc/autopi-ingest.env
```

In `/etc/autopi-ingest.env` hoeven maar twee regels ingevuld:

| Variabele | Waarde |
|---|---|
| `MQTT_HOST` | `<BROKER_IP>`, hetzelfde adres als in `AllowedIPs` (staat er als `VUL_HIER_HET_BROKER_IP_IN`) |
| `MQTT_PASS` | het wachtwoord van de login uit 8.2 (staat er als `VUL_HIER_HET_WACHTWOORD_IN`) |

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
| `PROFIEL_TOPIC` | `autopi/config/profiel` | de profielkeuze uit HA (10.5) |
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

### 8.6 De firmware laten posten

De firmware-url staat sinds 7.1 al op de lokale ontvanger. Herstart salt en
forceer een upload:

```bash
sudo systemctl restart salt-minion
sudo salt-call --local cloud.upload
sudo journalctl -u autopi-ingest -n 5 --no-pager    # "... record(s) verwerkt (... bytes)"
```

Op de broker verschijnen nu onder meer `autopi/track/pos` (GPS, zodra er een
fix is), `autopi/obd/bat` (12V) en gebeurtenissen onder `autopi/system/…`.
`eniro-soc` volgt in hoofdstuk 9; die post standaard al naar dezelfde lokale
ontvanger.

### 8.7 Alternatief: ontvanger op een server via HTTPS

Wil je geen VPN, dan kan de ontvanger ook op een eigen server draaien (een
kleine container of VM), bereikbaar via HTTPS — bijvoorbeeld via een tunnel of
reverse proxy met een geldig certificaat. De dongle post dan rechtstreeks naar
`https://autopi.voorbeeld.nl/ingest/VUL_HIER_JE_HEX_IN`.

De verschillen in het kort:

| | Ontvanger op de dongle (dit hoofdstuk) | Ontvanger op een server |
|---|---|---|
| Bestanden | `autopi-ingest-dongle.service`, `autopi-ingest-dongle.env.example` | `autopi-ingest.service`, `autopi-ingest.env.example` |
| Geheim | MQTT-wachtwoord | geheim pad (`openssl rand -hex 16`) + MQTT-wachtwoord |
| Publiek endpoint | nee | ja; afschermen met WAF- en rate-limitregels |
| 4G-verbruik | ~0,06 MB/dag (gemeten, met profielen) | 2-8 MB/dag: een TLS-handshake per upload, geen profielen |
| WireGuard | nodig | niet nodig |

Wat er dan anders gaat in deze handleiding:

- **8.2–8.6 sla je over.** Installatie, afscherming (WAF-regels, rate limit,
  níét filteren op User-Agent) en tests staan in `ingest/README.md`, onder
  "Installatie op een eigen server".
- **7.1:** de `url:` in `engines.conf` wordt je HTTPS-adres met het geheime pad.
- **9.2:** `INGEST_URL` in `/etc/eniro-soc.env` idem (de regel staat er al als
  commentaar). Dat bestand is daarmee gevoelig.
- **9.5:** `wwan-teller` post standaard lokaal; zet de url met
  `sudo systemctl edit wwan-teller` → `Environment=INGEST_URL=…`.
- **10.5:** profielen bestaan in deze variant niet; het profiel-blok in
  `homeassistant/eniro-autopi.yaml` mag je weglaten.
- **10.3:** de status van de servervariant staat op `autopi/status`, niet op
  `autopi/dongle/status`.
- **12.1:** draai `verify.sh` met `ONTVANGER=` (leeg), dan slaat hij de
  controles op de lokale ontvanger over.

---

## 9. De software op de dongle

De mappen staan sinds hoofdstuk 8 in `~/eniro-obd/`. Elk onderdeel heeft een
eigen README met de redenering; hier staat wat je moet doen.

### 9.1 Eerst: een journal dat een slaap overleeft

Elke keer slapen is voor de dongle een volledige shutdown. Standaard staat het
journal in het geheugen, en dan zijn de logs van een rit of een nachtlading
weg zodra je ze nodig hebt. Zet het daarom als eerste persistent:

```bash
sudo install -d /etc/systemd/journald.conf.d
printf '[Journal]\nStorage=persistent\nSystemMaxUse=300M\nSyncIntervalSec=30s\n' \
  | sudo tee /etc/systemd/journald.conf.d/persistent.conf
sudo install -d /var/log/journal
sudo systemctl restart systemd-journald
sudo journalctl --disk-usage
```

Alle drie de regels zijn nodig:

- `Storage=persistent` — op de eMMC (`/var/log/journal`), niet in het geheugen.
- `SystemMaxUse=300M` — anders mag het journal 10% van de schijf pakken.
- **`SyncIntervalSec=30s`** — zonder deze regel haalt wat er tijdens een boot
  geschreven wordt de eMMC niet voordat de Smart Power Manager de stroom eraf
  haalt. Je ziet dan in `journalctl --list-boots` boots die maar ~30 seconden
  beslaan, en de rest is weg.

**Testen** met een geforceerde slaap:

```bash
logger -t markeertest "voor de slaap"
sudo autopi power.sleep interval=120 confirm=true reason=test
```

De dongle gaat uit en is na een minuut of vier terug. Dan:

```bash
journalctl -t markeertest                # de markeerregel staat er nog
sudo journalctl --list-boots | tail -3   # de vorige boot loopt door tot het uitschakelen
```

Zoek in het journal **per boot-ID** (de lange hex-string uit `--list-boots`),
niet met `-b -1`: de allereerste kernelregels van elke boot dragen een
tijdstempel uit 2019 (vóór de klokchip geladen is), waardoor de nummering
soms naar de verkeerde boot wijst. Zie 13.2.

### 9.2 eniro-soc — het hoofdscript

Leest de BMS uit (en tijdens het rijden de kilometerstand) en post naar de
ontvanger op de dongle. Past zijn tempo aan op wat de auto doet:

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

Wat er daarna over 4G gaat, bepaalt de ontvanger (profielen, 10.5).
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
  brengt hem opnieuw op als hij `BUS-OFF` staat. Zie 13.3.

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

### 9.3 Wakker worden

Wakker **worden** doet de dongle zelf: de Smart Power Manager reageert op een
spanningsverandering (laadkabel erin, contact aan). Wat hij als reden zag:
`sudo salt-call --local spm.query status` (`last_trigger: up: volt_change`).
Het **wakker blijven** is het probleem; dat is 9.4.

### 9.4 autopi-wakelock — wakker blijven tijdens rijden en laden

Zonder dit gaat de dongle tijdens het laden gewoon slapen — precies wanneer je
data wil. Twee oorzaken: de reactor-uurtimer uit 7.3 (elke boot eindigt na
~61 minuten), en de `engine_stopped`-timer van 15 minuten.

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

### 9.5 wwan-teller — 4G-verbruik per boot

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
(zie 12.3).

### 9.6 autopi-diag — optioneel

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

### 9.7 Updates: bewust niet automatisch

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

De scripts uit deze repository werk je zelf bij: nieuwe versie naar
`/opt/<dienst>/` kopiëren en de dienst herstarten. De enige pakketten die je
installeert zijn `wireguard-tools` en `python3-paho-mqtt`.

---

## 10. Home Assistant

### 10.1 Kijken wat er binnenkomt

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

### 10.2 De bestanden laden als packages

In `homeassistant/` staan vier bestanden. Drie ervan zijn
Home Assistant-*packages*: elk bestand bevat alles voor één onderwerp.

| Bestand | Wat | Nodig |
|---|---|---|
| `eniro-mqtt.yaml` | alle MQTT-sensoren, onder de apparaten **Kia e-Niro** en **AutoPi dongle**, plus de locatie-tracker | ja |
| `eniro-autopi.yaml` | de helpers (profiel, accucapaciteit, laatst bekend verbruik), de actieradius en de automatiseringen (profiel naar de dongle, verbruik bewaren, drie meldingen) | ja |
| `abrp.yaml` | live telemetrie naar ABRP | optioneel, 10.7 |
| `secrets.example.yaml` | voorbeeld voor je eigen `secrets.yaml` | alleen bij ABRP — **geen package** |

De MQTT-sensoren kunnen alleen in YAML; Home Assistant kan MQTT-sensoren met
een eigen topic en template niet via de UI aanmaken. Zet in
`configuration.yaml` (eenmalig, als je nog geen packages gebruikt):

```yaml
homeassistant:
  packages: !include_dir_named packages
```

Heb je al een `homeassistant:`-sleutel, zet de regel `packages:` daar dan
onder. Kopieer daarna de bestanden naar `/config/packages/`:

```
/config/packages/eniro-mqtt.yaml
/config/packages/eniro-autopi.yaml
/config/packages/abrp.yaml          (alleen als je ABRP gebruikt)
```

**`secrets.example.yaml` hoort níét in `packages/`.** Daaruit kopieer je alleen
een blok naar je eigen `/config/secrets.yaml` (10.7).

Herstart Home Assistant (de eerste keer is een herstart nodig, daarna volstaat
herladen).

Liever zonder packages? Dan kan het ook; de kop van elk bestand beschrijft hoe.
De helpers en de automatiseringen uit `eniro-autopi.yaml` kun je dan via de UI
aanmaken en daar ook bewerken; de MQTT-sensoren plak je onder je bestaande
`mqtt:`-sleutel.

### 10.3 Sensoren en entity-ID's

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
| `sensor.e_niro_kilometerstand` | alleen tijdens rijden; houdt daartussen de vorige waarde vast |
| `sensor.e_niro_verbruik_100_km` | netto, na de eerste 100 km |
| `sensor.e_niro_actieradius` | SOC × bruikbare capaciteit / verbruik (uit `eniro-autopi.yaml`); valt terug op `input_number.e_niro_verbruik_laatst_bekend` als het verbruik na een herstart nog onbekend is |
| `sensor.e_niro_state_of_health` | |
| `binary_sensor.e_niro_laadt`, `binary_sensor.e_niro_snelladen`, `binary_sensor.e_niro_contact` | *laadt* = snelladen, of meer dan 1 A de accu in met het contact uit (dezelfde definitie als eniro-soc). *laadt* en *snelladen* worden onbeschikbaar na 15 minuten zonder BMS-record: tijdens laden komt er minstens elke 5 minuten een, dus stilte betekent dat de stand onbekend is, niet dat het laden gestopt is |
| `device_tracker.e_niro_locatie` | GPS van de dongle (10.4) |
| `sensor.autopi_12v_spanning`, `sensor.autopi_12v_niveau`, `sensor.autopi_12v_laadstatus` | 12V-accu, gemeten door de dongle |
| `sensor.autopi_laatste_meting` | tijdstip van de laatste 12V-meting |
| `binary_sensor.autopi_dongle_online` | of de dongle wakker en verbonden is |
| `sensor.autopi_4g_signaal`, `sensor.autopi_4g_ruisverhouding`, `sensor.autopi_netwerk` | van autopi-diag |

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
- **De kilometerstand komt alleen mee tijdens rijden.** Pas na de eerste rit
  met contact aan heeft de sensor een waarde.
- **Het verbruik ook, en na een herstart van Home Assistant is het `unknown`**
  tot de volgende rit (het retained bericht heeft dan geen verbruik). Daarom
  bewaart een automatisering de laatste geldige waarde in
  `input_number.e_niro_verbruik_laatst_bekend`, en rekent de actieradius
  daarmee verder. Zo stijgt de actieradius gewoon mee tijdens het laden. De
  eerste 100 km is er nog geen verbruik; vul de helper desgewenst één keer met
  de hand (bv. 150 Wh/km).

### 10.4 Locatie

De tracker `device_tracker.e_niro_locatie` staat al in `eniro-mqtt.yaml`.
**Controleer wel hoe de payload op `autopi/track/pos` eruitziet** (met 10.1) —
de veldnamen kunnen per firmwareversie verschillen. Op v1.22.9 staat de positie
in `loc.lat` / `loc.lon`, met `alt`, `sog` (snelheid in **km/h**, niet in
knopen — gecontroleerd op 286 metingen), `cog` (koers), `fix` en het aantal
satellieten. Heten de velden bij jou anders, pas dan de templates in het
bestand aan.

De tracker staat op `home` als de auto binnen 100 m van `zone.home` staat.

### 10.5 Profielen: dagelijks en reis

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
  (8.2). Anders blijft de dongle op `PROFIEL_STANDAARD` staan.

Controleren op de dongle:

```bash
sudo journalctl -u autopi-ingest | grep Profiel   # "Profiel: dagelijks → reis" bij een wissel
sudo cat /var/lib/autopi-ingest/profiel           # het onthouden profiel (alleen met sudo leesbaar)
```

### 10.6 Meldingen in plaats van spraak

De spraakmeldingen staan uit (7.2); `eniro-autopi.yaml` bevat drie
automatiseringen die er een melding van maken:

| Automatisering | Topic | Wanneer |
|---|---|---|
| AutoPi - accu kritiek | `autopi/vehicle/battery/critical_level` | altijd — de 12V-accu is laag en de dongle gaat hibernateren |
| AutoPi - geen 4G | `autopi/system/network/wwan0/offline` | pas als het vijf minuten aanhoudt; verdwijnt als de verbinding terugkomt |
| AutoPi - update mislukt | `autopi/system/release/failed` | altijd |

Ze maken een persistent notification (de bel in de zijbalk). Wil je ook een
pushmelding, voeg dan je eigen notify-actie toe; de kop van het bestand laat
zien hoe.

Omdat alles retained is, vuurt een MQTT-trigger ook op een oud bericht zodra
Home Assistant zich abonneert. Daarom vergelijken de automatiseringen het veld
`@ts` uit de payload met nu: ouder dan een uur geeft geen melding. Bouw je zelf
iets op deze topics, doe dan hetzelfde.

### 10.7 ABRP — optioneel

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

**Rij je met ABRP, zet dan het profiel op Reis** (10.5). In profiel dagelijks
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

## 11. Inbouwen in de auto

### 11.1 De OBD-poort

Die zit bij de e-Niro links onder het stuur, achter het onderpaneel van het
dashboard, en wijst naar beneden. De dongle is te groot om achter het
standaardklepje te blijven zitten; vandaar de verlengkabel.

**De verlengkabel:** alle 16 pinnen doorverbonden (minimaal 16 voor +12V, 4 en
5 voor massa, 6 en 14 voor CAN), haaks, en zo kort als de route toelaat. Een
lange dunne lintkabel geeft bij ~2 A merkbaar spanningsval. Een versie met
schakelaar is handig: dan kun je de dongle bij lange stilstand spanningsloos
maken zonder onder het dashboard te kruipen.

### 11.2 Het onderpaneel eraf

Uit de werkplaatshandleiding van de Niro
(https://www.kniro.net/crash_pad_lower_panel_repair_procedures-1443.html):

1. Wip het **zijkapje** los aan de linkerkant van het dashboard (zichtbaar met
   het portier open). Kunststof spatel, geen schroevendraaier.
2. Draai de schroef los die daarachter zit.
3. Trek het onderpaneel naar je toe; de rest zit op clips.
4. Koppel de connector van het tankklepje los.

Terugplaatsen in omgekeerde volgorde. Clips breken vaak bij de eerste keer;
vervang ze in plaats van ze terug te duwen.

### 11.3 Waar je van afblijft

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

### 11.4 Plaats van de dongle

De dongle heeft interne antennes voor 4G en GPS. Een plek hoger en met meer
"zicht" naar boven geeft een snellere GPS-fix en beter 4G-signaal. Niet achter
een metalen paneel wegwerken. Met `autopi-diag` (9.6) kun je een plek een paar
dagen meten voordat je hem definitief vastzet.

---

## 12. Controleren

### 12.1 Op de dongle

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

Draai je de ontvanger op een server (8.7), gebruik dan
`sudo ONTVANGER= ~/eniro-obd/scripts/verify.sh`.

### 12.2 De hele keten

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
- [ ] Na een slaap: `journalctl --list-boots` toont de vorige boot tot het
      moment van uitschakelen (9.1)
- [ ] `FORCE 5V`-jumper eraf, `CM4 Flash`-jumper eraf

### 12.3 Dataverbruik

Gemeten met deze configuratie (ontvanger op de dongle, WireGuard, profiel
dagelijks): **0,02–0,11 MB per dag, gemiddeld ~0,06 MB per dag**. Een
nachtlading van zeven uur kostte 0,058 MB. Met 500 MB is het datavolume daarmee
geen beperking meer. Ter vergelijking: met de ontvanger op een server achter
HTTPS was het 2-8 MB per dag.

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

## 13. Als het niet werkt

### 13.1 Symptomen

| Symptoom | Oorzaak | Oplossing |
|---|---|---|
| rpiboot blijft op `Waiting for BCM…` | jumper niet goed tijdens power-on, of een laad-only USB-kabel | spanning eraf, jumper controleren, andere kabel, opnieuw |
| Dongle boot niet na het flashen | `CM4 Flash`-jumper zit er nog op | eraf |
| Dongle valt na een paar minuten uit | sleep-timer (4 min) of `battery_critical_level` | 4.2; auto in ready |
| Dongle gaat ~61 minuten na elke boot uit, ook tijdens laden | de reactor-uurtimer (`inactivity_fallback`, `period: 3600`) | `autopi-wakelock` (9.4); controleer `journalctl -u autopi-wakelock` |
| Dongle slaapt na een lading niet meer | wakelock liet los zonder een timer terug te zetten (oude versie) | huidige `autopi_wakelock.py` installeren |
| SSH over wifi: time-out, ping werkt ook niet | geen firewallregel voor `wlan0` | 6.2 |
| SSH over wifi traag, pings van ~2000 ms | hotspot en wifi-client delen één radio | ethernetkabel voor langer werk; trage toegang die werkt is beter dan de hotspot uitzetten |
| `sudo` duurt seconden | hostnaam niet in `/etc/hosts` | 4.4 |
| `salt-call` blijft hangen | wacht op een Enter na een ATTENTION-melding over de sleep timer | Enter |
| `salt-call state.sls` toont wifi-wachtwoorden | de diff van `wpa_supplicant.conf` | `--state-output=terse` (6.1); uitvoer niet delen |
| 4G: `option-unsubscribed`, cause #33 | SIM niet geactiveerd, of lege APN met IPv4v6 in de attach-profile | SIM activeren in het portaal; 5.2 |
| 4G: modem valt elke ~4 minuten van de USB-bus | modem staat op 2G (`PS: detached`), `qmi-manager` reset het steeds | 5.2, `AT+WS46=28` |
| AT-commando's geven `CME: operation not allowed` | modem in power save na het stoppen van `qmi-manager` | `qmicli -d /dev/cdc-wdm0 --dms-set-operating-mode=online` |
| `AT+CSQ` geeft `99,99` | idem — radio staat uit, niet "geen bereik" | idem |
| `qmi-manager` logt "Successfully set operator selection to automatic" maar het klopt niet | bekende onjuiste melding op dit modem | zelf controleren met `AT+COPS?` |
| `wg show`: handshake `0` | er is nog geen verkeer geweest, of Endpoint, sleutel of firewall thuis klopt niet | eerst verkeer opwekken (8.4); dan 13.4 |
| `wg0` ontbreekt na een boot | `wg-quick` startte voordat er netwerk of DNS was | `wg-reresolve.timer` brengt hem binnen 5 minuten op; `journalctl -t wg-reresolve` |
| Tunnel werkt op 4G, niet thuis op wifi | router doet geen hairpin NAT | lokale DNS-override voor `<VPN_HOSTNAAM>` (8.4) |
| Tunnel werkte, na een tijd niet meer | thuis-IP veranderd, endpoint niet opnieuw opgezocht | `wg-reresolve` (8.4); DDNS-record op alleen DNS? |
| Ontvanger geeft `503`, `/healthz` ook | broker niet bereikbaar | `sudo wg show`; broker, login, wachtwoord in `/etc/autopi-ingest.env`; de dongle probeert het later opnieuw |
| Ontvanger: `Connection refused` / not authorised | MQTT-login of ACL | 8.2 |
| Upload geeft `404` | pad in `engines.conf` of `/etc/eniro-soc.env` wijkt af van `INGEST_PATH` | alle drie gelijk maken |
| Profielwissel komt niet aan | automatisering heeft nog niet gepubliceerd, of de login mag het topic niet lezen | profiel heen en terug wisselen; ACL (8.2); 10.5 |
| eniro-soc: `OSError 100 (ENETDOWN)` | `can0` staat DOWN — de firmware brengt hem niet zelf omhoog | het script doet het zelf (`CAN_AUTO_UP=1`); staat dat uit, zet het aan |
| SOC valt midden in een lading weg tot de volgende boot | CAN-controller in bus-off, zonder automatisch herstel | 13.3 |
| eniro-soc: geen antwoord van de BMS | auto staat uit | ready of lader; met het contact uit slaapt de bus |
| `candump can0` laat niets zien | de diagnostische bus zwijgt tot je hem aanspreekt | geen fout; `--once --dry-run` is de echte test |
| AutoPi's eigen `obd.*`-commando's: `Not supported by SocketCAN interface` | die laag hoort bij een andere hardware-uitvoering | niet nodig; eniro-soc praat rechtstreeks met SocketCAN |
| SOC loopt tijdens laden in sprongen op | dongle slaapt tijdens het laden | `autopi-wakelock` (9.4) |
| Sensoren in HA onbeschikbaar zodra de auto stilstaat | een `availability_topic` op `autopi/dongle/status` of `autopi/status` | weghalen; zie 10.3 |
| ABRP krijgt SOC 0% of kilometerstand 0 | entity-ID met `_2`, of een sensor die `unavailable` is | 10.3; de conditie in `abrp.yaml` laten staan |
| HA-melding over iets van vorige week na een herstart | retained bericht zonder `@ts`-controle | 10.6 |
| Journal leeg na een slaap, boots van ~30 s | geen `SyncIntervalSec` | 9.1 |
| `journalctl -b -1` laat de verkeerde boot zien | eerste kernelregels met een tijdstempel uit 2019 | per boot-ID zoeken (13.2) |
| `engines.conf` gewijzigd, salt-minion start niet | YAML-fout, meestal inspringing van de URL | backup terugzetten, 7.5 |
| Ineens veel meer 4G-verbruik | onbekend proces | `wwan_teller.py --rapport` en `pieken.log` (12.3) |

### 13.2 Het journal per boot

```bash
sudo journalctl --list-boots | tail -5
```

Kopieer de boot-ID (de lange hex-string in de tweede kolom) van de boot die je
wilt zien, en zoek daarop:

```bash
sudo journalctl -b <BOOT_ID> -u eniro-soc -u autopi-ingest -u autopi-wakelock --no-pager
sudo journalctl -b <BOOT_ID> | grep -iE 'sleep|hibernat' | tail -20
sudo journalctl -b <BOOT_ID> -k | grep -i can                 # kernelmeldingen over CAN
```

Gebruik niet `-b -1`: door de kernelregels met een tijdstempel uit 2019 wijst
de nummering soms naar een andere boot. Filteren op tijd kan ook
(`--since` / `--until`); let op dat het journal in UTC staat.
`sudo journalctl --disk-usage` laat zien hoeveel ruimte het inneemt (maximaal
300 MB).

### 13.3 CAN in bus-off

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

### 13.4 Geen WireGuard-handshake

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
   hairpin NAT (8.4).
5. **Handshake wel, broker niet bereikbaar?** Dan is het de firewall thuis
   (8.3) of `AllowedIPs` op de dongle.

`sudo ~/eniro-obd/scripts/verify.sh` doet de controles op handshake en broker
in één keer.

---

## 14. Naslag en doorverkopen

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
(9.1), de wakelock of het herverbinden van de tunnel te testen.

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
| `/etc/salt/minion.d/engines.conf` | firmwareconfig, `url:` naar de ontvanger | nee (lokaal pad) |
| `/etc/salt/grains` | wifi-netwerken | **ja**, wifi-wachtwoorden |
| `/etc/hostapd/hostapd_uap0.conf` | de eigen hotspot | **ja**, hotspot-wachtwoord |
| `/etc/autopi-ingest.env` | ontvanger | **ja**, MQTT-wachtwoord |
| `/etc/eniro-soc.env` | eniro-soc, ook gelezen door autopi-diag | nee (bij de servervariant wel: het pad) |
| `/etc/wireguard/wg0.conf` | tunnel | **ja**, privésleutel |
| `/usr/local/sbin/wg-reresolve.sh` | endpoint opnieuw opzoeken | nee, wel je hostnaam |
| `/etc/systemd/journald.conf.d/persistent.conf` | persistent journal | nee |
| `/var/lib/eniro-soc/` | wachtrij, verbruiksgeschiedenis, `status.json` | persoonlijk (kilometerstanden) |
| `/var/lib/autopi-ingest/profiel` | het onthouden profiel | nee |
| `/var/lib/wwan-teller/` | 4G-verbruik per boot, piek-log | persoonlijk |
| `/var/lib/autopi-diag/` | diag-buffer | persoonlijk (cel-ID's) |
| `/var/log/journal/` | het persistente journal | persoonlijk (GPS, SSID's) |

### De CAN-adressen

| Module | Verzoek → antwoord | Wat |
|---|---|---|
| BMS | `7E4` → `7EC`, service 22, PID's `0101` en `0105` | SOC, spanning, stroom, temperaturen, energie, SOH |
| Instrumentencluster | `7C6` → `7CE`, DID `B002` | kilometerstand (alleen met contact aan) |

Alle formules en meetwaarden: `docs/pids-e-niro.md`.

### Alles terugdraaien

- `engines.conf`: `sudo cp /etc/salt/minion.d/engines.conf.orig /etc/salt/minion.d/engines.conf && sudo systemctl restart salt-minion`
- De diensten: `sudo systemctl disable --now eniro-soc autopi-ingest autopi-wakelock.timer autopi-diag.timer wwan-teller.timer wg-reresolve.timer wg-quick@wg0`
- Alles: opnieuw flashen (hoofdstuk 3). Dat overschrijft de hele eMMC.

### De dongle doorverkopen

`scripts/wipe.sh` haalt weg wat over jou gaat: wifi-wachtwoorden, de
WireGuard-sleutels, het MQTT-wachtwoord, je eigen endpoint, meetdata, het
persistente journal, shell-geschiedenis en SSH-sleutels. Het zet de firmware-url
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
