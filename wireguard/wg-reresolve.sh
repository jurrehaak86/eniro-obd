#!/bin/sh
# wg-reresolve.sh - houdt wg0 in de lucht bij een dynamisch thuis-IP.
#
# Twee problemen die dit oplost:
#  1. WireGuard zoekt de endpoint-hostname alleen op bij het opstarten.
#     Verandert het thuis-IP daarna, dan blijft hij naar het oude adres sturen.
#     'wg set ... endpoint host:poort' zoekt de hostname opnieuw op.
#  2. Start wg-quick bij het booten voordat er netwerk is, dan faalt het
#     resolven en komt wg0 nooit op. Restart= mag niet op oneshot-units in de
#     systemd van deze image (241), dus vangen we het hier op.
#
# Draait elke 5 minuten via wg-reresolve.timer. Kost een DNS-opvraging per
# keer, ruwweg 2 KB per uur.
#
# Invullen: PEER = de PublicKey uit de [Peer]-sectie van /etc/wireguard/wg0.conf
# (dat is de publieke sleutel van je VPN-server, geen geheim), ENDPOINT = exact
# de Endpoint-regel uit diezelfde sectie.
set -u

IFACE=wg0
PEER="<SERVER_PUBLIC_KEY>"
ENDPOINT="<VPN_HOSTNAAM>:<POORT>"

if ! ip link show "$IFACE" >/dev/null 2>&1; then
    if wg-quick up "$IFACE" >/dev/null 2>&1; then
        logger -t wg-reresolve "$IFACE opgebracht (stond niet aan)"
    else
        logger -t wg-reresolve "$IFACE opbrengen mislukt - netwerk of DNS nog niet klaar?"
    fi
    exit 0
fi

wg set "$IFACE" peer "$PEER" endpoint "$ENDPOINT" 2>/dev/null \
    || logger -t wg-reresolve "endpoint $ENDPOINT niet op te zoeken"
