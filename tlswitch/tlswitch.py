#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TL-SwitchOS - transforme une machine Linux (Debian) a plusieurs ports Ethernet
en switch administrable de niveau 2 (VLAN 802.1Q, LACP, STP/RSTP, LLDP, SVI,
port security, DHCP snooping, IGMP snooping, storm control, SPAN, SNMPv3, syslog,
RADIUS, sauvegarde SCP, export Prometheus).

Un seul fichier, uniquement la bibliotheque standard de Python 3.

  tlswitch apply  [--dry-run] [fichier]   applique une configuration
  tlswitch cli                            console facon switch (aussi via "tlsh")
  tlswitch check  [fichier]               verifie la syntaxe d'un fichier
  tlswitch boot                           demarrage (startup, sinon sauvegarde, sinon usine)
  tlswitch guard                          surveillance (lance chaque minute)
  tlswitch rollback                       retour arriere immediat
  tlswitch upgrade [--verifier] [--force] [--sans-signature] <paquet.tlsu | usbflash0:NOM | http://...>
  tlswitch upgrade --retour               revenir a la version precedente du logiciel
  tlswitch archive                        sauvegarde de la config maintenant (archive path)
  tlswitch metrics                        serveur d'export Prometheus (service tlswitch-metrics)

Projet TECH-LINUX.
"""

import fcntl
import ipaddress
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time

VERSION = "1.0.4"

ETC = os.environ.get("TLSWITCH_ETC", "/etc/tlswitch")
RUN = os.environ.get("TLSWITCH_RUN", "/run/tlswitch")
STARTUP = os.path.join(ETC, "startup.conf")
PORTS_FILE = os.path.join(ETC, "ports")
RUNNING = os.path.join(RUN, "running.conf")
LOCK = os.path.join(RUN, "lock")
BRIDGE = "br0"
MAX_VID = 4094


class CliError(Exception):
    pass


# ---------------------------------------------------------------------------
# Outils
# ---------------------------------------------------------------------------

def parse_vlan_list(text):
    """'10,20-25,30' -> {10,20,21,...}"""
    out = set()
    for part in str(text).replace(" ", "").split(","):
        if not part:
            continue
        m = re.fullmatch(r"(\d+)(?:-(\d+))?", part)
        if not m:
            raise CliError("liste de VLAN invalide : %s" % text)
        a = int(m.group(1))
        b = int(m.group(2) or a)
        if a > b:
            a, b = b, a
        if a < 1 or b > MAX_VID:
            raise CliError("VLAN hors plage 1-%d : %s" % (MAX_VID, part))
        out.update(range(a, b + 1))
    if not out:
        raise CliError("liste de VLAN vide")
    return out


def fmt_vlan_list(vids):
    vids = sorted(vids)
    out, i = [], 0
    while i < len(vids):
        j = i
        while j + 1 < len(vids) and vids[j + 1] == vids[j] + 1:
            j += 1
        out.append(str(vids[i]) if i == j else "%d-%d" % (vids[i], vids[j]))
        i = j + 1
    return ",".join(out)


def natural_key(name):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", name)]


IF_PREFIX = {"ge": "GigabitEthernet1/0/", "po": "Port-channel", "vlan": "Vlan"}
IF_SHORT = {"ge": "Gi1/0/", "po": "Po", "vlan": "Vlan"}


def canon_if(text):
    """Nom Cisco ou Linux -> nom interne (ge1, po1, vlan10).
    Accepte : GigabitEthernet1/0/1, gi1/0/1, g1/0/1, Gi 1/0/1, ge1, Port-channel1, po1, Vlan10, vlan 10."""
    t = text.replace(" ", "").lower()
    m = re.fullmatch(r"([a-z-]*?)((?:\d+/)*)(\d+)", t)
    if not m:
        raise CliError("%% Interface invalide : %s" % text)
    pfx, n = m.group(1), int(m.group(3))
    if pfx in ("ge", "eth") or (pfx and ("gigabitethernet".startswith(pfx) or "tengigabitethernet".startswith(pfx)
                                         or "fastethernet".startswith(pfx) or pfx in ("te", "fa"))):
        return "ge%d" % n
    if pfx and ("port-channel".startswith(pfx) or pfx == "portchannel"):
        return "po%d" % n
    if pfx and "vlan".startswith(pfx):
        return "vlan%d" % n
    raise CliError("%% Interface invalide : %s" % text)


def disp_if(name, long=False):
    m = re.fullmatch(r"([a-z]+)(\d+)", name)
    if not m or m.group(1) not in IF_PREFIX:
        return name
    return (IF_PREFIX if long else IF_SHORT)[m.group(1)] + m.group(2)


def expand_if_range(text, known):
    """'Gi1/0/1-4, Gi1/0/7' ou 'ge1' -> liste de noms internes."""
    out = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        m = re.fullmatch(r"(.*?)(\d+)\s*-\s*(\d+)", part)
        if m:
            base = canon_if(m.group(1) + m.group(2))
            pfx = re.match(r"[a-z]+", base).group(0)
            a, b = int(m.group(2)), int(m.group(3))
            for n in range(min(a, b), max(a, b) + 1):
                out.append("%s%d" % (pfx, n))
        else:
            out.append(canon_if(part))
    for n in out:
        if n.startswith("ge") and n not in known:
            raise CliError("%% Interface inconnue : %s (ports : %s a %s)" % (
                disp_if(n), disp_if(sorted(known, key=natural_key)[0]) if known else "-",
                disp_if(sorted([k for k in known if k.startswith("ge")], key=natural_key)[-1]) if known else "-"))
    if not out:
        raise CliError("% Interface manquante")
    return out


def parse_ip(addr, mask=None):
    try:
        if mask:
            return str(ipaddress.ip_interface("%s/%s" % (addr, mask)))
        if "/" not in addr:
            raise CliError("indique le masque : 192.168.1.10/24 ou 192.168.1.10 255.255.255.0")
        return str(ipaddress.ip_interface(addr))
    except ValueError:
        raise CliError("adresse IP invalide : %s" % addr)


def sh(cmd, check=False):
    """Execute une commande, renvoie stdout (str)."""
    try:
        r = subprocess.run(cmd, shell=isinstance(cmd, str), capture_output=True, text=True)
    except FileNotFoundError:
        return ""
    if check and r.returncode != 0:
        raise CliError(r.stderr.strip() or "echec : %s" % cmd)
    return r.stdout


def sh_json(cmd):
    out = sh(cmd)
    try:
        return json.loads(out) if out.strip() else []
    except ValueError:
        return []


def discover_ports():
    """Ports physiques du switch : TLSWITCH_PORTS, /etc/tlswitch/ports, sinon ge* dans /sys."""
    env = os.environ.get("TLSWITCH_PORTS")
    if env:
        return env.split()
    if os.path.exists(PORTS_FILE):
        names = []
        with open(PORTS_FILE) as f:
            for line in f:
                line = line.split("#")[0].strip()
                if line:
                    names.append(line.split()[0])
        if names:
            return names
    try:
        return sorted([n for n in os.listdir("/sys/class/net") if re.fullmatch(r"ge\d+", n)], key=natural_key)
    except OSError:
        return []


def hash_secret(plain):
    r = subprocess.run(["openssl", "passwd", "-6", "-stdin"], input=plain + "\n",
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise CliError("% openssl indisponible pour chiffrer le secret")
    return r.stdout.strip()


def check_secret(plain, hashed):
    if not hashed or not hashed.startswith("$6$"):
        return False
    salt = hashed.split("$")[2]
    r = subprocess.run(["openssl", "passwd", "-6", "-salt", salt, "-stdin"], input=plain + "\n",
                       capture_output=True, text=True)
    import hmac
    return r.returncode == 0 and hmac.compare_digest(r.stdout.strip(), hashed)


def parse_secret(text):
    """'0 clair' | '5 $6$...' | 'clair' -> hash $6$"""
    parts = text.strip().split(None, 1)
    if len(parts) == 2 and parts[0] in ("0", "5", "9", "8"):
        kind, val = parts
    else:
        kind, val = "0", text.strip()
    if kind != "0":
        if not re.fullmatch(r"\$6\$[./A-Za-z0-9]{1,16}\$[./A-Za-z0-9]{86}", val):
            raise CliError("% Hash invalide (attendu : $6$...)")
        return val
    if len(val) < 8:
        raise CliError("% Secret trop court (8 caracteres minimum)")
    return hash_secret(val)


# ---------------------------------------------------------------------------
# Modele de configuration
# ---------------------------------------------------------------------------

class Port:
    def __init__(self, name):
        self.name = name
        self.description = ""
        self.shutdown = False
        self.mode = "access"
        self.access_vlan = 1
        self.allowed = None          # None = tous les VLAN definis
        self.native = 1
        self.channel_group = None
        self.lacp_mode = "active"
        self.portfast = False
        self.bpduguard = False
        self.mtu = None
        self.ps = False              # switchport port-security
        self.ps_max = 1
        self.ps_macs = []
        self.ps_violation = "shutdown"
        self.ps_aging = 0            # minutes, 0 = jamais
        self.dhcp_trust = False
        self.storm = {}              # broadcast|multicast -> paquets/s


class Svi:
    def __init__(self, vid):
        self.vid = vid
        self.description = ""
        self.shutdown = False
        self.addresses = []
        self.dhcp = False


class Model:
    def __init__(self, ports):
        self.hostname = "tl-switch"
        self.vlans = {1: "default"}
        self.ports = {p: Port(p) for p in ports}   # physiques + poN
        self.svis = {}
        self.gateway = None
        self.dns = []
        self.ntp = []
        self.stp_mode = "rstp"
        self.stp_priority = 32768
        self.lldp = True
        self.snmp_community = None
        self.snmp_location = ""
        self.users = {}            # nom -> [privilege, hash]
        self.must_change = set()   # comptes dont le mot de passe est provisoire
        self.enable_secret = None
        self.http = True
        self.ssh = True
        self.banner = ""
        self.snmp_contact = ""
        self.snmp_users = {}       # nom -> [sha|sha256|sha512, cle auth, aes|aes256|None, cle priv]
        self.log_hosts = []        # [ip, udp|tcp, port]
        self.log_level = "informational"
        self.radius = []           # [ip, port, cle]
        self.radius_timeout = 3
        self.aaa_radius = False
        self.dhcp_snooping = False
        self.dhcp_snooping_vlans = set()   # vide = toutes
        self.igmp_snooping = True
        self.igmp_querier = False
        self.monitor = {}          # n -> {"src": {port: rx|tx|both}, "dst": port ou None}
        self.archive_path = None
        self.archive_period = 0    # minutes
        self.archive_write = False
        self.archive_max = 10
        self.errdisable_recovery = 0
        self.prom = False
        self.prom_port = 9101
        self.prom_allow = []

    @property
    def physical(self):
        return sorted([p for p in self.ports if not p.startswith("po")], key=natural_key)

    @property
    def channels(self):
        return sorted([p for p in self.ports if p.startswith("po")], key=natural_key)

    def members(self, po):
        n = int(po[2:])
        return [p for p in self.physical if self.ports[p].channel_group == n]

    def span_dsts(self):
        return {s["dst"] for s in self.monitor.values() if s.get("dst")}

    def bridge_ports(self):
        """Ports qui sont dans le bridge : physiques hors agregat + port-channels
        (une destination SPAN sort du bridge : elle ne recoit que la copie du trafic)."""
        dst = self.span_dsts()
        return [p for p in self.physical if self.ports[p].channel_group is None and p not in dst] + self.channels

    def port_vlans(self, name):
        """{vid: untagged(bool)}, pvid"""
        p = self.ports[name]
        if p.mode == "access":
            return {p.access_vlan: True}, p.access_vlan
        allowed = set(self.vlans) if p.allowed is None else set(p.allowed)
        res = {v: False for v in allowed}
        pvid = None
        if p.native in allowed:
            res[p.native] = True
            pvid = p.native
        return res, pvid

    # -- rendu texte (running-config) --------------------------------------
    def render(self):
        L = ["!", "! TL-SwitchOS %s - configuration" % VERSION, "!", "hostname %s" % self.hostname, "!"]
        if self.enable_secret:
            L.append("enable secret 5 %s" % self.enable_secret)
        for u in sorted(self.users):
            if self.users[u][1] is None:
                L.append("username %s privilege %d radius" % (u, self.users[u][0]))
            else:
                L.append("username %s privilege %d secret 5 %s" % (u, self.users[u][0], self.users[u][1]))
            if u in self.must_change:
                L.append("username %s password-change-required" % u)
        if self.banner:
            L.append("banner motd ^%s^" % self.banner)
        L.append("!")
        for vid in sorted(self.vlans):
            L.append("vlan %d" % vid)
            if self.vlans[vid] and not (vid == 1 and self.vlans[vid] == "default"):
                L.append(" name %s" % self.vlans[vid])
            L.append("!")
        if self.stp_mode == "none":
            L.append("no spanning-tree")
        else:
            L.append("spanning-tree mode %s" % self.stp_mode)
            if self.stp_priority != 32768:
                L.append("spanning-tree priority %d" % self.stp_priority)
        L.append("lldp run" if self.lldp else "no lldp run")
        for s in self.ntp:
            L.append("ntp server %s" % s)
        if self.snmp_community:
            L.append("snmp-server community %s ro" % self.snmp_community)
        if self.snmp_location:
            L.append("snmp-server location %s" % self.snmp_location)
        if self.snmp_contact:
            L.append("snmp-server contact %s" % self.snmp_contact)
        for u in sorted(self.snmp_users):
            a, ka, p, kp = self.snmp_users[u]
            L.append("snmp-server user %s auth %s %s%s" % (u, a, ka, " priv %s %s" % (p, kp) if p else ""))
        for ip, proto, port in self.log_hosts:
            L.append("logging host %s%s" % (ip, "" if (proto, port) == ("udp", 514)
                                            else " transport %s port %d" % (proto, port)))
        if self.log_level != "informational":
            L.append("logging trap %s" % self.log_level)
        for ip, port, key in self.radius:
            L.append("radius-server host %s%s key %s" % (ip, "" if port == 1812 else " auth-port %d" % port, key))
        if self.radius_timeout != 3:
            L.append("radius-server timeout %d" % self.radius_timeout)
        if self.aaa_radius:
            L.append("aaa authentication login default group radius local")
        if not self.igmp_snooping:
            L.append("no ip igmp snooping")
        if self.igmp_querier:
            L.append("ip igmp snooping querier")
        if self.dhcp_snooping:
            L.append("ip dhcp snooping")
            if self.dhcp_snooping_vlans:
                L.append("ip dhcp snooping vlan %s" % fmt_vlan_list(self.dhcp_snooping_vlans))
        if self.errdisable_recovery:
            L.append("errdisable recovery interval %d" % self.errdisable_recovery)
        if self.archive_path:
            L.append("archive path %s" % self.archive_path)
        if self.archive_write:
            L.append("archive write-memory")
        if self.archive_period:
            L.append("archive time-period %d" % self.archive_period)
        if self.archive_max != 10:
            L.append("archive maximum %d" % self.archive_max)
        if self.prom:
            L.append("prometheus exporter")
            if self.prom_port != 9101:
                L.append("prometheus exporter port %d" % self.prom_port)
            for n in self.prom_allow:
                L.append("prometheus exporter allow %s" % n)
        L.append("!")
        for name in self.channels + self.physical:
            p = self.ports[name]
            L.append("interface %s" % disp_if(name, True))
            if p.description:
                L.append(" description %s" % p.description)
            if p.channel_group is not None:
                L.append(" channel-group %d mode %s" % (p.channel_group, p.lacp_mode))
            else:
                L.append(" switchport mode %s" % p.mode)
                if p.mode == "access":
                    if p.access_vlan != 1:
                        L.append(" switchport access vlan %d" % p.access_vlan)
                else:
                    if p.allowed is not None:
                        L.append(" switchport trunk allowed vlan %s" % (fmt_vlan_list(p.allowed) or "none"))
                    if p.native != 1:
                        L.append(" switchport trunk native vlan %d" % p.native)
                if p.portfast:
                    L.append(" spanning-tree portfast")
                if p.bpduguard:
                    L.append(" spanning-tree bpduguard enable")
                if p.ps:
                    L.append(" switchport port-security")
                if p.ps_max != 1:
                    L.append(" switchport port-security maximum %d" % p.ps_max)
                for mac in p.ps_macs:
                    L.append(" switchport port-security mac-address %s" % mac)
                if p.ps_violation != "shutdown":
                    L.append(" switchport port-security violation %s" % p.ps_violation)
                if p.ps_aging:
                    L.append(" switchport port-security aging time %d" % p.ps_aging)
                if p.dhcp_trust:
                    L.append(" ip dhcp snooping trust")
                for k in ("broadcast", "multicast"):
                    if k in p.storm:
                        L.append(" storm-control %s level pps %d" % (k, p.storm[k]))
            if p.mtu:
                L.append(" mtu %d" % p.mtu)
            if p.shutdown:
                L.append(" shutdown")
            L.append("!")
        for vid in sorted(self.svis):
            s = self.svis[vid]
            L.append("interface Vlan%d" % vid)
            if s.description:
                L.append(" description %s" % s.description)
            if s.dhcp:
                L.append(" ip address dhcp")
            for a in s.addresses:
                i = ipaddress.ip_interface(a)
                L.append(" ip address %s %s" % (i.ip, i.netmask) if i.version == 4 else " ipv6 address %s" % a)
            if s.shutdown:
                L.append(" shutdown")
            L.append("!")
        for n in sorted(self.monitor):
            sess = self.monitor[n]
            for src in sorted(sess["src"], key=natural_key):
                L.append("monitor session %d source interface %s %s" % (n, disp_if(src, True), sess["src"][src]))
            if sess.get("dst"):
                L.append("monitor session %d destination interface %s" % (n, disp_if(sess["dst"], True)))
        if self.monitor:
            L.append("!")
        if self.gateway:
            L.append("ip default-gateway %s" % self.gateway)
        for d in self.dns:
            L.append("ip name-server %s" % d)
        L.append("!")
        L.append("ip http secure-server" if self.http else "no ip http secure-server")
        L.append("ip ssh server" if self.ssh else "no ip ssh server")
        L.append("!")
        L.append("end")
        return "\n".join(L) + "\n"


# ---------------------------------------------------------------------------
# Interpreteur de commandes (commun au fichier de config et a la CLI)
# ---------------------------------------------------------------------------

KW, ARG, REST = 0, 1, 2


class Cmd:
    def __init__(self, pattern, func, help_):
        self.tokens = []
        for t in pattern.split():
            if t == "<...>":
                self.tokens.append((REST, t))
            elif t.startswith("<"):
                self.tokens.append((ARG, t))
            else:
                self.tokens.append((KW, t))
        self.func = func
        self.help = help_


class Grammar:
    def __init__(self):
        self.cmds = []

    def add(self, pattern, help_=""):
        def deco(f):
            self.cmds.append(Cmd(pattern, f, help_))
            return f
        return deco

    def match(self, words):
        """Renvoie (cmd, args) ou leve CliError. Abreviations acceptees."""
        cands = [(c, []) for c in self.cmds]
        for i, w in enumerate(words):
            nxt = []
            exact_kw = set()
            for c, args in cands:
                if len(c.tokens) > i and c.tokens[i][0] == KW and c.tokens[i][1] == w.lower():
                    exact_kw.add(c.tokens[i][1])
            for c, args in cands:
                if any(t[0] == REST for t in c.tokens[:i]):
                    nxt.append((c, args))     # deja dans un <...>
                    continue
                if len(c.tokens) <= i:
                    continue
                kind, tok = c.tokens[i]
                if kind == KW:
                    if exact_kw:
                        if tok in exact_kw:
                            nxt.append((c, args))
                    elif tok.startswith(w.lower()):
                        nxt.append((c, args))
                elif kind == ARG:
                    nxt.append((c, args + [w]))
                else:
                    nxt.append((c, args + [" ".join(words[i:])]))
            # priorite aux mots-cles sur les arguments
            kws = [(c, a) for c, a in nxt if len(c.tokens) > i and c.tokens[i][0] == KW
                   and not any(t[0] == REST for t in c.tokens[:i])]
            if kws:
                distinct = {c.tokens[i][1] for c, a in kws}
                if len(distinct) > 1 and not exact_kw:
                    raise CliError("% Commande ambigue : '" + " ".join(words[:i + 1]) + "' (" + ", ".join(sorted(distinct)) + ")")
                nxt = kws + [(c, a) for c, a in nxt if any(t[0] == REST for t in c.tokens[:i])]
            if not nxt:
                raise CliError("%% Commande invalide au mot '%s'" % w)
            cands = nxt
        done = [(c, a) for c, a in cands
                if len(c.tokens) == len(words) or (c.tokens[-1][0] == REST and len(c.tokens) <= len(words))]
        if not done:
            raise CliError("% Commande incomplete")
        return done[0]

    def help_for(self, words, partial):
        """Liste (mot, aide) possibles apres words, filtre par partial."""
        res = {}
        for c in self.cmds:
            ok = True
            for i, w in enumerate(words):
                if i >= len(c.tokens):
                    ok = False
                    break
                kind, tok = c.tokens[i]
                if kind == REST:
                    break
                if kind == KW and not tok.startswith(w.lower()):
                    ok = False
                    break
            if not ok or len(c.tokens) <= len(words):
                if ok and len(c.tokens) == len(words):
                    res["<cr>"] = ""
                continue
            kind, tok = c.tokens[len(words)]
            if kind == KW and not tok.startswith(partial.lower()):
                continue
            res.setdefault(tok, c.help if len(c.tokens) == len(words) + 1 else KW_HELP.get(tok, ""))
        return res


KW_HELP = {
    "show": "Afficher l'etat du switch", "configure": "Mode configuration", "interfaces": "Etat des interfaces",
    "ip": "Parametres IP", "mac": "Table MAC", "lldp": "LLDP", "cdp": "Voisins", "etherchannel": "Agregats",
    "archive": "Retour arriere", "clear": "Effacer", "copy": "Copier", "write": "Enregistrer",
    "no": "Annuler une commande", "spanning-tree": "Spanning-tree", "switchport": "Parametres L2",
    "snmp-server": "SNMP", "ntp": "NTP", "username": "Comptes", "enable": "Mode enable", "terminal": "Terminal",
    "interface": "Configurer une interface", "vlan": "VLAN", "channel-group": "Agregat LACP",
    "banner": "Banniere", "running-config": "Configuration active", "address": "Adresse IP",
    "default-gateway": "Passerelle", "name-server": "DNS", "trunk": "Mode trunk", "access": "Mode access",
    "http": "Interface web", "ssh": "SSH", "mode": "Mode", "secret": "Mot de passe", "do": "Commande exec",
    "logging": "Syslog distant", "radius-server": "Serveur RADIUS", "aaa": "Authentification",
    "monitor": "Port mirroring (SPAN)", "port-security": "Securite du port", "dhcp": "DHCP snooping",
    "igmp": "IGMP snooping", "storm-control": "Limitation des tempetes", "errdisable": "Ports err-disabled",
    "prometheus": "Export Prometheus", "test": "Tester", "radius": "Compte RADIUS",
}

EXEC = Grammar()
CONF = Grammar()
CONF_IF = Grammar()
CONF_SVI = Grammar()
CONF_VLAN = Grammar()

MODES = {"exec": EXEC, "config": CONF, "config-if": CONF_IF, "config-svi": CONF_SVI, "config-vlan": CONF_VLAN}
PROMPTS = {"exec": "#", "config": "(config)#", "config-if": "(config-if)#",
           "config-svi": "(config-if)#", "config-vlan": "(config-vlan)#"}


class Session:
    """Etat d'une session : mode courant + contexte. Partage par fichier, CLI et web."""

    def __init__(self, model, priv=15, user="root"):
        self.m = model
        self.mode = "config"
        self.ctx = None
        self.priv = priv          # 1 = lecture (prompt >), 15 = admin (prompt #)
        self.user = user
        self.interactive = False
        self.out = sys.stdout

    def say(self, text=""):
        self.out.write(text + "\n")

    def prompt(self):
        if self.mode == "exec":
            return self.m.hostname + ("#" if self.priv >= 15 else ">")
        return self.m.hostname + PROMPTS[self.mode]

    def run_line(self, line, from_file=False):
        raw = line.rstrip("\n")
        if raw.lstrip().startswith("!"):
            return
        line = raw.strip()
        if not line:
            return
        words = line.split()
        if from_file and not raw.startswith((" ", "\t")) and self.mode != "exec":
            self.mode, self.ctx = "config", None     # ligne non indentee = niveau global
        if self.mode != "exec" and words[0].lower() == "do" and len(words) > 1:
            cmd, args = EXEC.match(words[1:])
        elif self.mode.startswith("config-"):
            # dans un sous-mode, une commande globale fait remonter (comme IOS)
            try:
                cmd, args = MODES[self.mode].match(words)
            except CliError:
                cmd, args = CONF.match(words)
                self.mode, self.ctx = "config", None
        else:
            cmd, args = MODES[self.mode].match(words)
        if self.mode == "exec" and getattr(cmd.func, "priv15", False) and self.priv < 15:
            raise CliError("% Commande reservee au niveau 15 (tape enable)")
        cmd.func(self, *args)


def priv15(f):
    f.priv15 = True
    return f


# --- mode config global ------------------------------------------------------

@CONF.add("hostname <nom>", "Nom du switch")
def c_hostname(s, name):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,62}", name):
        raise CliError("% Nom invalide (lettres, chiffres, tirets)")
    s.m.hostname = name


@CONF.add("vlan <liste>", "Creer des VLAN (ex : 10,20-25)")
def c_vlan(s, lst):
    vids = parse_vlan_list(lst)
    for v in vids:
        s.m.vlans.setdefault(v, "VLAN%04d" % v)
    s.mode, s.ctx = "config-vlan", sorted(vids)


@CONF.add("no vlan <liste>", "Supprimer des VLAN")
def c_no_vlan(s, lst):
    for v in parse_vlan_list(lst):
        if v == 1:
            raise CliError("% Le VLAN 1 ne peut pas etre supprime")
        s.m.vlans.pop(v, None)


@CONF_VLAN.add("name <nom>", "Nom du VLAN")
def cv_name(s, name):
    for v in s.ctx:
        s.m.vlans[v] = name


@CONF.add("interface <...>", "Configurer un port (Gi1/0/1, range Gi1/0/1-8, Po1, Vlan10)")
def c_interface(s, text):
    t = text.strip()
    if t.lower().startswith("range "):
        t = t[6:]
    names = expand_if_range(t, s.m.ports)
    if names[0].startswith("vlan"):
        if len(names) != 1:
            raise CliError("% Une seule interface Vlan a la fois")
        v = int(names[0][4:])
        if not 1 <= v <= MAX_VID:
            raise CliError("% VLAN 1-4094")
        if v not in s.m.svis:
            s.m.svis[v] = Svi(v)
            s.m.vlans.setdefault(v, "VLAN%04d" % v)
        s.mode, s.ctx = "config-svi", v
        return
    if any(n.startswith("vlan") for n in names):
        raise CliError("% Ne melange pas Vlan et ports")
    for n in names:
        if n not in s.m.ports:
            if not 1 <= int(n[2:]) <= 64:
                raise CliError("% Port-channel 1-64")
            s.m.ports[n] = Port(n)
            s.m.ports[n].mode = "trunk"
    s.mode, s.ctx = "config-if", names


@CONF.add("no interface <...>", "Supprimer un Port-channel ou une interface Vlan")
def c_no_interface(s, text):
    name = canon_if(text)
    if name.startswith("vlan"):
        s.m.svis.pop(int(name[4:]), None)
    elif name.startswith("po"):
        for p in s.m.members(name):
            s.m.ports[p].channel_group = None
        s.m.ports.pop(name, None)
    else:
        raise CliError("% Seuls Port-channel et Vlan peuvent etre supprimes")


@CONF.add("ip default-gateway <ip>", "Passerelle par defaut")
def c_gw(s, ip):
    try:
        ipaddress.ip_address(ip)
    except ValueError:
        raise CliError("% Adresse invalide")
    s.m.gateway = ip


@CONF.add("no ip default-gateway", "Retirer la passerelle")
def c_no_gw(s):
    s.m.gateway = None


@CONF.add("ip name-server <...>", "Serveurs DNS")
def c_dns(s, rest):
    for ip in rest.split():
        ipaddress.ip_address(ip)
        if ip not in s.m.dns:
            s.m.dns.append(ip)


@CONF.add("no ip name-server", "Retirer les DNS")
def c_no_dns(s):
    s.m.dns = []


@CONF.add("ntp server <ip>", "Serveur NTP")
def c_ntp(s, ip):
    try:
        ipaddress.ip_address(ip)
    except ValueError:
        if not re.fullmatch(r"(?=.{1,253}$)[A-Za-z0-9]([A-Za-z0-9-]{0,62})(\.[A-Za-z0-9]([A-Za-z0-9-]{0,62}))*\.?", ip):
            raise CliError("% Adresse ou nom de serveur NTP invalide")
    if ip not in s.m.ntp:
        s.m.ntp.append(ip)


@CONF.add("no ntp server <ip>", "Retirer un serveur NTP")
def c_no_ntp(s, ip):
    if ip in s.m.ntp:
        s.m.ntp.remove(ip)


@CONF.add("spanning-tree mode <stp|rstp>", "Mode spanning-tree (rapid-pvst/mst accepte = rstp)")
def c_stp_mode(s, mode):
    mode = {"rapid-pvst": "rstp", "mst": "rstp", "pvst": "stp"}.get(mode.lower(), mode.lower())
    if mode not in ("stp", "rstp"):
        raise CliError("% stp ou rstp")
    s.m.stp_mode = mode


@CONF.add("spanning-tree priority <0-61440>", "Priorite du pont (multiple de 4096)")
def c_stp_prio(s, p):
    p = int(p)
    if p < 0 or p > 61440 or p % 4096:
        raise CliError("% Priorite : multiple de 4096 entre 0 et 61440")
    s.m.stp_priority = p


@CONF.add("no spanning-tree", "Desactiver le spanning-tree")
def c_no_stp(s):
    s.m.stp_mode = "none"


@CONF.add("lldp run", "Activer LLDP")
def c_lldp(s):
    s.m.lldp = True


@CONF.add("no lldp run", "Desactiver LLDP")
def c_no_lldp(s):
    s.m.lldp = False


@CONF.add("snmp-server community <nom> ro", "Communaute SNMP v2c en lecture")
def c_snmp(s, name):
    s.m.snmp_community = name


@CONF.add("snmp-server location <...>", "Emplacement SNMP")
def c_snmp_loc(s, loc):
    s.m.snmp_location = loc


@CONF.add("no snmp-server", "Desactiver SNMP")
def c_no_snmp(s):
    s.m.snmp_community = None


def compte_systeme(name):
    """Compte Linux du systeme (uid < 1000, hors comptes du switch) : jamais repris par la CLI."""
    if name in ("root", "daemon", "bin", "sys", "www-data", "sshd", "nobody"):
        return True
    try:
        import pwd
        import grp
        e = pwd.getpwnam(name)
    except (KeyError, ImportError):
        return False
    try:
        membres = set(grp.getgrnam("tlswitch").gr_mem)
    except KeyError:
        membres = set()
    return e.pw_uid < 1000 and name not in membres


@CONF.add("username <nom> privilege <1|15> secret <...>", "Compte SSH/web (secret 0 <clair> ou 5 <hash>)")
def c_username(s, name, priv, secret):
    if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,30}", name):
        raise CliError("% Nom de compte invalide (minuscules, chiffres, - _)")
    if compte_systeme(name) and name not in s.m.users:
        raise CliError("% Nom reserve (compte du systeme)")
    priv = int(priv)
    if priv not in (1, 15):
        raise CliError("% privilege 1 (lecture) ou 15 (admin)")
    s.m.users[name] = [priv, parse_secret(secret)]
    s.m.must_change.discard(name)


@CONF.add("username <nom> password-change-required", "Mot de passe provisoire : a changer a la connexion")
def c_username_mc(s, name):
    if name not in s.m.users:
        raise CliError("%% Compte inconnu : %s" % name)
    if s.m.users[name][1] is None:
        raise CliError("% Compte RADIUS : le mot de passe est gere par l'annuaire")
    s.m.must_change.add(name)


@CONF.add("username <nom> secret <...>", "Compte SSH/web niveau 1 (lecture)")
def c_username1(s, name, secret):
    c_username(s, name, "1", secret)


def local_admins(m):
    """Comptes niveau 15 avec un mot de passe local (secours si le RADIUS ne repond plus)."""
    return [n for n, u in m.users.items() if u[0] == 15 and u[1] is not None]


@CONF.add("username <nom> privilege <1|15> radius", "Compte verifie par le serveur RADIUS (annuaire AD)")
def c_username_radius(s, name, priv):
    if not re.fullmatch(r"[a-z_][a-z0-9_.-]{0,30}", name):
        raise CliError("% Nom de compte invalide (minuscules, chiffres, . - _)")
    if compte_systeme(name) and name not in s.m.users:
        raise CliError("% Nom reserve (compte du systeme)")
    priv = int(priv)
    if priv not in (1, 15):
        raise CliError("% privilege 1 (lecture) ou 15 (admin)")
    if local_admins(s.m) == [name]:
        raise CliError("% Impossible : garde au moins un compte niveau 15 avec un mot de passe local")
    s.m.users[name] = [priv, None]
    s.m.must_change.discard(name)


@CONF.add("no username <nom>", "Supprimer un compte")
def c_no_username(s, name):
    if name in s.m.users and s.m.users[name][0] == 15 and local_admins(s.m) == [name]:
        raise CliError("% Impossible : c'est le dernier compte niveau 15 avec un mot de passe local")
    s.m.users.pop(name, None)
    s.m.must_change.discard(name)


@CONF.add("enable secret <...>", "Mot de passe du mode enable")
def c_enable_secret(s, secret):
    s.m.enable_secret = parse_secret(secret)


@CONF.add("no enable secret", "Retirer le mot de passe enable")
def c_no_enable_secret(s):
    s.m.enable_secret = None


@CONF.add("ip http secure-server", "Interface web HTTPS")
def c_http(s):
    s.m.http = True


@CONF.add("no ip http secure-server", "Couper l'interface web")
def c_no_http(s):
    s.m.http = False


@CONF.add("ip http server", "Interface web (toujours en HTTPS)")
def c_http2(s):
    s.m.http = True


@CONF.add("no ip http server", "")
def c_no_http2(s):
    pass


@CONF.add("ip ssh server", "Acces SSH")
def c_ssh(s):
    s.m.ssh = True


@CONF.add("no ip ssh server", "Couper le SSH")
def c_no_ssh(s):
    s.m.ssh = False


@CONF.add("ip ssh version 2", "SSH v2 (toujours le cas)")
def c_ssh2(s):
    s.m.ssh = True


@CONF.add("banner motd <...>", "Banniere de connexion : banner motd ^texte^")
def c_banner(s, text):
    t = text.strip()
    if len(t) >= 2 and t[0] == t[-1] and not t[0].isalnum():
        t = t[1:-1]
    s.m.banner = t[:300]


@CONF.add("no banner motd", "")
def c_no_banner(s):
    s.m.banner = ""


@CONF.add("service password-encryption", "Accepte (les secrets sont toujours chiffres)")
def c_noop1(s):
    pass


@CONF.add("end", "Retour au mode exec")
def c_end(s):
    s.mode, s.ctx = "exec", None


@CONF.add("exit", "Retour au niveau precedent")
def c_exit(s):
    s.mode, s.ctx = "exec", None


# --- SNMPv3, syslog distant ----------------------------------------------------

SNMP_AUTH = ("sha", "sha256", "sha512")
SNMP_PRIV = ("aes", "aes256")


@CONF.add("snmp-server contact <...>", "Contact SNMP")
def c_snmp_contact(s, text):
    s.m.snmp_contact = text.strip()[:100]


@CONF.add("snmp-server user <nom> <...>", "Utilisateur SNMPv3 : auth sha256 <cle> priv aes <cle>")
def c_snmp_user(s, name, rest):
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,32}", name):
        raise CliError("% Nom d'utilisateur SNMP invalide")
    w = rest.split()
    # accepte aussi la forme longue : snmp-server user NOM GROUPE v3 auth sha CLE priv aes 128 CLE
    low = [x.lower() for x in w]
    if "auth" not in low:
        raise CliError("% Syntaxe : snmp-server user <nom> auth <sha|sha256|sha512> <cle> [priv <aes|aes256> <cle>]")
    i = low.index("auth")
    try:
        algo, ka = low[i + 1], w[i + 2]
    except IndexError:
        raise CliError("% Cle d'authentification manquante")
    if algo in ("sha1",):
        algo = "sha"
    if algo == "md5":
        raise CliError("% MD5 refuse (trop faible) : sha256 conseille")
    if algo not in SNMP_AUTH:
        raise CliError("% Authentification : sha, sha256 ou sha512")
    p, kp = None, None
    if "priv" in low:
        j = low.index("priv")
        rest_p = w[j + 1:]
        if not rest_p:
            raise CliError("% Chiffrement manquant")
        p = rest_p[0].lower()
        rest_p = rest_p[1:]
        if p == "aes" and rest_p and rest_p[0] in ("128", "192", "256"):
            p = "aes256" if rest_p[0] == "256" else "aes"
            if rest_p[0] == "192":
                raise CliError("% AES 192 non pris en charge : aes (128) ou aes256")
            rest_p = rest_p[1:]
        if p in ("des", "3des"):
            raise CliError("% DES refuse (trop faible) : aes")
        if p not in SNMP_PRIV or not rest_p:
            raise CliError("% Chiffrement : priv aes <cle> ou priv aes256 <cle>")
        kp = rest_p[0]
    for k in (ka, kp):
        if k is not None and (len(k) < 8 or '"' in k or "\\" in k):
            raise CliError("% Cle SNMP : 8 caracteres minimum, sans guillemet ni antislash")
    s.m.snmp_users[name] = [algo, ka, p, kp]


@CONF.add("no snmp-server user <nom>", "Supprimer un utilisateur SNMPv3")
def c_no_snmp_user(s, name):
    s.m.snmp_users.pop(name, None)


@CONF.add("no snmp-server community", "Supprimer la communaute v2c (garder SNMPv3)")
def c_no_snmp_comm(s):
    s.m.snmp_community = None


LOG_LEVELS = ("emergencies", "alerts", "critical", "errors", "warnings", "notifications",
              "informational", "debugging")
RSYSLOG_LEVEL = dict(zip(LOG_LEVELS, ("emerg", "alert", "crit", "err", "warning", "notice", "info", "debug")))


def _check_ip(ip):
    try:
        return str(ipaddress.ip_address(ip))
    except ValueError:
        raise CliError("%% Adresse IP invalide : %s" % ip)


def _log_host(s, ip, proto="udp", port="514"):
    ip = _check_ip(ip)
    proto = proto.lower()
    if proto not in ("udp", "tcp"):
        raise CliError("% transport udp ou tcp")
    port = int(port)
    if not 1 <= port <= 65535:
        raise CliError("% Port 1-65535")
    s.m.log_hosts = [h for h in s.m.log_hosts if h[0] != ip] + [[ip, proto, port]]
    if len(s.m.log_hosts) > 4:
        raise CliError("% 4 serveurs syslog maximum")


@CONF.add("logging host <ip>", "Serveur syslog distant (UDP 514)")
def c_log_host(s, ip):
    _log_host(s, ip)


@CONF.add("logging host <ip> transport <udp|tcp> port <port>", "Serveur syslog distant (UDP ou TCP)")
def c_log_host2(s, ip, proto, port):
    _log_host(s, ip, proto, port)


@CONF.add("no logging host <ip>", "Retirer un serveur syslog")
def c_no_log_host(s, ip):
    s.m.log_hosts = [h for h in s.m.log_hosts if h[0] != ip]


@CONF.add("logging trap <niveau>", "Niveau envoye au syslog (informational par defaut)")
def c_log_trap(s, lvl):
    if lvl.isdigit() and int(lvl) < 8:
        lvl = LOG_LEVELS[int(lvl)]
    full = [x for x in LOG_LEVELS if x.startswith(lvl.lower())]
    if len(full) != 1:
        raise CliError("% Niveau : " + ", ".join(LOG_LEVELS))
    s.m.log_level = full[0]


@CONF.add("no logging trap", "Niveau par defaut (informational)")
def c_no_log_trap(s):
    s.m.log_level = "informational"


# --- RADIUS (connexion des administrateurs par l'annuaire) --------------------

def _radius_key(key):
    if not re.fullmatch(r"[\x21-\x7e]{8,64}", key):
        raise CliError("% Cle RADIUS : 8 a 64 caracteres imprimables, sans espace")
    return key


def _radius_host(s, ip, port, key):
    ip = _check_ip(ip)
    port = int(port)
    if not 1 <= port <= 65535:
        raise CliError("% Port 1-65535")
    s.m.radius = [r for r in s.m.radius if r[0] != ip] + [[ip, port, _radius_key(key)]]
    if len(s.m.radius) > 3:
        raise CliError("% 3 serveurs RADIUS maximum")


@CONF.add("radius-server host <ip> key <cle>", "Serveur RADIUS (ex : NPS de l'annuaire AD)")
def c_radius(s, ip, key):
    _radius_host(s, ip, 1812, key)


@CONF.add("radius-server host <ip> auth-port <port> key <cle>", "Serveur RADIUS sur un autre port")
def c_radius2(s, ip, port, key):
    _radius_host(s, ip, port, key)


@CONF.add("no radius-server host <ip>", "Retirer un serveur RADIUS")
def c_no_radius(s, ip):
    s.m.radius = [r for r in s.m.radius if r[0] != ip]


@CONF.add("radius-server timeout <1-30>", "Delai de reponse RADIUS (secondes)")
def c_radius_to(s, n):
    n = int(n)
    if not 1 <= n <= 30:
        raise CliError("% 1 a 30 secondes")
    s.m.radius_timeout = n


@CONF.add("aaa authentication login default group radius local",
          "Connexion SSH et web par RADIUS, comptes locaux en secours")
def c_aaa(s):
    s.m.aaa_radius = True


@CONF.add("no aaa authentication login default", "Comptes locaux uniquement")
def c_no_aaa(s):
    s.m.aaa_radius = False


@CONF.add("aaa new-model", "Accepte")
def c_aaa_nm(s):
    pass


# --- DHCP snooping, IGMP snooping, err-disable --------------------------------

@CONF.add("ip dhcp snooping", "Bloquer les serveurs DHCP pirates (ports non 'trust')")
def c_dhcps(s):
    s.m.dhcp_snooping = True


@CONF.add("ip dhcp snooping vlan <liste>", "Limiter le DHCP snooping a ces VLAN")
def c_dhcps_vlan(s, lst):
    s.m.dhcp_snooping_vlans |= parse_vlan_list(lst)


@CONF.add("no ip dhcp snooping vlan <liste>", "Retirer des VLAN du DHCP snooping")
def c_no_dhcps_vlan(s, lst):
    s.m.dhcp_snooping_vlans -= parse_vlan_list(lst)


@CONF.add("no ip dhcp snooping", "Desactiver le DHCP snooping")
def c_no_dhcps(s):
    s.m.dhcp_snooping = False


@CONF.add("ip igmp snooping", "IGMP snooping (actif par defaut)")
def c_igmp(s):
    s.m.igmp_snooping = True


@CONF.add("no ip igmp snooping", "Desactiver l'IGMP snooping (le multicast est diffuse partout)")
def c_no_igmp(s):
    s.m.igmp_snooping = False
    s.m.igmp_querier = False


@CONF.add("ip igmp snooping querier", "Le switch joue le querier (reseau sans routeur multicast)")
def c_igmp_q(s):
    s.m.igmp_snooping = True
    s.m.igmp_querier = True


@CONF.add("no ip igmp snooping querier", "")
def c_no_igmp_q(s):
    s.m.igmp_querier = False


@CONF.add("errdisable recovery interval <30-86400>", "Reactiver seul un port err-disabled apres N secondes")
def c_errrec(s, n):
    n = int(n)
    if not 30 <= n <= 86400:
        raise CliError("% 30 a 86400 secondes")
    s.m.errdisable_recovery = n


@CONF.add("no errdisable recovery interval", "Pas de reactivation automatique")
def c_no_errrec(s):
    s.m.errdisable_recovery = 0


# --- SPAN (port mirroring) ------------------------------------------------------

def _mon(s, n):
    n = int(n)
    if not 1 <= n <= 4:
        raise CliError("% Session 1-4")
    return n, s.m.monitor.setdefault(n, {"src": {}, "dst": None})


@CONF.add("monitor session <1-4> source interface <...>", "Port(s) a copier : ... [rx|tx|both]")
def c_mon_src(s, n, text):
    w = text.split()
    d = "both"
    if w and w[-1].lower() in ("rx", "tx", "both"):
        d = w.pop().lower()
    names = expand_if_range(" ".join(w), s.m.ports)
    n, sess = _mon(s, n)
    for x in names:
        if x not in s.m.ports or x.startswith("vlan"):
            raise CliError("% Source : un port ou un Port-channel")
        if x in s.m.span_dsts():
            raise CliError("%% %s est deja une destination SPAN" % disp_if(x))
        sess["src"][x] = d


@CONF.add("monitor session <1-4> destination interface <if>", "Port qui recoit la copie (sonde, Wireshark)")
def c_mon_dst(s, n, text):
    x = canon_if(text)
    if not x.startswith("ge") or x not in s.m.ports:
        raise CliError("% Destination : un port physique")
    if s.m.ports[x].channel_group is not None:
        raise CliError("% Destination : pas un membre d'agregat")
    n, sess = _mon(s, n)
    for k, o in s.m.monitor.items():
        if x in o["src"] or (k != n and o.get("dst") == x):
            raise CliError("%% %s est deja utilise par la session %d" % (disp_if(x), k))
    sess["dst"] = x


@CONF.add("no monitor session <1-4>", "Supprimer une session SPAN")
def c_no_mon(s, n):
    s.m.monitor.pop(int(n), None)


# --- sauvegarde automatique (archive) -----------------------------------------

def parse_scp_url(url):
    """scp://compte@hote[:port]/dossier -> (compte, hote, port, dossier)"""
    mo = re.fullmatch(r"scp://([A-Za-z0-9._-]{1,32})@(\[[0-9A-Fa-f:]+\]|[A-Za-z0-9.-]{1,253})(?::(\d{1,5}))?(/[A-Za-z0-9._/~-]*)?", url)
    if not mo:
        raise CliError("% Adresse : scp://compte@serveur[:port]/dossier")
    port = int(mo.group(3) or 22)
    if not 1 <= port <= 65535:
        raise CliError("% Port 1-65535")
    path = mo.group(4) or "/"
    if ".." in path.split("/"):
        raise CliError("% Chemin invalide")
    if path.startswith("/~/"):
        path = path[3:] or "."
    return mo.group(1), mo.group(2).strip("[]"), port, path


@CONF.add("archive path <url>", "Sauvegarde vers scp://compte@serveur/dossier (NAS)")
def c_arch_path(s, url):
    parse_scp_url(url)
    s.m.archive_path = url


@CONF.add("no archive path", "Sauvegardes locales uniquement")
def c_no_arch_path(s):
    s.m.archive_path = None


@CONF.add("archive write-memory", "Sauvegarder a chaque write memory")
def c_arch_wm(s):
    s.m.archive_write = True


@CONF.add("no archive write-memory", "")
def c_no_arch_wm(s):
    s.m.archive_write = False


@CONF.add("archive time-period <minutes>", "Sauvegarde periodique (si la config a change)")
def c_arch_tp(s, n):
    n = int(n)
    if not 10 <= n <= 525600:
        raise CliError("% 10 a 525600 minutes")
    s.m.archive_period = n


@CONF.add("no archive time-period", "")
def c_no_arch_tp(s):
    s.m.archive_period = 0


@CONF.add("archive maximum <1-100>", "Nombre de sauvegardes gardees sur le switch")
def c_arch_max(s, n):
    n = int(n)
    if not 1 <= n <= 100:
        raise CliError("% 1 a 100")
    s.m.archive_max = n


@CONF.add("no archive", "Plus aucune sauvegarde automatique")
def c_no_arch(s):
    s.m.archive_path, s.m.archive_write, s.m.archive_period, s.m.archive_max = None, False, 0, 10


# --- export Prometheus ------------------------------------------------------------

@CONF.add("prometheus exporter", "Export des compteurs pour Prometheus / Zabbix (port 9101)")
def c_prom(s):
    s.m.prom = True


@CONF.add("prometheus exporter port <port>", "Port d'ecoute de l'export")
def c_prom_port(s, port):
    port = int(port)
    if not 1024 <= port <= 65535 or port in (443, 8443):
        raise CliError("% Port 1024-65535")
    s.m.prom = True
    s.m.prom_port = port


@CONF.add("prometheus exporter allow <reseau>", "Autoriser seulement ce reseau (ex : 192.0.2.0/24)")
def c_prom_allow(s, net):
    try:
        n = str(ipaddress.ip_network(net, strict=False))
    except ValueError:
        raise CliError("% Reseau invalide")
    if n not in s.m.prom_allow:
        s.m.prom_allow.append(n)


@CONF.add("no prometheus exporter allow <reseau>", "")
def c_no_prom_allow(s, net):
    try:
        n = str(ipaddress.ip_network(net, strict=False))
    except ValueError:
        n = net
    if n in s.m.prom_allow:
        s.m.prom_allow.remove(n)


@CONF.add("no prometheus exporter", "Couper l'export")
def c_no_prom(s):
    s.m.prom = False


# --- sous-modes ----------------------------------------------------------------

def _sub_exit(s):
    s.mode, s.ctx = "config", None


def _sub_end(s):
    s.mode, s.ctx = "exec", None


for g in (CONF_IF, CONF_SVI, CONF_VLAN):
    g.add("exit", "Retour au mode config")(_sub_exit)
    g.add("end", "Retour au mode exec")(_sub_end)


def ports_of(s):
    return [s.m.ports[n] for n in s.ctx]


@CONF_IF.add("description <...>", "Description du port")
def ci_desc(s, d):
    for p in ports_of(s):
        p.description = d[:64]


@CONF_IF.add("no description", "Retirer la description")
def ci_no_desc(s):
    for p in ports_of(s):
        p.description = ""


@CONF_IF.add("shutdown", "Desactiver le port")
def ci_shut(s):
    for p in ports_of(s):
        p.shutdown = True


@CONF_IF.add("no shutdown", "Activer le port")
def ci_noshut(s):
    for p in ports_of(s):
        p.shutdown = False


@CONF_IF.add("switchport mode <access|trunk>", "Mode du port")
def ci_mode(s, mode):
    mode = mode.lower()
    if "access".startswith(mode):
        mode = "access"
    elif "trunk".startswith(mode):
        mode = "trunk"
    else:
        raise CliError("% access ou trunk")
    for p in ports_of(s):
        p.mode = mode


@CONF_IF.add("switchport access vlan <vid>", "VLAN du port en mode access")
def ci_access(s, vid):
    v = parse_vlan_list(vid)
    if len(v) != 1:
        raise CliError("% Un seul VLAN")
    v = v.pop()
    s.m.vlans.setdefault(v, "VLAN%04d" % v)
    for p in ports_of(s):
        p.access_vlan = v


@CONF_IF.add("no switchport access vlan", "Remettre le VLAN 1")
def ci_no_access(s):
    for p in ports_of(s):
        p.access_vlan = 1


@CONF_IF.add("switchport trunk allowed vlan <liste>", "VLAN autorises (liste, all ou none)")
def ci_allowed(s, lst):
    for p in ports_of(s):
        if lst.lower() == "all":
            p.allowed = None
        elif lst.lower() == "none":
            p.allowed = set()
        else:
            p.allowed = parse_vlan_list(lst)


@CONF_IF.add("switchport trunk allowed vlan add <liste>", "Ajouter des VLAN au trunk")
def ci_allowed_add(s, lst):
    for p in ports_of(s):
        if p.allowed is not None:
            p.allowed |= parse_vlan_list(lst)


@CONF_IF.add("switchport trunk allowed vlan remove <liste>", "Retirer des VLAN du trunk")
def ci_allowed_rm(s, lst):
    for p in ports_of(s):
        if p.allowed is None:
            p.allowed = set(s.m.vlans)
        p.allowed -= parse_vlan_list(lst)


@CONF_IF.add("switchport trunk native vlan <vid>", "VLAN natif (non tague)")
def ci_native(s, vid):
    v = parse_vlan_list(vid)
    if len(v) != 1:
        raise CliError("% Un seul VLAN")
    v = v.pop()
    s.m.vlans.setdefault(v, "VLAN%04d" % v)
    for p in ports_of(s):
        p.native = v


@CONF_IF.add("channel-group <n> mode <active|passive|on>", "Ajouter le port a un agregat (LACP)")
def ci_cg(s, n, mode):
    n = int(n)
    if not 1 <= n <= 64:
        raise CliError("% channel-group 1-64")
    mode = mode.lower()
    if mode not in ("active", "passive", "on"):
        raise CliError("% active, passive ou on")
    po = "po%d" % n
    for p in ports_of(s):
        if p.name.startswith("po"):
            raise CliError("% Un port-channel ne peut pas etre membre")
        p.channel_group, p.lacp_mode = n, mode
    if po not in s.m.ports:
        first = ports_of(s)[0]
        new = Port(po)
        # le port-channel herite de la config L2 du premier membre
        for attr in ("mode", "access_vlan", "allowed", "native", "description"):
            setattr(new, attr, getattr(first, attr))
        s.m.ports[po] = new


@CONF_IF.add("no channel-group", "Retirer le port de l'agregat")
def ci_no_cg(s):
    for p in ports_of(s):
        old = p.channel_group
        p.channel_group = None
        po = "po%s" % old
        if old is not None and not s.m.members(po):
            s.m.ports.pop(po, None)


@CONF_IF.add("switchport trunk encapsulation dot1q", "Accepte (toujours 802.1Q)")
def ci_encap(s):
    pass


@CONF_IF.add("switchport nonegotiate", "Accepte (pas de DTP)")
def ci_noneg(s):
    pass


@CONF_IF.add("spanning-tree portfast edge", "Port d'extremite")
def ci_pf_edge(s):
    ci_pf(s)


@CONF_IF.add("spanning-tree portfast", "Port d'extremite (passe en forwarding de suite)")
def ci_pf(s):
    for p in ports_of(s):
        p.portfast = True


@CONF_IF.add("no spanning-tree portfast", "")
def ci_nopf(s):
    for p in ports_of(s):
        p.portfast = False


@CONF_IF.add("spanning-tree bpduguard enable", "Bloque le port s'il recoit des BPDU")
def ci_bg(s):
    for p in ports_of(s):
        p.bpduguard = True


@CONF_IF.add("no spanning-tree bpduguard", "")
def ci_nobg(s):
    for p in ports_of(s):
        p.bpduguard = False


@CONF_IF.add("mtu <68-9216>", "MTU du port (jumbo jusqu'a 9216)")
def ci_mtu(s, mtu):
    mtu = int(mtu)
    if not 68 <= mtu <= 9216:
        raise CliError("% MTU 68-9216")
    for p in ports_of(s):
        p.mtu = mtu


@CONF_IF.add("no mtu", "MTU par defaut")
def ci_nomtu(s):
    for p in ports_of(s):
        p.mtu = None


def _l2_ports(s):
    ps = ports_of(s)
    for p in ps:
        if p.channel_group is not None:
            raise CliError("%% %s est membre d'un agregat : configure le Port-channel" % disp_if(p.name))
    return ps


@CONF_IF.add("switchport port-security", "Limiter les adresses MAC autorisees sur le port")
def ci_ps(s):
    for p in _l2_ports(s):
        p.ps = True


@CONF_IF.add("no switchport port-security", "")
def ci_no_ps(s):
    for p in ports_of(s):
        p.ps = False


@CONF_IF.add("switchport port-security maximum <1-1024>", "Nombre d'adresses MAC (1 par defaut)")
def ci_ps_max(s, n):
    n = int(n)
    if not 1 <= n <= 1024:
        raise CliError("% 1 a 1024")
    for p in _l2_ports(s):
        p.ps_max = n


@CONF_IF.add("switchport port-security mac-address <mac>", "Adresse MAC autorisee en permanence")
def ci_ps_mac(s, mac):
    m = re.sub(r"[^0-9a-fA-F]", "", mac).lower()
    if len(m) != 12 or int(m[:2], 16) & 1:
        raise CliError("% Adresse MAC invalide")
    mac = ":".join(m[i:i + 2] for i in range(0, 12, 2))
    for p in _l2_ports(s):
        if mac not in p.ps_macs:
            p.ps_macs.append(mac)
        p.ps_max = max(p.ps_max, len(p.ps_macs))


@CONF_IF.add("no switchport port-security mac-address <mac>", "")
def ci_no_ps_mac(s, mac):
    m = re.sub(r"[^0-9a-fA-F]", "", mac).lower()
    mac = ":".join(m[i:i + 2] for i in range(0, 12, 2))
    for p in ports_of(s):
        if mac in p.ps_macs:
            p.ps_macs.remove(mac)


@CONF_IF.add("switchport port-security violation <protect|restrict|shutdown>", "Que faire d'une MAC en trop")
def ci_ps_viol(s, v):
    full = [x for x in ("protect", "restrict", "shutdown") if x.startswith(v.lower())]
    if len(full) != 1:
        raise CliError("% protect, restrict ou shutdown")
    for p in _l2_ports(s):
        p.ps_violation = full[0]


@CONF_IF.add("switchport port-security aging time <1-1440>", "Oublier les MAC apprises apres N minutes")
def ci_ps_aging(s, n):
    n = int(n)
    if not 1 <= n <= 1440:
        raise CliError("% 1 a 1440 minutes")
    for p in _l2_ports(s):
        p.ps_aging = n


@CONF_IF.add("no switchport port-security aging time", "")
def ci_no_ps_aging(s):
    for p in ports_of(s):
        p.ps_aging = 0


@CONF_IF.add("ip dhcp snooping trust", "Port de confiance : serveur DHCP ou lien montant")
def ci_trust(s):
    for p in _l2_ports(s):
        p.dhcp_trust = True


@CONF_IF.add("no ip dhcp snooping trust", "")
def ci_no_trust(s):
    for p in ports_of(s):
        p.dhcp_trust = False


@CONF_IF.add("storm-control <broadcast|multicast> level pps <n>", "Limiter les tempetes (paquets/s, ex : 500 ou 1k)")
def ci_storm(s, kind, n):
    full = [x for x in ("broadcast", "multicast") if x.startswith(kind.lower())]
    if len(full) != 1:
        raise CliError("% broadcast ou multicast")
    mo = re.fullmatch(r"(\d+)([km]?)", n.lower())
    if not mo:
        raise CliError("% Nombre de paquets/s (ex : 500, 1k)")
    v = int(mo.group(1)) * {"": 1, "k": 1000, "m": 1000000}[mo.group(2)]
    if not 10 <= v <= 10000000:
        raise CliError("% 10 a 10m paquets/s")
    for p in _l2_ports(s):
        p.storm[full[0]] = v


@CONF_IF.add("no storm-control <broadcast|multicast>", "")
def ci_no_storm(s, kind):
    for p in ports_of(s):
        for k in ("broadcast", "multicast"):
            if k.startswith(kind.lower()):
                p.storm.pop(k, None)


@CONF_SVI.add("ip address <ip/len>", "Adresse IP (192.168.1.10/24)")
def cs_ip(s, a):
    a = parse_ip(a)
    svi = s.m.svis[s.ctx]
    svi.dhcp = False
    if a not in svi.addresses:
        svi.addresses.append(a)


@CONF_SVI.add("ip address <ip> <masque>", "Adresse IP et masque")
def cs_ip_mask(s, a, mask):
    if a.lower() == "dhcp":
        raise CliError("% Commande invalide")
    s_ = parse_ip(a, mask)
    cs_ip(s, s_)


@CONF_SVI.add("ipv6 address <ip/len>", "Adresse IPv6")
def cs_ip6(s, a):
    cs_ip(s, a)


@CONF_SVI.add("ip address dhcp", "Adresse obtenue par DHCP")
def cs_dhcp(s):
    svi = s.m.svis[s.ctx]
    svi.dhcp, svi.addresses = True, []


@CONF_SVI.add("no ip address", "Retirer les adresses")
def cs_noip(s):
    svi = s.m.svis[s.ctx]
    svi.dhcp, svi.addresses = False, []


@CONF_SVI.add("description <...>", "Description")
def cs_desc(s, d):
    s.m.svis[s.ctx].description = d[:64]


@CONF_SVI.add("shutdown", "Desactiver")
def cs_shut(s):
    s.m.svis[s.ctx].shutdown = True


@CONF_SVI.add("no shutdown", "Activer")
def cs_noshut(s):
    s.m.svis[s.ctx].shutdown = False


# ---------------------------------------------------------------------------
# Chargement
# ---------------------------------------------------------------------------

def load_config(path, ports=None, strict=True):
    m = Model(ports if ports is not None else discover_ports())
    if not os.path.exists(path):
        return m, []
    s = Session(m)
    errors = []
    with open(path) as f:
        for no, line in enumerate(f, 1):
            try:
                s.run_line(line, from_file=True)
                if s.mode == "exec":
                    s.mode = "config"
            except CliError as e:
                errors.append("%s:%d: %s -> %s" % (path, no, line.strip(), str(e).lstrip("% ")))
    if errors and strict:
        raise CliError("\n".join(errors))
    return m, errors


# ---------------------------------------------------------------------------
# Application sur le systeme (reconciliation, idempotent)
# ---------------------------------------------------------------------------

class Applier:
    def __init__(self, model, dry=False, out=sys.stdout):
        self.m = model
        self.dry = dry
        self.out = out
        self.cmds = []
        self.warn = []

    def run(self, cmd, ignore=False, daemon=False):
        """Lance une commande. daemon=True : programme qui passe en arriere-plan
        (dhclient -nw, udhcpc -b...) ; sa sortie n'est pas capturee, sinon le
        processus fils garde le tube ouvert et le demarrage reste bloque."""
        self.cmds.append(cmd)
        if self.dry:
            self.out.write("  " + cmd + "\n")
            return ""
        try:
            if daemon:
                r = subprocess.run(cmd, shell=True, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   timeout=30, text=True)
                r.stdout = r.stderr = ""
            else:
                r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=90)
        except subprocess.TimeoutExpired:
            self.warn.append("%s -> delai depasse" % cmd)
            return ""
        if r.returncode != 0 and not ignore:
            self.warn.append("%s -> %s" % (cmd, r.stderr.strip()))
        return r.stdout

    def links(self):
        if self.dry:
            return {}
        return {l["ifname"]: l for l in sh_json("ip -j -d link show")}

    def exists(self, name, links):
        return self.dry is False and name in links

    def apply(self):
        m = self.m
        links = self.links()
        sysd = shutil.which("systemctl") and not self.dry

        # 1. nom d'hote
        if not self.dry:
            try:
                cur = open("/etc/hostname").read().strip()
            except OSError:
                cur = ""
            if cur != m.hostname:
                self.run("hostnamectl set-hostname %s" % m.hostname, ignore=True)
                if cur != m.hostname:
                    try:
                        with open("/etc/hostname", "w") as f:
                            f.write(m.hostname + "\n")
                        hosts = open("/etc/hosts").read()
                        if cur and cur in hosts:
                            hosts = re.sub(r"\b%s\b" % re.escape(cur), m.hostname, hosts)
                        elif m.hostname not in hosts:
                            hosts += "127.0.1.1\t%s\n" % m.hostname
                        open("/etc/hosts", "w").write(hosts)
                    except OSError:
                        pass
        else:
            self.run("hostnamectl set-hostname %s" % m.hostname)

        # 2. bridge (filtrage VLAN materiel-like, pas de netfilter)
        if not self.exists(BRIDGE, links):
            self.run("ip link add %s type bridge vlan_filtering 1 vlan_default_pvid 0 mcast_snooping 1 "
                     "nf_call_iptables 0 nf_call_ip6tables 0 nf_call_arptables 0" % BRIDGE)
        self.run("ip link set %s type bridge vlan_filtering 1 vlan_default_pvid 0 priority %d"
                 % (BRIDGE, m.stp_priority))
        self.run("ip link set %s type bridge mcast_snooping %d mcast_querier %d"
                 % (BRIDGE, 1 if m.igmp_snooping else 0, 1 if m.igmp_querier else 0), ignore=True)
        # adresse MAC du switch = celle du 1er port (comme un vrai switch). Sans ca,
        # systemd donne au pont une MAC inventee que les hyperviseurs (ESXi :
        # "Forged transmits / MAC changes = Reject") jettent : plus de DHCP ni de ping.
        if not self.dry:
            first = list(m.physical)[:1]
            mac = ""
            if first and first[0] in links:
                mac = links[first[0]].get("permaddr") or links[first[0]].get("address", "")
            if mac and links.get(BRIDGE, {}).get("address") != mac:
                self.run("ip link set %s address %s" % (BRIDGE, mac), ignore=True)
        use_mstpd = m.stp_mode == "rstp" and (self.dry or shutil.which("mstpctl"))
        if m.stp_mode == "rstp" and not use_mstpd:
            # une seule fois par demarrage (pas a chaque commande) ; rappel dans show version
            marque = os.path.join(RUN, "mstpd-absent")
            if not os.path.exists(marque):
                self.warn.append("mstpd absent : RSTP remplace par STP classique (apt install mstpd)")
                try:
                    os.makedirs(RUN, exist_ok=True)
                    open(marque, "w").close()
                except OSError:
                    pass
        if use_mstpd:
            self.run("systemctl start --no-block mstpd", ignore=True)
        # avec mstpd, stp_state 1 passe la main au demon (via /sbin/bridge-stp)
        self.run("ip link set %s type bridge stp_state %d" % (BRIDGE, 0 if m.stp_mode == "none" else 1), ignore=True)
        if use_mstpd:
            self.run("mstpctl addbridge %s" % BRIDGE, ignore=True)
            self.run("mstpctl setforcevers %s rstp" % BRIDGE, ignore=True)
            self.run("mstpctl settreeprio %s 0 %d" % (BRIDGE, m.stp_priority // 4096), ignore=True)
        self.run("ip link set %s up" % BRIDGE)

        # 3. agregats (bond)
        wanted_po = set(m.channels)
        for name, l in links.items():
            if re.fullmatch(r"po\d+", name) and name not in wanted_po:
                self.run("ip link del %s" % name)
        for po in m.channels:
            members = m.members(po)
            modes = {m.ports[p].lacp_mode for p in members} or {"active"}
            bond_mode = "balance-xor" if modes == {"on"} else "802.3ad"
            if not self.exists(po, links):
                extra = " lacp_rate fast ad_select bandwidth" if bond_mode == "802.3ad" else ""
                if "passive" in modes and bond_mode == "802.3ad":
                    extra += " lacp_active off"
                self.run("ip link add %s type bond mode %s miimon 100 xmit_hash_policy layer3+4%s"
                         % (po, bond_mode, extra))
            cur_members = {n for n, l in links.items() if l.get("master") == po}
            for p in cur_members - set(members):
                self.run("ip link set %s nomaster" % p)
            for p in members:
                if links.get(p, {}).get("master") != po:
                    self.run("ip link set %s down" % p)
                    self.run("ip link set %s master %s" % (p, po))
                self.run("ip link set %s up" % p)

        # 4. ports du bridge
        bports = m.bridge_ports()
        errdis = errdisable_load() if not self.dry else {}
        for name, l in links.items():
            if l.get("master") == BRIDGE and name not in bports and not name.startswith("vlan"):
                self.run("ip link set %s nomaster" % name)
        for name in bports:
            p = m.ports[name]
            if links.get(name, {}).get("master") != BRIDGE:
                self.run("ip link set %s master %s" % (name, BRIDGE))
            if p.mtu:
                self.run("ip link set %s mtu %d" % (name, p.mtu))
                for mb in (m.members(name) if name.startswith("po") else []):
                    self.run("ip link set %s mtu %d" % (mb, p.mtu))
            self.run("ip link set %s alias %s" % (name, shlex.quote(p.description or name)), ignore=True)
            self.run("bridge link set dev %s guard %s" % (name, "on" if p.bpduguard else "off"), ignore=True)
            if m.stp_mode == "rstp" and shutil.which("mstpctl") and not self.dry:
                self.run("mstpctl setportadminedge %s %s %s" % (BRIDGE, name, "yes" if p.portfast else "no"), ignore=True)
                self.run("mstpctl setportbpduguard %s %s %s" % (BRIDGE, name, "yes" if p.bpduguard else "no"), ignore=True)
            elif self.dry and m.stp_mode == "rstp" and p.portfast:
                self.run("mstpctl setportadminedge %s %s yes" % (BRIDGE, name))
            self.run("ip link set %s %s" % (name, "down" if p.shutdown or name in errdis else "up"))
        for name in m.physical:
            p = m.ports[name]
            if p.channel_group is not None:
                self.run("ip link set %s alias %s" % (name, shlex.quote(p.description or name)), ignore=True)
                if p.shutdown:
                    self.run("ip link set %s down" % name)

        # 5. VLAN par port
        cur_vlans = {}
        if not self.dry:
            for e in sh_json("bridge -j vlan show"):
                vl = {}
                for v in e.get("vlans", []):
                    a = v["vlan"]
                    b = v.get("vlanEnd", a)
                    for vid in range(a, b + 1):
                        vl[vid] = ("Egress Untagged" in v.get("flags", []), "PVID" in v.get("flags", []))
                cur_vlans[e["ifname"]] = vl
        for name in bports:
            want, pvid = m.port_vlans(name)
            have = cur_vlans.get(name, {})
            for vid in sorted(set(have) - set(want)):
                self.run("bridge vlan del dev %s vid %d" % (name, vid))
            for vid in sorted(want):
                state = (want[vid], vid == pvid)
                if have.get(vid) != state:
                    flags = (" pvid" if vid == pvid else "") + (" untagged" if want[vid] else "")
                    if vid in have:
                        self.run("bridge vlan del dev %s vid %d" % (name, vid))
                    self.run("bridge vlan add dev %s vid %d%s" % (name, vid, flags))

        # 6. SVI (interfaces IP du switch)
        want_self = set(m.svis)
        have_self = set(cur_vlans.get(BRIDGE, {}))
        for vid in sorted(have_self - want_self):
            self.run("bridge vlan del dev %s vid %d self" % (BRIDGE, vid))
        for vid in sorted(want_self - have_self):
            self.run("bridge vlan add dev %s vid %d self" % (BRIDGE, vid))
        for name in list(links):
            mm = re.fullmatch(r"vlan(\d+)", name)
            if mm and int(mm.group(1)) not in m.svis:
                self.run("ip link del %s" % name)
        dhcp_ifaces = []
        for vid, svi in sorted(m.svis.items()):
            dev = "vlan%d" % vid
            if not self.exists(dev, links):
                self.run("ip link add link %s name %s type vlan id %d" % (BRIDGE, dev, vid))
            cur_addr = set()
            if not self.dry:
                for e in sh_json("ip -j addr show dev %s" % dev):
                    for a in e.get("addr_info", []):
                        if a.get("family") == "inet" or (a.get("family") == "inet6" and a.get("scope") == "global"):
                            if not a.get("dynamic"):
                                cur_addr.add("%s/%s" % (a["local"], a["prefixlen"]))
            want_addr = {str(ipaddress.ip_interface(a)) for a in svi.addresses}
            for a in cur_addr - want_addr:
                if not svi.dhcp:
                    self.run("ip addr del %s dev %s" % (a, dev))
            for a in want_addr - cur_addr:
                self.run("ip addr add %s dev %s" % (a, dev))
            self.run("ip link set %s alias %s" % (dev, shlex.quote(svi.description or dev)), ignore=True)
            self.run("ip link set %s %s" % (dev, "down" if svi.shutdown else "up"))
            if svi.dhcp and not svi.shutdown:
                dhcp_ifaces.append(dev)
        self.dhcp(dhcp_ifaces)

        # 7. passerelle et DNS
        if m.gateway:
            self.run("ip route replace default via %s" % m.gateway)
        elif not self.dry and not dhcp_ifaces:
            cur = sh("ip route show default").strip()
            if cur and "vlan" in cur:
                self.run("ip route del default", ignore=True)
        if m.dns and not self.dry:
            try:
                if os.path.islink("/etc/resolv.conf"):
                    os.unlink("/etc/resolv.conf")
                with open("/etc/resolv.conf", "w") as f:
                    f.write("# genere par TL-SwitchOS\n" + "".join("nameserver %s\n" % d for d in m.dns))
            except OSError as e:
                self.warn.append("resolv.conf : %s" % e)

        # 8. services (LLDP, SNMP, NTP)
        self.service("lldpd", m.lldp)
        if m.lldp:
            self.run("lldpcli configure system hostname %s" % m.hostname, ignore=True)
            self.run("lldpcli configure system interface pattern 'ge*,po*'", ignore=True)
        self.snmp()
        self.ntp()
        self.syslog()
        self.radius()
        self.users()
        # 9. securite des ports, DHCP snooping, storm control (nftables) et SPAN (tc)
        self.filters()
        self.span()
        self.metrics()
        if m.archive_path and not self.dry:
            try:
                archive_key()
            except (OSError, subprocess.SubprocessError) as e:
                self.warn.append("cle de sauvegarde : %s" % e)
        self.service("tlswitch-web", m.http)
        self.service("ssh", m.ssh)
        if not self.dry:
            try:
                hint = ""
                if "admin" in m.must_change:
                    hint = ("  Identifiant provisoire :  admin\n"
                            "  Mot de passe provisoire : admin\n"
                            "  (un nouveau mot de passe sera demande a la connexion)\n")
                acc = ""
                for vid in sorted(m.svis):
                    if m.svis[vid].shutdown:
                        continue
                    ip = "\\4{vlan%d}" % vid
                    acc += "  Vlan%-5d web : https://%s   ssh : ssh admin@%s\n" % (vid, ip, ip)
                txt = "\nTL-SwitchOS %s - %s - console \\l\n\n%s\n%s\n" % (VERSION, m.hostname, acc, hint)
                # modele lu par tlswitch-ecran : il y met les vraies adresses et rafraichit l'ecran
                write_atomic(os.path.join(ETC, "issue.modele"), txt)
                with open("/etc/issue", "w") as f:
                    f.write(txt)
                subprocess.run(["/usr/local/sbin/tlswitch-ecran", "--une-fois"], timeout=10,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except (OSError, subprocess.SubprocessError):
                pass
            try:
                with open(os.path.join(ETC, "motd"), "w") as f:
                    f.write(m.banner + ("\n" if m.banner else ""))
            except OSError:
                pass
        return self.warn

    def users(self):
        """Comptes Linux miroir de 'username' : shell = CLI, groupe tlswitch."""
        m = self.m
        if self.dry:
            for u, (priv, h) in sorted(m.users.items()):
                self.run("useradd/usermod %s (privilege %d, shell tlsh) ; %s" % (
                    u, priv, "mot de passe verifie par RADIUS" if h is None else "chpasswd -e"))
            return
        if not shutil.which("useradd"):
            return
        import grp
        import pwd
        try:
            members = set(grp.getgrnam("tlswitch").gr_mem)
        except KeyError:
            self.run("groupadd -f tlswitch", ignore=True)
            members = set()
        tlsh = shutil.which("tlsh") or "/usr/local/bin/tlsh"
        for u, (priv, h) in m.users.items():
            if compte_systeme(u):
                self.warn.append("compte %s ignore : c'est un compte du systeme" % u)
                continue
            try:
                pw = pwd.getpwnam(u)
                if pw.pw_shell != tlsh:
                    self.run("usermod -s %s %s" % (tlsh, u), ignore=True)
                if u not in members:
                    self.run("usermod -aG tlswitch %s" % u, ignore=True)
            except KeyError:
                self.run("useradd -m -s %s -G tlswitch %s" % (tlsh, u), ignore=True)
            try:
                import spwd  # absent en 3.13 : on lit /etc/shadow
                cur = spwd.getspnam(u).sp_pwdp
            except Exception:
                cur = ""
                try:
                    for line in open("/etc/shadow"):
                        f = line.split(":")
                        if f[0] == u:
                            cur = f[1]
                except OSError:
                    pass
            if h is None:
                # compte RADIUS : aucun mot de passe local ("*" n'est pas un compte verrouille)
                if cur != "*":
                    self.run("usermod -p '*' %s" % u, ignore=True)
            elif cur != h:
                subprocess.run(["chpasswd", "-e"], input="%s:%s\n" % (u, h), text=True)
                self.run("usermod -U %s" % u, ignore=True)
        for u in members - set(m.users):
            # compte retire de la config : verrouille, pas supprime (on garde ses fichiers)
            self.run("gpasswd -d %s tlswitch" % u, ignore=True)
            self.run("usermod -L -s /usr/sbin/nologin %s" % u, ignore=True)

    def service(self, name, on):
        if self.dry:
            self.run("systemctl %s %s" % ("enable --now" if on else "disable --now", name))
            return
        if not shutil.which("systemctl"):
            return
        active = subprocess.run(["systemctl", "is-active", "--quiet", name]).returncode == 0
        if on and not active:
            self.run("systemctl enable --now --no-block %s" % name, ignore=True)
        if not on and active:
            self.run("systemctl disable --now --no-block %s" % name, ignore=True)

    def dhcp(self, ifaces):
        """DHCP pour les SVI 'ip address dhcp' via dhclient (ou udhcpc)."""
        for dev in ifaces:
            # chemins autorises par le profil AppArmor de dhclient (Debian/Ubuntu)
            pidf = "/run/dhclient-%s.pid" % dev
            leases = "/var/lib/dhcp/dhclient-%s.leases" % dev
            if not self.dry and os.path.exists(pidf):
                try:
                    os.kill(int(open(pidf).read()), 0)
                    continue
                except (OSError, ValueError):
                    pass
            if shutil.which("dhclient") or self.dry:
                self.run("dhclient -4 -nw -pf %s -lf %s %s" % (pidf, leases, dev), ignore=True, daemon=True)
            elif shutil.which("udhcpc"):
                self.run("udhcpc -b -p %s -i %s" % (pidf, dev), ignore=True, daemon=True)
            elif shutil.which("dhcpcd"):
                self.run("dhcpcd -b -4 %s" % dev, ignore=True, daemon=True)
            else:
                self.warn.append("aucun client DHCP (apt install isc-dhcp-client)")

    def snmp(self):
        m = self.m
        conf = "/etc/snmp/snmpd.conf.d/tlswitch.conf"
        if m.snmp_community or m.snmp_users:
            L = ["# genere par TL-SwitchOS", "agentaddress udp:161,udp6:161",
                 "sysName %s" % m.hostname, "sysLocation %s" % (m.snmp_location or "-"),
                 "sysContact %s" % (m.snmp_contact or "-")]
            if m.snmp_community:
                L += ["rocommunity %s" % m.snmp_community, "rocommunity6 %s" % m.snmp_community]
            for u, (a, ka, p, kp) in sorted(m.snmp_users.items()):
                A = {"sha": "SHA", "sha256": "SHA-256", "sha512": "SHA-512"}[a]
                if p:
                    L.append('createUser %s %s "%s" %s "%s"' % (u, A, ka, {"aes": "AES", "aes256": "AES256"}[p], kp))
                    L.append("rouser %s priv" % u)
                else:
                    L.append('createUser %s %s "%s"' % (u, A, ka))
                    L.append("rouser %s auth" % u)
            content = "\n".join(L) + "\n"
            if self.dry:
                self.run("ecrire %s ; systemctl restart snmpd" % conf)
            elif os.path.isdir("/etc/snmp"):
                old = open(conf).read() if os.path.exists(conf) else ""
                if old != content:
                    self.run("systemctl stop snmpd", ignore=True)
                    snmp_forget_users()
                    write_atomic(conf, content)
                    self.run("systemctl enable snmpd", ignore=True)
                    self.run("systemctl start --no-block snmpd", ignore=True)
            else:
                self.warn.append("snmpd absent (apt install snmpd)")
        elif not self.dry and os.path.exists(conf):
            os.unlink(conf)
            self.run("systemctl disable --now --no-block snmpd", ignore=True)
            snmp_forget_users()

    def syslog(self):
        """Copie du journal vers des serveurs syslog (rsyslog, file d'attente si le serveur est absent)."""
        m = self.m
        conf = "/etc/rsyslog.d/60-tlswitch.conf"
        content = ""
        if m.log_hosts:
            lvl = RSYSLOG_LEVEL[m.log_level]
            content = "# genere par TL-SwitchOS\n"
            for i, (ip, proto, port) in enumerate(m.log_hosts):
                content += ('*.%s action(type="omfwd" target="%s" port="%d" protocol="%s" '
                            'queue.type="LinkedList" queue.size="10000" queue.filename="tlswitch%d" '
                            'queue.saveOnShutdown="on" action.resumeRetryCount="-1")\n'
                            % (lvl, ip, port, proto, i))
        if self.dry:
            if content:
                self.run("ecrire %s ; systemctl restart rsyslog" % conf)
            return
        old = open(conf).read() if os.path.exists(conf) else ""
        if old == content:
            return
        if content and not os.path.isdir("/etc/rsyslog.d"):
            self.warn.append("rsyslog absent (apt install rsyslog) : syslog distant inactif")
            return
        if content:
            write_atomic(conf, content, 0o644)
            self.run("systemctl enable rsyslog", ignore=True)
        elif os.path.exists(conf):
            os.unlink(conf)
        self.run("systemctl restart --no-block rsyslog", ignore=True)

    def radius(self):
        """SSH : PAM essaie le RADIUS puis le mot de passe local. Le web utilise radius_auth()."""
        m = self.m
        conf = "/etc/pam_radius_auth.conf"
        pam = "/etc/pam.d/sshd"
        on = bool(m.aaa_radius and m.radius)
        line = "auth sufficient pam_radius_auth.so  # tlswitch-radius"
        if self.dry:
            if on:
                self.run("ecrire %s ; %s : %s" % (conf, pam, line))
            return
        mod = [p for p in ("/lib/x86_64-linux-gnu/security/pam_radius_auth.so",
                           "/usr/lib/x86_64-linux-gnu/security/pam_radius_auth.so",
                           "/lib/security/pam_radius_auth.so") if os.path.exists(p)]
        if on and not mod:
            self.warn.append("libpam-radius-auth absent (apt install libpam-radius-auth) : "
                             "RADIUS seulement pour le web")
            on = False
        if on:
            txt = "# genere par TL-SwitchOS : serveur[:port] cle delai\n"
            for ip, port, key in m.radius:
                host = "[%s]" % ip if ":" in ip else ip
                txt += "%s:%d %s %d\n" % (host, port, key, m.radius_timeout)
            old = open(conf).read() if os.path.exists(conf) else ""
            if old != txt:
                write_atomic(conf, txt, 0o600)
        try:
            lines = open(pam).read().splitlines()
        except OSError:
            return
        has = any("tlswitch-radius" in l for l in lines)
        if on and not has:
            out = []
            done = False
            for l in lines:
                if not done and l.strip().startswith("@include common-auth"):
                    out.append(line)
                    done = True
                out.append(l)
            if not done:
                out.insert(0, line)
            write_atomic(pam, "\n".join(out) + "\n", 0o644)
        elif not on and has:
            write_atomic(pam, "\n".join(l for l in lines if "tlswitch-radius" not in l) + "\n", 0o644)

    def filters(self):
        """Une table nftables (famille bridge) : storm control, DHCP snooping, port security."""
        text = nft_script(self.m)
        state = os.path.join(RUN, "nft.regles")
        if self.dry:
            if text:
                self.run("nft -f - <<table bridge tlswitch (%d lignes)>>" % text.count("\n"))
            return
        if not shutil.which("nft"):
            if text:
                self.warn.append("nftables absent (apt install nftables) : port security, "
                                 "DHCP snooping et storm control inactifs")
            return
        present = subprocess.run(["nft", "list", "table", "bridge", "tlswitch"],
                                 capture_output=True).returncode == 0
        try:
            old = open(state).read()
        except OSError:
            old = None
        if present and old == text:
            return
        script = "table bridge tlswitch\ndelete table bridge tlswitch\n" + text if present else text
        if text:
            r = subprocess.run(["nft", "-f", "-"], input=script, capture_output=True, text=True)
            if r.returncode != 0:
                self.warn.append("nftables : %s" % r.stderr.strip()[-300:])
                return
        elif present:
            subprocess.run(["nft", "delete", "table", "bridge", "tlswitch"], capture_output=True)
        write_atomic(state, text, 0o644)
        try:
            os.unlink(PS_BASE)          # compteurs remis a zero avec la table
        except OSError:
            pass

    def span(self):
        """Port mirroring : copie du trafic des sources vers la destination (tc mirred)."""
        m = self.m
        want = []
        for n in sorted(m.monitor):
            sess = m.monitor[n]
            if sess.get("dst"):
                for src in sorted(sess["src"], key=natural_key):
                    want.append((src, sess["src"][src], sess["dst"]))
        key = repr(want)
        state = os.path.join(RUN, "span.etat")
        cmds = []
        for src, d, dst in want:
            cmds.append("tc qdisc replace dev %s clsact" % src)
            if d in ("rx", "both"):
                cmds.append("tc filter add dev %s ingress matchall skip_hw action mirred egress mirror dev %s" % (src, dst))
            if d in ("tx", "both"):
                cmds.append("tc filter add dev %s egress matchall skip_hw action mirred egress mirror dev %s" % (src, dst))
        for dst in sorted(m.span_dsts()):
            cmds.append("ip link set %s %s" % (dst, "down" if m.ports[dst].shutdown else "up"))
        if self.dry:
            for c in cmds:
                self.run(c)
            return
        try:
            old = open(state).read()
        except OSError:
            old = "[]"
        if old == key:
            return
        if old != "[]" or want:
            for name in m.physical + m.channels:
                self.run("tc qdisc del dev %s clsact" % name, ignore=True)
        if want and not shutil.which("tc"):
            self.warn.append("tc absent (iproute2) : SPAN inactif")
            return
        for c in cmds:
            self.run(c)
        write_atomic(state, key, 0o644)

    def metrics(self):
        m = self.m
        self.service("tlswitch-metrics", m.prom)
        if self.dry or not m.prom:
            return
        f = os.path.join(RUN, "metrics.port")
        try:
            old = open(f).read()
        except OSError:
            old = None
        if old is not None and old != str(m.prom_port):
            self.run("systemctl restart --no-block tlswitch-metrics", ignore=True)
        write_atomic(f, str(m.prom_port), 0o644)

    def ntp(self):
        conf = "/etc/systemd/timesyncd.conf.d/tlswitch.conf"
        content = "[Time]\nNTP=%s\n" % " ".join(self.m.ntp) if self.m.ntp else ""
        if self.dry:
            if content:
                self.run("ecrire %s ; systemctl restart systemd-timesyncd" % conf)
            return
        old = open(conf).read() if os.path.exists(conf) else ""
        if old == content:
            return
        if content:
            os.makedirs(os.path.dirname(conf), exist_ok=True)
            open(conf, "w").write(content)
        elif os.path.exists(conf):
            os.unlink(conf)
        self.run("systemctl restart --no-block systemd-timesyncd", ignore=True)


class Lock:
    def __enter__(self):
        os.makedirs(RUN, exist_ok=True)
        self.f = open(LOCK, "w")
        fcntl.flock(self.f, fcntl.LOCK_EX)
        return self

    def __exit__(self, *a):
        fcntl.flock(self.f, fcntl.LOCK_UN)
        self.f.close()


def write_atomic(path, text, mode=0o600):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def log(msg):
    subprocess.run(["logger", "-t", "tlswitch", msg], capture_output=True)


# ---------------------------------------------------------------------------
# Securite des ports (nftables), err-disable, SNMPv3, RADIUS, sauvegardes
# ---------------------------------------------------------------------------

ERRDIS = os.path.join(RUN, "errdisable.json")
PS_BASE = os.path.join(RUN, "ps-base.json")


def errdisable_load():
    try:
        return json.load(open(ERRDIS))
    except (OSError, ValueError):
        return {}


def errdisable_set(name, reason):
    d = errdisable_load()
    d[name] = {"raison": reason, "depuis": time.time()}
    write_atomic(ERRDIS, json.dumps(d), 0o644)


def errdisable_clear(name=None):
    d = errdisable_load()
    for n in ([name] if name else list(d)):
        d.pop(n, None)
    write_atomic(ERRDIS, json.dumps(d), 0o644)


def _nft_name(prefix, port):
    return "%s_%s" % (prefix, port)


def nft_script(m):
    """Table nftables du switch, ou "" si aucune fonction n'est utilisee.
    Ordre par port : storm control, DHCP snooping, port security (accepte ou jette)."""
    counters, sets, rules = [], [], []
    bports = m.bridge_ports()
    snoop_vids = set(m.dhcp_snooping_vlans) if m.dhcp_snooping_vlans else set(m.vlans)
    for name in sorted(bports, key=natural_key):
        p = m.ports[name]
        iif = 'iifname "%s"' % name
        for kind in ("broadcast", "multicast"):
            if kind in p.storm:
                c = _nft_name("storm" + kind[0], name)
                counters.append(c)
                rules.append('%s meta pkttype %s limit rate over %d/second burst %d packets counter name "%s" drop'
                             % (iif, kind, p.storm[kind], p.storm[kind], c))
        if m.dhcp_snooping and not p.dhcp_trust and set(m.port_vlans(name)[0]) & snoop_vids:
            c = _nft_name("dhcp", name)
            counters.append(c)
            # reponses de serveur DHCP (offer, ack) venant d'un port non fiable : jetees
            rules.append('%s udp sport 67 udp dport 68 counter name "%s" drop' % (iif, c))
            rules.append('%s ip6 nexthdr udp udp sport 547 counter name "%s" drop' % (iif, c))
        if p.ps:
            st = _nft_name("ps", name)
            counters.append(st)
            size = max(p.ps_max, len(p.ps_macs), 1)
            flags = "dynamic, timeout" if p.ps_aging else "dynamic"
            body = "type ether_addr; size %d; flags %s;" % (size, flags)
            if p.ps_macs:
                body += " elements = { %s };" % ", ".join(p.ps_macs)
            sets.append("  set %s { %s }" % (st, body))
            rules.append('%s ether saddr @%s accept' % (iif, st))
            if len(p.ps_macs) < size:
                # aging : seules les MAC apprises expirent, les statiques restent
                rules.append('%s add @%s { ether saddr%s } accept'
                             % (iif, st, " timeout %dm" % p.ps_aging if p.ps_aging else ""))
            rules.append('%s counter name "%s" drop' % (iif, st))
    if not rules:
        return ""
    L = ["table bridge tlswitch {"]
    L += ["  counter %s { }" % c for c in sorted(set(counters))]
    L += sets
    L += ["  chain entree {", "    type filter hook prerouting priority -200; policy accept;"]
    L += ["    " + r for r in rules]
    L += ["  }", "}"]
    return "\n".join(L) + "\n"


def nft_counters():
    out = {}
    try:
        data = json.loads(sh("nft -j list counters table bridge tlswitch") or "{}")
    except ValueError:
        return out
    for e in data.get("nftables", []):
        c = e.get("counter")
        if c and c.get("table") == "tlswitch":
            out[c["name"]] = c.get("packets", 0)
    return out


def ps_violations(m):
    """Appele par le guard : compare les compteurs de rejets port security a la derniere lecture."""
    cnt = nft_counters()
    try:
        base = json.load(open(PS_BASE))
    except (OSError, ValueError):
        base = {}
    errdis = errdisable_load()
    for name in m.bridge_ports():
        p = m.ports[name]
        c = _nft_name("ps", name)
        if not p.ps or c not in cnt:
            continue
        old = base.get(c, cnt[c] if c not in base else 0)
        if cnt[c] > old and name not in errdis:
            n = cnt[c] - old
            if p.ps_violation == "shutdown":
                errdisable_set(name, "psecure-violation")
                sh(["ip", "link", "set", name, "down"])
                log("%%PORT_SECURITY-2-PSECURE_VIOLATION: %s err-disabled (%d trames d'une MAC non autorisee)"
                    % (disp_if(name), n))
            elif p.ps_violation == "restrict":
                log("%%PORT_SECURITY-2-PSECURE_VIOLATION: %s : %d trames d'une MAC non autorisee jetees"
                    % (disp_if(name), n))
        base[c] = cnt[c]
    write_atomic(PS_BASE, json.dumps(base), 0o644)


def errdisable_recover(m):
    if not m.errdisable_recovery:
        return
    for name, e in errdisable_load().items():
        if time.time() - e.get("depuis", 0) >= m.errdisable_recovery and name in m.ports:
            errdisable_clear(name)
            if not m.ports[name].shutdown:
                sh(["ip", "link", "set", name, "up"])
            log("errdisable : %s reactive (recovery interval)" % disp_if(name))


def snmp_forget_users():
    """snmpd recopie les comptes v3 dans /var/lib/snmp/snmpd.conf : on les oublie pour
    qu'un utilisateur retire de la config ne reste pas valable."""
    f = "/var/lib/snmp/snmpd.conf"
    try:
        lines = open(f).read().splitlines(True)
    except OSError:
        return
    keep = [l for l in lines if not l.startswith("usmUser")]
    if keep != lines:
        write_atomic(f, "".join(keep), 0o600)


def radius_auth(m, user, password):
    """Client RADIUS (PAP + Message-Authenticator). True accepte, False refuse,
    None aucun serveur n'a repondu."""
    import hashlib
    import hmac
    import socket
    if not m.radius or not password:
        return None if not m.radius else False
    pw = password.encode()[:128]
    pw += b"\0" * (-len(pw) % 16)
    for ip, port, key in m.radius:
        secret = key.encode()
        ident = os.urandom(1)[0]
        ra = os.urandom(16)
        enc, prev = b"", ra
        for i in range(0, len(pw), 16):
            b = hashlib.md5(secret + prev).digest()
            prev = bytes(x ^ y for x, y in zip(pw[i:i + 16], b))
            enc += prev

        def attr(t, v):
            return bytes([t, len(v) + 2]) + v
        attrs = (attr(1, user.encode()[:64]) + attr(2, enc) + attr(32, m.hostname.encode())
                 + attr(6, (1).to_bytes(4, "big"))
                 + attr(80, b"\0" * 16))
        length = 20 + len(attrs)
        head = bytes([1, ident]) + length.to_bytes(2, "big")
        ma = hmac.new(secret, head + ra + attrs, hashlib.md5).digest()
        attrs = attrs[:-16] + ma
        pkt = head + ra + attrs
        fam = socket.AF_INET6 if ":" in ip else socket.AF_INET
        so = socket.socket(fam, socket.SOCK_DGRAM)
        so.settimeout(m.radius_timeout)
        try:
            for _ in range(2):
                so.sendto(pkt, (ip, port))
                try:
                    data, _a = so.recvfrom(4096)
                except socket.timeout:
                    continue
                if len(data) < 20 or data[1] != ident:
                    continue
                code = data[0]
                rlen = int.from_bytes(data[2:4], "big")
                expect = hashlib.md5(data[:4] + ra + data[20:rlen] + secret).digest()
                if not hmac.compare_digest(expect, data[4:20]):
                    log("radius : reponse de %s avec une mauvaise signature (cle ?)" % ip)
                    break
                if code == 2:
                    return True
                if code == 3:
                    return False
            # pas de reponse : serveur suivant
        except OSError as e:
            log("radius : %s injoignable (%s)" % (ip, e))
        finally:
            so.close()
    return None


def login_ok(m, user, password):
    """Verification commune (web) : RADIUS si 'aaa ... group radius local', puis compte local."""
    u = m.users.get(user)
    if not u:
        return False
    # compte local d'abord : pas d'attente si le RADIUS est en panne, et le mot de passe
    # local ne part jamais vers le serveur RADIUS
    if u[1] is not None and check_secret(password, u[1]):
        return True
    if m.aaa_radius and m.radius:
        return radius_auth(m, user, password) is True
    return False


ARCH_DIR = os.path.join(ETC, "archive")
ARCH_KEY = os.path.join(ETC, "archive-cle")
ARCH_KNOWN = os.path.join(ETC, "archive-hotes-connus")
ARCH_STATE = os.path.join(ARCH_DIR, "etat.json")


def archive_key():
    """Cle SSH du switch pour deposer les sauvegardes (a autoriser sur le NAS)."""
    if not os.path.exists(ARCH_KEY):
        os.makedirs(ETC, exist_ok=True)
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "tlswitch-sauvegarde",
                        "-f", ARCH_KEY], check=True, capture_output=True)
    try:
        return open(ARCH_KEY + ".pub").read().strip()
    except OSError:
        return ""


def archive_list():
    try:
        return sorted(f for f in os.listdir(ARCH_DIR) if f.endswith(".conf"))
    except OSError:
        return []


def archive_state():
    try:
        return json.load(open(ARCH_STATE))
    except (OSError, ValueError):
        return {}


def archive_now(m, motif="manuelle"):
    """Copie horodatee de la running-config : sur le switch (archive maximum) et, si
    'archive path', sur le serveur en SCP. Renvoie (ok, message)."""
    try:
        text = open(RUNNING).read()
    except OSError:
        text = m.render()
    st = archive_state()
    st["essai"] = time.time()
    hist = archive_list()
    if motif == "periodique" and hist:
        try:
            if open(os.path.join(ARCH_DIR, hist[-1])).read() == text:
                write_atomic(ARCH_STATE, json.dumps(st), 0o600)
                return True, "configuration inchangee depuis %s" % hist[-1]
        except OSError:
            pass
    name = "%s-%s.conf" % (m.hostname, time.strftime("%Y%m%d-%H%M%S"))
    write_atomic(os.path.join(ARCH_DIR, name), text, 0o600)
    for old in archive_list()[:-m.archive_max]:
        try:
            os.unlink(os.path.join(ARCH_DIR, old))
        except OSError:
            pass
    ok, msg = True, "sauvegarde locale %s" % name
    if m.archive_path:
        try:
            user, host, port, path = parse_scp_url(m.archive_path)
            archive_key()
            dest = "%s@%s:%s" % (user, "[%s]" % host if ":" in host else host,
                                 (path.rstrip("/") or "") + "/" + name if path != "." else name)
            r = subprocess.run(["scp", "-q", "-i", ARCH_KEY, "-P", str(port), "-o", "BatchMode=yes",
                                "-o", "StrictHostKeyChecking=accept-new", "-o", "UserKnownHostsFile=" + ARCH_KNOWN,
                                "-o", "ConnectTimeout=10", os.path.join(ARCH_DIR, name), dest],
                               capture_output=True, text=True, timeout=120)
            if r.returncode == 0:
                msg = "%s copie sur %s" % (name, host)
            else:
                ok, msg = False, "echec SCP vers %s : %s" % (host, (r.stderr.strip() or "code %d" % r.returncode)[-200:])
        except (CliError, OSError, subprocess.SubprocessError) as e:
            ok, msg = False, "echec SCP : %s" % e
    st.update({"date": time.time(), "motif": motif, "fichier": name, "ok": ok, "message": msg})
    write_atomic(ARCH_STATE, json.dumps(st), 0o600)
    log("archive (%s) : %s" % (motif, msg))
    return ok, msg


def archive_due(m):
    if not m.archive_period:
        return False
    return time.time() - archive_state().get("essai", 0) >= m.archive_period * 60


def archive_detached(motif):
    if os.environ.get("TLSWITCH_DRY") or os.environ.get("TLSWITCH_NO_DAEMON"):
        return
    try:
        subprocess.Popen([sys.executable, os.path.abspath(__file__), "archive", "--motif", motif],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Export Prometheus (texte, http://switch:9101/metrics)
# ---------------------------------------------------------------------------

STP_NUM = {"disabled": 0, "listening": 1, "learning": 2, "forwarding": 3, "blocking": 4, "discarding": 4}


def _lbl(v):
    return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def metrics_text(m):
    links = link_table()
    stp = stp_states()
    errdis = errdisable_load()
    cnt = nft_counters() if shutil.which("nft") else {}
    out = []

    def fam(name, typ, hlp):
        out.append("# HELP %s %s" % (name, hlp))
        out.append("# TYPE %s %s" % (name, typ))

    fam("tlswitch_info", "gauge", "Version et nom du switch")
    out.append('tlswitch_info{version="%s",hostname="%s"} 1' % (VERSION, _lbl(m.hostname)))
    try:
        up = float(open("/proc/uptime").read().split()[0])
    except (OSError, ValueError):
        up = 0
    fam("tlswitch_uptime_seconds", "gauge", "Temps depuis le demarrage")
    out.append("tlswitch_uptime_seconds %d" % up)
    ports = m.physical + m.channels
    rows = []
    for name in ports:
        p = m.ports[name]
        l = links.get(name, {})
        st = l.get("stats64", {})
        lab = 'port="%s",ifname="%s",description="%s"' % (disp_if(name), name, _lbl(p.description))
        rows.append((name, p, l, st, lab))
    fam("tlswitch_port_up", "gauge", "1 si le lien est connecte")
    for name, p, l, st, lab in rows:
        out.append("tlswitch_port_up{%s} %d" % (lab, 1 if port_status(m, name, links) == "connected" else 0))
    fam("tlswitch_port_admin_up", "gauge", "0 si le port est en shutdown ou err-disabled")
    for name, p, l, st, lab in rows:
        out.append("tlswitch_port_admin_up{%s} %d" % (lab, 0 if p.shutdown or name in errdis else 1))
    fam("tlswitch_port_speed_bits", "gauge", "Vitesse negociee en bit/s")
    for name, p, l, st, lab in rows:
        try:
            v = int(open("/sys/class/net/%s/speed" % name).read())
        except (OSError, ValueError):
            v = 0
        out.append("tlswitch_port_speed_bits{%s} %d" % (lab, max(v, 0) * 1000000))
    for d in ("rx", "tx"):
        for k, typ in (("bytes", "octets"), ("packets", "paquets"), ("errors", "erreurs"),
                       ("dropped", "paquets jetes"), ("multicast", "paquets multicast")):
            if d == "tx" and k == "multicast":
                continue
            n = "tlswitch_port_%s_%s_total" % (d, k)
            fam(n, "counter", "%s %s" % (typ.capitalize(), "recus" if d == "rx" else "emis"))
            for name, p, l, st, lab in rows:
                out.append("%s{%s} %d" % (n, lab, st.get(d, {}).get(k, 0)))
    fam("tlswitch_port_stp_state", "gauge", "Etat STP : 0 disabled, 1 listening, 2 learning, 3 forwarding, 4 blocking")
    for name, p, l, st, lab in rows:
        if name in stp:
            out.append("tlswitch_port_stp_state{%s} %d" % (lab, STP_NUM.get(stp[name], 0)))
    try:
        tc = int(open("/sys/class/net/%s/bridge/topology_change" % BRIDGE).read())
    except (OSError, ValueError):
        tc = 0
    fam("tlswitch_stp_topology_change", "gauge", "1 pendant un changement de topologie STP")
    out.append("tlswitch_stp_topology_change %d" % tc)
    root = sh("cat /sys/class/net/%s/bridge/root_id 2>/dev/null" % BRIDGE).strip()
    me = sh("cat /sys/class/net/%s/bridge/bridge_id 2>/dev/null" % BRIDGE).strip()
    fam("tlswitch_stp_is_root", "gauge", "1 si ce switch est la racine STP")
    out.append("tlswitch_stp_is_root %d" % (1 if root and root == me else 0))
    fam("tlswitch_port_err_disabled", "gauge", "1 si le port est err-disabled")
    for name, p, l, st, lab in rows:
        out.append("tlswitch_port_err_disabled{%s} %d" % (lab, 1 if name in errdis else 0))
    for pre, n, hlp in (("ps", "tlswitch_port_security_drops_total", "Trames jetees par le port security"),
                        ("dhcp", "tlswitch_dhcp_snooping_drops_total", "Reponses DHCP jetees (port non fiable)"),
                        ("stormb", "tlswitch_storm_broadcast_drops_total", "Broadcast jete par le storm control"),
                        ("stormm", "tlswitch_storm_multicast_drops_total", "Multicast jete par le storm control")):
        vals = [(lab, cnt[_nft_name(pre, name)]) for name, p, l, st, lab in rows if _nft_name(pre, name) in cnt]
        if vals:
            fam(n, "counter", hlp)
            out += ["%s{%s} %d" % (n, lab, v) for lab, v in vals]
    macs = {}
    for e in sh_json("bridge -j fdb show br %s" % BRIDGE):
        if "self" in e.get("flags", []) or e.get("state") == "permanent" or not e.get("ifname"):
            continue
        macs[e["ifname"]] = macs.get(e["ifname"], 0) + 1
    fam("tlswitch_mac_entries", "gauge", "Adresses MAC apprises par port")
    for name, p, l, st, lab in rows:
        out.append("tlswitch_mac_entries{%s} %d" % (lab, macs.get(name, 0)))
    fam("tlswitch_vlans", "gauge", "Nombre de VLAN definis")
    out.append("tlswitch_vlans %d" % len(m.vlans))
    return "\n".join(out) + "\n"


def metrics_serve():
    import http.server
    import socket
    import socketserver
    m, _ = load_running()
    port = m.prom_port

    class H(http.server.BaseHTTPRequestHandler):
        server_version = "tlswitch-metrics"
        sys_version = ""

        def log_message(self, *a):
            pass

        def do_GET(self):
            mm, _ = load_running()
            ip = self.client_address[0]
            if ip.startswith("::ffff:"):
                ip = ip[7:]
            try:
                a = ipaddress.ip_address(ip)
                allowed = not mm.prom_allow or any(a in ipaddress.ip_network(n) for n in mm.prom_allow)
            except ValueError:
                allowed = False
            if not mm.prom or not allowed:
                self.send_error(403)
                return
            if self.path.split("?")[0] not in ("/metrics", "/"):
                self.send_error(404)
                return
            body = metrics_text(mm).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    class S(socketserver.ThreadingMixIn, http.server.HTTPServer):
        daemon_threads = True
        address_family = socket.AF_INET6

        def server_bind(self):
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
            super().server_bind()

    class S4(socketserver.ThreadingMixIn, http.server.HTTPServer):
        daemon_threads = True

    try:
        srv = S(("::", port), H)
    except OSError:                     # IPv6 desactive sur le switch
        srv = S4(("0.0.0.0", port), H)
    srv.serve_forever()



def commit(model, dry=False, out=sys.stdout):
    """Applique un modele et enregistre la running-config."""
    if os.environ.get("TLSWITCH_DRY"):           # tests : rien n'est touche sur le systeme
        dry, out = True, open(os.devnull, "w")
    with Lock():
        warn = Applier(model, dry=dry, out=out).apply()
        if not dry or os.environ.get("TLSWITCH_DRY"):
            write_atomic(RUNNING, model.render())
    return warn


def load_running():
    path = RUNNING if os.path.exists(RUNNING) else STARTUP
    return load_config(path, strict=False)


def unsaved():
    try:
        run = open(RUNNING).read()
    except OSError:
        return False
    try:
        return run != open(STARTUP).read()
    except OSError:
        return True


# ---------------------------------------------------------------------------
# Fiabilite : retour arriere automatique (configure ... revert timer)
# ---------------------------------------------------------------------------

RB_CONF = os.path.join(RUN, "rollback.conf")
RB_DEADLINE = os.path.join(RUN, "rollback.deadline")
RB_PID = os.path.join(RUN, "rollback.pid")


def mgmt_fingerprint(m):
    """Tout ce qui peut couper l'acces au switch (IP, VLAN des ports, agregats, SSH, web)."""
    parts = [repr(sorted((v, s.addresses, s.dhcp, s.shutdown) for v, s in m.svis.items())),
             str(m.gateway), str(m.ssh), str(m.http), str(m.stp_mode), repr(sorted(m.users)),
             str(m.dhcp_snooping and any(s.dhcp for s in m.svis.values()))]
    mgmt_vids = set(m.svis)
    for name in sorted(m.ports, key=natural_key):
        p = m.ports[name]
        vl = ""
        if name in m.bridge_ports():
            want, pvid = m.port_vlans(name)
            vl = sorted((v, u) for v, u in want.items() if v in mgmt_vids), pvid in mgmt_vids and pvid
        parts.append("%s:%s:%s:%s:%s:%s:%s:%s:%s" % (name, p.shutdown, p.channel_group, p.lacp_mode, p.mtu, vl,
                                                 vl and (p.ps, p.ps_max, sorted(p.ps_macs), p.dhcp_trust),
                                                 name in m.span_dsts(), vl and sorted(p.storm.items())))
    return "|".join(parts)


def rollback_pending():
    try:
        return max(0, int(float(open(RB_DEADLINE).read()) - time.time()))
    except (OSError, ValueError):
        return None


def rollback_arm(snapshot_text, seconds):
    """Arme (ou prolonge) le retour arriere. Garde la photo la plus ancienne."""
    os.makedirs(RUN, exist_ok=True)
    if not os.path.exists(RB_CONF):
        write_atomic(RB_CONF, snapshot_text)
    write_atomic(RB_DEADLINE, str(time.time() + seconds))
    try:
        os.kill(int(open(RB_PID).read()), 0)
        return                          # le gardien tourne deja
    except (OSError, ValueError):
        pass
    if os.environ.get("TLSWITCH_NO_DAEMON"):
        return
    # double fork : le gardien survit a la coupure de la session SSH ou web
    if os.fork() == 0:
        os.setsid()
        if os.fork() != 0:
            os._exit(0)
        devnull = os.open(os.devnull, os.O_RDWR)
        for fd in (0, 1, 2):
            os.dup2(devnull, fd)
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        with open(RB_PID, "w") as f:
            f.write(str(os.getpid()))
        try:
            while True:
                left = rollback_pending()
                if left is None or not os.path.exists(RB_CONF):
                    break
                if left <= 0:
                    rollback_now("delai expire sans confirmation")
                    break
                time.sleep(1)
        finally:
            try:
                os.unlink(RB_PID)
            except OSError:
                pass
            os._exit(0)
    else:
        os.wait()


def rollback_now(reason):
    try:
        text = open(RB_CONF).read()
    except OSError:
        return False
    path = RB_CONF + ".apply"
    write_atomic(path, text)
    m, _ = load_config(path, strict=False)
    commit(m, out=open(os.devnull, "w"))
    rollback_cancel()
    log("RETOUR ARRIERE applique : %s" % reason)
    return True


def rollback_cancel():
    for f in (RB_CONF, RB_DEADLINE):
        try:
            os.unlink(f)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Changement de mot de passe (premiere connexion, CLI et web)
# ---------------------------------------------------------------------------

def password_problem(user, new, model):
    """Renvoie un message d'erreur ou None si le mot de passe est acceptable."""
    if len(new) < 8:
        return "8 caracteres minimum"
    if any(c.isspace() for c in new):
        return "pas d'espace"
    if new.lower() in ("admin", "password", "motdepasse", "tlswitch", user.lower()):
        return "trop facile a deviner"
    if user in model.users and check_secret(new, model.users[user][1]):
        return "identique a l'ancien"
    return None


def change_password(user, new):
    """Change le mot de passe, leve l'obligation, applique et ENREGISTRE (survit au redemarrage)."""
    with Lock():
        m, _ = load_running()
    if user not in m.users:
        raise CliError("% Compte inconnu")
    if m.users[user][1] is None:
        raise CliError("% Compte RADIUS : le mot de passe se change dans l'annuaire")
    err = password_problem(user, new, m)
    if err:
        raise CliError("% Mot de passe refuse : " + err)
    old = m.users[user][1]
    h = hash_secret(new)
    m.users[user][1] = h
    if user in m.must_change and m.enable_secret == old:
        m.enable_secret = h          # l'enable secret provisoire suit le nouveau mot de passe
    m.must_change.discard(user)
    commit(m)
    if os.path.exists(STARTUP):
        shutil.copy2(STARTUP, STARTUP + ".bak")
    write_atomic(STARTUP, m.render())
    log("mot de passe change pour %s" % user)


# ---------------------------------------------------------------------------
# Execution d'une ligne (CLI et web) avec application et protection
# ---------------------------------------------------------------------------

def execute(s, line, remote=True, auto_revert=300):
    """Execute une ligne dans la session s. Applique si la config change.
    Si la modification touche l'acces de gestion depuis une session distante,
    arme un retour arriere (auto_revert secondes) a confirmer par 'configure confirm'."""
    with Lock():
        fresh, _ = load_running()          # recharge : d'autres sessions ont pu modifier
    s.m = fresh
    s.armed = False
    before = s.m.render()
    fp_before = mgmt_fingerprint(s.m)
    s.run_line(line)
    after = s.m.render()
    if after == before:
        return False
    if s.priv < 15:
        raise CliError("% Modification refusee : niveau 15 requis")
    armed = False
    if remote and auto_revert and mgmt_fingerprint(s.m) != fp_before:
        rollback_arm(before, auto_revert)
        armed = True
    for w in commit(s.m):
        s.say("%% Attention : %s" % w)
    s.armed = armed
    if armed:
        s.say("%% Modification de l'acces de gestion : retour arriere automatique dans %d s."
              % rollback_pending())
        s.say("%% Si tu as toujours la main, tape 'configure confirm' (ou 'do configure confirm').")
    return True


# ---------------------------------------------------------------------------
# Commandes show et exec
# ---------------------------------------------------------------------------

def link_table():
    return {l["ifname"]: l for l in sh_json("ip -j -s -d link show")}


def speed_of(name):
    try:
        v = int(open("/sys/class/net/%s/speed" % name).read())
        if v <= 0:
            return "auto"
        return "%dG" % (v // 1000) if v >= 1000 else "%dM" % v
    except (OSError, ValueError):
        return "auto"


def duplex_of(name):
    try:
        return open("/sys/class/net/%s/duplex" % name).read().strip() or "auto"
    except OSError:
        return "auto"


def port_status(m, name, links):
    l = links.get(name)
    p = m.ports.get(name)
    if p and p.shutdown:
        return "disabled"
    if name in errdisable_load():
        return "err-disabled"
    if not l:
        return "absent"
    if "LOWER_UP" in l.get("flags", []) and l.get("operstate") in ("UP", "UNKNOWN"):
        return "connected"
    return "notconnect"


def stp_states():
    return {e["ifname"]: e.get("state", "") for e in sh_json("bridge -j link show")}


def port_vlan_label(m, name):
    p = m.ports[name]
    if name in m.span_dsts():
        return "span"
    if p.channel_group is not None:
        return "Po%d" % p.channel_group
    return str(p.access_vlan) if p.mode == "access" else "trunk"


@EXEC.add("show running-config", "Configuration active")
@priv15
def x_run(s):
    s.say(s.m.render().rstrip())


@EXEC.add("show running-config interface <...>", "Configuration d'une interface")
def x_run_if(s, text):
    want = {disp_if(n, True) for n in expand_if_range(text, s.m.ports)}
    show = False
    for line in s.m.render().splitlines():
        if line.startswith("interface "):
            show = line[10:] in want
        elif not line.startswith(" "):
            if show:
                s.say("!")
            show = False
        if show:
            s.say(line)


@EXEC.add("show startup-config", "Configuration au demarrage")
@priv15
def x_start(s):
    if os.path.exists(STARTUP):
        s.say(open(STARTUP).read().rstrip())
    else:
        s.say("% Pas de startup-config")


@EXEC.add("show interfaces status", "Etat des ports")
def x_ifstatus(s):
    m = s.m
    links = link_table()
    s.say("%-9s %-20s %-11s %-6s %-6s %-6s %s" % ("Port", "Name", "Status", "Vlan", "Duplex", "Speed", "Type"))
    s.say("-" * 76)
    for name in m.physical + m.channels:
        p = m.ports[name]
        drv = ""
        try:
            drv = os.path.basename(os.readlink("/sys/class/net/%s/device/driver" % name))
        except OSError:
            drv = "bond" if name.startswith("po") else ""
        s.say("%-9s %-20s %-11s %-6s %-6s %-6s %s" % (
            disp_if(name), p.description[:20], port_status(m, name, links), port_vlan_label(m, name),
            duplex_of(name) if not name.startswith("po") else "-", speed_of(name), drv))


@EXEC.add("show interfaces trunk", "Ports en trunk")
def x_trunk(s):
    m = s.m
    st = stp_states()
    s.say("%-9s %-10s %-8s %s" % ("Port", "Mode", "Native", "Vlans allowed and active"))
    for name in m.bridge_ports():
        p = m.ports[name]
        if p.mode == "trunk":
            want, pvid = m.port_vlans(name)
            s.say("%-9s %-10s %-8s %s" % (disp_if(name), "on", pvid or "-", fmt_vlan_list(want) or "none"))


@EXEC.add("show interfaces counters", "Compteurs de tous les ports")
def x_counters(s):
    links = link_table()
    s.say("%-9s %15s %12s %15s %12s %8s" % ("Port", "InOctets", "InPkts", "OutOctets", "OutPkts", "Erreurs"))
    for name in s.m.physical + s.m.channels:
        st = links.get(name, {}).get("stats64", {})
        rx, tx = st.get("rx", {}), st.get("tx", {})
        s.say("%-9s %15s %12s %15s %12s %8s" % (disp_if(name), rx.get("bytes", 0), rx.get("packets", 0),
                                                tx.get("bytes", 0), tx.get("packets", 0),
                                                rx.get("errors", 0) + tx.get("errors", 0)))


@EXEC.add("show interfaces <...>", "Detail d'une interface")
def x_if(s, text):
    for name in expand_if_range(text, s.m.ports):
        dev = name
        l = link_table().get(dev)
        if not l:
            raise CliError("%% Interface absente : %s" % disp_if(name))
        st = l.get("stats64", l.get("stats", {}))
        rx, tx = st.get("rx", {}), st.get("tx", {})
        up = "UP" in l.get("flags", [])
        s.say("%s is %s, line protocol is %s" % (disp_if(name, True), "up" if up else "administratively down",
                                                 "up" if "LOWER_UP" in l.get("flags", []) else "down"))
        s.say("  Description: %s" % (l.get("ifalias") or ""))
        s.say("  Hardware address is %s, MTU %s bytes, %s, %s-duplex" % (l.get("address"), l.get("mtu"),
                                                                       speed_of(dev), duplex_of(dev)))
        if l.get("master"):
            s.say("  Member of %s" % disp_if(l["master"]))
        s.say("  %s packets input, %s bytes, %s errors, %s drops" % (rx.get("packets"), rx.get("bytes"),
                                                                    rx.get("errors"), rx.get("dropped")))
        s.say("  %s packets output, %s bytes, %s errors, %s drops" % (tx.get("packets"), tx.get("bytes"),
                                                                     tx.get("errors"), tx.get("dropped")))


@EXEC.add("show vlan", "VLAN et ports associes")
def x_vlan(s):
    x_vlan_brief(s)


@EXEC.add("show vlan brief", "VLAN et ports associes")
def x_vlan_brief(s):
    m = s.m
    s.say("%-5s %-32s %-9s %s" % ("VLAN", "Name", "Status", "Ports"))
    s.say("---- -------------------------------- --------- -------------------------------")
    for vid in sorted(m.vlans):
        ports = [disp_if(n) for n in m.bridge_ports() if m.ports[n].mode == "access" and m.ports[n].access_vlan == vid]
        chunks = [", ".join(ports[i:i + 4]) for i in range(0, len(ports), 4)] or [""]
        s.say("%-5d %-32s %-9s %s" % (vid, m.vlans[vid][:32], "active", chunks[0]))
        for c in chunks[1:]:
            s.say("%-48s %s" % ("", c))


@EXEC.add("show mac address-table", "Table des adresses MAC")
def x_mac(s):
    _mac(s, None, None)


@EXEC.add("show mac address-table vlan <vid>", "Filtrer par VLAN")
def x_mac_vlan(s, vid):
    _mac(s, int(vid), None)


@EXEC.add("show mac address-table interface <...>", "Filtrer par port")
def x_mac_if(s, text):
    _mac(s, None, canon_if(text))


@EXEC.add("show mac address-table count", "Nombre d'adresses MAC")
def x_mac_count(s):
    _mac(s, None, None, count_only=True)


def mac_rows(vid=None, port=None):
    rows = []
    for e in sh_json("bridge -j fdb show br %s" % BRIDGE):
        if "self" in e.get("flags", []) or e.get("ifname") == BRIDGE:
            continue
        v = e.get("vlan")
        if v is None:
            continue
        if vid and v != vid or port and e.get("ifname") != port:
            continue
        typ = "STATIC" if e.get("state") == "static" else "DYNAMIC"
        if e.get("state") == "permanent":
            continue                       # MAC des ports eux-memes
        rows.append((v, e["mac"], typ, e.get("ifname")))
    return sorted(rows)


def _mac(s, vid, port, count_only=False):
    rows = mac_rows(vid, port)
    if not count_only:
        s.say("          Mac Address Table")
        s.say("-------------------------------------------")
        s.say("%-6s %-17s %-9s %s" % ("Vlan", "Mac Address", "Type", "Ports"))
        s.say("%-6s %-17s %-9s %s" % ("----", "-----------", "--------", "-----"))
        for v, mac, typ, ifn in rows:
            s.say("%-6s %-17s %-9s %s" % (v, mac.replace(":", "")[:4] + "." + mac.replace(":", "")[4:8] + "." +
                                          mac.replace(":", "")[8:], typ, disp_if(ifn)))
    s.say("Total Mac Addresses for this criterion: %d" % len(rows))


@EXEC.add("clear mac address-table dynamic", "Vider les MAC apprises")
@priv15
def x_clear_mac(s):
    sh("ip link set %s type bridge fdb_flush" % BRIDGE)
    s.say("Table MAC videe.")


def lldp_rows():
    data = sh_json("lldpcli -f json0 show neighbors details")
    rows = []
    if not isinstance(data, dict):
        return rows
    for lldp in data.get("lldp", []):
        for itf in lldp.get("interface", []):
            ch = (itf.get("chassis") or [{}])[0]
            name = (ch.get("name") or [{}])[0].get("value", "?")
            mgmt = (ch.get("mgmt-ip") or [{}])[0].get("value", "")
            descr = (ch.get("descr") or [{}])[0].get("value", "")
            port = (itf.get("port") or [{}])[0]
            pid = (port.get("id") or [{}])[0].get("value", "")
            pdesc = (port.get("descr") or [{}])[0].get("value", "")
            rows.append({"local": itf.get("name"), "name": name, "port": pdesc or pid,
                         "mgmt": mgmt, "descr": descr})
    return rows


@EXEC.add("show lldp neighbors", "Voisins LLDP")
def x_lldp(s):
    if not shutil.which("lldpcli"):
        raise CliError("% lldpd n'est pas installe")
    s.say("%-22s %-10s %-22s %s" % ("Device ID", "Local Intf", "Port ID", "Mgmt"))
    for r in lldp_rows():
        s.say("%-22s %-10s %-22s %s" % (r["name"][:22], disp_if(r["local"] or ""), r["port"][:22], r["mgmt"]))


@EXEC.add("show cdp neighbors", "Voisins (via LLDP)")
def x_cdp(s):
    x_lldp(s)


@EXEC.add("show spanning-tree", "Etat du spanning-tree")
def x_stp(s):
    m = s.m
    s.say("Spanning tree enabled protocol %s, bridge priority %d" % (m.stp_mode, m.stp_priority))
    if m.stp_mode == "rstp" and shutil.which("mstpctl"):
        s.say(sh("mstpctl showbridge %s" % BRIDGE).rstrip())
        s.say(sh("mstpctl showport %s" % BRIDGE).rstrip())
        return
    root = sh("cat /sys/class/net/%s/bridge/root_id 2>/dev/null" % BRIDGE).strip()
    me = sh("cat /sys/class/net/%s/bridge/bridge_id 2>/dev/null" % BRIDGE).strip()
    s.say("  Root ID    %s%s" % (root, "  (this switch is the root)" if root and root == me else ""))
    s.say("  Bridge ID  %s" % me)
    st = stp_states()
    s.say("%-9s %s" % ("Interface", "Sts"))
    for name in m.bridge_ports():
        s.say("%-9s %s" % (disp_if(name), st.get(name, "-")))


@EXEC.add("show etherchannel summary", "Agregats de liens (LACP)")
def x_ec(s):
    m = s.m
    links = link_table()
    s.say("Flags:  D - down   P - bundled in port-channel   U - in use   S - Layer2")
    s.say("%-6s %-14s %-9s %s" % ("Group", "Port-channel", "Protocol", "Ports"))
    s.say("------+--------------+---------+------------------------------")
    for po in m.channels:
        info = ""
        try:
            info = open("/proc/net/bonding/%s" % po).read()
        except OSError:
            pass
        agg = re.findall(r"Slave Interface: (\S+).*?Aggregator ID: (\d+)", info, re.S)
        active_agg = re.search(r"Active Aggregator Info:\s*\n\s*Aggregator ID: (\d+)", info)
        mem = []
        for p in m.members(po):
            up = "LOWER_UP" in links.get(p, {}).get("flags", [])
            bundled = up and (not active_agg or any(a == p and i == active_agg.group(1) for a, i in agg))
            mem.append("%s(%s)" % (disp_if(p), "P" if bundled else "D"))
        proto = "LACP" if any(m.ports[p].lacp_mode != "on" for p in m.members(po)) else "-"
        state = "SU" if "LOWER_UP" in links.get(po, {}).get("flags", []) else "SD"
        s.say("%-6s %-14s %-9s %s" % (po[2:], "%s(%s)" % (disp_if(po), state), proto, " ".join(mem)))


@EXEC.add("show ip interface brief", "Adresses IP du switch")
def x_ipbrief(s):
    s.say("%-12s %-20s %-8s %s" % ("Interface", "IP-Address", "Method", "Status"))
    for e in sh_json("ip -j addr show"):
        n = e["ifname"]
        if not (n.startswith("vlan") or n.startswith("mgmt")):
            continue
        addrs = [("%s/%s" % (a["local"], a["prefixlen"])) for a in e.get("addr_info", []) if a["family"] == "inet"]
        dyn = any(a.get("dynamic") for a in e.get("addr_info", []) if a["family"] == "inet")
        if n.startswith("vlan") and n[4:].isdigit():
            sv = s.m.svis.get(int(n[4:]))
            dyn = dyn or bool(sv and sv.dhcp)
        if not addrs and dyn:
            addrs = ["(attente DHCP)"]
        s.say("%-12s %-20s %-8s %s" % (disp_if(n) if n.startswith("vlan") else "Mgmt0" if n == "mgmt0" else n,
                                       ", ".join(addrs) or "unassigned", "DHCP" if dyn else "manual",
                                       e.get("operstate", "").lower()))


@EXEC.add("show ip route", "Table de routage")
def x_route(s):
    s.say(sh("ip route show").rstrip())


@EXEC.add("show version", "Version et materiel")
def x_version(s):
    up = sh("uptime -p").strip()
    cpu = ""
    try:
        cpu = [l.split(":", 1)[1].strip() for l in open("/proc/cpuinfo") if l.startswith("model name")][0]
    except (OSError, IndexError):
        pass
    mem = ""
    try:
        mem = "%d Mo" % (int(open("/proc/meminfo").readline().split()[1]) // 1024)
    except (OSError, ValueError, IndexError):
        pass
    s.say("TL-SwitchOS Software, Version %s - TECH-LINUX" % VERSION)
    s.say("Kernel : Linux %s" % os.uname().release)
    s.say("%s uptime is %s" % (s.m.hostname, up.replace("up ", "")))
    s.say("Processor : %s, %s" % (cpu, mem))
    s.say("%d Gigabit Ethernet interfaces (%s - %s)" % (len(s.m.physical),
                                                        disp_if(s.m.physical[0]) if s.m.physical else "-",
                                                        disp_if(s.m.physical[-1]) if s.m.physical else "-"))
    s.say("Configuration register : %s" % ("startup-config" if os.path.exists(STARTUP) else "usine"))
    prev = lib_version(PREV)
    if prev:
        s.say("Previous software : %s (install rollback pour y revenir)" % prev)
    if s.m.stp_mode == "rstp" and not shutil.which("mstpctl"):
        s.say("Spanning tree : STP classique (mstpd absent pour RSTP : apt install mstpd)")


@EXEC.add("show clock", "Date et heure")
def x_clock(s):
    s.say(time.strftime("*%H:%M:%S.000 %Z %a %b %d %Y"))


@EXEC.add("show logging", "Journal du switch")
def x_log(s):
    s.say(sh("journalctl -n 50 --no-pager -o short -t tlswitch -t kernel -u lldpd -u mstpd -u tlswitch-web "
             "2>/dev/null | grep -Ei 'tlswitch|br0|ge[0-9]+|po[0-9]+|bond|lldp|mstp|link' | tail -40").rstrip())


@EXEC.add("show users", "Sessions ouvertes")
def x_users(s):
    s.say(sh("who").rstrip())


@EXEC.add("show archive config rollback", "Retour arriere en attente")
def x_rb(s):
    left = rollback_pending()
    s.say("Aucun retour arriere en attente." if left is None else
          "Retour arriere dans %d s (configure confirm pour garder)." % left)


@EXEC.add("show environment", "Temperatures et ventilateurs")
def x_env(s):
    out = sh("sensors 2>/dev/null").rstrip()
    if not out:
        for z in sorted(os.listdir("/sys/class/thermal")) if os.path.isdir("/sys/class/thermal") else []:
            try:
                t = int(open("/sys/class/thermal/%s/temp" % z).read()) / 1000
                typ = open("/sys/class/thermal/%s/type" % z).read().strip()
                out += "%-20s %.1f C\n" % (typ, t)
            except (OSError, ValueError):
                pass
    s.say(out.rstrip() or "Pas de capteur disponible.")



# --- show / exec des fonctions 1.0.3 -------------------------------------------

@EXEC.add("show port-security", "Ports securises, MAC autorisees et violations")
def x_ps(s):
    m = s.m
    cnt = nft_counters()
    errdis = errdisable_load()
    s.say("%-9s %-8s %-8s %-10s %-11s %s" % ("Port", "MaxMAC", "Apprises", "Rejets", "Action", "Etat"))
    s.say("-" * 62)
    for name in m.bridge_ports():
        p = m.ports[name]
        if not p.ps:
            continue
        learned = len(re.findall(r"([0-9a-f]{2}(?::[0-9a-f]{2}){5})",
                                 sh("nft list set bridge tlswitch %s 2>/dev/null" % _nft_name("ps", name))))
        s.say("%-9s %-8d %-8d %-10d %-11s %s" % (disp_if(name), max(p.ps_max, len(p.ps_macs)), learned,
                                               cnt.get(_nft_name("ps", name), 0), p.ps_violation,
                                               "err-disabled" if name in errdis else "ok"))
        for mac in p.ps_macs:
            s.say("          statique %s" % mac)


@EXEC.add("show errdisable recovery", "Ports err-disabled")
def x_errdis(s):
    d = errdisable_load()
    s.say("Reactivation automatique : %s" % ("%d s" % s.m.errdisable_recovery if s.m.errdisable_recovery else "non"))
    for n, e in sorted(d.items()):
        s.say("%-9s %-20s depuis %s" % (disp_if(n), e.get("raison", ""),
                                        time.strftime("%H:%M:%S", time.localtime(e.get("depuis", 0)))))
    if not d:
        s.say("Aucun port err-disabled.")


@EXEC.add("clear errdisable interface <...>", "Reactiver un port err-disabled")
@priv15
def x_clear_errdis(s, text):
    for n in expand_if_range(text, s.m.ports):
        errdisable_clear(n)
        if n in s.m.ports and not s.m.ports[n].shutdown:
            sh(["ip", "link", "set", n, "up"])
        s.say("%s reactive" % disp_if(n))


@EXEC.add("clear port-security dynamic", "Oublier les MAC apprises (les statiques restent)")
@priv15
def x_clear_ps(s):
    for name in s.m.bridge_ports():
        p = s.m.ports[name]
        if p.ps:
            sh(["nft", "flush", "set", "bridge", "tlswitch", _nft_name("ps", name)])
            for mac in p.ps_macs:
                sh(["nft", "add", "element", "bridge", "tlswitch", _nft_name("ps", name), "{ %s }" % mac])
    s.say("MAC apprises effacees.")


@EXEC.add("show ip dhcp snooping", "Etat du DHCP snooping")
def x_dhcps(s):
    m = s.m
    s.say("DHCP snooping : %s" % ("actif" if m.dhcp_snooping else "inactif"))
    if not m.dhcp_snooping:
        return
    s.say("VLAN surveillees : %s" % (fmt_vlan_list(m.dhcp_snooping_vlans) if m.dhcp_snooping_vlans else "tous"))
    cnt = nft_counters()
    s.say("%-9s %-8s %s" % ("Port", "Trust", "Reponses DHCP jetees"))
    for name in m.bridge_ports():
        p = m.ports[name]
        s.say("%-9s %-8s %s" % (disp_if(name), "yes" if p.dhcp_trust else "no",
                                "-" if p.dhcp_trust else cnt.get(_nft_name("dhcp", name), 0)))


@EXEC.add("show ip igmp snooping", "Etat de l'IGMP snooping")
def x_igmp(s):
    m = s.m
    s.say("IGMP snooping : %s, querier : %s" % ("actif" if m.igmp_snooping else "inactif",
                                              "oui" if m.igmp_querier else "non"))


@EXEC.add("show ip igmp snooping groups", "Groupes multicast et ports abonnes")
def x_igmp_groups(s):
    s.say("%-6s %-40s %s" % ("Vlan", "Groupe", "Port"))
    for e in sh_json("bridge -j mdb show"):
        for g in e.get("mdb", [e]) if isinstance(e, dict) else []:
            if "grp" in g:
                s.say("%-6s %-40s %s" % (g.get("vid", "-"), g["grp"], disp_if(g.get("port", ""))))


@EXEC.add("show storm-control", "Limites de broadcast et multicast")
def x_storm(s):
    m = s.m
    cnt = nft_counters()
    s.say("%-9s %-12s %-12s %s" % ("Port", "Broadcast", "Multicast", "Jetes"))
    for name in m.bridge_ports():
        p = m.ports[name]
        if not p.storm:
            continue
        s.say("%-9s %-12s %-12s %d" % (disp_if(name), "%d pps" % p.storm["broadcast"] if "broadcast" in p.storm else "-",
                                       "%d pps" % p.storm["multicast"] if "multicast" in p.storm else "-",
                                       cnt.get(_nft_name("stormb", name), 0) + cnt.get(_nft_name("stormm", name), 0)))


@EXEC.add("show monitor", "Sessions de port mirroring (SPAN)")
def x_monitor(s):
    if not s.m.monitor:
        s.say("Aucune session SPAN.")
    for n, sess in sorted(s.m.monitor.items()):
        s.say("Session %d" % n)
        s.say("  Sources      : %s" % (", ".join("%s (%s)" % (disp_if(x), d) for x, d in
                                                sorted(sess["src"].items(), key=lambda i: natural_key(i[0]))) or "-"))
        s.say("  Destination  : %s" % (disp_if(sess["dst"]) if sess.get("dst") else "- (session inactive)"))


@EXEC.add("show snmp", "Configuration SNMP")
def x_snmp(s):
    m = s.m
    s.say("Communaute v2c : %s" % ("configuree (lecture)" if m.snmp_community else "non"))
    for u, (a, ka, p, kp) in sorted(m.snmp_users.items()):
        s.say("Utilisateur v3 : %-16s auth %-7s priv %s" % (u, a, p or "aucun (authNoPriv)"))
    s.say("Location : %s   Contact : %s" % (m.snmp_location or "-", m.snmp_contact or "-"))


@EXEC.add("show logging hosts", "Serveurs syslog distants")
def x_loghosts(s):
    m = s.m
    if not m.log_hosts:
        s.say("Aucun serveur syslog distant.")
    for ip, proto, port in m.log_hosts:
        s.say("%s %s/%d (niveau %s)" % (ip, proto, port, m.log_level))


@EXEC.add("show aaa servers", "Serveurs RADIUS")
def x_aaa(s):
    m = s.m
    s.say("Connexion : %s" % ("RADIUS puis comptes locaux" if m.aaa_radius else "comptes locaux"))
    for ip, port, key in m.radius:
        s.say("RADIUS %s port %d, delai %d s" % (ip, port, m.radius_timeout))
    r = [u for u, v in sorted(m.users.items()) if v[1] is None]
    if r:
        s.say("Comptes RADIUS : %s" % ", ".join(r))


@EXEC.add("test aaa group radius <compte> <motdepasse>", "Tester une connexion RADIUS")
@priv15
def x_test_aaa(s, user, pw):
    r = radius_auth(s.m, user, pw)
    s.say({True: "Utilisateur authentifie (Access-Accept).", False: "Refuse (Access-Reject).",
           None: "Aucun serveur RADIUS n'a repondu (adresse, cle, pare-feu ?)."}[r])


@EXEC.add("show archive", "Sauvegardes de la configuration")
def x_archive(s):
    m = s.m
    st = archive_state()
    s.say("Destination     : %s" % (m.archive_path or "switch seulement"))
    s.say("A chaque write  : %s" % ("oui" if m.archive_write else "non"))
    s.say("Periodique      : %s" % ("toutes les %d min" % m.archive_period if m.archive_period else "non"))
    if st.get("date"):
        s.say("Derniere        : %s  %s  %s" % (time.strftime("%Y-%m-%d %H:%M", time.localtime(st["date"])),
                                               "OK" if st.get("ok") else "ECHEC", st.get("message", "")))
    hist = archive_list()
    s.say("Sur le switch (%d/%d) :" % (len(hist), m.archive_max))
    for f in hist[::-1]:
        s.say("  %s" % f)
    if m.archive_path:
        try:
            s.say("Cle publique a autoriser sur le serveur (~/.ssh/authorized_keys) :")
            s.say("  " + archive_key())
        except (OSError, subprocess.SubprocessError):
            pass


@EXEC.add("archive config", "Sauvegarder la configuration maintenant")
@priv15
def x_archive_now(s):
    ok, msg = archive_now(s.m, "manuelle")
    s.say(("" if ok else "% ") + msg)


@EXEC.add("show metrics", "Apercu de l'export Prometheus")
def x_metrics(s):
    m = s.m
    s.say("Export Prometheus : %s" % ("http://<ip-du-switch>:%d/metrics" % m.prom_port if m.prom else "inactif"))
    if m.prom_allow:
        s.say("Autorise depuis : %s" % ", ".join(m.prom_allow))


@EXEC.add("enable", "Passer au niveau 15")
def x_enable(s):
    if s.priv >= 15:
        return
    if not s.m.enable_secret:
        raise CliError("% Pas d'enable secret configure")
    if not s.interactive:
        raise CliError("% enable impossible ici")
    import getpass
    pw = getpass.getpass("Password: ")
    if check_secret(pw, s.m.enable_secret):
        s.priv = 15
        log("enable reussi pour %s" % s.user)
    else:
        time.sleep(2)
        log("enable REFUSE pour %s" % s.user)
        raise CliError("% Bad secrets")


@EXEC.add("disable", "Revenir au niveau 1")
def x_disable(s):
    s.priv = 1


@EXEC.add("configure terminal", "Passer en mode configuration")
@priv15
def x_conf(s):
    s.mode, s.ctx = "config", None
    if s.interactive:
        s.say("Enter configuration commands, one per line.  End with CNTL/Z.")


@EXEC.add("configure terminal revert timer <minutes>", "Config avec retour arriere si non confirmee")
@priv15
def x_conf_revert(s, minutes):
    mins = int(minutes)
    if not 1 <= mins <= 120:
        raise CliError("% 1 a 120 minutes")
    rollback_arm(s.m.render(), mins * 60)
    s.say("Retour arriere arme : %d min. Valide avec 'configure confirm'." % mins)
    x_conf(s)


@EXEC.add("configure confirm", "Garder les modifications (annule le retour arriere)")
@priv15
def x_conf_confirm(s):
    if rollback_pending() is None:
        s.say("Rien a confirmer.")
        return
    rollback_cancel()
    log("modifications confirmees par %s" % s.user)
    s.say("Modifications confirmees, retour arriere annule.")


@EXEC.add("configure replace startup-config", "Revenir a la config enregistree")
@priv15
def x_conf_replace(s):
    m, errs = load_config(STARTUP, strict=False)
    for w in commit(m):
        s.say("%% Attention : %s" % w)
    s.m = m
    s.say("Configuration remplacee par la startup-config.")


def _write(s):
    os.makedirs(ETC, exist_ok=True)
    if os.path.exists(STARTUP):
        shutil.copy2(STARTUP, STARTUP + ".bak")
    write_atomic(STARTUP, s.m.render())
    rollback_cancel()
    log("configuration enregistree par %s" % s.user)
    s.say("Building configuration...\n[OK]")
    if s.m.archive_write:
        archive_detached("write-memory")


@EXEC.add("write memory", "Enregistrer la configuration")
@priv15
def x_write(s):
    _write(s)


@EXEC.add("write", "Enregistrer la configuration")
@priv15
def x_write2(s):
    _write(s)


@EXEC.add("copy running-config startup-config", "Enregistrer la configuration")
@priv15
def x_copy(s):
    _write(s)


@EXEC.add("write erase", "Effacer la config enregistree (usine au prochain demarrage)")
@priv15
def x_erase(s):
    if s.interactive:
        a = input("Effacer la startup-config ? [confirm] ").strip().lower()
        if a not in ("", "y", "o", "yes", "oui"):
            return
    if os.path.exists(STARTUP):
        shutil.move(STARTUP, STARTUP + ".erased")
    s.say("[OK]")


def _capture(cmd, s, timeout=30):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        s.say((r.stdout + r.stderr).rstrip())
    except subprocess.TimeoutExpired:
        s.say("% Delai depasse")
    except FileNotFoundError:
        s.say("% Outil absent")


def _safe_host(h):
    if not re.fullmatch(r"[A-Za-z0-9.:_-]{1,253}", h) or h.startswith("-"):
        raise CliError("% Destination invalide")
    return h


@EXEC.add("ping <hote>", "Ping")
def x_ping(s, host):
    _capture(["ping", "-c", "4", "-W", "2", _safe_host(host)], s)


@EXEC.add("traceroute <hote>", "Traceroute")
def x_trace(s, host):
    tool = shutil.which("traceroute") or shutil.which("tracepath") or "traceroute"
    _capture([tool, _safe_host(host)], s, timeout=60)


@EXEC.add("reload", "Redemarrer le switch")
@priv15
def x_reload(s):
    if not s.interactive:
        raise CliError("% reload uniquement en console ou SSH")
    if unsaved():
        a = input("System configuration has been modified. Save? [yes/no]: ").strip().lower()
        if a.startswith(("y", "o")):
            _write(s)
    a = input("Proceed with reload? [confirm] ").strip().lower()
    if a in ("", "y", "o", "yes", "oui"):
        log("reload demande par %s" % s.user)
        subprocess.call(["systemctl", "reboot"])


@EXEC.add("start-shell", "Shell Linux (niveau 15, console et SSH)")
@priv15
def x_shell(s):
    if not s.interactive:
        raise CliError("% Indisponible ici")
    log("shell Linux ouvert par %s" % s.user)
    subprocess.call(["/bin/bash", "-l"])


@EXEC.add("clear", "Effacer l'ecran")
@EXEC.add("clear screen", "Effacer l'ecran")
def x_clear(s):
    try:
        if s.out.isatty():
            s.out.write("\033[H\033[2J\033[3J")
            s.out.flush()
    except (AttributeError, ValueError, OSError):
        pass


@EXEC.add("terminal length <n>", "Accepte")
def x_termlen(s, n):
    pass


@EXEC.add("show history", "Historique des commandes")
def x_hist(s):
    try:
        import readline
        for i in range(1, readline.get_current_history_length() + 1):
            s.say("  " + readline.get_history_item(i))
    except ImportError:
        pass


def _quit(s):
    raise EOFError


for _w in ("exit", "quit", "logout"):
    EXEC.add(_w, "Quitter")(_quit)


# ---------------------------------------------------------------------------
# Console interactive (console serie, SSH)
# ---------------------------------------------------------------------------

BANNER = r"""
  _____ _      ____          _ _       _      ___  ____
 |_   _| |    / ___|_      _(_) |_ ___| |__  / _ \/ ___|
   | | | |    \___ \ \ /\ / / | __/ __| '_ \| | | \___ \
   | | | |___  ___) \ V  V /| | || (__| | | | |_| |___) |
   |_| |_____||____/ \_/\_/ |_|\__\___|_| |_|\___/|____/
       Les connexions doivent se faire depuis un bastion
"""


def who_am_i(model):
    """Utilisateur reel (via sudo) et son niveau de privilege."""
    user = os.environ.get("SUDO_USER") or os.environ.get("USER") or "root"
    if os.geteuid() == 0 and not os.environ.get("SUDO_USER"):
        return "root", 15
    if user in model.users:
        return user, model.users[user][0]
    return user, 1


def show_help(s, text):
    words = text.split()
    partial = "" if text.endswith(" ") or not words else words.pop()
    g = MODES[s.mode]
    if s.mode != "exec" and words[:1] == ["do"]:
        g, words = EXEC, words[1:]
    opts = g.help_for(words, partial)
    if s.mode.startswith("config-") and not words:
        for k, v in CONF.help_for([], partial).items():
            opts.setdefault(k, v)
    if not opts:
        s.say("% Unrecognized command")
    for k in sorted(opts):
        s.say("  %-26s %s" % (k, opts[k]))


def cli(argv=None):
    model, errs = load_running()
    user, priv = who_am_i(model)
    s = Session(model, priv=priv, user=user)
    s.mode = "exec"
    interactive = sys.stdin.isatty()
    s.interactive = interactive
    remote = bool(os.environ.get("SSH_CONNECTION")) or "--remote" in (argv or [])
    can_apply = os.geteuid() == 0
    readline = None
    hist = None
    if interactive:
        try:
            import readline

            def complete(text, state):
                buf = readline.get_line_buffer()
                words = buf.split()
                if buf.endswith(" ") or not words:
                    done, partial = words, ""
                else:
                    done, partial = words[:-1], words[-1]
                g = MODES[s.mode]
                if s.mode != "exec" and done[:1] == ["do"]:
                    g, done = EXEC, done[1:]
                opts = sorted(k + " " for k in g.help_for(done, partial) if not k.startswith("<") and k != "<cr>")
                return opts[state] if state < len(opts) else None

            readline.set_completer(complete)
            readline.set_completer_delims(" ")
            readline.parse_and_bind("tab: complete")
            hist = os.path.expanduser("~%s/.tlsh_history" % (user if user != "root" else ""))
            try:
                readline.read_history_file(hist)
            except OSError:
                pass
        except ImportError:
            readline = None
        s.say(BANNER)
        try:
            motd = open(os.path.join(ETC, "motd")).read().strip()
            if motd:
                s.say(motd + "\n")
        except OSError:
            pass
        if user in model.must_change:
            import getpass
            s.say("Mot de passe provisoire : choisis ton mot de passe definitif (8 caracteres minimum).")
            while True:
                try:
                    n1 = getpass.getpass("Nouveau mot de passe : ")
                    n2 = getpass.getpass("Confirmation : ")
                except (EOFError, KeyboardInterrupt):
                    s.say("")
                    return 1
                if n1 != n2:
                    s.say("% Les deux saisies sont differentes.")
                    continue
                try:
                    change_password(user, n1)
                    s.say("Mot de passe change et enregistre.\n")
                    s.m, _ = load_running()
                    break
                except CliError as e:
                    s.say(str(e))
        left = rollback_pending()
        if left is not None:
            s.say("%% ATTENTION : retour arriere automatique dans %d s (configure confirm pour garder)\n" % left)
        log("connexion CLI de %s (%s)" % (user, os.environ.get("SSH_CONNECTION", "console").split(" ")[0]))
    for e in errs:
        s.say("%% %s" % e)

    while True:
        prompt = s.prompt() + ("" if s.mode == "exec" else "") 
        try:
            if interactive:
                line = input(prompt)
            else:
                line = sys.stdin.readline()
                if not line:
                    break
                s.say(prompt + line.rstrip())
        except EOFError:
            if s.mode != "exec":            # Ctrl+Z / Ctrl+D = end
                s.mode, s.ctx = "exec", None
                s.say("")
                continue
            break
        except KeyboardInterrupt:
            s.say("")
            continue
        line = line.strip()
        if not line:
            continue
        if line.endswith("?"):
            show_help(s, line[:-1])
            continue
        try:
            if can_apply:
                execute(s, line, remote=remote)
            else:
                before = s.m.render()
                s.run_line(line)
                if s.m.render() != before:
                    s.m, _ = load_running()
                    raise CliError("% Modification refusee : lecture seule")
        except EOFError:
            break
        except CliError as e:
            msg = str(e)
            s.say(msg if msg.startswith("%") else "% " + msg)
        except (ValueError, KeyError) as e:
            s.say("%% Valeur invalide : %s" % e)
    if readline and hist:
        try:
            readline.write_history_file(hist)
        except OSError:
            pass
    if interactive and unsaved():
        s.say("Note : la configuration n'est pas enregistree (write memory).")
    return 0


# ---------------------------------------------------------------------------
# Demarrage, performances, point d'entree
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Mises a jour par paquet (.tlsu)
#
#   Paquet = tar contenant MANIFEST.json, MANIFEST.sig (signature ed25519 du
#   manifeste) et paquet.tar.gz (le dossier tlswitch/ de la nouvelle version).
#   L'ANCIENNE version verifie, installe, controle et revient en arriere toute
#   seule si la nouvelle ne fonctionne pas.
# ---------------------------------------------------------------------------

LIB = os.environ.get("TLSWITCH_LIB", "/usr/local/lib/tlswitch")
VAR = os.environ.get("TLSWITCH_VAR", "/var/lib/tlswitch")
MAJ_DIR = os.path.join(VAR, "maj")
PREV = os.path.join(VAR, "precedente")
MAJ_PUB = os.path.join(ETC, "maj.pub")
MAJ_STATE = os.path.join(VAR, "maj.json")       # sur disque : survit au redemarrage
MAJ_LOG = os.path.join(VAR, "maj.log")
MAJ_HIST = os.path.join(ETC, "historique-maj")
MAJ_MAX = 256 * 1024 * 1024


class MajError(Exception):
    pass


def vtuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", str(v))[:3]) or (0,)


def lib_version(path):
    """Version installee dans un dossier (lue dans le fichier, sans l'executer)."""
    try:
        m = re.search(r'^VERSION = "([^"]+)"', open(os.path.join(path, "tlswitch.py")).read(), re.M)
        return m.group(1) if m else None
    except OSError:
        return None


class MajJournal:
    """Affiche, journalise et publie l'avancement (lu par l'interface web)."""

    def __init__(self, out=None, de="", vers=""):
        self.out = out
        self.lignes = []
        self.etat = {"etat": "en_cours", "etape": "", "de": de, "vers": vers, "debut": int(time.time())}
        os.makedirs(VAR, exist_ok=True)

    def __call__(self, msg):
        if self.out:
            self.out(msg)
        self.lignes = (self.lignes + [msg])[-60:]
        try:
            with open(MAJ_LOG, "a") as f:
                f.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
        except OSError:
            pass
        self.publier(etape=msg)

    def publier(self, **kw):
        self.etat.update(kw)
        self.etat["lignes"] = self.lignes
        try:
            write_atomic(MAJ_STATE, json.dumps(self.etat), 0o644)
        except OSError:
            pass


def usb_mount():
    """Monte (en lecture seule) la premiere partition d'une cle USB. Renvoie le dossier."""
    mnt = os.path.join(RUN, "usb")
    if os.path.ismount(mnt):
        return mnt
    try:
        tree = json.loads(sh(["lsblk", "-J", "-o", "PATH,TRAN,TYPE,FSTYPE"]) or "{}")
    except ValueError:
        tree = {}
    cands = []
    for disk in tree.get("blockdevices", []):
        if disk.get("tran") != "usb":
            continue
        for part in (disk.get("children") or []) or [disk]:
            if part.get("fstype"):
                cands.append(part["path"])
    if not cands:
        raise MajError("aucune cle USB detectee (branche-la puis reessaie)")
    os.makedirs(mnt, exist_ok=True)
    for dev in cands:
        if subprocess.run(["mount", "-o", "ro", dev, mnt], capture_output=True).returncode == 0:
            return mnt
    raise MajError("cle USB detectee mais illisible (FAT32, exFAT ou ext4 attendu)")


def usb_umount():
    mnt = os.path.join(RUN, "usb")
    if os.path.ismount(mnt):
        subprocess.run(["umount", mnt], capture_output=True)


def maj_source(src, say=print):
    """usbflash0:NOM, flash:NOM, http(s)://... ou chemin -> fichier local."""
    os.makedirs(MAJ_DIR, exist_ok=True)
    if re.match(r"(?i)^https?://", src):
        import urllib.request
        nom = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(src.split("?")[0])) or "paquet.tlsu"
        dest = os.path.join(MAJ_DIR, nom)
        say("Telechargement de %s" % src)
        try:
            with urllib.request.urlopen(src, timeout=30) as r, open(dest + ".part", "wb") as f:
                n = 0
                while True:
                    b = r.read(1 << 16)
                    if not b:
                        break
                    n += len(b)
                    if n > MAJ_MAX:
                        raise MajError("fichier trop gros")
                    f.write(b)
        except (OSError, ValueError) as e:
            raise MajError("telechargement impossible : %s" % e)
        os.replace(dest + ".part", dest)
        return dest
    low = src.lower()
    for pfx in ("usbflash0:", "usb:"):
        if low.startswith(pfx):
            path = os.path.join(usb_mount(), src[len(pfx):].lstrip("/"))
            break
    else:
        if low.startswith("flash:"):
            path = os.path.join(MAJ_DIR, src[6:].lstrip("/"))
        else:
            path = src
    if not os.path.isfile(path):
        raise MajError("fichier introuvable : %s" % src)
    return path


def maj_lire(path, sans_signature=False):
    """Verifie un paquet. Renvoie (manifeste, octets de paquet.tar.gz)."""
    import hashlib
    import tarfile
    import tempfile
    if os.path.getsize(path) > MAJ_MAX:
        raise MajError("fichier trop gros")
    try:
        with tarfile.open(path, "r:") as t:
            parts = {}
            for mb in t.getmembers():
                nom = os.path.normpath(mb.name)
                if nom in ("MANIFEST.json", "MANIFEST.sig", "paquet.tar.gz") and mb.isfile():
                    parts[nom] = t.extractfile(mb).read()
    except (tarfile.TarError, OSError) as e:
        raise MajError("ce n'est pas un paquet TL-SwitchOS (%s)" % e)
    if "MANIFEST.json" not in parts or "paquet.tar.gz" not in parts:
        raise MajError("ce n'est pas un paquet TL-SwitchOS (manifeste absent)")
    signe = False
    if os.path.exists(MAJ_PUB) and not sans_signature:      # signature AVANT de lire quoi que ce soit
        if "MANIFEST.sig" not in parts:
            raise MajError("paquet NON signe : refuse (la cle de ce switch exige une signature)")
        with tempfile.TemporaryDirectory() as d:
            open(os.path.join(d, "m"), "wb").write(parts["MANIFEST.json"])
            open(os.path.join(d, "s"), "wb").write(parts["MANIFEST.sig"])
            r = subprocess.run(["openssl", "pkeyutl", "-verify", "-pubin", "-inkey", MAJ_PUB, "-rawin",
                                "-in", os.path.join(d, "m"), "-sigfile", os.path.join(d, "s")],
                               capture_output=True)
        if r.returncode != 0:
            raise MajError("signature INVALIDE : paquet refuse (modifie, ou signe avec une autre cle)")
        signe = True
    try:
        man = json.loads(parts["MANIFEST.json"].decode())
    except ValueError:
        raise MajError("manifeste illisible")
    if not isinstance(man, dict) or man.get("produit") != "TL-SwitchOS" or not man.get("version"):
        raise MajError("ce paquet n'est pas pour TL-SwitchOS")
    man["signe"] = signe
    if hashlib.sha256(parts["paquet.tar.gz"]).hexdigest() != man.get("sha256"):
        raise MajError("contenu altere (somme SHA256 differente du manifeste)")
    return man, parts["paquet.tar.gz"]


def maj_info(path, sans_signature=False):
    """Ce que ferait l'installation de ce paquet (pour la CLI et le web)."""
    man, _ = maj_lire(path, sans_signature)
    cur, new = vtuple(VERSION), vtuple(man["version"])
    if vtuple(man.get("version_min", "0")) > cur:
        raise MajError("ce paquet exige au moins la version %s (installee : %s) : installe d'abord "
                       "la version intermediaire" % (man["version_min"], VERSION))
    man["action"] = ("mise a niveau" if new > cur else "reinstallation" if new == cur
                     else "retour a une version anterieure")
    man["actuelle"] = VERSION
    man.setdefault("type", "fonctionnalites")
    try:
        os_rel = dict(l.strip().split("=", 1) for l in open("/etc/os-release") if "=" in l)
        man["debian_actuel"] = os_rel.get("VERSION_CODENAME", "").strip('"')
    except OSError:
        man["debian_actuel"] = ""
    return man


def _extraire(blob, dest):
    import io as _io
    import tarfile
    shutil.rmtree(dest, ignore_errors=True)
    os.makedirs(dest)
    with tarfile.open(fileobj=_io.BytesIO(blob), mode="r:gz") as t:
        for mb in t.getmembers():
            n = os.path.normpath(mb.name)
            if n.startswith(("/", "..")) or not (mb.isfile() or mb.isdir()):
                raise MajError("paquet refuse : chemin interdit %s" % mb.name)
            if n != "tlswitch" and not n.startswith("tlswitch/"):
                continue
            target = os.path.join(dest, os.path.relpath(n, "tlswitch"))
            if mb.isdir():
                os.makedirs(target, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "wb") as f:
                f.write(t.extractfile(mb).read())
            os.chmod(target, 0o755 if mb.mode & 0o100 else 0o644)
    if not os.path.isfile(os.path.join(dest, "tlswitch.py")):
        raise MajError("paquet incomplet (tlswitch.py absent)")


def _systemd():
    return os.path.isdir("/run/systemd/system") and shutil.which("systemctl")


def _web_ok(timeout=40):
    """L'interface web repond-elle (si elle est activee) ?"""
    import socket
    try:
        m, _ = load_running()
        if not m.http:
            return True
    except Exception:
        pass
    port = int(os.environ.get("TLSWITCH_WEB_PORT", "443"))
    t0 = time.time()
    while time.time() - t0 < timeout:
        for fam, addr in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
            try:
                with socket.socket(fam, socket.SOCK_STREAM) as so:
                    so.settimeout(2)
                    so.connect((addr, port))
                    return True
            except OSError:
                pass
        time.sleep(1)
    return False


def _activer(say, version_attendue):
    """Fait tourner le logiciel installe dans LIB. Leve MajError s'il ne fonctionne pas."""
    py = [sys.executable or "python3", os.path.join(LIB, "tlswitch.py")]
    r = subprocess.run(py + ["version"], capture_output=True, text=True, timeout=60)
    if r.returncode != 0 or r.stdout.strip() != version_attendue:
        raise MajError("la nouvelle version ne demarre pas : %s" % (r.stderr.strip()[-300:] or r.stdout.strip()))
    conf = RUNNING if os.path.exists(RUNNING) else STARTUP
    if os.path.exists(conf):
        say("Verification de la configuration avec la nouvelle version")
        r = subprocess.run(py + ["check", conf], capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            raise MajError("la nouvelle version refuse la configuration actuelle :\n%s" % r.stderr.strip()[-600:])
        say("Application de la configuration (le trafic continue)")
        env = dict(os.environ)
        r = subprocess.run(py + ["apply", conf], capture_output=True, text=True, timeout=300, env=env)
        if r.returncode != 0:
            raise MajError("echec de l'application de la configuration : %s" % r.stderr.strip()[-600:])
    if _systemd():
        say("Redemarrage des services TL-SwitchOS")
        subprocess.run(["systemctl", "daemon-reload"], capture_output=True)
        subprocess.run(["systemctl", "restart", "tlswitch-web.service"], capture_output=True)
        say("Controle de l'interface web")
        if not _web_ok():
            raise MajError("l'interface web ne repond plus avec la nouvelle version")


def _theme_grub(src, say):
    th = "/boot/grub/themes/tlswitch"
    if os.path.isfile(os.path.join(src, "boot", "theme.txt")) and os.path.isdir(th):
        for f in os.listdir(os.path.join(src, "boot")):
            if f.endswith((".txt", ".png", ".pf2")) and not f.startswith(("isolinux", "logo_installer")):
                shutil.copy2(os.path.join(src, "boot", f), os.path.join(th, f))
        say("Theme GRUB mis a jour")


def _script(path, env_extra, say, timeout):
    """Lance un script du paquet en affichant sa sortie au fil de l'eau. Renvoie le code retour."""
    env = dict(os.environ, **env_extra)
    p = subprocess.Popen(["bash", path], cwd=os.path.dirname(path), env=env, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True, errors="replace")
    t0 = time.time()
    for line in p.stdout:
        line = line.rstrip()
        if line:
            say("   " + line[-200:])
        if time.time() - t0 > timeout:
            p.kill()
            say("   (delai depasse)")
            break
    return p.wait()


def _hook(src, de, vers, say):
    hook = os.path.join(src, "maj.sh")
    if not os.path.isfile(hook):
        return
    say("Integration systeme (maj.sh)")
    rc = _script(hook, {"TLS_ANCIENNE": de, "TLS_NOUVELLE": vers, "TLS_SRC": src}, say, 1800)
    if rc != 0:
        raise MajError("maj.sh a echoue (code %d)" % rc)


def _majeure_prealables(man, say):
    """Avant une mise a niveau de Debian : place, depots joignables, apt libre."""
    say("Verifications avant la mise a niveau de Debian vers %s" % man.get("debian", "?"))
    libre = shutil.disk_usage("/").free
    if libre < 2 * 1024 ** 3:
        raise MajError("pas assez de place sur le disque (%d Mo libres, 2 Go necessaires)" % (libre // 1024 ** 2))
    if os.environ.get("TLSWITCH_NO_APT"):
        return
    r = subprocess.run(["apt-get", "update", "-q"], capture_output=True, text=True, timeout=600)
    if r.returncode != 0 or "Err:" in r.stdout:
        raise MajError("depots Debian injoignables (Internet sur le VLAN de gestion ?) : %s"
                       % (r.stderr.strip() or r.stdout.strip())[-300:])
    r = subprocess.run(["dpkg", "--audit"], capture_output=True, text=True)
    if r.stdout.strip():
        raise MajError("paquets Debian dans un etat incoherent, corrige d'abord (dpkg --audit)")
    say("Place disque, depots et paquets : OK")


def _sauvegarde_config(say):
    import tarfile
    os.makedirs(VAR, exist_ok=True)
    f = os.path.join(VAR, "sauvegarde-%s.tar.gz" % time.strftime("%Y%m%d-%H%M"))
    with tarfile.open(f, "w:gz") as t:
        t.add(ETC, arcname="tlswitch")
    say("Configuration sauvegardee : %s" % f)


def _os_upgrade(man, say):
    script = os.path.join(LIB, "os-upgrade.sh")
    if not os.path.isfile(script):
        raise MajError("paquet majeur sans os-upgrade.sh")
    say("Mise a niveau de Debian vers %s : peut durer 20 a 60 minutes, NE PAS couper le courant"
        % man.get("debian", "?"))
    rc = _script(script, {"TLS_CODENAME": str(man.get("debian", ""))}, say, 4 * 3600)
    if rc != 0:
        raise MajError("la mise a niveau de Debian a echoue (code %d)" % rc)


def _redemarrer(say, version):
    """Redemarre le switch. Au demarrage, boot() confirme (ou le secours remet l'ancienne version)."""
    say("Redemarrage du switch dans 10 secondes (le trafic sera coupe environ 1 a 2 minutes)")
    say.publier(etat="redemarrage", attendue=version)
    log("redemarrage apres mise a jour vers %s" % version)
    if os.environ.get("TLSWITCH_NO_REBOOT"):
        return
    time.sleep(10)
    subprocess.run(["sync"])
    subprocess.run(["systemctl", "reboot"]) if _systemd() else subprocess.run(["reboot"])


def maj_apres_demarrage():
    """Appele par boot() : termine l'etat d'une mise a jour qui attendait le redemarrage."""
    try:
        st = json.load(open(MAJ_STATE))
    except (OSError, ValueError):
        return
    if st.get("etat") != "redemarrage":
        return
    attendue = st.get("attendue")
    if attendue == VERSION:
        st["etat"] = "ok"
        msg = "Redemarrage reussi : TL-SwitchOS %s est actif." % VERSION
    else:
        st["etat"] = "annulee"
        msg = "Au redemarrage, la version %s n'a pas pu demarrer : retour a %s." % (attendue, VERSION)
        _historique("%s ANNULEE au redemarrage, retour a %s" % (attendue, VERSION))
    st["lignes"] = (st.get("lignes") or []) + [msg]
    st["etape"] = msg
    try:
        write_atomic(MAJ_STATE, json.dumps(st), 0o644)
    except OSError:
        pass
    log(msg)


def _historique(texte):
    try:
        with open(MAJ_HIST, "a") as f:
            f.write("%s  %s\n" % (time.strftime("%Y-%m-%d %H:%M"), texte))
    except OSError:
        pass


def _swap_in(src_dir):
    """Remplace LIB par une copie de src_dir, de facon (quasi) atomique."""
    new, old = LIB + ".new", LIB + ".old"
    shutil.rmtree(new, ignore_errors=True)
    shutil.rmtree(old, ignore_errors=True)
    shutil.copytree(src_dir, new, symlinks=False)
    os.chmod(os.path.join(new, "tlswitch.py"), 0o755)
    subprocess.run(["sync"])
    if os.path.isdir(LIB):
        os.rename(LIB, old)
    os.rename(new, LIB)
    shutil.rmtree(old, ignore_errors=True)


def maj_installer(src, force=False, sans_signature=False, out=print):
    """Installe un paquet. Renvoie True si la nouvelle version tourne, False si on est revenu en arriere."""
    os.makedirs(RUN, exist_ok=True)
    lockf = open(os.path.join(RUN, "maj.lock"), "w")
    try:
        fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        raise MajError("une mise a jour est deja en cours")
    say = MajJournal(out, de=VERSION)
    try:
        path = maj_source(src, say)
        say("Verification du paquet %s" % os.path.basename(path))
        man = maj_info(path, sans_signature)
        _, blob = maj_lire(path, sans_signature)
        say.publier(vers=man["version"])
        say("TL-SwitchOS %s -> %s (%s, %s)" % (VERSION, man["version"], man["action"],
                                                "signature valide" if man["signe"] else "NON signe"))
        if man["action"] != "mise a niveau" and not force:
            raise MajError("version %s deja installee ou plus recente (ajoute 'force' pour la forcer)"
                           % VERSION)
        if man.get("type") == "majeure":
            _majeure_prealables(man, say)
        staging = os.path.join(MAJ_DIR, "nouvelle")
        _extraire(blob, staging)
        if lib_version(staging) != man["version"]:
            raise MajError("le manifeste annonce %s mais le paquet contient %s"
                           % (man["version"], lib_version(staging)))
        for f in ("tlswitch.py", "tlswitch_web.py"):
            p = os.path.join(staging, f)
            if os.path.exists(p):
                r = subprocess.run([sys.executable or "python3", "-m", "py_compile", p], capture_output=True, text=True)
                if r.returncode != 0:
                    raise MajError("%s contient une erreur : %s" % (f, r.stderr.strip()[-300:]))
        shutil.rmtree(os.path.join(staging, "__pycache__"), ignore_errors=True)
    except MajError as e:
        say("ECHEC : %s" % e)
        say.publier(etat="echec", erreur=str(e))
        usb_umount()
        raise
    finally:
        pass
    # point de non-retour franchi seulement apres sauvegarde
    say("Sauvegarde de la version %s (pour 'install rollback')" % VERSION)
    garde = PREV + ".garde"                    # l'ancienne sauvegarde, rendue si on annule
    shutil.rmtree(garde, ignore_errors=True)
    if os.path.isdir(LIB):
        shutil.rmtree(PREV + ".tmp", ignore_errors=True)
        shutil.copytree(LIB, PREV + ".tmp")
        if os.path.isdir(PREV):
            os.rename(PREV, garde)
        os.rename(PREV + ".tmp", PREV)
    if os.path.exists(STARTUP):
        shutil.copy2(STARTUP, os.path.join(VAR, "startup-avant-maj.conf"))
    try:
        say("Installation des fichiers")
        _swap_in(staging)
        _hook(LIB, VERSION, man["version"], say)
        _activer(say, man["version"])
        _theme_grub(LIB, say)
    except (MajError, OSError, subprocess.SubprocessError) as e:
        say("PROBLEME : %s" % e)
        say("Retour automatique a la version %s" % VERSION)
        try:
            _swap_in(PREV)
            _activer(say, VERSION)
            say("Version %s restauree, le switch fonctionne comme avant." % VERSION)
        except (MajError, OSError, subprocess.SubprocessError) as e2:
            say("Restauration incomplete : %s (redemarre le switch)" % e2)
        if os.path.isdir(garde):                # la sauvegarde d'avant redevient la precedente
            shutil.rmtree(PREV, ignore_errors=True)
            os.rename(garde, PREV)
        _historique("%s -> %s ANNULEE : %s" % (VERSION, man["version"], str(e).splitlines()[0]))
        log("mise a jour vers %s annulee : %s" % (man["version"], e))
        say.publier(etat="annulee", erreur=str(e))
        usb_umount()
        return False
    shutil.rmtree(staging, ignore_errors=True)
    shutil.rmtree(garde, ignore_errors=True)
    try:
        os.remove(os.path.join(VAR, "secours.fait"))     # le secours pourra re-servir
    except OSError:
        pass
    usb_umount()
    if man.get("type") == "majeure":
        _sauvegarde_config(say)
        try:
            _os_upgrade(man, say)
        except (MajError, OSError, subprocess.SubprocessError) as e:
            # Debian ne peut pas etre remis en arriere : on le dit clairement, on ne redemarre pas
            say("ECHEC : %s" % e)
            say("Le switch continue de fonctionner sans redemarrer. Corrige (journal ci-dessus) puis relance "
                "la meme mise a jour avec 'force'.")
            _historique("%s -> %s : TL-SwitchOS installe, Debian NON mis a niveau (%s)"
                        % (VERSION, man["version"], str(e).splitlines()[0]))
            say.publier(etat="echec", erreur=str(e))
            return False
    _historique("%s -> %s OK%s" % (VERSION, man["version"],
                                    " (Debian %s)" % man.get("debian") if man.get("type") == "majeure" else ""))
    log("mise a jour %s -> %s reussie" % (VERSION, man["version"]))
    say("Mise a jour installee : TL-SwitchOS %s." % man["version"])
    if man.get("redemarrage", True):
        _redemarrer(say, man["version"])
    else:
        say.publier(etat="ok")
    return True


def maj_retour(out=print):
    """install rollback : revient a la version sauvegardee (et garde l'actuelle a sa place)."""
    prev = lib_version(PREV)
    if not prev:
        raise MajError("aucune version precedente sauvegardee")
    cur = lib_version(LIB) or VERSION
    say = MajJournal(out, de=cur, vers=prev)
    say("Retour de %s a %s" % (cur, prev))
    tmp = os.path.join(VAR, "echange")
    shutil.rmtree(tmp, ignore_errors=True)
    shutil.copytree(LIB, tmp)
    try:
        _swap_in(PREV)
        _activer(say, prev)
    except (MajError, OSError, subprocess.SubprocessError) as e:
        say("PROBLEME : %s, on reste en %s" % (e, cur))
        _swap_in(tmp)
        _activer(say, cur)
        say.publier(etat="annulee", erreur=str(e))
        return False
    shutil.rmtree(PREV, ignore_errors=True)
    os.rename(tmp, PREV)
    _historique("%s -> %s (install rollback)" % (cur, prev))
    log("retour a la version %s" % prev)
    say("Retour termine : TL-SwitchOS %s est installe (%s reste disponible)." % (prev, cur))
    _redemarrer(say, prev)
    return True


def maj_lancer_detache(args):
    """Lance 'tlswitch upgrade ...' hors du service web (qui va redemarrer pendant la mise a jour)."""
    cmd = [os.path.join(LIB, "tlswitch.py"), "upgrade"] + args
    if not os.path.exists(cmd[0]):
        cmd[0] = os.path.abspath(__file__)
    cmd = [sys.executable or "python3"] + cmd
    MajJournal(None, de=VERSION).publier(etape="Demarrage de la mise a jour")
    if _systemd() and shutil.which("systemd-run"):
        unit = "tlswitch-maj-%d" % int(time.time())
        r = subprocess.run(["systemd-run", "--unit", unit, "--collect", "--quiet"] + cmd, capture_output=True, text=True)
        if r.returncode == 0:
            return
    subprocess.Popen(cmd, start_new_session=True, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=dict(os.environ))


def maj_etat():
    try:
        st = json.load(open(MAJ_STATE))
    except (OSError, ValueError):
        st = {"etat": "aucune"}
    st["actuelle"] = VERSION
    st["precedente"] = lib_version(PREV)
    return st


# --- commandes CLI facon IOS-XE ----------------------------------------------

def _cli_say(s):
    return lambda msg: (s.say(msg), sys.stdout.flush() if s.interactive else None)


@EXEC.add("install add file <source>", "Verifier un paquet de mise a jour (usbflash0:, flash:, http://)")
@priv15
def x_install_check(s, src):
    try:
        path = maj_source(src, s.say)
        man = maj_info(path)
    except MajError as e:
        raise CliError("%% %s" % e)
    finally:
        usb_umount()
    s.say("Paquet          : %s" % os.path.basename(path))
    s.say("Version         : %s (installee : %s) -> %s" % (man["version"], VERSION, man["action"]))
    s.say("Signature       : %s" % ("valide" if man["signe"] else "AUCUNE (pas de cle sur ce switch)"))
    s.say("Date            : %s" % man.get("date", "-"))
    if man.get("type") == "majeure":
        s.say("Type            : MAJEURE (Debian %s -> %s, 20 a 60 min)" % (man.get("debian_actuel") or "?",
                                                                       man.get("debian", "?")))
    if man.get("notes"):
        s.say("Nouveautes      :")
        for line in str(man["notes"]).splitlines():
            s.say("  " + line)
    s.say("Pour installer : install add file %s activate" % src)


def _install(s, src, force):
    if not s.interactive:
        raise CliError("% Mise a jour : depuis la console, le SSH ou la page Mise a jour du web")
    try:
        man = maj_info(maj_source(src, s.say))
    except MajError as e:
        raise CliError("%% %s" % e)
    if man.get("type") == "majeure":
        s.say("*** MISE A JOUR MAJEURE : TL-SwitchOS %s + Debian %s -> %s ***"
              % (man["version"], man.get("debian_actuel") or "?", man.get("debian", "?")))
        s.say("    20 a 60 min, Internet necessaire, ne pas couper le courant. La configuration est gardee")
        s.say("    et sauvegardee, mais Debian ne peut pas revenir en arriere automatiquement.")
    a = input("La configuration est conservee. Le switch REDEMARRE a la fin (coupure d'environ 1 a 2 min).\n"
              "Proceed with installation? [y/n] ").strip().lower()
    if not a.startswith(("y", "o")):
        return
    if unsaved():
        s.say("Note : la running-config non enregistree est conservee telle quelle.")
    try:
        ok = maj_installer(src, force=force, out=_cli_say(s))
    except MajError as e:
        raise CliError("%% %s" % e)
    if ok:
        s.say("Le switch redemarre. Reconnecte-toi dans 1 a 2 minutes.")


@EXEC.add("install add file <source> activate", "Installer un paquet de mise a jour")
@priv15
def x_install(s, src):
    _install(s, src, False)


@EXEC.add("install add file <source> activate force", "Installer meme si la version n'est pas plus recente")
@priv15
def x_install_force(s, src):
    _install(s, src, True)


@EXEC.add("install rollback", "Revenir a la version precedente du logiciel")
@priv15
def x_install_rb(s):
    prev = lib_version(PREV)
    if not prev:
        raise CliError("% Aucune version precedente sauvegardee")
    if not s.interactive:
        raise CliError("% Depuis la console, le SSH ou la page Mise a jour du web")
    a = input("Revenir de %s a %s ? [y/n] " % (VERSION, prev)).strip().lower()
    if a.startswith(("y", "o")):
        try:
            maj_retour(out=_cli_say(s))
        except MajError as e:
            raise CliError("%% %s" % e)


@EXEC.add("show install summary", "Version installee, version precedente, historique")
def x_install_sum(s):
    s.say("Version active    : %s" % VERSION)
    s.say("Version precedente: %s" % (lib_version(PREV) or "aucune"))
    s.say("Signature exigee  : %s" % ("oui" if os.path.exists(MAJ_PUB) else "non (pas de cle)"))
    s.say("Depot APT         : %s" % ("configure" if os.path.exists(APT_SOURCE) else "non configure"))
    try:
        s.say("ATTENTION         : la version %s du depot n'a pas pu etre installee" % open(APT_ECHEC).read().strip())
    except OSError:
        pass
    if os.path.exists("/run/reboot-required"):
        s.say("Redemarrage       : necessaire pour terminer une mise a jour (reload)")
    try:
        hist = open(MAJ_HIST).read().strip().splitlines()[-10:]
    except OSError:
        hist = []
    s.say("Historique        :" + ("" if hist else " (vide)"))
    for line in hist:
        s.say("  " + line)


# --- depot APT : TL-SwitchOS + Debian en une seule mise a jour -----------------
APT_SOURCE = "/etc/apt/sources.list.d/tlswitch.sources"
APT_ECHEC = os.path.join(VAR, "apt-echec")
APT_ENV = dict(os.environ, DEBIAN_FRONTEND="noninteractive", APT_LISTCHANGES_FRONTEND="none")
APT_OPTS = ["-y", "-q", "-o", "Dpkg::Options::=--force-confdef", "-o", "Dpkg::Options::=--force-confold"]


def apt_maj_dispo(say):
    """apt-get update puis liste des paquets a mettre a jour : [(nom, actuelle, nouvelle)]."""
    r = subprocess.run(["apt-get", "update", "-q"], capture_output=True, text=True, env=APT_ENV, timeout=600)
    if r.returncode != 0:
        raise MajError("depots injoignables : %s" % (r.stderr.strip().splitlines() or ["?"])[-1])
    r = subprocess.run(["apt-get", "-s", "-q", "full-upgrade"], capture_output=True, text=True, env=APT_ENV,
                       timeout=600)
    paquets = []
    for line in r.stdout.splitlines():
        mo = re.match(r"^Inst (\S+) (?:\[(\S+)\] )?\((\S+)", line)
        if mo:
            paquets.append((mo.group(1), mo.group(2) or "-", mo.group(3)))
    r = subprocess.run(["apt-cache", "policy", "tlswitch"], capture_output=True, text=True, env=APT_ENV)
    mo = re.search(r"Candidat\s*:\s*(\S+)|Candidate:\s*(\S+)", r.stdout)
    cand = (mo.group(1) or mo.group(2)) if mo else None
    if cand and cand not in ("(aucun)", "(none)") and vtuple(cand) > vtuple(VERSION) \
            and not any(p[0] == "tlswitch" for p in paquets):
        paquets.insert(0, ("tlswitch", VERSION, cand))
    return paquets


def apt_mettre_a_jour(say):
    """Installe tout ce que proposent les depots (Debian + TL-SwitchOS). Renvoie True si redemarrage requis."""
    cmds = [["apt-get"] + APT_OPTS + ["install", "tlswitch"], ["apt-get"] + APT_OPTS + ["full-upgrade"]]
    # paquet tlswitch deja marque installe mais version active plus ancienne (installation
    # precedente interrompue ou refusee) : on le reinstalle pour relancer la mise a jour
    r = subprocess.run(["dpkg-query", "-W", "-f=${Status} ${Version}", "tlswitch"], capture_output=True, text=True)
    if r.returncode == 0 and "ok installed" in r.stdout and vtuple(r.stdout.split()[-1]) > vtuple(VERSION):
        cmds[0] = ["apt-get"] + APT_OPTS + ["install", "--reinstall", "tlswitch"]
    for cmd in cmds:
        say("$ " + " ".join(c for c in cmd if not c.startswith(("-o", "Dpkg"))))
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=APT_ENV)
        for line in p.stdout:
            line = line.rstrip()
            if line and not line.startswith(("Reading", "Lecture", "Building", "Construction")):
                say("  " + line)
        if p.wait() != 0:
            raise MajError("apt a echoue (code %d)" % p.returncode)
    subprocess.run(["apt-get", "-q", "-y", "autoremove", "--purge"], capture_output=True, env=APT_ENV)
    subprocess.run(["apt-get", "clean"], capture_output=True)
    return os.path.exists("/run/reboot-required")


def maj_depot(out=print):
    """Mise a jour par le depot APT, lancee depuis le web (tlswitch upgrade --depot).
    Pas de verrou maj.lock ici : le paquet tlswitch le prend lui-meme pendant apt."""
    say = MajJournal(out, de=VERSION)
    say("Recherche des mises a jour (depot TL-SwitchOS et Debian)")
    try:
        paquets = apt_maj_dispo(say)
        if not paquets:
            say("Tout est a jour (TL-SwitchOS %s)." % VERSION)
            say.publier(etat="ok")
            return True
        tl = [p for p in paquets if p[0] == "tlswitch"]
        say("%d paquet(s) a mettre a jour%s" % (len(paquets), ", dont TL-SwitchOS %s" % tl[0][2] if tl else ""))
        if tl:
            say.publier(vers=tl[0][2])
        reboot = apt_mettre_a_jour(say)
    except (MajError, OSError, subprocess.SubprocessError) as e:
        say("ECHEC : %s" % e)
        say.publier(etat="echec", erreur=str(e))
        return False
    nouvelle = lib_version(LIB) or VERSION
    if os.path.exists(APT_ECHEC):
        say("ATTENTION : TL-SwitchOS %s n'a pas pu etre installe, la version %s reste active."
            % (open(APT_ECHEC).read().strip(), nouvelle))
    _historique("depot APT : mises a jour installees (TL-SwitchOS %s)" % nouvelle)
    if reboot:
        _redemarrer(say, nouvelle)
    else:
        say("Mises a jour installees, pas de redemarrage necessaire.")
        say.publier(etat="ok")
    return True


@EXEC.add("show install updates", "Mises a jour disponibles (depot TL-SwitchOS et Debian)")
@priv15
def x_install_updates(s):
    if not os.path.exists(APT_SOURCE):
        s.say("Note : depot TL-SwitchOS non configure (seules les mises a jour Debian sont visibles).")
    try:
        paquets = apt_maj_dispo(s.say)
    except (MajError, OSError, subprocess.SubprocessError) as e:
        raise CliError("%% %s" % e)
    if not paquets:
        s.say("Tout est a jour (TL-SwitchOS %s)." % VERSION)
        return
    s.say("%-28s %-22s %s" % ("Paquet", "Installe", "Disponible"))
    for n, a, b in paquets:
        s.say("%-28s %-22s %s" % (n, a, b))
    s.say("Pour installer : install update")


@EXEC.add("install update", "Installer les mises a jour du depot (TL-SwitchOS et Debian)")
@priv15
def x_install_update(s):
    if not s.interactive:
        raise CliError("% Mise a jour : depuis la console, le SSH ou la page Mise a jour du web")
    try:
        paquets = apt_maj_dispo(s.say)
    except (MajError, OSError, subprocess.SubprocessError) as e:
        raise CliError("%% %s" % e)
    if not paquets:
        s.say("Tout est a jour (TL-SwitchOS %s)." % VERSION)
        return
    tl = [p for p in paquets if p[0] == "tlswitch"]
    s.say("%d paquet(s) a mettre a jour%s." % (len(paquets), ", dont TL-SwitchOS %s" % tl[0][2] if tl else ""))
    a = input("La configuration est conservee. Proceed with update? [y/n] ").strip().lower()
    if not a.startswith(("y", "o")):
        return
    try:
        reboot = apt_mettre_a_jour(_cli_say(s))
    except (MajError, OSError, subprocess.SubprocessError) as e:
        raise CliError("%% %s" % e)
    if os.path.exists(APT_ECHEC):
        s.say("ATTENTION : la nouvelle version de TL-SwitchOS n'a pas pu etre installee, l'ancienne reste active "
              "(voir show install summary).")
    if not reboot:
        s.say("Mises a jour installees, pas de redemarrage necessaire.")
        return
    a = input("Redemarrage necessaire. Redemarrer maintenant ? [y/n] ").strip().lower()
    if a.startswith(("y", "o")):
        s.say("Le switch redemarre. Reconnecte-toi dans 1 a 2 minutes.")
        subprocess.run(["sync"])
        subprocess.run(["systemctl", "reboot"])
    else:
        s.say("A faire plus tard : reload")


@EXEC.add("dir <fs>", "Lister usbflash0: ou flash:")
def x_dir(s, fs):
    fs = fs.lower().rstrip(":")
    try:
        d = usb_mount() if fs in ("usbflash0", "usb") else MAJ_DIR if fs == "flash" else None
    except MajError as e:
        raise CliError("%% %s" % e)
    if d is None:
        raise CliError("% Systemes de fichiers : usbflash0: flash:")
    s.say("Directory of %s:/" % fs)
    try:
        noms = sorted(os.listdir(d))
    except OSError:
        noms = []
    for n in noms:
        p = os.path.join(d, n)
        if os.path.isfile(p):
            s.say("  %10d  %s  %s" % (os.path.getsize(p), time.strftime("%b %d %Y %H:%M",
                                                                       time.localtime(os.path.getmtime(p))), n))
    if not noms:
        s.say("  (vide)")
    if fs != "flash":
        usb_umount()


def factory_model():
    m = Model(discover_ports())
    m.svis[1] = Svi(1)
    m.svis[1].dhcp = True
    return m


def boot():
    """Au demarrage : startup-config, sinon sa sauvegarde, sinon lecture tolerante, sinon usine."""
    tune()
    rollback_cancel()
    maj_apres_demarrage()
    for path, strict in ((STARTUP, True), (STARTUP + ".bak", True), (STARTUP, False)):
        if not os.path.exists(path):
            continue
        try:
            m, errs = load_config(path, strict=strict)
        except CliError as e:
            log("config %s rejetee : %s" % (path, str(e).splitlines()[0]))
            continue
        warns = commit(m)
        log("demarrage avec %s (%d erreurs, %d avertissements)" % (path, len(errs), len(warns)))
        for w in warns:
            log("avertissement : %s" % w)
        return 0
    log("aucune configuration valide : configuration usine (Vlan1 en DHCP)")
    commit(factory_model())
    return 0


def guard():
    """Lance chaque minute (timer systemd) : repare ce qui a decroche."""
    if not os.path.exists("/sys/class/net/%s" % BRIDGE):
        log("guard : bridge absent, reapplication complete")
        m, _ = load_running()
        commit(m)
        return 0
    m, _ = load_running()
    wanted = {"lldpd": m.lldp, "tlswitch-web": m.http, "ssh": m.ssh, "tlswitch-metrics": m.prom,
              "mstpd": m.stp_mode == "rstp" and bool(shutil.which("mstpctl"))}
    for svc, on in wanted.items():
        if on and subprocess.run(["systemctl", "is-active", "--quiet", svc]).returncode != 0:
            log("guard : %s arrete, redemarrage" % svc)
            subprocess.run(["systemctl", "restart", svc])
    # ports desactives par le noyau (ex : lien qui a disparu puis revenu) : on remet l'etat voulu
    # table nftables effacee (ex : service nftables qui vide tout) : on la remet
    if nft_script(m) and shutil.which("nft") and subprocess.run(
            ["nft", "list", "table", "bridge", "tlswitch"], capture_output=True).returncode != 0:
        log("guard : regles de securite des ports absentes, reapplication")
        Applier(m).filters()
    ps_violations(m)
    errdisable_recover(m)
    if archive_due(m):
        archive_detached("periodique")
    errdis = errdisable_load()
    links = {l["ifname"]: l for l in sh_json("ip -j link show")}
    for name in m.physical + m.channels:
        p = m.ports[name]
        l = links.get(name)
        if name in errdis:
            continue
        if l and not p.shutdown and "UP" not in l.get("flags", []):
            log("guard : %s etait down administrativement, reactivation" % name)
            sh(["ip", "link", "set", name, "up"])
    return 0


def tune():
    """Reglages de performance des ports (appeles au demarrage)."""
    ncpu = os.cpu_count() or 1
    # processeur toujours a pleine frequence : latence stable
    cpu_dir = "/sys/devices/system/cpu"
    if os.path.isdir(cpu_dir):
        for c in os.listdir(cpu_dir):
            gov = os.path.join(cpu_dir, c, "cpufreq", "scaling_governor")
            if os.path.exists(gov):
                try:
                    open(gov, "w").write("performance")
                except OSError:
                    pass
    rps_mask = "%x" % ((1 << min(ncpu, 32)) - 1)
    for name in discover_ports():
        if not os.path.exists("/sys/class/net/%s" % name):
            continue
        ring = sh(["ethtool", "-g", name])
        mx = re.search(r"Pre-set maximums:.*?RX:\s+(\d+).*?TX:\s+(\d+)", ring, re.S)
        if mx:
            sh(["ethtool", "-G", name, "rx", mx.group(1), "tx", mx.group(2)])
        sh(["ethtool", "--set-eee", name, "eee", "off"])          # pas de micro-coupures EEE
        sh(["ethtool", "-K", name, "gro", "on", "gso", "on", "tso", "on"])
        sh(["ethtool", "-A", name, "autoneg", "off", "rx", "off", "tx", "off"])  # pas de pause frames
        # regroupe les interruptions : indispensable sur un petit processeur (Atom)
        if subprocess.run(["ethtool", "-C", name, "adaptive-rx", "on"], capture_output=True).returncode != 0:
            subprocess.run(["ethtool", "-C", name, "rx-usecs", "50"], capture_output=True)
        queues = os.listdir("/sys/class/net/%s/queues" % name) if os.path.isdir("/sys/class/net/%s/queues" % name) else []
        rxq = [q for q in queues if q.startswith("rx-")]
        if len(rxq) == 1 and ncpu > 1:                            # carte mono-file : repartir en logiciel
            try:
                open("/sys/class/net/%s/queues/%s/rps_cpus" % (name, rxq[0]), "w").write(rps_mask)
            except OSError:
                pass
        try:
            open("/sys/class/net/%s/tx_queue_len" % name, "w").write("10000")
        except OSError:
            pass


def main(argv):
    prog = os.path.basename(argv[0])
    args = argv[1:]
    if prog == "tlsh" or args[:1] == ["cli"]:
        return cli(args[1:])
    if not args or args[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    cmd = args.pop(0)
    if cmd == "version":
        print(VERSION)
        return 0
    if cmd == "boot":
        return boot()
    if cmd == "rollback":
        return 0 if rollback_now("demande manuelle") else 1
    if cmd == "tune":
        tune()
        return 0
    if cmd == "upgrade":
        if os.geteuid() != 0 and not os.environ.get("TLSWITCH_VAR"):
            print("Il faut etre root.", file=sys.stderr)
            return 1
        try:
            if "--retour" in args:
                return 0 if maj_retour() else 3
            if "--depot" in args:
                return 0 if maj_depot() else 3
            opts = [a for a in args if a.startswith("--")]
            rest = [a for a in args if not a.startswith("--")]
            if not rest:
                print("usage : tlswitch upgrade [--verifier] [--force] [--sans-signature] <paquet>", file=sys.stderr)
                return 1
            if "--verifier" in opts:
                man = maj_info(maj_source(rest[0]), "--sans-signature" in opts)
                print(json.dumps(man, indent=1, ensure_ascii=False))
                return 0
            return 0 if maj_installer(rest[0], force="--force" in opts,
                                      sans_signature="--sans-signature" in opts) else 3
        except MajError as e:
            print("Mise a jour refusee : %s" % e, file=sys.stderr)
            return 2
    if cmd == "guard":
        return guard()
    if cmd == "metrics":
        metrics_serve()
        return 0
    if cmd == "archive":
        motif = args[args.index("--motif") + 1] if "--motif" in args[:-1] else "manuelle"
        with Lock():
            m, _ = load_running()
        ok, msg = archive_now(m, motif)
        print(msg)
        return 0 if ok else 1
    if cmd in ("apply", "check"):
        dry = "--dry-run" in args
        args = [a for a in args if a != "--dry-run"]
        path = args[0] if args else STARTUP
        try:
            m, _ = load_config(path)
        except CliError as e:
            print("Erreurs dans la configuration :\n%s" % e, file=sys.stderr)
            return 2
        if cmd == "check":
            print("OK : %d ports, %d VLAN, %d agregats, %d SVI, %d comptes" % (
                len(m.physical), len(m.vlans), len(m.channels), len(m.svis), len(m.users)))
            return 0
        if os.geteuid() != 0 and not dry:
            print("Il faut etre root.", file=sys.stderr)
            return 1
        if dry:
            print("Commandes qui seraient executees (systeme vide) :")
        warns = commit(m, dry=dry)
        for w in warns:
            print("Attention : %s" % w, file=sys.stderr)
        if not dry:
            log("configuration appliquee depuis %s" % path)
            print("Configuration appliquee (%d avertissements)." % len(warns))
        return 0
    print("Commande inconnue : %s (voir --help)" % cmd, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv) or 0)
