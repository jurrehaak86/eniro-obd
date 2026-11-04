#!/bin/bash
# install.sh - richt een AutoPi-dongle in voor eniro-obd vanuit één config.env.
#
#   sudo ./install.sh --dry-run [config.env]   laat zien wat er zou veranderen
#   sudo ./install.sh [config.env]             voert het uit
#   sudo ./install.sh --export-config [pad]    leest een bestaande dongle uit
#                                              naar een ingevulde config.env
#   sudo ./install.sh --rollback <ts>          zet de bestanden van run <ts> terug
#   sudo ./install.sh --purge-backups <ts>     wist de backups van run <ts>
#
# Draai het op de dongle, vanuit een kloon van de repository. Wat het doet, stap
# voor stap, staat in INSTALLATIE.md, bijlage A. Het kan veilig opnieuw: wat al
# klopt, blijft staan, en wat niet verandert wordt niet herstart.
#
# Voor tests (zie de README): ROOT=<map> zet alle paden onder een andere map, en
# INSTALL_TEST=1 vervangt alles wat het systeem raakt (apt, systemctl, salt-call,
# iptables, chown) door stubs die alleen een logregel schrijven.
#
# Geen set -x: de bestanden die hier langskomen bevatten wachtwoorden.
set -u -o pipefail
umask 077
# Python 2 (salt) schrijft anders geen niet-ASCII naar een pipe; geen .pyc in de kloon.
export PYTHONIOENCODING=utf-8 PYTHONDONTWRITEBYTECODE=1

VERSIE=1.2.0
REPO=$(cd "$(dirname "$0")" && pwd)
HELPER="$REPO/scripts/install_helper.py"
ROOT=${ROOT:-}
ROOT=${ROOT%/}
INSTALL_TEST=${INSTALL_TEST:-0}
TS=${INSTALL_TS:-$(date +%Y%m%d-%H%M%S)}
STATEDIR=/var/lib/eniro-obd-install

DRY=0
MODE=install
CONFIG=""
ARG=""
VERIFY=1
while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run|-n)    DRY=1 ;;
        --export-config) MODE="export"; if [ $# -gt 1 ] && [ "${2#-}" = "$2" ]; then ARG=$2; shift; fi ;;
        --rollback)      MODE=rollback; ARG=${2:-}; shift ;;
        --purge-backups) MODE=purge; ARG=${2:-}; shift ;;
        --no-verify)     VERIFY=0 ;;
        -h|--help)       sed -n '2,20p' "$0"; exit 0 ;;
        -*)              echo "onbekende optie: $1" >&2; exit 2 ;;
        *)               CONFIG=$1 ;;
    esac
    shift
done
[ -n "$CONFIG" ] || CONFIG="$REPO/config.env"

kop()  { echo; echo "== $*"; }
zeg()  { echo "   $*"; }
die()  { echo "FOUT: $*" >&2; exit 1; }
R()    { echo "$ROOT$1"; }

if [ "$INSTALL_TEST" != 1 ] && [ "$(id -u)" -ne 0 ]; then
    die "draai dit als root: sudo $0 $*"
fi

# ------------------------------------------------------------------ systeem
# Alles wat het systeem raakt loopt hierdoor, zodat het in testmodus te
# vervangen is. In testmodus houdt $ROOT/.teststate bij wat 'aan' staat.
TST="$ROOT/.teststate"
TESTLOG="$TST/commando.log"
tstub() { mkdir -p "$TST"; echo "$*" >> "$TESTLOG"; }
tset()  { mkdir -p "$TST"; grep -qxF "$2" "$TST/$1" 2>/dev/null || echo "$2" >> "$TST/$1"; }
tget()  { grep -qxF "$2" "$TST/$1" 2>/dev/null; }

sys_run() {          # sys_run <commando...>  - uitvoer naar /dev/null
    if [ "$INSTALL_TEST" = 1 ]; then tstub "$*"; return 0; fi
    "$@" </dev/null >/dev/null 2>&1
}
pkg_ok() {
    if [ "$INSTALL_TEST" = 1 ]; then tget pkgs "$1"; return; fi
    dpkg-query -W -f='${Status}' "$1" 2>/dev/null | grep -q 'install ok installed'
}
user_ok() {
    if [ "$INSTALL_TEST" = 1 ]; then grep -q "^$1:" "$(R /etc/passwd)" 2>/dev/null; return; fi
    id -u "$1" >/dev/null 2>&1
}
unit_enabled() {
    if [ "$INSTALL_TEST" = 1 ]; then tget enabled "$1"; return; fi
    [ "$(systemctl is-enabled "$1" 2>/dev/null)" = enabled ]
}
unit_active() {
    if [ "$INSTALL_TEST" = 1 ]; then tget active "$1"; return; fi
    systemctl is-active --quiet "$1"
}
live_rules_ok() {    # staan de SSH- en ICMP-regel live in iptables?
    local sub=$1
    if [ "$INSTALL_TEST" = 1 ]; then tget iptables "$sub"; return; fi
    iptables -C INPUT -s "$sub" -i wlan0 -p tcp -m state --state NEW -m tcp --dport 22 -j ACCEPT 2>/dev/null &&
    iptables -C INPUT -s "$sub" -i wlan0 -p icmp -j ACCEPT 2>/dev/null
}
salt_ok() {
    if [ "$INSTALL_TEST" = 1 ]; then tstub "salt-call --local test.ping"; [ "${INSTALL_TEST_SALT_FAIL:-0}" != 1 ]; return; fi
    local uit
    uit=$(timeout 120 salt-call --local test.ping </dev/null 2>/dev/null) || return 1
    echo "$uit" | grep -q True
}
do_chown() {         # do_chown <eigenaar:groep> <pad>
    if [ "$INSTALL_TEST" = 1 ]; then tstub "chown $1 ${2#"$ROOT"}"; return 0; fi
    chown "$1" "$2"
}
svc() {              # svc enable|restart|start <unit>
    sys_run systemctl "$1" "$2"
    if [ "$INSTALL_TEST" = 1 ]; then
        case "$1" in enable) tset enabled "$2" ;; restart|start) tset active "$2" ;; esac
    fi
}

# Python met PyYAML: de systeem-Python heeft het meestal niet, die van salt wel.
find_pyy() {
    if [ -n "${INSTALL_PY:-}" ]; then echo "$INSTALL_PY"; return; fi
    if python3 -c 'import yaml' 2>/dev/null; then echo python3; return; fi
    local sc py
    sc=$(command -v salt-call 2>/dev/null) || return 1
    py=$(sed -n '1s/^#! *//p' "$sc")
    # shellcheck disable=SC2086
    [ -n "$py" ] && $py -c 'import yaml' 2>/dev/null && { echo "$py"; return; }
    return 1
}
PYY=$(find_pyy) || die "geen Python met yaml gevonden (ook niet die van salt-call)"
PY3=${INSTALL_PY3:-python3}
H()  { $PYY "$HELPER" "$@"; }      # met yaml
H3() { $PY3 "$HELPER" "$@"; }      # systeem-Python (voor 'same py')

# ------------------------------------------------------------------ rollback/purge

manifest() { echo "$(R $STATEDIR)/$1.manifest"; }

if [ "$MODE" = rollback ] || [ "$MODE" = purge ]; then
    [ -n "$ARG" ] || die "geef de tijdstempel van de run, bijvoorbeeld --$MODE 20261027-101500"
    M=$(manifest "$ARG")
    [ -r "$M" ] || die "geen run $ARG gevonden ($M)"
    if [ "$MODE" = rollback ]; then
        kop "Terugdraaien van run $ARG"
        tac "$M" | while read -r soort pad; do
            case "$soort" in
                B) if [ -e "$ROOT$pad.bak-$ARG" ]; then cp -p "$ROOT$pad.bak-$ARG" "$ROOT$pad"; zeg "terug: $pad"
                   else zeg "!! backup ontbreekt: $pad.bak-$ARG"; fi ;;
                N) rm -f "$ROOT$pad"; zeg "weg (was nieuw): $pad" ;;
            esac
        done
        sys_run systemctl daemon-reload
        cat <<TXT

   De bestanden staan terug. Laat de diensten ze inlezen - het eenvoudigst met
   een herstart van de dongle:  sudo reboot
   Of los: sudo systemctl restart salt-minion eniro-soc autopi-ingest wg-quick@wg0 hostapd
   Wifi teruggezet? Dan ook:
     sudo salt-call --local state.sls network.wlan.client.config --state-output=terse --out=quiet </dev/null
TXT
    else
        kop "Backups van run $ARG wissen"
        while read -r soort pad; do
            [ "$soort" = B ] || continue
            f="$ROOT$pad.bak-$ARG"
            [ -e "$f" ] || continue
            shred -u "$f" 2>/dev/null || rm -f "$f"
            zeg "gewist: $pad.bak-$ARG"
        done < "$M"
        rm -f "$M"
    fi
    exit 0
fi

# ------------------------------------------------------------------ export

if [ "$MODE" = export ]; then
    UIT=${ARG:-$REPO/config.env}
    [ -e "$UIT" ] && die "$UIT bestaat al - geef een ander pad of haal hem eerst weg"
    kop "Huidige configuratie uitlezen naar $UIT"
    H export "$ROOT" "$UIT" "$REPO/config.env.example" || die "uitlezen mislukt"
    if [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != root ]; then do_chown "$SUDO_USER:" "$UIT"; fi
    zeg "geschreven, rechten 600. Bevat geheimen: niet delen."
    echo
    echo "Controleer het bestand en draai dan:  sudo $0 --dry-run $UIT"
    exit 0
fi

# ------------------------------------------------------------------ config controleren

kop "eniro-obd install.sh $VERSIE$([ $DRY = 1 ] && echo ' - PROEFDRAAI, er verandert niets')"
[ -r "$CONFIG" ] || die "config niet gevonden: $CONFIG
   Maak hem:  cp config.env.example config.env && chmod 600 config.env  en vul hem in."
perm=$(stat -c %a "$CONFIG" 2>/dev/null) || die "kan de rechten van $CONFIG niet lezen"
[ "$perm" = 600 ] || die "$CONFIG heeft rechten $perm; dat moet 600 zijn (er staan wachtwoorden in):
   chmod 600 $CONFIG"
zeg "config: $CONFIG"
if ! H validate "$CONFIG"; then
    echo
    die "config.env is niet compleet of niet geldig - zie de lijst hierboven. Er is niets veranderd."
fi
get() { H get "$CONFIG" "$1"; }

WORK=$(mktemp -d) || die "mktemp mislukt"
trap 'rm -rf "$WORK"' EXIT
declare -A CHANGED=()
NCONF=0; NCODE=0; NUNIT=0; NDOC=0; NFUNC=0; ACTIES=0
MANI=$(manifest "$TS")

if [ "$DRY" = 0 ]; then
    mkdir -p "$(R $STATEDIR)" && chmod 700 "$(R $STATEDIR)"
    [ -e "$MANI" ] && die "er is al een run met tijdstempel $TS - wacht een seconde"
    : > "$MANI"
    mkdir -p "$(R /var/log)"
    LOG=$(R "/var/log/eniro-obd-install-$TS.log")
    # Uitvoer ook naar een log (zonder geheimen). HUP negeren: valt de
    # SSH-verbinding weg als wifi of hotspot herstart, dan loopt dit door.
    trap '' HUP
    exec > >(tee -a "$LOG") 2>&1
    zeg "log: ${LOG#"$ROOT"}"
fi

# ------------------------------------------------------------------ bestanden

# mode_van <keep:NNN|NNN> <doel>  - bij keep: de huidige rechten aanhouden
mode_van() {
    case "$1" in
        keep:*) if [ -e "$2" ]; then stat -c %a "$2"; else echo "${1#keep:}"; fi ;;
        *) echo "$1" ;;
    esac
}

# apply_file <soort> <pad> <rechten> <eigenaar> <bron> [exact|text|py]
#   soort: conf (diff gemaskeerd tonen) | unit (diff) | code | doc (alleen aantallen)
apply_file() {
    local soort=$1 pad=$2 bron=$5 vgl=${6:-exact} own=$4
    local doel mode cm verb nota=""
    doel=$(R "$pad")
    mode=$(mode_van "$3" "$doel")
    if [ -e "$doel" ]; then
        if cmp -s "$doel" "$bron" || { [ "$vgl" != exact ] && H3 same "$vgl" "$doel" "$bron"; }; then
            cmp -s "$doel" "$bron" || { nota=" (alleen commentaar wijkt af; blijft staan)"; NFUNC=$((NFUNC+1)); }
            cm=$(stat -c %a "$doel")
            if [ "$cm" = "$mode" ]; then
                zeg "ongewijzigd  $pad$nota"
            elif [ "$DRY" = 1 ]; then
                zeg "rechten      $pad: $cm -> $mode$nota"; ACTIES=$((ACTIES+1))
            else
                chmod "$mode" "$doel"; zeg "rechten      $pad: $cm -> $mode$nota"; ACTIES=$((ACTIES+1))
            fi
            return 0
        fi
        verb="gewijzigd   "
    else
        verb="nieuw       "
    fi
    zeg "$verb $pad"
    CHANGED[$pad]=1
    case "$soort" in
        conf) NCONF=$((NCONF+1)) ;; code) NCODE=$((NCODE+1)) ;;
        unit) NUNIT=$((NUNIT+1)) ;; doc) NDOC=$((NDOC+1)) ;;
    esac
    if [ "$DRY" = 1 ]; then
        case "$soort" in
            conf|unit) if [ -e "$doel" ]; then H maskdiff "$CONFIG" "$doel" "$bron" "${pad#/}"
                       else H maskdiff "$CONFIG" "$doel" "$bron" "${pad#/}" summary; fi ;;
            *)         H maskdiff "$CONFIG" "$doel" "$bron" "${pad#/}" summary ;;
        esac
        return 0
    fi
    mkdir -p "$(dirname "$doel")"
    if [ -e "$doel" ]; then
        cp -p "$doel" "$doel.bak-$TS" || die "backup van $pad mislukt"
        echo "B $pad" >> "$MANI"
    else
        echo "N $pad" >> "$MANI"
    fi
    install -m "$mode" "$bron" "$doel" || die "schrijven naar $pad mislukt"
    do_chown "$own" "$doel"
}

# render <soort> <pad op de dongle> <sjabloon of -> -> $WORK/<soort>
render() {
    H render "$1" "$CONFIG" "$(R "$2")" "$3" "$WORK/$1" || die "kon $2 niet opbouwen - er is niets veranderd"
}

ensure_dir() {      # ensure_dir <pad> <rechten> [eigenaar]
    local d; d=$(R "$1")
    [ -d "$d" ] && return 0
    if [ "$DRY" = 1 ]; then zeg "nieuwe map   $1"; return 0; fi
    install -d -m "$2" "$d" || die "map $1 aanmaken mislukt"
    if [ -n "${3:-}" ]; then do_chown "$3" "$d"; fi
}

veranderd() { local p; for p in "$@"; do [ -n "${CHANGED[$p]:-}" ] && return 0; done; return 1; }

# dienst <unit> [bestanden...]: inschakelen; herstarten als een bestand
# veranderde, anders starten als hij niet draait.
dienst() {
    local u=$1; shift
    if ! unit_enabled "$u"; then
        ACTIES=$((ACTIES+1))
        if [ "$DRY" = 1 ]; then zeg "zou inschakelen  $u"; else svc enable "$u"; zeg "ingeschakeld  $u"; fi
    fi
    if veranderd "$@"; then
        ACTIES=$((ACTIES+1))
        if [ "$DRY" = 1 ]; then zeg "zou herstarten   $u"; else svc restart "$u"; zeg "herstart  $u"; fi
    elif ! unit_active "$u"; then
        ACTIES=$((ACTIES+1))
        if [ "$DRY" = 1 ]; then zeg "zou starten      $u"; else svc start "$u"; zeg "gestart  $u"; fi
    else
        zeg "ongewijzigd  $u (aan en actief)"
    fi
}

# ------------------------------------------------------------------ eerst alles opbouwen
# Alle doelbestanden worden eerst in een tijdelijke map opgebouwd. Gaat daar
# iets mis (engines.conf onverwacht, YAML ongeldig), dan stopt het script
# voordat er iets veranderd is.

SSH_SUB=$(get SSH_WIFI_SUBNET)
DIAG=$(get DIAG_INSTALL)
HA_DISC=$(get HA_DISCOVERY)
NWIFI=$(H nwifi "$CONFIG")
NKEYS=$(H sshkeys "$CONFIG" | grep -c . || true)

render eniro-env  /etc/eniro-soc.env      "$REPO/eniro-soc/eniro-soc.env.example"
render ingest-env /etc/autopi-ingest.env  "$REPO/ingest/autopi-ingest-dongle.env.example"
render wg0        /etc/wireguard/wg0.conf "$REPO/wireguard/wg0.conf.example"
render reresolve  /usr/local/sbin/wg-reresolve.sh "$REPO/wireguard/wg-reresolve.sh"
render engines    /etc/salt/minion.d/engines.conf -
render hostapd    /etc/hostapd/hostapd_uap0.conf -
[ "$NWIFI" -gt 0 ] && render grains /etc/salt/grains -
[ "$NKEYS" -gt 0 ] && render authkeys /home/pi/.ssh/authorized_keys -
[ -n "$SSH_SUB" ] && render rules /etc/iptables/rules.v4 -
printf '[Journal]\nStorage=persistent\nSystemMaxUse=100M\nSyncIntervalSec=30s\n' > "$WORK/journald"

# Zelftests van de scripts, vóór er iets geïnstalleerd wordt.
python3 "$REPO/eniro-soc/eniro_soc.py" --selftest >/dev/null 2>&1 || die "selftest van eniro_soc.py faalt (python3 eniro-soc/eniro_soc.py --selftest)"
python3 "$REPO/ingest/autopi_ingest.py" --selftest >/dev/null 2>&1 || die "selftest van autopi_ingest.py faalt"

# ------------------------------------------------------------------ 1. pakketten
kop "1. Pakketten (python3-paho-mqtt, wireguard-tools uit de Raspbian-repository)"
mist=()
for p in python3-paho-mqtt wireguard-tools; do pkg_ok "$p" || mist+=("$p"); done
if [ ${#mist[@]} -eq 0 ]; then
    zeg "ongewijzigd  beide aanwezig"
else
    ACTIES=$((ACTIES+1))
    if [ "$DRY" = 1 ]; then zeg "zou installeren: ${mist[*]}"
    else
        sys_run apt-get update || die "apt-get update mislukt - is er internet?"
        sys_run apt-get install -y "${mist[@]}" || die "installeren mislukt: sudo apt-get install -y ${mist[*]}"
        if [ "$INSTALL_TEST" = 1 ]; then for p in "${mist[@]}"; do tset pkgs "$p"; done; fi
        zeg "geïnstalleerd: ${mist[*]}"
    fi
fi

# ------------------------------------------------------------------ 2. gebruiker
kop "2. Systeemgebruiker autopi-ingest"
if user_ok autopi-ingest; then zeg "ongewijzigd  bestaat"
else
    ACTIES=$((ACTIES+1))
    if [ "$DRY" = 1 ]; then zeg "zou aanmaken: autopi-ingest (systeemgebruiker, geen shell)"
    else
        sys_run useradd --system --home /var/lib/autopi-ingest --shell /usr/sbin/nologin autopi-ingest || die "useradd mislukt"
        [ "$INSTALL_TEST" = 1 ] && echo "autopi-ingest:x:999:999::/var/lib/autopi-ingest:/usr/sbin/nologin" >> "$(R /etc/passwd)"
        zeg "aangemaakt"
    fi
fi

# ------------------------------------------------------------------ 3. journal
kop "3. Journal persistent (overleeft een slaap)"
ensure_dir /etc/systemd/journald.conf.d 755
apply_file conf /etc/systemd/journald.conf.d/persistent.conf 644 root:root "$WORK/journald" text
if [ ! -d "$(R /var/log/journal)" ]; then
    ACTIES=$((ACTIES+1)); CHANGED[/var/log/journal]=1
    if [ "$DRY" = 1 ]; then zeg "nieuwe map   /var/log/journal"; else install -d -m 2755 "$(R /var/log/journal)"; zeg "map aangemaakt  /var/log/journal"; fi
fi

# ------------------------------------------------------------------ 4/5. env
kop "4. /etc/eniro-soc.env"
apply_file conf /etc/eniro-soc.env 600 root:root "$WORK/eniro-env"
kop "5. /etc/autopi-ingest.env (MQTT)"
apply_file conf /etc/autopi-ingest.env 600 root:root "$WORK/ingest-env"

# ------------------------------------------------------------------ 6. wireguard
kop "6. WireGuard (split tunnel naar alleen de broker)"
ensure_dir /etc/wireguard 700
apply_file conf /etc/wireguard/wg0.conf 600 root:root "$WORK/wg0"
apply_file conf /usr/local/sbin/wg-reresolve.sh 755 root:root "$WORK/reresolve" text
apply_file unit /etc/systemd/system/wg-reresolve.service 644 root:root "$REPO/wireguard/wg-reresolve.service" text
apply_file unit /etc/systemd/system/wg-reresolve.timer   644 root:root "$REPO/wireguard/wg-reresolve.timer" text

# ------------------------------------------------------------------ 7. engines.conf
kop "7. engines.conf (uploads naar de lokale ontvanger, meetfrequenties)"
ENG=/etc/salt/minion.d/engines.conf
if ! cmp -s "$(R $ENG)" "$WORK/engines" && [ "$DRY" = 0 ] && [ ! -e "$(R $ENG).orig" ]; then
    cp -p "$(R $ENG)" "$(R $ENG).orig"; zeg "eenmalige backup  $ENG.orig"
fi
apply_file conf $ENG keep:644 root:root "$WORK/engines"

# ------------------------------------------------------------------ 8. ssh
kop "8. SSH: sleutel(s) en SSH over wifi"
if [ "$NKEYS" -gt 0 ]; then
    ensure_dir /home/pi/.ssh 700 pi:pi
    apply_file conf /home/pi/.ssh/authorized_keys 600 pi:pi "$WORK/authkeys"
else
    zeg "geen SSH_PUBKEY_* in de config - overgeslagen"
fi
if [ -n "$SSH_SUB" ]; then
    apply_file conf /etc/iptables/rules.v4 keep:644 root:root "$WORK/rules"
    if live_rules_ok "$SSH_SUB"; then
        zeg "ongewijzigd  live firewallregels voor wlan0 vanaf het thuissubnet"
    else
        ACTIES=$((ACTIES+1))
        if [ "$DRY" = 1 ]; then zeg "zou live toevoegen: SSH (tcp/22) en ping op wlan0 vanaf het thuissubnet"
        else
            if [ "$INSTALL_TEST" = 1 ]; then N=12
            else N=$(iptables -L INPUT --line-numbers -n | awk '$2=="REJECT"||$2=="DROP"{print $1; exit}'); fi
            [ -n "$N" ] || die "geen afsluitende REJECT/DROP in INPUT gevonden"
            sys_run iptables -I INPUT "$N" -s "$SSH_SUB" -i wlan0 -p tcp -m state --state NEW -m tcp --dport 22 -j ACCEPT
            sys_run iptables -I INPUT "$N" -s "$SSH_SUB" -i wlan0 -p icmp -j ACCEPT
            [ "$INSTALL_TEST" = 1 ] && tset iptables "$SSH_SUB"
            zeg "live toegevoegd (vóór regel $N); /etc/iptables/rules.v4 houdt ze na een reboot"
        fi
    fi
else
    zeg "geen SSH_WIFI_SUBNET - firewall overgeslagen"
fi

# ------------------------------------------------------------------ 9. software
kop "9. Software naar /opt en de units"
installeer() {   # installeer <map in repo> <doelmap> <script> [units...]
    local src=$1 dst=$2 py=$3; shift 3
    ensure_dir "$dst" 755
    apply_file code "$dst/$py" 755 root:root "$REPO/$src/$py" py
    apply_file doc  "$dst/README.md" 644 root:root "$REPO/$src/README.md" text
    local u
    for u in "$@"; do
        apply_file unit "/etc/systemd/system/${u##*:}" 644 root:root "$REPO/$src/${u%%:*}" text
    done
}
installeer eniro-soc  /opt/eniro-soc      eniro_soc.py       eniro-soc.service:eniro-soc.service
installeer ingest     /opt/autopi-ingest  autopi_ingest.py   autopi-ingest-dongle.service:autopi-ingest.service
# MQTT Discovery: de definitie van de HA-entiteiten naast de ontvanger.
if [ "$HA_DISC" = 1 ]; then
    apply_file code /opt/autopi-ingest/discovery.json 644 root:root "$REPO/homeassistant/discovery.json"
elif [ -e "$(R /opt/autopi-ingest/discovery.json)" ]; then
    zeg "discovery.json staat er al; HA_DISCOVERY=0 laat hem ongemoeid (ongebruikt)"
fi
installeer wakelock   /opt/autopi-wakelock autopi_wakelock.py autopi-wakelock.service:autopi-wakelock.service autopi-wakelock.timer:autopi-wakelock.timer
installeer wwan-teller /opt/wwan-teller   wwan_teller.py     wwan-teller.service:wwan-teller.service wwan-teller.timer:wwan-teller.timer
if [ "$DIAG" = 1 ]; then
    installeer diag /opt/autopi-diag autopi_diag.py autopi-diag.service:autopi-diag.service autopi-diag.timer:autopi-diag.timer
elif [ -e "$(R /etc/systemd/system/autopi-diag.timer)" ]; then
    zeg "autopi-diag staat er al; DIAG_INSTALL=0 laat hem ongemoeid"
fi

# ------------------------------------------------------------------ 10. diensten
kop "10. Diensten"
unit_veranderd=0
for p in "${!CHANGED[@]}"; do case "$p" in /etc/systemd/system/*) unit_veranderd=1 ;; esac; done
if [ "$unit_veranderd" = 1 ]; then
    ACTIES=$((ACTIES+1))
    if [ "$DRY" = 1 ]; then zeg "zou doen: systemctl daemon-reload"; else sys_run systemctl daemon-reload; zeg "daemon-reload"; fi
fi
if veranderd /etc/systemd/journald.conf.d/persistent.conf /var/log/journal; then
    ACTIES=$((ACTIES+1))
    if [ "$DRY" = 1 ]; then zeg "zou herstarten   systemd-journald"; else sys_run systemctl restart systemd-journald; zeg "herstart  systemd-journald"; fi
fi
if veranderd $ENG; then
    ACTIES=$((ACTIES+1))
    if [ "$DRY" = 1 ]; then zeg "zou herstarten   salt-minion, en controleren met test.ping"
    else
        sys_run systemctl restart salt-minion
        [ "$INSTALL_TEST" = 1 ] || sleep 10
        if salt_ok; then zeg "herstart  salt-minion (test.ping: True)"
        else
            cp -p "$(R $ENG).bak-$TS" "$(R $ENG)"
            sys_run systemctl restart salt-minion
            die "salt-minion geeft geen True met de nieuwe engines.conf - de oude is teruggezet. Er kan al iets anders veranderd zijn; zie --rollback $TS"
        fi
    fi
fi
dienst wg-quick@wg0        /etc/wireguard/wg0.conf
dienst wg-reresolve.timer  /usr/local/sbin/wg-reresolve.sh /etc/systemd/system/wg-reresolve.service /etc/systemd/system/wg-reresolve.timer
dienst autopi-ingest       /opt/autopi-ingest/autopi_ingest.py /etc/autopi-ingest.env /etc/systemd/system/autopi-ingest.service /opt/autopi-ingest/discovery.json
dienst eniro-soc           /opt/eniro-soc/eniro_soc.py /etc/eniro-soc.env /etc/systemd/system/eniro-soc.service
dienst autopi-wakelock.timer /opt/autopi-wakelock/autopi_wakelock.py /etc/systemd/system/autopi-wakelock.service /etc/systemd/system/autopi-wakelock.timer
dienst wwan-teller.timer   /opt/wwan-teller/wwan_teller.py /etc/systemd/system/wwan-teller.service /etc/systemd/system/wwan-teller.timer
[ "$DIAG" = 1 ] && dienst autopi-diag.timer /opt/autopi-diag/autopi_diag.py /etc/systemd/system/autopi-diag.service /etc/systemd/system/autopi-diag.timer /etc/eniro-soc.env

# ------------------------------------------------------------------ 11. wifi
kop "11. Wifi-netwerken (/etc/salt/grains)"
if [ "$NWIFI" -gt 0 ]; then
    apply_file conf /etc/salt/grains keep:600 root:root "$WORK/grains"
    if veranderd /etc/salt/grains; then
        ACTIES=$((ACTIES+1))
        if [ "$DRY" = 1 ]; then zeg "zou toepassen: state.sls network.wlan.client.config (stil)"
        else
            zeg "wpa_supplicant bijwerken - wifi kan even wegvallen"
            # Stil: de uitvoer van state.sls bevat de wifi-wachtwoorden.
            sys_run salt-call --local state.sls network.wlan.client.config --state-output=terse --out=quiet \
                || zeg "!! state.sls gaf een fout; controleer met: sudo grep -c '^network=' /etc/wpa_supplicant/wpa_supplicant.conf"
        fi
    fi
else
    zeg "geen WIFI_* in de config - grains ongemoeid (wifi via de webinterface)"
fi

# ------------------------------------------------------------------ 12. hotspot
kop "12. Hotspot (eigen SSID en wachtwoord)"
apply_file conf /etc/hostapd/hostapd_uap0.conf keep:600 root:root "$WORK/hostapd"
HOSTAPD_HERSTART=0
if ! unit_enabled hostapd; then
    ACTIES=$((ACTIES+1))
    if [ "$DRY" = 1 ]; then zeg "zou inschakelen  hostapd"; else svc enable hostapd; zeg "ingeschakeld  hostapd"; fi
fi
if veranderd /etc/hostapd/hostapd_uap0.conf; then
    ACTIES=$((ACTIES+1)); HOSTAPD_HERSTART=1
    [ "$DRY" = 1 ] && zeg "zou herstarten   hostapd (als laatste)"
fi

# ------------------------------------------------------------------ 13. controle
kop "13. Controle"
if [ "$DRY" = 1 ]; then zeg "overgeslagen in de proefdraai"
elif [ "$VERIFY" = 0 ]; then zeg "overgeslagen (--no-verify)"
elif [ "$INSTALL_TEST" = 1 ]; then tstub "verify.sh"; zeg "verify.sh (stub)"
else
    sh "$REPO/scripts/verify.sh" || zeg "verify.sh meldt fouten - staat de auto in ready of aan de lader? Zie INSTALLATIE 9."
fi

# ------------------------------------------------------------------ samenvatting
kop "Samenvatting"
zeg "configuratie: $NCONF · code: $NCODE · units: $NUNIT · documentatie: $NDOC · overige acties: $ACTIES"
[ "$NFUNC" -gt 0 ] && zeg "($NFUNC bestand(en) wijken alleen in commentaar af van de repo; die blijven staan)"
TOTAAL=$((NCONF+NCODE+NUNIT+NDOC+ACTIES))
if [ "$DRY" = 1 ]; then
    if [ "$TOTAAL" -eq 0 ]; then echo; echo "Alles ongewijzigd."
    else echo; echo "Proefdraai: er is niets veranderd. Uitvoeren: sudo $0${CONFIG:+ $CONFIG}"; fi
    exit 0
fi
if [ ! -s "$MANI" ]; then
    rm -f "$MANI"
    echo; echo "Alles ongewijzigd."
else
    cat <<TXT

Klaar. Elk overschreven bestand heeft een backup <bestand>.bak-$TS.
   Terugdraaien:   sudo $0 --rollback $TS
   Backups wissen: sudo $0 --purge-backups $TS
   (de backups bevatten wachtwoorden - wis ze als alles werkt)
TXT
fi
if [ "$HOSTAPD_HERSTART" = 1 ]; then
    echo
    echo "LET OP: hostapd herstart over 5 seconden. Zit je via de hotspot verbonden,"
    echo "verbind dan opnieuw met de nieuwe SSID."
    if [ "$INSTALL_TEST" = 1 ]; then tstub "systemctl restart hostapd (uitgesteld)"
    else nohup sh -c 'sleep 5; systemctl restart hostapd' >/dev/null 2>&1 & fi
fi
exit 0
