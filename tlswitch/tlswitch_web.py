#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TL-SwitchOS - interface web HTTPS.

Toutes les modifications passent par les memes commandes Cisco que la CLI
(elles sont affichees dans l'interface), avec le meme retour arriere
automatique : si une modification coupe l'acces du navigateur, le switch
revient tout seul a la config precedente.
"""

import http.server
import io
import json
import os
import secrets
import socketserver
import ssl
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import base64
import hashlib
import re

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tlswitch as T  # noqa: E402

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
CERT = os.path.join(T.ETC, "web.crt")
KEY = os.path.join(T.ETC, "web.key")
SESSION_TTL = 30 * 60
SSO_MAX = 12 * 3600         # une session SSO se refait chez le fournisseur au bout de 12 h
REVERT = 90                 # secondes laissees au navigateur pour confirmer

SESSIONS = {}               # sid -> dict
SSO_ATTENTE = {}            # state -> {nonce, verifier, retour, exp}
FAILS = {}                  # ip -> [compteur, debut]
LOCK = threading.Lock()


def new_session(user, priv, must_change=False, sso=False, groupes=None):
    sid = secrets.token_urlsafe(32)
    with LOCK:
        SESSIONS[sid] = {"user": user, "priv": priv, "csrf": secrets.token_urlsafe(24),
                         "exp": time.time() + SESSION_TTL, "console": None, "must_change": must_change,
                         "sso": sso, "groupes": list(groupes or []), "fin": time.time() + SSO_MAX}
    return sid


def get_session(sid):
    if not sid:
        return None
    now = time.time()
    with LOCK:
        for k in [k for k, v in SESSIONS.items() if v["exp"] < now]:
            SESSIONS.pop(k, None)
        s = SESSIONS.get(sid)
        if not s:
            return None
    # compte SSO : valable tant que le SSO reste configure, 12 h au plus ; le niveau suit
    # les groupes autorises de la config (groupe retire ou change : niveau revu ou session fermee)
    if s.get("sso"):
        try:
            m = T.load_running()[0]
            niveau = T.sso_niveau(m, s.get("groupes")) if T.sso_actif(m) else 0
        except Exception:
            niveau = 0
        with LOCK:
            if not niveau or now > s.get("fin", 0):
                SESSIONS.pop(sid, None)
                return None
            if niveau != s["priv"]:
                s["priv"] = niveau
                s["console"] = None
            s["exp"] = now + SESSION_TTL
            return s
    # compte supprime ou niveau change depuis la connexion : la session suit la config
    try:
        u = T.load_running()[0].users.get(s["user"])
    except Exception:
        u = None
    with LOCK:
        if not u:
            SESSIONS.pop(sid, None)
            return None
        if u[0] != s["priv"]:
            s["priv"] = u[0]
            s["console"] = None
        s["exp"] = now + SESSION_TTL
        return s


def fermer_sessions(user, sauf=None):
    """Mot de passe change : les autres sessions web de ce compte sont fermees."""
    with LOCK:
        for k in [k for k, v in SESSIONS.items() if v["user"] == user and k != sauf]:
            SESSIONS.pop(k, None)


def throttled(ip):
    with LOCK:
        n, t0 = FAILS.get(ip, [0, time.time()])
        if time.time() - t0 > 300:
            FAILS.pop(ip, None)
            return False
        return n >= 5


def fail(ip):
    with LOCK:
        if len(FAILS) > 10000:               # memoire bornee
            FAILS.clear()
        n, t0 = FAILS.get(ip, [0, time.time()])
        FAILS[ip] = [n + 1, t0]


# ---------------------------------------------------------------------------
# Donnees
# ---------------------------------------------------------------------------

def state(priv=15):
    m, errs = T.load_running()
    links = T.link_table()
    stp = T.stp_states()
    ports = []
    for name in m.physical + m.channels:
        p = m.ports[name]
        l = links.get(name, {})
        st = l.get("stats64", {})
        want, pvid = m.port_vlans(name) if name in m.bridge_ports() else ({}, None)
        ports.append({
            "name": name, "disp": T.disp_if(name), "long": T.disp_if(name, True),
            "desc": p.description, "status": T.port_status(m, name, links),
            "speed": T.speed_of(name), "mode": p.mode, "access": p.access_vlan,
            "allowed": None if p.allowed is None else sorted(p.allowed), "native": p.native,
            "cg": p.channel_group, "lacp": p.lacp_mode, "shutdown": p.shutdown, "portfast": p.portfast,
            "bpduguard": p.bpduguard, "mtu": p.mtu, "stp": stp.get(name, ""),
            "vlans": T.fmt_vlan_list(want), "pvid": pvid, "mac": l.get("address", ""),
            "rx": st.get("rx", {}).get("bytes", 0), "tx": st.get("tx", {}).get("bytes", 0),
            "rxerr": st.get("rx", {}).get("errors", 0), "txerr": st.get("tx", {}).get("errors", 0),
            "members": [T.disp_if(x) for x in m.members(name)] if name.startswith("po") else [],
        })
    ips = []
    for e in T.sh_json("ip -j addr show"):
        n = e["ifname"]
        if n.startswith("vlan") or n == "mgmt0":
            for a in e.get("addr_info", []):
                if a["family"] == "inet":
                    ips.append({"if": T.disp_if(n) if n.startswith("vlan") else "Mgmt0",
                                "addr": "%s/%s" % (a["local"], a["prefixlen"]), "dhcp": bool(a.get("dynamic"))})
    up = 0
    try:
        up = int(float(open("/proc/uptime").read().split()[0]))
    except (OSError, ValueError):
        pass
    load = ""
    try:
        load = open("/proc/loadavg").read().split()[0]
    except OSError:
        pass
    return {
        "hostname": m.hostname, "version": T.VERSION, "uptime": up, "load": load,
        "ports": ports, "vlans": [{"id": v, "name": n} for v, n in sorted(m.vlans.items())],
        "svis": [{"vid": v, "addresses": s.addresses, "dhcp": s.dhcp, "desc": s.description,
                  "shutdown": s.shutdown} for v, s in sorted(m.svis.items())],
        "ips": ips, "gateway": m.gateway, "dns": m.dns, "ntp": m.ntp, "stp_mode": m.stp_mode,
        "stp_priority": m.stp_priority, "lldp": m.lldp, "snmp": (m.snmp_community or "") if priv >= 15 else ("***" if m.snmp_community else ""),
        "snmp_location": m.snmp_location, "users": [{"name": u, "priv": v[0]} for u, v in sorted(m.users.items())],
        "unsaved": T.unsaved(), "rollback": T.rollback_pending(), "errors": errs,
        "domain": m.domain, "domain_vu": T.suffixe_dns(m),
        "sso": ({"issuer": m.sso.get("issuer", ""), "client_id": m.sso.get("client_id", ""),
                 "secret": bool(m.sso.get("secret")), "admin_group": m.sso.get("admin_group", ""),
                 "read_group": m.sso.get("read_group", ""), "label": m.sso.get("label", ""),
                 "actif": T.sso_actif(m), "retours": T.sso_adresses_retour(m)} if priv >= 15 else None),
    }


def run_config(sess, lines):
    m, _ = T.load_running()
    s = T.Session(m, priv=sess["priv"], user=sess["user"])
    s.mode = "config"
    s.out = io.StringIO()
    errors = []
    armed = False
    for line in lines:
        line = str(line).strip()
        if not line or line.startswith("!"):
            continue
        try:
            T.execute(s, line, remote=True, auto_revert=REVERT)
            armed = armed or getattr(s, "armed", False)
        except T.CliError as e:
            errors.append("%s -> %s" % (line, str(e).lstrip("% ")))
        except (ValueError, KeyError) as e:
            errors.append("%s -> valeur invalide (%s)" % (line, e))
    T.log("web : %d commande(s) par %s" % (len(lines), sess["user"]))
    return {"output": s.out.getvalue(), "errors": errors,
            "rollback": T.rollback_pending() if armed else None}


def run_exec(sess, line):
    c = sess.get("console")
    if c is None:
        m, _ = T.load_running()
        c = T.Session(m, priv=sess["priv"], user=sess["user"])
        c.mode = "exec"
        sess["console"] = c
    c.out = io.StringIO()
    c.armed = False
    line = line.strip()
    try:
        if line.endswith("?"):
            T.show_help(c, line[:-1])
        elif line:
            T.execute(c, line, remote=True, auto_revert=REVERT)
    except EOFError:
        c.mode = "exec"
    except T.CliError as e:
        c.say(str(e) if str(e).startswith("%") else "% " + str(e))
    except (ValueError, KeyError) as e:
        c.say("%% Valeur invalide : %s" % e)
    return {"output": c.out.getvalue(), "prompt": c.prompt(),
            "rollback": T.rollback_pending() if getattr(c, "armed", False) else None}


# ---------------------------------------------------------------------------
# Serveur HTTP
# ---------------------------------------------------------------------------

class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "TL-SwitchOS"
    sys_version = ""

    def log_message(self, fmt, *args):
        pass

    def headers_common(self):
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'")
        self.send_header("Cache-Control", "no-store")

    def send(self, code, body, ctype="application/json", extra=None):
        data = body if isinstance(body, bytes) else (json.dumps(body) if ctype == "application/json"
                                                      else body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if "text" in ctype or "json" in ctype else ""))
        self.send_header("Content-Length", str(len(data)))
        self.headers_common()
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def sid(self):
        for part in self.headers.get("Cookie", "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "tlsid":
                return v
        return None

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n < 0 or n > 1_000_000:
            raise ValueError("trop gros")
        raw = self.rfile.read(n) if n else b"{}"
        return json.loads(raw.decode() or "{}")

    def auth(self, write=False):
        s = get_session(self.sid())
        if not s:
            self.send(401, {"error": "non connecte"})
            return None
        if self.command == "POST" and not secrets.compare_digest(self.headers.get("X-CSRF") or "", s["csrf"]):
            self.send(403, {"error": "jeton CSRF invalide"})
            return None
        if s.get("must_change") and urllib.parse.urlparse(self.path).path != "/api/password":
            self.send(403, {"error": "change d'abord ton mot de passe provisoire"})
            return None
        if write and s["priv"] < 15:
            self.send(403, {"error": "niveau 15 requis"})
            return None
        return s

    # -- GET --------------------------------------------------------------
    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/index.html"):
            return self.static("index.html", "text/html")
        if path in ("/app.js", "/app.css", "/logo.svg", "/favicon.png"):
            ctype = {"js": "text/javascript", "css": "text/css", "svg": "image/svg+xml",
                     "png": "image/png"}[path.rsplit(".", 1)[1]]
            return self.static(path[1:], ctype)
        if path == "/api/me":
            s = get_session(self.sid())
            if not s:
                rep = {"logged": False}
                try:
                    m = T.load_running()[0]
                    rep["host"] = m.hostname
                    if T.sso_actif(m):
                        rep["sso"] = m.sso.get("label") or "SSO"
                except Exception:
                    pass
                return self.send(200, rep)
            return self.send(200, {"logged": True, "user": s["user"], "priv": s["priv"], "csrf": s["csrf"],
                                   "must_change": s.get("must_change", False), "sso": s.get("sso", False)})
        if path == "/sso/debut":
            return self.sso_debut()
        if path == T.SSO_RETOUR:
            return self.sso_retour()
        s = self.auth()
        if not s:
            return
        try:
            if path == "/api/state":
                return self.send(200, state(s["priv"]))
            if path == "/api/mac":
                return self.send(200, [{"vlan": v, "mac": mac, "type": t, "port": T.disp_if(p or "")}
                                       for v, mac, t, p in T.mac_rows()])
            if path == "/api/lldp":
                rows = T.lldp_rows()
                for r in rows:
                    r["local"] = T.disp_if(r["local"] or "")
                return self.send(200, rows)
            if path in ("/api/running", "/api/download") and s["priv"] < 15:
                return self.send(403, {"error": "niveau 15 requis"})
            if path == "/api/running":
                m, _ = T.load_running()
                return self.send(200, m.render(), "text/plain")
            if path == "/api/download":
                m, _ = T.load_running()
                return self.send(200, m.render(), "text/plain",
                                 {"Content-Disposition": 'attachment; filename="%s.conf"' % m.hostname})
            if path == "/api/maj/depot":
                if s["priv"] < 15:
                    return self.send(403, {"error": "niveau 15 requis"})
                rep = {"configure": os.path.exists(T.APT_SOURCE), "paquets": []}
                try:
                    rep["paquets"] = [{"nom": n, "actuelle": a, "nouvelle": b} for n, a, b in T.apt_maj_dispo(None)]
                except (T.MajError, OSError, subprocess.SubprocessError) as e:
                    rep["error"] = str(e)
                return self.send(200, rep)
            if path == "/api/maj/etat":
                st = T.maj_etat()
                try:
                    st["historique"] = open(T.MAJ_HIST).read().strip().splitlines()[-15:]
                except OSError:
                    st["historique"] = []
                st["signature"] = os.path.exists(T.MAJ_PUB)
                return self.send(200, st)
            if path == "/api/central":
                if s["priv"] < 15:
                    return self.send(403, {"error": "niveau 15 requis"})
                try:
                    import tlswitch_central
                except ImportError:
                    return self.send(200, {"absent": True})
                return self.send(200, tlswitch_central.status())
            if path == "/api/log":
                out = io.StringIO()
                c = T.Session(T.load_running()[0])
                c.out = out
                T.x_log(c)
                return self.send(200, out.getvalue(), "text/plain")
        except Exception as e:      # le web ne doit jamais tomber
            T.log("web : erreur %s" % e)
            return self.send(500, {"error": "erreur interne (voir show logging)"})
        self.send(404, {"error": "introuvable"})

    # -- SSO (OpenID Connect, flux "authorization code" + PKCE) ------------------
    def page(self, code, titre, texte, extra=None):
        html = ("<!doctype html><html lang=fr><head><meta charset=utf-8><title>TL-SwitchOS</title>"
                "<link rel=stylesheet href=/app.css></head><body><div class=login><div class=login-box>"
                "<img src=/logo.svg alt='' class=login-logo><h1>%s</h1><p class=muted>%s</p>"
                "<a class='btn primary block' href=/>Retour a la connexion</a></div></div></body></html>"
                % (titre, texte))
        self.send(code, html, "text/html", extra)

    def adresse_retour(self):
        host = self.headers.get("Host", "")
        if not re.fullmatch(r"[A-Za-z0-9.\-]+(:\d+)?|\[[0-9A-Fa-f:.]+\](:\d+)?", host):
            raise ValueError("en-tete Host invalide")
        return "https://%s%s" % (host, T.SSO_RETOUR)

    def sso_debut(self):
        try:
            m = T.load_running()[0]
            if not T.sso_actif(m):
                return self.page(404, "SSO non configure", "Connecte-toi avec un compte du switch.")
            conf = T.sso_decouverte(m.sso["issuer"])
            state, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
            verifier = secrets.token_urlsafe(48)
            defi = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
            retour = self.adresse_retour()
            with LOCK:
                for k in [k for k, v in SSO_ATTENTE.items() if v["exp"] < time.time()]:
                    SSO_ATTENTE.pop(k, None)
                if len(SSO_ATTENTE) > 1000:
                    SSO_ATTENTE.clear()
                SSO_ATTENTE[state] = {"nonce": nonce, "verifier": verifier, "retour": retour,
                                      "exp": time.time() + 600}
            q = urllib.parse.urlencode({"response_type": "code", "client_id": m.sso["client_id"],
                                        "redirect_uri": retour, "scope": "openid profile email",
                                        "state": state, "nonce": nonce, "code_challenge": defi,
                                        "code_challenge_method": "S256"})
            url = conf["authorization_endpoint"] + ("&" if "?" in conf["authorization_endpoint"] else "?") + q
            # le navigateur qui revient doit etre celui qui est parti (contre la connexion forcee)
            self.send(302, b"", "text/plain", {"Location": url, "Set-Cookie":
                      "tlssso=%s; Path=/sso; Max-Age=600; HttpOnly; Secure; SameSite=Lax" % state})
        except Exception as e:
            T.log("web : SSO indisponible : %s" % e)
            self.page(502, "Fournisseur SSO injoignable", "Le switch n'arrive pas a joindre le fournisseur "
                      "d'identite. Connecte-toi avec un compte du switch (show ip http sso pour le detail).")

    def sso_retour(self):
        ip = self.client_address[0]
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        state = (q.get("state") or [""])[0]
        cookie = ""
        for part in self.headers.get("Cookie", "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "tlssso":
                cookie = v
        with LOCK:
            att = SSO_ATTENTE.pop(state, None)
        efface = "tlssso=; Path=/sso; Max-Age=0; HttpOnly; Secure; SameSite=Lax"
        if not att or att["exp"] < time.time() or not secrets.compare_digest(cookie, state):
            return self.page(400, "Connexion SSO expiree", "Recommence depuis la page de connexion.",
                             {"Set-Cookie": efface})
        if q.get("error"):
            return self.page(403, "Connexion refusee", "Le fournisseur a refuse : %s"
                             % re.sub(r"[^A-Za-z0-9 _.-]", "", (q.get("error") or [""])[0])[:60],
                             {"Set-Cookie": efface})
        try:
            m = T.load_running()[0]
            conf = T.sso_decouverte(m.sso["issuer"])
            data = urllib.parse.urlencode({"grant_type": "authorization_code", "code": (q.get("code") or [""])[0],
                                           "redirect_uri": att["retour"], "client_id": m.sso["client_id"],
                                           "client_secret": m.sso["secret"],
                                           "code_verifier": att["verifier"]}).encode()
            req = urllib.request.Request(conf["token_endpoint"], data=data,
                                         headers={"Content-Type": "application/x-www-form-urlencoded",
                                                  "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=15) as r:
                jet = json.loads(r.read(500000).decode())
            # jeton recu directement du fournisseur en HTTPS verifie (OIDC Core 3.1.3.7) :
            # on controle emetteur, destinataire, date et nonce
            p = jet["id_token"].split(".")[1]
            claims = json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)).decode())
            aud = claims.get("aud")
            aud = aud if isinstance(aud, list) else [aud]
            if claims.get("iss") != conf.get("issuer"):
                raise ValueError("emetteur inattendu")
            if m.sso["client_id"] not in aud:
                raise ValueError("jeton destine a un autre client")
            if float(claims.get("exp", 0)) < time.time() - 60:
                raise ValueError("jeton expire")
            if not secrets.compare_digest(str(claims.get("nonce", "")), att["nonce"]):
                raise ValueError("nonce invalide")
            if "groups" not in claims and conf.get("userinfo_endpoint") and jet.get("access_token"):
                req = urllib.request.Request(conf["userinfo_endpoint"],
                                             headers={"Authorization": "Bearer " + jet["access_token"]})
                with urllib.request.urlopen(req, timeout=15) as r:
                    info = json.loads(r.read(200000).decode())
                if info.get("sub") == claims.get("sub"):
                    claims.update({k: v for k, v in info.items() if k in ("groups", "preferred_username", "email")})
        except Exception as e:
            T.log("web : SSO, connexion refusee depuis %s : %s" % (ip, e))
            return self.page(403, "Connexion SSO impossible", "Le jeton du fournisseur n'a pas pu etre verifie "
                             "(voir show logging).", {"Set-Cookie": efface})
        user = re.sub(r"[^A-Za-z0-9._@-]", "", str(claims.get("preferred_username") or claims.get("email")
                                                  or claims.get("sub") or ""))[:64]
        groupes = claims.get("groups") or []
        if isinstance(groupes, str):
            groupes = [groupes]
        niveau = T.sso_niveau(m, [str(g) for g in groupes])
        if not user or not niveau:
            T.log("web : SSO, %s refuse (aucun groupe autorise) depuis %s" % (user or "?", ip))
            return self.page(403, "Acces refuse", "Ton compte (%s) n'est dans aucun groupe autorise sur ce "
                             "switch." % (user or "?"), {"Set-Cookie": efface})
        sid = new_session(user, niveau, sso=True, groupes=[str(g) for g in groupes])
        T.log("web : connexion SSO de %s (niveau %d) depuis %s" % (user, niveau, ip))
        html = "<!doctype html><meta charset=utf-8><meta http-equiv=refresh content='0;url=/'><a href=/>Continuer</a>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(html.encode())))
        self.headers_common()
        self.send_header("Set-Cookie", "tlsid=%s; Path=/; HttpOnly; Secure; SameSite=Strict" % sid)
        self.send_header("Set-Cookie", efface)
        self.end_headers()
        self.wfile.write(html.encode())

    def static(self, name, ctype):
        try:
            with open(os.path.join(STATIC, name), "rb") as f:
                self.send(200, f.read(), ctype)
        except OSError:
            self.send(404, b"absent", "text/plain")

    # -- POST -------------------------------------------------------------
    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        ip = self.client_address[0]
        if path == "/api/maj/envoi":            # fichier brut, pas du JSON
            return self.maj_envoi()
        try:
            data = self.body()
        except (ValueError, UnicodeDecodeError):
            return self.send(400, {"error": "requete invalide"})
        if path == "/api/login":
            if throttled(ip):
                return self.send(429, {"error": "trop d'essais, attends 5 minutes"})
            user = str(data.get("user", ""))[:32]
            pw = str(data.get("password", ""))[:128]
            m, _ = T.load_running()
            u = m.users.get(user)
            if u and T.login_ok(m, user, pw):
                sid = new_session(user, u[0], user in m.must_change)
                T.log("web : connexion de %s depuis %s" % (user, ip))
                if u[0] >= 15 and user not in m.must_change:
                    T.grub_auto(user, pw)      # verrou du demarrage pose s'il manque
                return self.send(200, {"ok": True}, extra={
                    "Set-Cookie": "tlsid=%s; Path=/; HttpOnly; Secure; SameSite=Strict" % sid})
            fail(ip)
            time.sleep(1.5)
            T.log("web : ECHEC de connexion pour '%s' depuis %s" % (user, ip))
            return self.send(401, {"error": "identifiants invalides"})
        if path == "/api/logout":
            with LOCK:
                SESSIONS.pop(self.sid(), None)
            return self.send(200, {"ok": True}, extra={"Set-Cookie": "tlsid=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Strict"})
        try:
            if path == "/api/central/accept":
                s = self.auth(write=True)
                if s:
                    import tlswitch_central
                    try:
                        d = tlswitch_central.accept(str(data.get("code", ""))[:12], s["user"], T.log)
                    except ValueError as e:
                        return self.send(200, {"error": str(e)})
                    return self.send(200, {"ok": True, "nom": d.get("nom", ""), "ip": d.get("ip", "")})
                return
            if path == "/api/password":
                s = self.auth()
                if s:
                    m, _ = T.load_running()
                    u = m.users.get(s["user"])
                    old = str(data.get("old", ""))[:128]
                    new = str(data.get("new", ""))[:128]
                    if s.get("sso"):
                        return self.send(200, {"error": "compte SSO : le mot de passe se change chez le fournisseur"})
                    if u and u[1] is None:
                        return self.send(200, {"error": "compte RADIUS : le mot de passe se change dans l'annuaire"})
                    if not u or (not s.get("must_change") and not T.check_secret(old, u[1])):
                        time.sleep(1)
                        return self.send(200, {"error": "mot de passe actuel incorrect"})
                    try:
                        T.change_password(s["user"], new)
                    except T.CliError as e:
                        return self.send(200, {"error": str(e).lstrip("% ")})
                    s["must_change"] = False
                    fermer_sessions(s["user"], sauf=self.sid())
                    self.send(200, {"ok": True})
                return
            if path == "/api/config":
                s = self.auth(write=True)
                if s:
                    lines = data.get("lines") or []
                    if not isinstance(lines, list) or len(lines) > 2000:
                        return self.send(400, {"error": "liste de commandes invalide"})
                    self.send(200, run_config(s, lines))
                return
            if path == "/api/confirm":
                s = self.auth(write=True)
                if s:
                    if T.rollback_pending() is not None:
                        T.rollback_cancel()
                        T.log("web : modification confirmee par %s (navigateur toujours joignable)" % s["user"])
                    self.send(200, {"ok": True})
                return
            if path == "/api/save":
                s = self.auth(write=True)
                if s:
                    c = T.Session(T.load_running()[0], user=s["user"])
                    c.out = io.StringIO()
                    T._write(c)
                    self.send(200, {"ok": True})
                return
            if path == "/api/restore":
                s = self.auth(write=True)
                if s:
                    text = str(data.get("text", ""))
                    tmp = os.path.join(T.RUN, "restore.conf")
                    T.write_atomic(tmp, text)
                    try:
                        m, _ = T.load_config(tmp, strict=True)
                    except T.CliError as e:
                        return self.send(200, {"errors": str(e).splitlines()})
                    before = T.load_running()[0].render()
                    T.rollback_arm(before, REVERT)
                    warns = T.commit(m)
                    T.log("web : configuration restauree par %s" % s["user"])
                    self.send(200, {"errors": [], "warnings": warns, "rollback": T.rollback_pending()})
                return
            if path in ("/api/maj/installer", "/api/maj/retour"):
                s = self.auth(write=True)
                if s:
                    if T.maj_etat().get("etat") == "en_cours" and os.path.exists(os.path.join(T.RUN, "maj.lock")):
                        import fcntl
                        with open(os.path.join(T.RUN, "maj.lock")) as lf:
                            try:
                                fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
                                fcntl.flock(lf, fcntl.LOCK_UN)
                            except OSError:
                                return self.send(200, {"error": "une mise a jour est deja en cours"})
                    if path.endswith("retour"):
                        if not T.lib_version(T.PREV):
                            return self.send(200, {"error": "aucune version precedente"})
                        args = ["--retour"]
                    elif data.get("depot"):
                        args = ["--depot"]
                    else:
                        f = os.path.join(T.MAJ_DIR, "envoi.tlsu")
                        if not os.path.exists(f):
                            return self.send(200, {"error": "envoie d'abord le paquet"})
                        args = (["--force"] if data.get("force") else []) + [f]
                    T.log("web : mise a jour lancee par %s (%s)" % (s["user"], " ".join(args)))
                    T.maj_lancer_detache(args)
                    self.send(200, {"ok": True})
                return
            if path == "/api/exec":
                s = self.auth()
                if s:
                    self.send(200, run_exec(s, str(data.get("line", ""))[:500]))
                return
        except Exception as e:
            T.log("web : erreur %s" % e)
            return self.send(500, {"error": "erreur interne (voir show logging)"})
        self.send(404, {"error": "introuvable"})


def _maj_envoi(self):
    s = self.auth(write=True)
    if not s:
        return
    n = int(self.headers.get("Content-Length") or 0)
    if n <= 0 or n > T.MAJ_MAX:
        return self.send(400, {"error": "fichier vide ou trop gros"})
    os.makedirs(T.MAJ_DIR, exist_ok=True)
    dest = os.path.join(T.MAJ_DIR, "envoi.tlsu")
    with open(dest + ".part", "wb") as f:
        left = n
        while left > 0:
            b = self.rfile.read(min(left, 1 << 16))
            if not b:
                break
            f.write(b)
            left -= len(b)
    if left:
        return self.send(400, {"error": "envoi interrompu"})
    os.replace(dest + ".part", dest)
    try:
        info = T.maj_info(dest)
    except T.MajError as e:
        os.remove(dest)
        return self.send(200, {"error": str(e)})
    T.log("web : paquet %s envoye par %s" % (info["version"], s["user"]))
    self.send(200, info)


Handler.maj_envoi = _maj_envoi


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    address_family = __import__("socket").AF_INET6

    def server_bind(self):
        import socket
        try:
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        except OSError:
            pass
        super().server_bind()

    ssl_ctx = None

    def get_request(self):
        sock, addr = self.socket.accept()
        sock.settimeout(20)                 # un client lent ne bloque rien
        if self.ssl_ctx:
            # poignee de main TLS faite dans le thread du client, pas dans la boucle d'accept
            sock = self.ssl_ctx.wrap_socket(sock, server_side=True, do_handshake_on_connect=False)
        return sock, addr


def main():
    port = int(os.environ.get("TLSWITCH_WEB_PORT", "443"))
    try:
        srv = Server(("::", port), Handler)            # IPv6 + IPv4
    except OSError:
        Server.address_family = __import__("socket").AF_INET
        srv = Server(("0.0.0.0", port), Handler)
    if os.environ.get("TLSWITCH_WEB_PLAIN") != "1":
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(CERT, KEY)
        srv.ssl_ctx = ctx
    T.log("interface web demarree sur le port %d" % port)
    srv.serve_forever()


if __name__ == "__main__":
    main()
