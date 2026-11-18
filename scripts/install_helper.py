#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""install_helper.py - hulpje voor install.sh (eniro-obd v1.2).

Leest config.env, valideert het, en rekent per bestand op de dongle uit hoe het
eruit moet zien. install.sh vergelijkt dat met wat er staat en beslist.

Draait onder Python 2.7 en 3. De delen die YAML lezen (grains, engines.conf)
hebben PyYAML nodig; op de AutoPi heeft alleen de Python van salt die.
# AANNAME: de Python van salt-minion op de AutoPi-image is 2.7 (de wlan-template
# van salt gebruikt network.iteritems()). Daarom geen f-strings en geen
# Python-3-only syntax in dit bestand.

Dit script schrijft nooit een geheim naar stdout of stderr, behalve bij
'export' (naar een bestand met rechten 600) en 'secrets' (alleen voor de
maskering in install.sh, via een pipe).
"""
from __future__ import print_function, unicode_literals

import difflib
import io
import os
import re
import sys

try:
    text_type = unicode  # noqa: F821  (Python 2)
except NameError:
    text_type = str

LOKALE_URL = "http://127.0.0.1:8181/ingest/lokaal"
INGEST_PATH = "/ingest/lokaal"

# ------------------------------------------------------------------ config

DEFAULTS = [
    ("MQTT_PORT", "1883"),
    ("MQTT_USER", "autopi-dongle"),
    ("MQTT_PREFIX", "autopi"),
    ("WG_PRESHARED_KEY", ""),
    ("WG_ALLOWED_IPS", ""),          # leeg = <MQTT_HOST>/32
    ("HOTSPOT_COUNTRY", "NL"),
    ("SSH_WIFI_SUBNET", ""),
    ("BUNDEL_INTERVAL", "1800"),
    ("PROFIEL_STANDAARD", "dagelijks"),
    ("INTERVAL_CHARGING", "300"),
    ("INTERVAL_IDLE", "1800"),
    ("INTERVAL_UNMETERED", "60"),
    ("UPLOAD_WAIT", "300"),
    ("GPS_INTERVAL", "60"),
    ("INACTIVITY_PERIOD", "900"),
    ("BATTERY_CHECK_INTERVAL", "30"),
    ("SHAKE_THRESHOLD", "0.05"),
    ("SPRAAK_UIT", "1"),
    ("DIAG_INSTALL", "0"),
    # Leeg of ontbrekend = 0 (uit): een config.env van voor discovery zet het
    # dan niet stilletjes aan. config.env.example heeft HA_DISCOVERY=1, dus een
    # verse installatie krijgt het wel.
    ("HA_DISCOVERY", "0"),
    ("HA_DISCOVERY_PREFIX", "homeassistant"),
]
REQUIRED = ["MQTT_HOST", "MQTT_PASS", "WG_ADDRESS", "WG_PRIVATE_KEY",
            "WG_SERVER_PUBLIC_KEY", "WG_ENDPOINT", "HOTSPOT_SSID", "HOTSPOT_PSK"]
SECRET_KEYS = set(["MQTT_PASS", "WG_PRIVATE_KEY", "WG_PRESHARED_KEY", "HOTSPOT_PSK"])
RE_WIFI = re.compile(r"^WIFI_([1-9][0-9]?)_(SSID|PSK|PRIORITY)$")
RE_SSHKEY = re.compile(r"^SSH_PUBKEY_([1-9][0-9]?)$")
KNOWN = set(REQUIRED) | set(k for k, _ in DEFAULTS)


def is_secret(key):
    return key in SECRET_KEYS or (RE_WIFI.match(key) is not None and key.endswith("_PSK"))


class ConfigError(Exception):
    pass


def rd(path):
    if not path or not os.path.exists(path):
        return None
    with io.open(path, encoding="utf-8") as f:
        return f.read()


def wr(path, text, mode=0o600):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with io.open(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(path, mode)


def parse_config(path):
    """KEY=waarde per regel. 'enkele' of "dubbele" aanhalingstekens worden
    letterlijk genomen (geen $-expansie, geen escapes). Commentaar alleen op
    een eigen regel."""
    text = rd(path)
    if text is None:
        raise ConfigError("%s bestaat niet" % path)
    cfg = {}
    order = []
    for nr, line in enumerate(text.splitlines(), 1):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        m = re.match(r"^([A-Z][A-Z0-9_]*)=(.*)$", s)
        if not m:
            raise ConfigError("regel %d: geen KEY=waarde" % nr)
        key, val = m.group(1), m.group(2)
        if len(val) >= 1 and val[0] in "'\"":
            q = val[0]
            if len(val) < 2 or val[-1] != q:
                raise ConfigError("regel %d (%s): aanhalingsteken niet afgesloten" % (nr, key))
            val = val[1:-1]
        if key in cfg:
            raise ConfigError("regel %d: %s staat er twee keer in" % (nr, key))
        cfg[key] = val
        order.append(key)
    return cfg


def effective(cfg):
    eff = dict(cfg)
    for k, d in DEFAULTS:
        if eff.get(k, "") == "":
            eff[k] = d
    if eff.get("WG_ALLOWED_IPS", "") == "" and eff.get("MQTT_HOST"):
        eff["WG_ALLOWED_IPS"] = eff["MQTT_HOST"] + "/32"
    return eff


def wifi_networks(cfg):
    nums = set()
    for k in cfg:
        m = RE_WIFI.match(k)
        if m:
            nums.add(int(m.group(1)))
    out = []
    for n in sorted(nums):
        ssid = cfg.get("WIFI_%d_SSID" % n, "")
        psk = cfg.get("WIFI_%d_PSK" % n, "")
        prio = cfg.get("WIFI_%d_PRIORITY" % n, "")
        if ssid == "" and psk == "" and prio == "":
            continue
        out.append((n, ssid, psk, prio))
    return out


def ssh_keys(cfg):
    keys = []
    for k in sorted(cfg, key=lambda x: (len(x), x)):
        if RE_SSHKEY.match(k) and cfg[k].strip():
            keys.append(cfg[k].strip())
    return keys


RE_IPV4 = re.compile(r"^(25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])(\.(25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])){3}$")
RE_HOST = re.compile(r"^(?=.{1,253}$)([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$")
RE_WGKEY = re.compile(r"^[A-Za-z0-9+/]{42}[AEIMQUYcgkosw048]=$")


def ok_ip(v):
    return RE_IPV4.match(v) is not None


def ok_cidr(v):
    if "/" not in v:
        return False
    ip, pre = v.split("/", 1)
    return ok_ip(ip) and pre.isdigit() and 0 <= int(pre) <= 32


def ok_port(v):
    return v.isdigit() and 1 <= int(v) <= 65535


def ok_int(v, lo=1, hi=10 ** 7):
    return v.isdigit() and lo <= int(v) <= hi


def placeholder(v):
    return "VUL_" in v or re.search(r"<[A-Z_]+>", v) is not None


def validate(cfg):
    fouten, waarsch = [], []
    eff = effective(cfg)
    for k in REQUIRED:
        if cfg.get(k, "") == "":
            fouten.append("%s ontbreekt (verplicht)" % k)
    for k, v in cfg.items():
        if v and placeholder(v):
            fouten.append("%s bevat nog een placeholder" % k)
        if not (k in KNOWN or RE_WIFI.match(k) or RE_SSHKEY.match(k)):
            waarsch.append("onbekende sleutel %s - tikfout?" % k)
        if "\n" in v or "\r" in v:
            fouten.append("%s bevat een regeleinde" % k)

    def chk(key, cond, msg):
        v = eff.get(key, "")
        if v != "" and not placeholder(v) and not cond(v):
            fouten.append("%s: %s" % (key, msg))

    chk("MQTT_HOST", ok_ip, "moet een IPv4-adres zijn (het LAN-adres van de broker)")
    chk("MQTT_PORT", ok_port, "geen geldige poort (1-65535)")
    chk("MQTT_USER", lambda v: re.match(r"^\S+$", v) is not None, "mag geen spaties bevatten")
    chk("MQTT_PREFIX", lambda v: re.match(r"^[^\s#+]+$", v) is not None, "geen spaties, # of +")
    p = cfg.get("MQTT_PASS", "")
    if p and (p != p.strip() or re.search(r"[\"'\\]", p)):
        waarsch.append("MQTT_PASS bevat aanhalingstekens, een backslash of spaties aan de rand; "
                       "systemd interpreteert die in een EnvironmentFile - liever vermijden")
    chk("WG_ADDRESS", lambda v: all(ok_cidr(x.strip()) for x in v.split(",")),
        "moet een adres met prefix zijn, bijvoorbeeld 10.0.99.8/32")
    for k in ("WG_PRIVATE_KEY", "WG_SERVER_PUBLIC_KEY", "WG_PRESHARED_KEY"):
        chk(k, lambda v: RE_WGKEY.match(v) is not None,
            "geen geldige WireGuard-sleutel (44 tekens base64, eindigt op '=')")
    if cfg.get("WG_PRIVATE_KEY") and cfg.get("WG_PRIVATE_KEY") == cfg.get("WG_SERVER_PUBLIC_KEY"):
        fouten.append("WG_PRIVATE_KEY en WG_SERVER_PUBLIC_KEY zijn gelijk - verwisseld?")

    def ok_ep(v):
        if v.count(":") != 1:
            return False
        h, pt = v.split(":")
        return ok_port(pt) and (ok_ip(h) or RE_HOST.match(h) is not None)
    chk("WG_ENDPOINT", ok_ep, "moet host:poort zijn, bijvoorbeeld vpn.voorbeeld.nl:51820")
    if cfg.get("WG_ALLOWED_IPS", "") != "":
        chk("WG_ALLOWED_IPS", lambda v: all(ok_cidr(x.strip()) for x in v.split(",")),
            "kommagescheiden lijst van adressen met prefix")
    if "0.0.0.0/0" in eff.get("WG_ALLOWED_IPS", ""):
        waarsch.append("WG_ALLOWED_IPS bevat 0.0.0.0/0 - dan gaat al het verkeer door de tunnel")
    if eff.get("MQTT_HOST") and eff.get("WG_ALLOWED_IPS") and ok_ip(eff["MQTT_HOST"]):
        if (eff["MQTT_HOST"] + "/32") not in [x.strip() for x in eff["WG_ALLOWED_IPS"].split(",")] \
                and "0.0.0.0/0" not in eff["WG_ALLOWED_IPS"]:
            waarsch.append("WG_ALLOWED_IPS bevat %s/32 niet - bereikt de dongle de broker wel?" % "MQTT_HOST")

    chk("HOTSPOT_SSID", lambda v: 1 <= len(v.encode("utf-8")) <= 32, "1 tot 32 tekens")
    chk("HOTSPOT_PSK", lambda v: 8 <= len(v) <= 63 and all(32 <= ord(c) < 127 for c in v),
        "8 tot 63 gewone ASCII-tekens")
    hp = cfg.get("HOTSPOT_PSK", "")
    if hp and len(hp) < 16 and not placeholder(hp):
        waarsch.append("HOTSPOT_PSK is korter dan 16 tekens; de hotspot geeft root-toegang via poort 9000")
    if hp in ("autopi2018",):
        fouten.append("HOTSPOT_PSK is nog het fabriekswachtwoord")
    if cfg.get("HOTSPOT_SSID") == "AutoPi-Initial":
        waarsch.append("HOTSPOT_SSID is nog de fabrieksnaam")
    chk("HOTSPOT_COUNTRY", lambda v: re.match(r"^[A-Z]{2}$", v) is not None, "twee hoofdletters, bijvoorbeeld NL")

    for n, ssid, psk, prio in wifi_networks(cfg):
        if ssid == "":
            fouten.append("WIFI_%d_SSID ontbreekt (wel een PSK of prioriteit)" % n)
        elif len(ssid.encode("utf-8")) > 32:
            fouten.append("WIFI_%d_SSID is langer dan 32 bytes" % n)
        if ssid and '"' in ssid:
            fouten.append("WIFI_%d_SSID bevat een dubbel aanhalingsteken; de firmware kan dat niet aan" % n)
        if psk == "":
            fouten.append("WIFI_%d_PSK ontbreekt (open netwerken worden niet ondersteund)" % n)
        elif not (8 <= len(psk) <= 63 and all(32 <= ord(c) < 127 for c in psk)) or '"' in psk:
            fouten.append("WIFI_%d_PSK: 8 tot 63 gewone ASCII-tekens, zonder dubbel aanhalingsteken" % n)
        if prio != "" and not re.match(r"^-?[0-9]{1,4}$", prio):
            fouten.append("WIFI_%d_PRIORITY moet een geheel getal zijn" % n)
    ssids = [s for _, s, _, _ in wifi_networks(cfg)]
    if len(ssids) != len(set(ssids)):
        fouten.append("een wifi-SSID staat er twee keer in")

    for k in cfg:
        if RE_SSHKEY.match(k) and cfg[k].strip():
            parts = cfg[k].split()
            if len(parts) < 2 or not re.match(r"^(ssh-(rsa|ed25519|dss)|ecdsa-sha2-nistp(256|384|521)|sk-[a-z0-9@.-]+)$", parts[0]) \
                    or not re.match(r"^[A-Za-z0-9+/]+=*$", parts[1]):
                fouten.append("%s is geen publieke SSH-sleutel (ssh-ed25519 AAAA... commentaar)" % k)
            if "PRIVATE KEY" in cfg[k]:
                fouten.append("%s lijkt een PRIVÉsleutel - alleen de publieke (.pub) hoort hier" % k)
    chk("SSH_WIFI_SUBNET", ok_cidr, "moet een netwerk met prefix zijn, bijvoorbeeld 192.168.1.0/24")

    for k in ("BUNDEL_INTERVAL", "INTERVAL_CHARGING", "INTERVAL_IDLE", "INTERVAL_UNMETERED",
              "GPS_INTERVAL", "INACTIVITY_PERIOD", "BATTERY_CHECK_INTERVAL"):
        chk(k, lambda v: ok_int(v, 1, 86400), "geheel getal in seconden (1-86400)")
    chk("UPLOAD_WAIT", lambda v: ok_int(v, 0, 86400), "geheel getal in seconden (0-86400)")
    chk("SHAKE_THRESHOLD", lambda v: re.match(r"^(0|[1-9][0-9]*)?\.[0-9]+$|^[0-9]+$", v) is not None,
        "een getal, bijvoorbeeld 0.05")
    chk("PROFIEL_STANDAARD", lambda v: v in ("dagelijks", "reis"), "dagelijks of reis")
    chk("SPRAAK_UIT", lambda v: v in ("0", "1"), "0 of 1")
    chk("DIAG_INSTALL", lambda v: v in ("0", "1"), "0 of 1")
    chk("HA_DISCOVERY", lambda v: v in ("0", "1"), "0 of 1")
    chk("HA_DISCOVERY_PREFIX", lambda v: re.match(r"^[A-Za-z0-9_-]+(/[A-Za-z0-9_-]+)*$", v) is not None,
        "letters, cijfers, _ en -, eventueel met / ertussen (standaard homeassistant)")
    if eff.get("INACTIVITY_PERIOD", "").isdigit() and int(eff["INACTIVITY_PERIOD"]) < 600:
        waarsch.append("INACTIVITY_PERIOD onder 600 s: onderhoud wordt een race tegen de slaap")
    return fouten, waarsch


def secrets_of(cfg):
    s = set()
    for k, v in cfg.items():
        if is_secret(k) and len(v) >= 4:
            s.add(v)
    return s

# ------------------------------------------------------------- key=waarde

def norm_list(v):
    return ",".join(x.strip() for x in v.split(",") if x.strip())


def kv_render(current, template, setmap, sep="=", header=None):
    """Zet KEY=waarde op de plek waar KEY al staat; anders achteraan.
    Commentaar en andere regels blijven staan. Een waarde die al klopt wordt
    niet aangeraakt."""
    base = current if current is not None else template
    lines = base.splitlines()
    done = set()
    out = []
    for line in lines:
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line)
        if m and m.group(1) in setmap:
            k = m.group(1)
            want = setmap[k]
            if m.group(2) == want:
                out.append(line)
            else:
                out.append("%s=%s" % (k, want))
            done.add(k)
        else:
            out.append(line)
    missing = [k for k in setmap if k not in done]
    if missing:
        if header:
            out.append("")
            out.append(header)
        for k in sorted(missing):
            out.append("%s=%s" % (k, setmap[k]))
    return "\n".join(out) + "\n"


def kv_get(text, key):
    val = None
    if text is None:
        return None
    for line in text.splitlines():
        m = re.match(r"^%s=(.*)$" % re.escape(key), line)
        if m:
            val = m.group(1)
    return val

# ------------------------------------------------------------- wg0.conf

WG_KEYS = {
    "interface": [("PrivateKey", "WG_PRIVATE_KEY"), ("Address", "WG_ADDRESS")],
    "peer": [("PublicKey", "WG_SERVER_PUBLIC_KEY"), ("PresharedKey", "WG_PRESHARED_KEY"),
             ("AllowedIPs", "WG_ALLOWED_IPS"), ("Endpoint", "WG_ENDPOINT")],
}
WG_REMOVE = {"interface": ["DNS"], "peer": []}


def wg_parse(text):
    """-> {'interface': {key_lower: (key, value)}, 'peer': {...}} (eerste peer)."""
    res = {"interface": {}, "peer": {}}
    sect = None
    npeer = 0
    for line in text.splitlines():
        s = line.strip()
        m = re.match(r"^\[(\w+)\]$", s)
        if m:
            name = m.group(1).lower()
            if name == "peer":
                npeer += 1
                sect = "peer" if npeer == 1 else None
            elif name == "interface":
                sect = "interface"
            else:
                sect = None
            continue
        m = re.match(r"^([A-Za-z]+)\s*=\s*(.*?)\s*$", s)
        if m and sect and not s.startswith("#"):
            res[sect][m.group(1).lower()] = (m.group(1), m.group(2))
    res["npeer"] = npeer
    return res


def wg_render(current, template, eff):
    base = current if current is not None else template
    want = {}
    for sect, pairs in WG_KEYS.items():
        for key, ck in pairs:
            v = eff.get(ck, "")
            if key in ("Address", "AllowedIPs"):
                v = norm_list(v)
            want[(sect, key.lower())] = (key, v if v != "" else None)
    for sect, keys in WG_REMOVE.items():
        for key in keys:
            want[(sect, key.lower())] = (key, None)
    out = []
    sect = None
    npeer = 0
    seen = set()

    def flush(section):
        # sleutels die in deze sectie nog ontbreken: achteraan de sectie
        add = []
        for (s, kl), (key, v) in sorted(want.items()):
            if s == section and v is not None and (s, kl) not in seen:
                add.append("%s = %s" % (key, v))
                seen.add((s, kl))
        # voor de lege regels aan het eind van de sectie zetten
        tail = []
        while out and out[-1].strip() == "":
            tail.append(out.pop())
        out.extend(add)
        out.extend(tail)

    for line in base.splitlines():
        s = line.strip()
        m = re.match(r"^\[(\w+)\]$", s)
        if m:
            if sect:
                flush(sect)
            name = m.group(1).lower()
            if name == "peer":
                npeer += 1
                sect = "peer" if npeer == 1 else None
            elif name == "interface":
                sect = "interface"
            else:
                sect = None
            out.append(line)
            continue
        m = re.match(r"^([A-Za-z]+)\s*=\s*(.*?)\s*$", s)
        if m and sect and not s.startswith("#") and (sect, m.group(1).lower()) in want:
            kl = m.group(1).lower()
            key, v = want[(sect, kl)]
            if v is None or (sect, kl) in seen:
                continue  # weghalen (of dubbel)
            seen.add((sect, kl))
            cur = m.group(2)
            if key in ("Address", "AllowedIPs"):
                cur = norm_list(cur)
            if cur == v:
                out.append(line)
            else:
                out.append("%s = %s" % (m.group(1), v))
            continue
        out.append(line)
    if sect:
        flush(sect)
    return "\n".join(out).rstrip("\n") + "\n"

# ------------------------------------------------------------- hostapd

def hostapd_render(current, eff):
    setmap = {
        "ssid": eff["HOTSPOT_SSID"],
        "wpa_passphrase": eff["HOTSPOT_PSK"],
        "country_code": eff["HOTSPOT_COUNTRY"],
        "ignore_broadcast_ssid": "0",
    }
    # AANNAME: hostapd_uap0.conf heeft gewone key=waarde-regels (zoals elke
    # hostapd-config) en bevat al ssid= en wpa_passphrase=. Wat ontbreekt komt
    # achteraan.
    return kv_render(current, "", setmap, header="# toegevoegd door eniro-obd install.sh")

# ------------------------------------------------------------- grains (YAML)

def need_yaml():
    try:
        import yaml  # noqa
        return yaml
    except ImportError:
        raise ConfigError("deze Python heeft geen PyYAML - gebruik de Python van salt")


def grains_networks(text):
    yaml = need_yaml()
    g = yaml.safe_load(text) if text else None
    g = g or {}
    ws = g.get("wpa_supplicant")
    # AANNAME: de netwerken staan genest als wpa_supplicant: networks: [..]
    # (de wlan-template van de firmware leest grains.get('wpa_supplicant:networks')).
    if isinstance(ws, dict) and isinstance(ws.get("networks"), list):
        return g, ws["networks"]
    return g, None


def grains_render(current, cfg):
    yaml = need_yaml()
    g, nets = grains_networks(current)
    nets = nets or []
    new = []
    for n, ssid, psk, prio in wifi_networks(cfg):
        bestaand = None
        for e in nets:
            if isinstance(e, dict) and text_type(e.get("ssid")) == ssid:
                bestaand = e
                break
        e = dict(bestaand) if bestaand else {}
        e["ssid"] = ssid
        e["psk"] = psk
        if prio != "":
            e["priority"] = int(prio)
        new.append(e)
    if new == nets and current is not None:
        return current
    if not isinstance(g.get("wpa_supplicant"), dict):
        g["wpa_supplicant"] = {}
    g["wpa_supplicant"]["networks"] = new
    return yaml.safe_dump(g, default_flow_style=False, allow_unicode=True, width=1000, encoding=None)

# ------------------------------------------------------------- engines.conf (YAML)

def _walk(node, path, out, parent_ctx):
    """Loopt de gecomponeerde YAML-boom af. Voor elke scalar: (pad, node, context).
    context = sleutels en namen op het pad + scalaire waarden in de ouder-mapping."""
    import yaml
    if isinstance(node, yaml.MappingNode):
        local = []
        for k, v in node.value:
            if isinstance(v, yaml.ScalarNode):
                local.append(text_type(v.value))
        ctx = parent_ctx + local
        for k, v in node.value:
            key = text_type(k.value)
            if isinstance(v, yaml.ScalarNode):
                out.append((path + (key,), v, ctx + [key]))
            else:
                _walk(v, path + (key,), out, ctx + [key])
    elif isinstance(node, yaml.SequenceNode):
        for i, v in enumerate(node.value):
            hint = []
            if isinstance(v, yaml.MappingNode):
                for k, vv in v.value:
                    if text_type(k.value) == "name" and isinstance(vv, yaml.ScalarNode):
                        hint.append(text_type(vv.value))
            _walk(v, path + (i,), out, parent_ctx + hint)


def engines_scalars(text):
    yaml = need_yaml()
    root = yaml.compose(text)
    out = []
    if root is not None:
        _walk(root, (), out, [])
    return out


def engines_targets(text):
    """Zoekt de plekken die install.sh beheert. Geeft {naam: [(pad, node)]}."""
    t = {"url": [], "gps": [], "period": [], "battery": [], "shake": [], "speak": []}
    for path, node, ctx in engines_scalars(text):
        key = path[-1]
        if key == "url" and "cloud_manager" in ctx and "endpoint" in ctx:
            # AANNAME: cloud_manager -> endpoint -> url (INSTALLATIE A.3.1)
            t["url"].append((path, node))
        elif key == "interval" and "gnss_manager" in ctx and "poll_logger" in ctx:
            # AANNAME: gnss_manager -> workers -> {name: poll_logger, interval: ..}
            t["gps"].append((path, node))
        elif key == "interval" and "spm_manager" in ctx and "_battery_checker" in ctx:
            t["battery"].append((path, node))
        elif key == "shake_g_threshold":
            # AANNAME: er is precies een shake_g_threshold (motion_event_trigger)
            t["shake"].append((path, node))
        elif key == "period" and any("inactivity" in c for c in ctx):
            # AANNAME: de twee sleep-timer-reactors herken je aan 'inactivity' in
            # hun naam of in een waarde naast 'period' (inactivity_fallback,
            # inactivity_after_sleep). De engine_stopped-timer (900 s) niet.
            t["period"].append((path, node))
        elif key == "regex" and (node.value.startswith("^(?:system/release|")
                                 or node.value == "^disabled/speak_events$"):
            t["speak"].append((path, node))
    return t


def engines_get(text):
    t = engines_targets(text)

    def val(name):
        vs = sorted(set(text_type(n.value) for _, n in t[name]))
        return vs
    speak = None
    if t["speak"]:
        speak = "1" if all(n.value == "^disabled/speak_events$" for _, n in t["speak"]) else "0"
    return {"url": val("url"), "gps": val("gps"), "period": val("period"),
            "battery": val("battery"), "shake": val("shake"), "speak": speak}


def _flatten(obj, path=()):
    if isinstance(obj, dict):
        r = {}
        for k, v in obj.items():
            r.update(_flatten(v, path + (text_type(k),)))
        return r
    if isinstance(obj, list):
        r = {}
        for i, v in enumerate(obj):
            r.update(_flatten(v, path + (i,)))
        return r
    return {path: obj}


def engines_render(current, eff, warn):
    yaml = need_yaml()
    if current is None:
        raise ConfigError("engines.conf niet gevonden")
    t = engines_targets(current)
    want = {
        "url": LOKALE_URL,
        "gps": eff["GPS_INTERVAL"],
        "period": eff["INACTIVITY_PERIOD"],
        "battery": eff["BATTERY_CHECK_INTERVAL"],
        "shake": eff["SHAKE_THRESHOLD"],
    }
    if eff.get("SPRAAK_UIT") == "1":
        want["speak"] = "^disabled/speak_events$"
    elif any(n.value == "^disabled/speak_events$" for _, n in t["speak"]):
        warn.append("engines.conf: SPRAAK_UIT=0, maar de spraak staat al uit; terugzetten kan alleen "
                    "uit de backup (engines.conf.orig) - niets veranderd")
    verwacht_aantal = {"url": 1, "gps": 1, "period": 2, "battery": 1, "shake": 1, "speak": 1}
    for name in want:
        if not t[name]:
            if name in ("url", "gps"):
                raise ConfigError("engines.conf: '%s' niet gevonden - structuur wijkt af, niets veranderd" % name)
            warn.append("engines.conf: '%s' niet gevonden - overgeslagen" % name)
        elif len(t[name]) != verwacht_aantal[name]:
            warn.append("engines.conf: '%s' %d keer gevonden (verwacht %d) - allemaal gezet"
                        % (name, len(t[name]), verwacht_aantal[name]))
    lines = current.split("\n")
    edits = []
    doel = {}
    for name, value in want.items():
        for path, node in t[name]:
            if text_type(node.value) == value:
                continue
            sm, em = node.start_mark, node.end_mark
            if sm.line != em.line:
                raise ConfigError("engines.conf: '%s' staat over meer regels - niet aangepast" % name)
            if node.style in ("'", '"'):
                nieuw = node.style + value + node.style
            elif node.style in (None, ""):
                nieuw = value
            else:
                raise ConfigError("engines.conf: '%s' heeft een onverwachte YAML-stijl" % name)
            edits.append((sm.line, sm.column, em.column, nieuw))
            doel[path] = value
    for line, c0, c1, nieuw in sorted(edits, reverse=True):
        lines[line] = lines[line][:c0] + nieuw + lines[line][c1:]
    new = "\n".join(lines)
    if not edits:
        return current
    # Validatie: de nieuwe YAML moet laden, en precies de bedoelde waarden
    # mogen veranderd zijn - verder niets.
    try:
        oud_f = _flatten(yaml.safe_load(current))
        new_f = _flatten(yaml.safe_load(new))
    except Exception as e:
        raise ConfigError("engines.conf wordt ongeldige YAML (%s) - niets veranderd" % e.__class__.__name__)
    veranderd = set(k for k in set(oud_f) | set(new_f) if oud_f.get(k, object()) != new_f.get(k, object()))
    if veranderd != set(doel):
        raise ConfigError("engines.conf: de aanpassing raakt meer of minder dan bedoeld - niets veranderd")
    for p, v in doel.items():
        if new_f[p] != yaml.safe_load(v):
            raise ConfigError("engines.conf: waarde klopt na aanpassen niet - niets veranderd")
    return new

# ------------------------------------------------------------- authorized_keys, iptables

def authkeys_render(current, keys):
    lines = (current or "").splitlines()
    have = set()
    for l in lines:
        p = l.split()
        if len(p) >= 2 and not l.strip().startswith("#"):
            # sleutel kan opties ervoor hebben; zoek het base64-deel
            for i, x in enumerate(p[:-1]):
                if re.match(r"^(ssh-|ecdsa-|sk-)", x):
                    have.add(p[i + 1])
    add = [k for k in keys if k.split()[1] not in have]
    if not add and current is not None:
        return current
    out = lines + add
    return "\n".join(out) + "\n"


def rule_lines(sub):
    return ["-A INPUT -s %s -i wlan0 -p icmp -j ACCEPT" % sub,
            "-A INPUT -s %s -i wlan0 -p tcp -m state --state NEW -m tcp --dport 22 -j ACCEPT" % sub]


def rule_present(text, sub, kind):
    for l in (text or "").splitlines():
        if not l.startswith("-A INPUT ") or "-i wlan0" not in l or "-j ACCEPT" not in l:
            continue
        if re.search(r"(^|\s)-s %s(\s|$)" % re.escape(sub), l) is None:
            continue
        if kind == "ssh" and "--dport 22" in l and "-p tcp" in l:
            return True
        if kind == "icmp" and "-p icmp" in l:
            return True
    return False


def rules_render(current, sub):
    if current is None:
        raise ConfigError("rules.v4 niet gevonden")
    need = []
    if not rule_present(current, sub, "icmp"):
        need.append(rule_lines(sub)[0])
    if not rule_present(current, sub, "ssh"):
        need.append(rule_lines(sub)[1])
    if not need:
        return current
    lines = current.split("\n")
    infilter = False
    for i, l in enumerate(lines):
        if l.startswith("*"):
            infilter = (l.strip() == "*filter")
        if infilter and l.startswith("-A INPUT ") and re.search(r"-j (REJECT|DROP)\b", l):
            # AANNAME: rules.v4 is iptables-save-uitvoer met de afsluitende
            # REJECT/DROP onderaan INPUT, zoals INSTALLATIE A.2 beschrijft.
            return "\n".join(lines[:i] + need + lines[i:])
    raise ConfigError("rules.v4: geen afsluitende REJECT/DROP in INPUT gevonden")


def rules_subnet(text):
    for l in (text or "").splitlines():
        if l.startswith("-A INPUT ") and "-i wlan0" in l and "--dport 22" in l and "-j ACCEPT" in l:
            m = re.search(r"(?:^|\s)-s (\S+)", l)
            if m:
                return m.group(1)
    return None

# ------------------------------------------------------------- maskeren en diff

MASK_RE = re.compile(r"^(?P<pre>[-+ ]?\s*(?:-\s+)?(?:psk|wpa_passphrase|MQTT_PASS|PrivateKey|PresharedKey|"
                     r"auth_token|passw\w*|secret\w*|token)\s*[:=]\s*)(?P<val>.+)$", re.I)
URL_RE = re.compile(r"^(?P<pre>[-+ ]?\s*(?:url|INGEST_URL)\s*[:=]\s*)(?P<val>\S+)\s*$", re.I)


def mask_line(line, secrets):
    m = MASK_RE.match(line)
    if m:
        line = m.group("pre") + "***"
    m = URL_RE.match(line)
    if m and not re.match(r"^https?://(127\.0\.0\.1|localhost)[:/]", m.group("val")):
        line = m.group("pre") + "*** (extern adres, gemaskeerd)"
    for s in sorted(secrets, key=len, reverse=True):
        if s and s in line:
            line = line.replace(s, "***")
    return line


def maskdiff(old, new, label, secrets, summary=False):
    a = (old or "").splitlines()
    b = (new or "").splitlines()
    d = list(difflib.unified_diff(a, b, "huidig/" + label, "nieuw/" + label, n=1, lineterm=""))
    if summary:
        plus = sum(1 for l in d if l.startswith("+") and not l.startswith("+++"))
        min_ = sum(1 for l in d if l.startswith("-") and not l.startswith("---"))
        return ["      (+%d/-%d regels)" % (plus, min_)]
    return ["      " + mask_line(l, secrets) for l in d]


def strip_comments(text, kind):
    if kind == "py":
        import ast
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return None
        for node in ast.walk(tree):
            body = getattr(node, "body", None)
            if isinstance(body, list) and body and isinstance(body[0], ast.Expr):
                v = body[0].value
                if (hasattr(ast, "Constant") and isinstance(v, ast.Constant) and isinstance(v.value, str)) \
                        or v.__class__.__name__ == "Str":
                    body[0] = ast.Pass()
        return ast.dump(tree)
    out = []
    for i, l in enumerate(text.splitlines()):
        s = l.strip()
        if not s or (s.startswith("#") and not (i == 0 and s.startswith("#!"))):
            continue
        out.append(l.rstrip())
    return "\n".join(out)

# ------------------------------------------------------------- export

def quote(v):
    if v == "" or re.match(r"^[A-Za-z0-9._/:@,+=-]+$", v):
        return v
    if "'" not in v:
        return "'" + v + "'"
    if '"' not in v:
        return '"' + v + '"'
    raise ConfigError("een waarde bevat zowel ' als \" - kan niet in config.env")


def export(root, out, template):
    P = lambda p: os.path.join(root, p.lstrip("/")) if root else p  # noqa: E731
    vals = {}
    missing = []
    ing = rd(P("/etc/autopi-ingest.env"))
    soc = rd(P("/etc/eniro-soc.env"))
    for k in ("MQTT_HOST", "MQTT_PORT", "MQTT_USER", "MQTT_PASS", "MQTT_PREFIX",
              "BUNDEL_INTERVAL", "PROFIEL_STANDAARD"):
        v = kv_get(ing, k)
        if v is None:
            missing.append("%s (niet in /etc/autopi-ingest.env)" % k)
        else:
            vals[k] = v
    # Discovery: staat het niet in de env, dan is het uit (de code-standaard).
    # Zo blijft een proefdraai met de geëxporteerde config 'ongewijzigd'.
    v = kv_get(ing, "HA_DISCOVERY")
    vals["HA_DISCOVERY"] = "1" if v == "1" else "0"
    if v is None:
        missing.append("HA_DISCOVERY (niet in /etc/autopi-ingest.env): op 0 gezet, discovery blijft uit. "
                       "Overstappen: zie INSTALLATIE, Home Assistant")
    v = kv_get(ing, "HA_DISCOVERY_PREFIX")
    vals["HA_DISCOVERY_PREFIX"] = "" if v in (None, "homeassistant") else v
    for k in ("INTERVAL_CHARGING", "INTERVAL_IDLE", "INTERVAL_UNMETERED", "UPLOAD_WAIT"):
        v = kv_get(soc, k)
        if v is None:
            missing.append("%s (niet in /etc/eniro-soc.env)" % k)
        else:
            vals[k] = v
    wgt = rd(P("/etc/wireguard/wg0.conf"))
    if wgt is None:
        missing.append("WG_* (geen /etc/wireguard/wg0.conf)")
    else:
        w = wg_parse(wgt)
        if w["npeer"] > 1:
            missing.append("wg0.conf heeft meer dan een [Peer]; alleen de eerste is overgenomen")
        for sect, pairs in WG_KEYS.items():
            for key, ck in pairs:
                kv = w[sect].get(key.lower())
                if kv:
                    vals[ck] = norm_list(kv[1]) if key in ("Address", "AllowedIPs") else kv[1]
                elif ck != "WG_PRESHARED_KEY":
                    missing.append("%s (niet in wg0.conf)" % ck)
        if w["interface"].get("dns"):
            missing.append("wg0.conf heeft een DNS-regel; install.sh haalt die weg (zie INSTALLATIE A.4)")
    if vals.get("WG_ALLOWED_IPS") == (vals.get("MQTT_HOST", "") + "/32"):
        vals["WG_ALLOWED_IPS"] = ""   # = standaardwaarde
    hp = rd(P("/etc/hostapd/hostapd_uap0.conf"))
    for k, ck in (("ssid", "HOTSPOT_SSID"), ("wpa_passphrase", "HOTSPOT_PSK"), ("country_code", "HOTSPOT_COUNTRY")):
        v = kv_get(hp, k)
        if v is None:
            missing.append("%s (niet in hostapd_uap0.conf)" % ck)
        else:
            vals[ck] = v
    eng = rd(P("/etc/salt/minion.d/engines.conf"))
    if eng is None:
        missing.append("engines.conf niet gevonden")
    else:
        try:
            e = engines_get(eng)
        except ConfigError as ex:
            missing.append(text_type(ex))
            e = None
        if e:
            for name, ck in (("gps", "GPS_INTERVAL"), ("period", "INACTIVITY_PERIOD"),
                             ("battery", "BATTERY_CHECK_INTERVAL"), ("shake", "SHAKE_THRESHOLD")):
                if len(e[name]) == 1:
                    vals[ck] = e[name][0]
                elif not e[name]:
                    missing.append("%s (niet gevonden in engines.conf)" % ck)
                else:
                    missing.append("%s (verschillende waarden in engines.conf: %s)" % (ck, ", ".join(e[name])))
            if e["speak"] is not None:
                vals["SPRAAK_UIT"] = e["speak"]
            if e["url"] and e["url"] != [LOKALE_URL]:
                missing.append("engines.conf: url is niet de lokale ontvanger; install.sh zet hem daarop")
    wifi = []
    gt = rd(P("/etc/salt/grains"))
    if gt is not None:
        _, nets = grains_networks(gt)
        for e in nets or []:
            if isinstance(e, dict) and e.get("ssid") is not None:
                wifi.append((text_type(e.get("ssid")), text_type(e.get("psk", "")),
                             "" if e.get("priority") is None else text_type(e.get("priority"))))
    keys = []
    ak = rd(P("/home/pi/.ssh/authorized_keys"))
    for l in (ak or "").splitlines():
        if l.strip() and not l.strip().startswith("#"):
            keys.append(l.strip())
    sub = rules_subnet(rd(P("/etc/iptables/rules.v4")))
    vals["SSH_WIFI_SUBNET"] = sub or ""
    vals["DIAG_INSTALL"] = "1" if os.path.exists(P("/etc/systemd/system/autopi-diag.timer")) else "0"

    # Sjabloon invullen: gewone sleutels op hun plek, wifi en SSH-sleutels als blok.
    lines = template.splitlines()
    out_lines = []
    wifi_done = ssh_done = False
    for l in lines:
        m = re.match(r"^([A-Z][A-Z0-9_]*)=", l)
        if m and RE_WIFI.match(m.group(1)):
            if not wifi_done:
                for i, (s, p, pr) in enumerate(wifi, 1):
                    out_lines.append("WIFI_%d_SSID=%s" % (i, quote(s)))
                    out_lines.append("WIFI_%d_PSK=%s" % (i, quote(p)))
                    out_lines.append("WIFI_%d_PRIORITY=%s" % (i, quote(pr)))
                if not wifi:
                    out_lines.append("WIFI_1_SSID=")
                    out_lines.append("WIFI_1_PSK=")
                    out_lines.append("WIFI_1_PRIORITY=")
                wifi_done = True
            continue
        if m and RE_SSHKEY.match(m.group(1)):
            if not ssh_done:
                for i, k in enumerate(keys, 1):
                    out_lines.append("SSH_PUBKEY_%d=%s" % (i, quote(k)))
                if not keys:
                    out_lines.append("SSH_PUBKEY_1=")
                ssh_done = True
            continue
        if m and m.group(1) in vals:
            out_lines.append("%s=%s" % (m.group(1), quote(vals[m.group(1)])))
            continue
        out_lines.append(l)
    kop = ["# Geëxporteerd door install.sh --export-config van een bestaande dongle.",
           "# Bevat geheimen: chmod 600, niet delen, niet committen.", ""]
    wr(out, "\n".join(kop + out_lines) + "\n", 0o600)
    # samenvatting zonder waarden
    cfg = parse_config(out)
    gevuld = sorted(k for k, v in cfg.items() if v != "")
    print("   %d velden ingevuld, %d wifi-netwerk(en), %d SSH-sleutel(s)" % (len(gevuld), len(wifi), len(keys)))
    for m_ in missing:
        print("   let op: %s" % m_)
    return 0

# ------------------------------------------------------------- main

def main(argv):
    if len(argv) < 2:
        print("gebruik: zie install.sh", file=sys.stderr)
        return 2
    cmd = argv[1]
    try:
        if cmd == "validate":
            cfg = parse_config(argv[2])
            fouten, waarsch = validate(cfg)
            for w in waarsch:
                print("   waarschuwing: %s" % w)
            for f in fouten:
                print("   FOUT: %s" % f)
            return 1 if fouten else 0
        if cmd == "get":
            key = argv[3]
            if is_secret(key):
                print("geheim veld - niet via get", file=sys.stderr)
                return 2
            print(effective(parse_config(argv[2])).get(key, ""))
            return 0
        if cmd == "sshkeys":
            for k in ssh_keys(parse_config(argv[2])):
                print(k)
            return 0
        if cmd == "nwifi":
            print(len(wifi_networks(parse_config(argv[2]))))
            return 0
        if cmd == "render":
            kind, cfgp, cur, tpl, out = argv[2:7]
            cfg = parse_config(cfgp)
            eff = effective(cfg)
            current = rd(cur)
            template = rd(tpl) if tpl != "-" else None
            warn = []
            if kind == "eniro-env":
                res = kv_render(current, template, {
                    "INGEST_URL": LOKALE_URL,
                    "INTERVAL_CHARGING": eff["INTERVAL_CHARGING"],
                    "INTERVAL_IDLE": eff["INTERVAL_IDLE"],
                    "INTERVAL_UNMETERED": eff["INTERVAL_UNMETERED"],
                    "UPLOAD_WAIT": eff["UPLOAD_WAIT"],
                }, header="# toegevoegd door eniro-obd install.sh")
            elif kind == "ingest-env":
                setmap = {
                    "INGEST_PATH": INGEST_PATH,
                    "MQTT_HOST": eff["MQTT_HOST"], "MQTT_PORT": eff["MQTT_PORT"],
                    "MQTT_USER": eff["MQTT_USER"], "MQTT_PASS": eff["MQTT_PASS"],
                    "MQTT_PREFIX": eff["MQTT_PREFIX"],
                    "BUNDEL_INTERVAL": eff["BUNDEL_INTERVAL"],
                    "PROFIEL_STANDAARD": eff["PROFIEL_STANDAARD"],
                }
                # Discovery: alleen erbij zetten als het aan moet, of als de
                # regel er al staat. HA_DISCOVERY=0 is ook de standaard in de
                # code; een bestaande installatie zonder de regel blijft zo
                # byte voor byte gelijk (geen herstart).
                basis = current if current is not None else template
                for k in ("HA_DISCOVERY", "HA_DISCOVERY_PREFIX"):
                    if eff["HA_DISCOVERY"] == "1" or kv_get(basis, k) is not None:
                        setmap[k] = eff[k]
                res = kv_render(current, template, setmap,
                                header="# toegevoegd door eniro-obd install.sh")
            elif kind == "wg0":
                res = wg_render(current, template, eff)
            elif kind == "reresolve":
                res = kv_render(template, None, {
                    "PEER": '"%s"' % eff["WG_SERVER_PUBLIC_KEY"],
                    "ENDPOINT": '"%s"' % eff["WG_ENDPOINT"]})
            elif kind == "hostapd":
                if current is None:
                    raise ConfigError("hostapd_uap0.conf niet gevonden")
                res = hostapd_render(current, eff)
            elif kind == "grains":
                res = grains_render(current, cfg)
            elif kind == "engines":
                res = engines_render(current, eff, warn)
            elif kind == "authkeys":
                res = authkeys_render(current, ssh_keys(cfg))
            elif kind == "rules":
                res = rules_render(current, eff["SSH_WIFI_SUBNET"])
            else:
                raise ConfigError("onbekend soort: %s" % kind)
            for w in warn:
                print("   waarschuwing: %s" % w, file=sys.stderr)
            wr(out, res, 0o600)
            return 0
        if cmd == "maskdiff":
            cfgp, old, new, label = argv[2:6]
            summary = len(argv) > 6 and argv[6] == "summary"
            sec = secrets_of(parse_config(cfgp)) if cfgp != "-" else set()
            for l in maskdiff(rd(old), rd(new), label, sec, summary):
                print(l)
            return 0
        if cmd == "same":
            kind, a, b = argv[2:5]
            ta, tb = rd(a), rd(b)
            if ta is None or tb is None:
                return 1
            sa, sb = strip_comments(ta, kind), strip_comments(tb, kind)
            return 0 if (sa is not None and sa == sb) else 1
        if cmd == "rulepresent":
            return 0 if (rule_present(rd(argv[2]), argv[3], "ssh") and rule_present(rd(argv[2]), argv[3], "icmp")) else 1
        if cmd == "rulelines":
            for l in rule_lines(argv[2]):
                print(l)
            return 0
        if cmd == "export":
            return export(argv[2], argv[3], rd(argv[4]))
        if cmd == "secrets":
            # alleen voor de lekcontrole in de tests; schrijft naar een bestand
            sec = secrets_of(parse_config(argv[2]))
            wr(argv[3], "\n".join(sorted(sec)) + "\n", 0o600)
            return 0
    except ConfigError as e:
        print("   FOUT: %s" % e, file=sys.stderr)
        return 2
    print("onbekend commando: %s" % cmd, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
