#!/bin/bash
# wipe.sh - haalt persoonlijke sporen van een AutoPi af voordat hij van eigenaar
# wisselt. Draai als root, op het apparaat zelf.
#
#   sudo ./wipe.sh              # laat zien wat er zou gebeuren, verandert niets
#   sudo ./wipe.sh --yes        # voert het uit
#   sudo ./wipe.sh --check      # alleen de eindcontrole
#
# Dit wist geen metingen die al verstuurd zijn en geen firmware. Het haalt weg
# wat over JOU gaat: wifi-wachtwoorden, je endpoint, je sleutels, je ritten en
# je logs. Lees het door voor je het draait - je kunt het niet terugdraaien.
#
# Ook meegenomen: de WireGuard-sleutels, het MQTT-wachtwoord van de lokale
# ontvanger, de status- en tellerbestanden, het journal en de rsyslog-logs, en de
# config.env en backups van install.sh.
set -u

DOEN=0
ALLEEN_CHECK=0
for a in "$@"; do
    case "$a" in
        --yes|-y)  DOEN=1 ;;
        --check)   ALLEEN_CHECK=1 ;;
        *) echo "onbekende optie: $a"; exit 2 ;;
    esac
done

[ "$(id -u)" -eq 0 ] || { echo "Draai dit als root."; exit 1; }

doe() {
    if [ "$DOEN" -eq 1 ]; then
        echo "   > $*"
        # shellcheck disable=SC2294  # bewust: de commando's komen als tekst binnen
        eval "$@"
    else
        echo "   zou doen: $*"
    fi
}
kop() { echo; echo "== $1"; }

# Wist een .voor-wipe-backup, maar pas als het bijgewerkte bestand de controle
# doorstaat. De backup bevat de oude waarden (token, endpoint, hotspot-PSK) en
# mag dus niet blijven staan; bij een mislukte controle is hij nodig om te
# herstellen, en dan blijft hij staan met een waarschuwing.
#   ruim_backup <bestand> <controle-commando>
ruim_backup() {
    b="$1.voor-wipe"
    if [ "$DOEN" -ne 1 ]; then
        echo "   zou doen: controleren dat $1 klopt, dan shred -u '$b'"
        return 0
    fi
    [ -e "$b" ] || return 0
    if eval "$2"; then
        shred -u "$b" 2>/dev/null || rm -f "$b"
        echo "   > $b gewist (bevatte de oude waarden)"
    else
        echo "   !! $1 doorstaat de controle niet - $b blijft staan om te herstellen."
        echo "   !! Herstel, draai opnieuw, en wis $b daarna zelf: hij bevat je oude geheimen."
    fi
}

if [ "$ALLEEN_CHECK" -eq 0 ]; then

[ "$DOEN" -eq 1 ] || echo "PROEFDRAAI - er verandert niets. Voeg --yes toe om het echt te doen."

kop "1. Wifi-wachtwoorden"
# De PSK's van ALLE toegevoegde netwerken (thuis, vakantieadressen, travel
# router) staan in platte tekst in de salt-grains, onder wpa_supplicant.
# Ook back-ups van de grains bevatten ze.
# De systeem-Python heeft niet altijd PyYAML; salt wel. Dan de interpreter van salt-call gebruiken.
PY_YAML=python3
if ! python3 -c 'import yaml' 2>/dev/null; then
    PY_YAML=$(sed -n '1s/^#! *//p' "$(command -v salt-call)")
    $PY_YAML -c 'import yaml' 2>/dev/null || { echo "FOUT: geen Python met yaml gevonden — wifi-netwerken NIET gewist" >&2; exit 1; }
fi
doe "$PY_YAML - <<'PY'
import yaml
p = '/etc/salt/grains'
try:
    g = yaml.safe_load(open(p)) or {}
except Exception as e:
    raise SystemExit('grains niet leesbaar: %s' % e)
weg = [k for k in list(g) if str(k).startswith('wpa_supplicant')]
for k in weg:
    g.pop(k)
if weg:
    yaml.safe_dump(g, open(p, 'w'), default_flow_style=False)
    print('uit grains verwijderd: %s' % ', '.join(weg))
else:
    print('geen wpa_supplicant in grains')
PY"
for f in /etc/salt/grains.bak* /etc/salt/grains.*~ /etc/salt/grains.orig; do
    [ -e "$f" ] || continue
    doe "shred -u '$f' 2>/dev/null || rm -f '$f'"
done
for f in /etc/wpa_supplicant/wpa_supplicant*.conf; do
    [ -e "$f" ] || continue
    doe "sed -i '/^network={/,/^}/d' '$f'"
done

kop "2. Endpoint en token in engines.conf"
CONF=/etc/salt/minion.d/engines.conf
if [ -f "$CONF" ]; then
    doe "cp -n '$CONF' '$CONF.voor-wipe'"
    # Terug naar de standaard: de ontvanger op de dongle zelf. Dat adres is
    # niet persoonlijk; een eigen endpoint met geheim pad wel.
    doe "sed -i -E 's#(url: *).*#\\1http://127.0.0.1:8181/ingest/lokaal#' '$CONF'"
    doe "sed -i -E 's#(auth_token: *).*#\\1VUL_HIER_JE_EIGEN_TOKEN_IN#' '$CONF'"
else
    echo "   $CONF bestaat niet - overslaan"
fi

kop "3. Eigen configuratie terug naar de template"
# /etc/autopi-ingest.env bevat het MQTT-wachtwoord (en bij de servervariant
# het geheime pad).
for f in /etc/eniro-soc.env /etc/eniro-soc.secret /etc/autopi-ingest.env \
         /etc/eniro-soc.*.bak* /etc/autopi-ingest.env.bak* \
         /etc/salt/minion.d/engines.conf.bak* /etc/salt/minion.d/engines.conf.orig; do
    [ -e "$f" ] || continue
    doe "shred -u '$f' 2>/dev/null || rm -f '$f'"
done
echo "   let op: zet na afloop een verse .env terug uit eniro-soc.env.example"
echo "   en autopi-ingest-dongle.env.example"
# Backups die install.sh (v1.2+) maakte: <bestand>.bak-<datum>-<tijd>. Ze
# bevatten de oude wachtwoorden, sleutels en hotspot-PSK.
while IFS= read -r f; do
    doe "shred -u '$f' 2>/dev/null || rm -f '$f'"
done < <(find /etc /home/pi /root /usr/local/sbin /opt -xdev -type f -name '*.bak-[0-9]*-[0-9]*' 2>/dev/null)

kop "3a. config.env van install.sh"
# config.env bevat ALLE geheimen in een bestand: wifi, hotspot, MQTT, WireGuard.
# Zoek hem in de kloon van de repository (en eventuele exports ernaast).
HIER=$(cd "$(dirname "$0")/.." 2>/dev/null && pwd)
gezien=" "
for f in "$HIER"/config.env* /home/pi/eniro-obd/config.env* /root/eniro-obd/config.env*; do
    [ -f "$f" ] || continue
    case "$f" in *.example) continue ;; esac
    echt=$(readlink -f "$f")
    case "$gezien" in *" $echt "*) continue ;; esac
    gezien="$gezien$echt "
    doe "shred -u '$echt' 2>/dev/null || rm -f '$echt'"
done
for f in /var/lib/eniro-obd-install /var/log/eniro-obd-install-*.log; do
    [ -e "$f" ] || continue
    doe "rm -rf '$f'"
done

kop "3b. WireGuard - sleutels en endpoint"
# wg0.conf bevat de privesleutel van de dongle (en eventueel een PSK);
# wg-reresolve.sh bevat de hostname van jouw VPN-server.
doe "systemctl disable --now wg-reresolve.timer 2>/dev/null || true"
doe "wg-quick down wg0 2>/dev/null || true"
doe "systemctl disable wg-quick@wg0 2>/dev/null || true"
for f in /etc/wireguard/*; do
    [ -e "$f" ] || continue
    doe "shred -u '$f' 2>/dev/null || rm -f '$f'"
done
RR=/usr/local/sbin/wg-reresolve.sh
if [ -f "$RR" ]; then
    doe "sed -i -E 's#^PEER=.*#PEER=\"<SERVER_PUBLIC_KEY>\"#; s#^ENDPOINT=.*#ENDPOINT=\"<VPN_HOSTNAAM>:<POORT>\"#' '$RR'"
fi

kop "4. Meetgegevens - ritten, kilometerstand, locaties"
# De diag-buffer bevat cell-ID's, de spool bevat kilometerstanden.
# /var/lib/eniro-soc: spool, verbruik.jsonl, status.json, geladen-sinds-punt.
# /var/lib/autopi-ingest: profiel, discovery-hash (en bij de servervariant ruwe payloads).
# /var/lib/wwan-teller: 4G-verbruik per boot, piek-log met open verbindingen.
for d in /var/lib/eniro-soc /var/lib/autopi-diag /var/lib/autopi-wakelock \
         /var/lib/autopi-ingest /var/lib/wwan-teller; do
    [ -d "$d" ] || continue
    doe "find '$d' -mindepth 1 -delete"
done

kop "5. Salt-cache en onverzonden uploads"
doe "rm -rf /var/cache/salt/minion/* 2>/dev/null || true"

kop "6. Logs"
# Hier staan GPS-coordinaten, SSID's en het geheime pad in. Dit is de grote.
doe "journalctl --rotate"
doe "journalctl --vacuum-time=1s"
# Het journal staat persistent op de eMMC (/var/log/journal) en overleeft dus
# elke slaap. Vacuum laat de actieve bestanden staan; die gaan hier weg.
if [ -d /var/log/journal ]; then
    doe "systemctl stop systemd-journald.socket systemd-journald-dev-log.socket systemd-journald 2>/dev/null || true"
    doe "find /var/log/journal -type f -delete"
    doe "systemctl start systemd-journald 2>/dev/null || true"
fi
doe "find /var/log -type f -name '*.gz' -delete"
doe "find /var/log -type f \\( -name '*.1' -o -name '*.old' \\) -delete"
# rsyslog schrijft dezelfde regels ook naar platte tekst (/var/log/syslog,
# daemon.log, messages, ...). Daarin staat net zoveel als in het journal.
doe "find /var/log -type f \\( -name syslog -o -name messages -o -name '*.log' \\) -exec truncate -s 0 {} +"

kop "7. Shell-geschiedenis"
for h in /root/.bash_history /home/pi/.bash_history /root/.python_history /home/pi/.python_history; do
    [ -e "$h" ] || continue
    doe "shred -u '$h' 2>/dev/null || rm -f '$h'"
done

kop "8. SSH"
doe "rm -f /home/pi/.ssh/authorized_keys /root/.ssh/authorized_keys"
doe "rm -f /home/pi/.ssh/known_hosts /root/.ssh/known_hosts"
# Nieuwe hostsleutels, anders deelt de koper jouw fingerprint.
doe "rm -f /etc/ssh/ssh_host_*"
doe "dpkg-reconfigure openssh-server 2>/dev/null || ssh-keygen -A"

kop "9. Hotspot: nieuw wachtwoord"
HOSTAPD=/etc/hostapd/hostapd_uap0.conf
if [ -f "$HOSTAPD" ]; then
    NIEUW=$(tr -dc 'A-Za-z0-9' </dev/urandom | head -c 16)
    doe "cp -n '$HOSTAPD' '$HOSTAPD.voor-wipe'"
    doe "sed -i -E 's/^wpa_passphrase=.*/wpa_passphrase=$NIEUW/' '$HOSTAPD'"
    echo
    echo "   >>> NIEUWE HOTSPOT-PSK: $NIEUW"
    echo "   >>> Schrijf deze op het kaartje dat meegaat. Hij staat nergens anders."
else
    echo "   $HOSTAPD bestaat niet - overslaan"
fi

kop "9b. Back-ups van deze wipe opruimen"
# Controle engines.conf: de url staat op de lokale ontvanger en er staat geen
# auth_token meer in dat niet de placeholder is.
ruim_backup "$CONF" "grep -Eq 'url: *http://127\\.0\\.0\\.1:8181/ingest/lokaal' '$CONF' && ! grep -E 'auth_token:' '$CONF' | grep -vq 'VUL_HIER_JE_EIGEN_TOKEN_IN'"
# Controle hotspot: de nieuwe PSK staat erin.
if [ -n "${NIEUW:-}" ]; then
    ruim_backup "$HOSTAPD" "grep -qx 'wpa_passphrase=$NIEUW' '$HOSTAPD'"
fi

kop "10. Nog met de hand"
cat <<'TXT'
   - passwd pi            een nieuw wachtwoord, en op het kaartje
   - de SIM eruit         die hangt aan jouw account
   - op je VPN-server     de WireGuard-client van de dongle intrekken
   - op je MQTT-broker    de login van de dongle intrekken
   - reboot               en daarna --check opnieuw draaien
TXT

fi

# ---------------------------------------------------------------- eindcontrole
echo
echo "== Eindcontrole"
echo "Vul hieronder je eigen termen in en draai het. Geen uitvoer is het bewijs."
echo
cat <<'TXT'
   grep -rIls --binary-files=without-match \
     -e 'jouwdomein.nl' -e 'JOUW-WIFI-SSID' -e 'jouw-hex-pad' \
     -e 'JOUW-VPN-HOSTNAAM' -e 'JOUW-BROKER-IP' \
     /etc /home /root /opt /var/lib /var/log 2>/dev/null
TXT
echo
echo "Draai dit ook een keer NA een reboot: sommige diensten schrijven bij het"
echo "opstarten opnieuw weg wat je net hebt gewist."
