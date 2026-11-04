#!/bin/sh
# verify.sh - kijkt in een halve minuut of de keten werkt, zonder iets te sturen.
# Draai dit op de dongle, met de auto in ready of aan de lader.
#
#   sudo ./verify.sh
set -u

IFACE="${CAN_IFACE:-can0}"
SCRIPT="${ENIRO_SOC:-/opt/eniro-soc/eniro_soc.py}"
# De lokale ontvanger (standaardopstelling). Bij de servervariant draait hij
# niet op de dongle; zet dan ONTVANGER= (leeg) om die controles over te slaan.
ONTVANGER="${ONTVANGER-http://127.0.0.1:8181}"
WG="${WG_IFACE:-wg0}"
ok=0
fout=0

zeg()  { printf '%-34s %s\n' "$1" "$2"; }
goed() { zeg "$1" "OK      $2"; ok=$((ok+1)); }
mis()  { zeg "$1" "FOUT    $2"; fout=$((fout+1)); }

echo "== eniro-soc verificatie =="
echo

# 1. Is het script er
if [ -r "$SCRIPT" ]; then goed "script aanwezig" "$SCRIPT"
else mis "script aanwezig" "niet gevonden op $SCRIPT"; fi

# 2. Staat de CAN-interface omhoog
if [ -r "/sys/class/net/$IFACE/flags" ]; then
    vlaggen=$(cat "/sys/class/net/$IFACE/flags")
    if [ $(( $(printf '%d' "$vlaggen") & 1 )) -eq 1 ]
    then goed "$IFACE omhoog" "IFF_UP gezet"
    else mis "$IFACE omhoog" "staat DOWN - eniro-soc brengt hem zelf omhoog bij de start"
    fi
else
    mis "$IFACE bestaat" "geen /sys/class/net/$IFACE - verkeerde interface?"
fi

# 3. Decodeerlogica zonder auto
if [ -r "$SCRIPT" ]; then
    if python3 "$SCRIPT" --selftest >/tmp/eniro-selftest.$$ 2>&1
    then goed "selftest decodering" "vaste testframes kloppen"
    else mis "selftest decodering" "zie /tmp/eniro-selftest.$$"
    fi
fi

# 4. Een echte meting, niets versturen
if [ -r "$SCRIPT" ]; then
    echo
    echo "-- een meting uit de auto (--once --dry-run) --"
    # Uitvoer en exitcode apart: in een pijp naar sed telt de exitcode van sed.
    meting=$(python3 "$SCRIPT" --once --dry-run 2>&1); rc=$?
    printf '%s\n' "$meting" | sed 's/^/   /'
    case $rc in
        0) goed "meting uit de auto" "de BMS antwoordde" ;;
        3) zeg "meting uit de auto" "--      geen antwoord: auto uit? Zet hem in ready of aan de lader en draai opnieuw" ;;
        *) mis "meting uit de auto" "eniro_soc.py --once faalde (exitcode $rc)" ;;
    esac
fi

# 5. Services
echo
for dienst in eniro-soc autopi-ingest autopi-wakelock.timer autopi-diag.timer \
              wwan-teller.timer wg-quick@wg0 wg-reresolve.timer; do
    st=$(systemctl is-enabled "$dienst" 2>/dev/null)
    case "$st" in
        enabled)   goed "$dienst" "enabled" ;;
        ""|not-found) ;;                       # niet geinstalleerd - overslaan
        *)         zeg "$dienst" "--      aanwezig, $st" ;;
    esac
done
for dienst in eniro-soc autopi-ingest; do
    if [ "$(systemctl is-enabled "$dienst" 2>/dev/null)" = enabled ]; then
        if systemctl is-active --quiet "$dienst"
        then goed "$dienst draait" "active"
        else mis "$dienst draait" "niet actief - journalctl -u $dienst -n 30"
        fi
    fi
done

# 6. Waar posten eniro-soc en de firmware naartoe (zonder het pad te tonen)
waarheen() {
    case "$1" in
        "")                       echo "niet gevonden" ;;
        http://127.0.0.1:*|http://localhost:*) echo "lokale ontvanger" ;;
        *)                        echo "extern endpoint (servervariant)" ;;
    esac
}
url=$(sed -n 's/^INGEST_URL=//p' /etc/eniro-soc.env 2>/dev/null | tail -1)
zeg "eniro-soc INGEST_URL" "--      $(waarheen "$url")"
url=$(sed -n 's/^ *url: *//p' /etc/salt/minion.d/engines.conf 2>/dev/null | head -1)
zeg "firmware url (engines.conf)" "--      $(waarheen "$url")"

# 7. Lokale ontvanger: /healthz geeft 200 als de broker bereikbaar is
if [ -n "$ONTVANGER" ]; then
    code=$(python3 - "$ONTVANGER/healthz" <<'PY' 2>/dev/null
import sys, urllib.request, urllib.error
try:
    print(urllib.request.urlopen(sys.argv[1], timeout=10).status)
except urllib.error.HTTPError as e:
    print(e.code)
except Exception:
    print(0)
PY
)
    case "$code" in
        200) goed "ontvanger + broker" "$ONTVANGER/healthz geeft 200" ;;
        503) mis  "ontvanger + broker" "ontvanger draait, broker onbereikbaar - tunnel?" ;;
        *)   mis  "ontvanger + broker" "geen antwoord op $ONTVANGER - draait autopi-ingest?" ;;
    esac
fi

# 8. WireGuard-tunnel (na stap 7: die wekt verkeer op, dus een handshake)
if ip link show "$WG" >/dev/null 2>&1; then
    goed "$WG aanwezig" "interface staat"
    hs=$(wg show "$WG" latest-handshakes 2>/dev/null | awk '{print $2}' | sort -n | tail -1)
    nu=$(date +%s)
    if [ -z "$hs" ]; then
        mis "$WG handshake" "wg show gaf niets - wireguard-tools aanwezig, als root?"
    elif [ "$hs" -eq 0 ]; then
        mis "$WG handshake" "nog nooit - klopt Endpoint, sleutel en firewall thuis?"
    elif [ $((nu - hs)) -gt 900 ]; then
        zeg "$WG handshake" "--      $((nu - hs)) s geleden (oud; normaal bij weinig verkeer)"
    else
        goed "$WG handshake" "$((nu - hs)) s geleden"
    fi
elif [ -e "/etc/wireguard/$WG.conf" ]; then
    mis "$WG aanwezig" "config staat er, interface niet - sudo wg-quick up $WG"
fi

echo
echo "$ok goed, $fout fout."
[ "$fout" -eq 0 ] || exit 1
