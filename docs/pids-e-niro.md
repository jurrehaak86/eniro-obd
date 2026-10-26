# Kia e-Niro — BMS-PID's ontcijferd en geverifieerd

Vastgesteld op **02-09-2026**, met de auto in ready op de oprit.
Referentie: dashboard 48-49% SOC · 192 km actieradius · 179.542 km · 21 °C.

## Hoe je het uitvraagt

```
CAN-interface  can0   (mcp251x, bitrate 500000)
BMS request    7E4
BMS response   7EC
Service        22 (readDataByIdentifier), PID's 0101 t/m 0105
```

De OBD-bus is **stil tot je iets vraagt** — dat is bij HKMC normaal en geen
storing. Multi-frame antwoorden vereisen dat jij flow control stuurt.

```bash
candump -l can0 &
cansend can0 7E4#0322010100000000   # verzoek
sleep 0.3
cansend can0 7E4#3000000000000000   # flow control: ga door, geen limiet
```

`220101` levert **62 bytes**, `220105` **46 bytes**.

## De byteconventie — bewezen, niet aangenomen

De letters uit de Torque/JejuSoul-tabellen beginnen bij de **eerste byte ná de
echo** `62 01 0X`. Dus `a` = byte 1 na de echo, en dan door: `a`…`z`, `aa`…`az`,
`ba`…`bg`.

Dat is niet gegokt maar op vijf punten geverifieerd (zie onderaan).

## Meetwaarden van deze sessie

### `220101` — 59 databytes

```
a=FF  b=F7  c=E7  d=FF  e=5F  f=42  g=68  h=42  i=68  j=03  k=00  l=14  m=0E
n=28  o=15  p=14  q=15  r=15  s=14  t=15  u=00  v=00  w=17  x=B9  y=0C  z=B8
aa=58 ab=00 ac=00 ad=90 ae=00 af=0E ag=21 ah=F9 ai=00 aj=0E ak=20 al=E5 am=00
an=05 ao=54 ap=15 aq=00 ar=05 as=31 at=A9 au=02 av=76 aw=9B ax=A8 ay=0D az=01
ba=6A bb=00 bc=00 bd=00 be=00 bf=03 bg=E8
```

| Grootheid | Formule | Uitkomst |
|---|---|---|
| **SOC BMS** | `e / 2` | **47,5 %** |
| Aux 12V-spanning | `ad * 0.1` | **14,4 V** |
| Accuspanning HV | `((m<8)+n) / 10` | **362,4 V** |
| Accustroom | `((signed(k)*256)+l) / 10` | **2,0 A** |
| Accuvermogen | stroom × spanning / 1000 | **0,72 kW** |
| Accu max temp | `signed(o)` | **21 °C** |
| Accu min temp | `signed(p)` | **20 °C** |
| Moduletemp 1-4 | `signed(q..t)` | **21, 21, 20, 21 °C** |
| Inlaattemperatuur | `signed(w)` | **23 °C** |
| Inverter-condensatorspanning | `((az<8)+ba)` | **362 V** |
| Isolatieweerstand | `((bf<8)+bg)` | **1000 kOhm** |
| BMS main relay | bit 0 van `j` | **1** (dicht) |
| HV aan het snelladen | bit 7 van `j` | **0** |
| BMS ignition | bit 2 van `ay` | **1** (contact aan) |
| Cumulatief geladen | `((ae<24)+(af<16)+(ag<8)+ah)/10` | **92.620,1 Ah** |
| Cumulatief ontladen | `((ai<24)+(aj<16)+(ak<8)+al)/10` | **92.592,5 Ah** |
| Cumulatieve energie geladen | `((am<24)+(an<16)+(ao<8)+ap)/10` | **34.920,5 kWh** |
| Cumulatieve energie verbruikt | `((aq<24)+(ar<16)+(as<8)+at)/10` | **34.039,3 kWh** |

### `220105` — 43 databytes

```
a=00  b=3F  c=FF  d=90  e=00  f=00  g=00  h=00  i=00  j=00  k=00  l=00  m=00
n=00  o=00  p=00  q=42  r=68  s=42  t=68  u=00  v=01  w=50  x=17  y=00  z=03
aa=E8 ab=2F ac=03 ad=E8 ae=01 af=61 ag=00 ah=00 ai=B8 aj=B8 ak=00 al=00 am=15
an=00 ao=00 ap=00 aq=00
```

| Grootheid | Formule | Uitkomst |
|---|---|---|
| **SOC Display** | `af / 2` | **48,5 %** |
| State of Health | `((z<8)+aa) / 10` | **100,0 %** |
| Celspanningsafwijking | `u / 50` | **0,00 V** |
| Accuverwarming temp 1 | `signed(x)` | **23 °C** |

### `220102`, `220103`, `220104` — celspanningen

Alle drie geven `FF` gevolgd door 32× dezelfde waarde:

```
7EC#1027620102FFFFFF  +  21FFB8B8B8B8B8B8 … 25B8B8B8B8B8AAAA
```

`0xB8` = 184 → **× 0,02 = 3,68 V per cel**. Alle 98 cellen identiek, geen
enkele afwijking. 3 × 32 = 96 cellen plus de twee uit de header = 98, wat klopt
met het 64 kWh-pakket.

## Waarom we weten dat dit klopt

| Controle | Berekend | Onafhankelijke bron |
|---|---|---|
| SOC | 47,5 % (BMS) / 48,5 % (display) | dashboard **48-49 %** |
| Aux 12V | 14,4 V | `obd.battery` gaf **14,44 V** |
| Pakspanning | 362,4 V | 98 cellen × 3,68 V = **360,6 V** |
| Accutemperatuur | 20-21 °C | buitentemperatuur **21 °C** |
| Verbruik levenslang | 34.920 kWh / 179.542 km = **194 Wh/km** | plausibel voor een e-Niro |
| Isolatieweerstand | 1000 kOhm | maximumwaarde, verwacht bij een gezond pak |
| Celafwijking | 0,00 V | alle cellen meten identiek 3,68 V |

Zes anker­punten over de hele lengte van het bericht — van byte 5 tot byte 59.
Als de offsets fout zouden zijn, zou dat niet allemaal kunnen kloppen.

## Twee dingen die niet kloppen

**Max regen en max vermogen geven beide 170,00 kW.** De formule
`((f<8)+g)/100` levert `0x4268 / 100` = 170, en dat kan niet — die velden
horen tussen 0 en 98 kW te liggen. Beide velden geven exact dezelfde waarde,
wat erop wijst dat het hier iets anders betekent dan de tabel suggereert.
Mogelijk model- of firmwareafhankelijk. **Niet gebruiken tot uitgezocht.**

**SOH van 100,0 % bij 179.542 km.** De byte zegt `0x03E8` = 1000, dus 100,0 %.
De decodering is niet het probleem; de vraag is of de BMS dit eerlijk
rapporteert. Bij een e-Niro met deze kilometerstand zou je 90-95 % verwachten.
Meet dit over een paar maanden opnieuw — als het getal nooit beweegt, zegt het
niets.

## Volgende stap: dit automatiseren

In `engines.conf` staat `obd_manager` met `workers: null`. Daar komt een worker
die deze twee queries periodiek uitvoert en het resultaat via `returner: cloud`
in de keten zet die al staat — dan verschijnt de SOC op MQTT onder `autopi/`.

Aandachtspunt: AutoPi's eigen `obd.query` werkte op dit board niet
(SocketCAN, geen STN — zie `../archief/autopi/obd-werkblad-eniro.md`). Waarschijnlijk moeten we
het zelf doen: een klein script dat `cansend`/`isotp` gebruikt en publiceert.
Dat is te doen, en het hoeft alleen te draaien als het voertuig wakker is.

Polling volgens de eerder bedachte strategie: rijden 1-2 min, laden 10 min,
idle 30-60 min — met het databudget als tweede randvoorwaarde.

## Bronnen
- PID-formules: https://github.com/JejuSoul/OBD-PIDs-for-HKMC-EVs
  (`Hyundai Kona EV & Kia Niro EV/extendedpids/003_Kona&Niro_EV_BMS.csv`)
- Ruwe frames: `candump-2026-09-02_150806.log` op het apparaat
