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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tlswitch as T  # noqa: E402

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
CERT = os.path.join(T.ETC, "web.crt")
KEY = os.path.join(T.ETC, "web.key")
SESSION_TTL = 30 * 60
REVERT = 90                 # secondes laissees au navigateur pour confirmer

SESSIONS = {}               # sid -> dict
FAILS = {}                  # ip -> [compteur, debut]
LOCK = threading.Lock()


def new_session(user, priv, must_change=False):
    sid = secrets.token_urlsafe(32)
    with LOCK:
        SESSIONS[sid] = {"user": user, "priv": priv, "csrf": secrets.token_urlsafe(24),
                         "exp": time.time() + SESSION_TTL, "console": None, "must_change": must_change}
    return sid


def get_session(sid):
    with LOCK:
        s = SESSIONS.get(sid)
        if not s or s["exp"] < time.time():
            SESSIONS.pop(sid, None)
            return None
        s["exp"] = time.time() + SESSION_TTL
        return s


def throttled(ip):
    with LOCK:
        n, t0 = FAILS.get(ip, [0, time.time()])
        if time.time() - t0 > 300:
            FAILS.pop(ip, None)
            return False
        return n >= 5


def fail(ip):
    with LOCK:
        n, t0 = FAILS.get(ip, [0, time.time()])
        FAILS[ip] = [n + 1, t0]


# ---------------------------------------------------------------------------
# Donnees
# ---------------------------------------------------------------------------

def state():
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
        "stp_priority": m.stp_priority, "lldp": m.lldp, "snmp": m.snmp_community or "",
        "snmp_location": m.snmp_location, "users": [{"name": u, "priv": v[0]} for u, v in sorted(m.users.items())],
        "unsaved": T.unsaved(), "rollback": T.rollback_pending(), "errors": errs,
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
        self.send_header("Strict-Transport-Security", "max-age=31536000")
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
        if n > 1_000_000:
            raise ValueError("trop gros")
        raw = self.rfile.read(n) if n else b"{}"
        return json.loads(raw.decode() or "{}")

    def auth(self, write=False):
        s = get_session(self.sid())
        if not s:
            self.send(401, {"error": "non connecte"})
            return None
        if self.command == "POST" and self.headers.get("X-CSRF") != s["csrf"]:
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
                return self.send(200, {"logged": False})
            return self.send(200, {"logged": True, "user": s["user"], "priv": s["priv"], "csrf": s["csrf"],
                                   "must_change": s.get("must_change", False)})
        s = self.auth()
        if not s:
            return
        try:
            if path == "/api/state":
                return self.send(200, state())
            if path == "/api/mac":
                return self.send(200, [{"vlan": v, "mac": mac, "type": t, "port": T.disp_if(p or "")}
                                       for v, mac, t, p in T.mac_rows()])
            if path == "/api/lldp":
                rows = T.lldp_rows()
                for r in rows:
                    r["local"] = T.disp_if(r["local"] or "")
                return self.send(200, rows)
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
            if path == "/api/log":
                out = io.StringIO()
                c = T.Session(T.load_running()[0])
                c.out = out
                T.x_log(c)
                return self.send(200, out.getvalue(), "text/plain")
        except Exception as e:      # le web ne doit jamais tomber
            T.log("web : erreur %s" % e)
            return self.send(500, {"error": str(e)})
        self.send(404, {"error": "introuvable"})

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
            if u and T.check_secret(pw, u[1]):
                sid = new_session(user, u[0], user in m.must_change)
                T.log("web : connexion de %s depuis %s" % (user, ip))
                return self.send(200, {"ok": True}, extra={
                    "Set-Cookie": "tlsid=%s; Path=/; HttpOnly; Secure; SameSite=Strict" % sid})
            fail(ip)
            time.sleep(1.5)
            T.log("web : ECHEC de connexion pour '%s' depuis %s" % (user, ip))
            return self.send(401, {"error": "identifiants invalides"})
        if path == "/api/logout":
            with LOCK:
                SESSIONS.pop(self.sid(), None)
            return self.send(200, {"ok": True}, extra={"Set-Cookie": "tlsid=; Path=/; Max-Age=0; Secure"})
        try:
            if path == "/api/password":
                s = self.auth()
                if s:
                    m, _ = T.load_running()
                    u = m.users.get(s["user"])
                    old = str(data.get("old", ""))[:128]
                    new = str(data.get("new", ""))[:128]
                    if not u or (not s.get("must_change") and not T.check_secret(old, u[1])):
                        time.sleep(1)
                        return self.send(200, {"error": "mot de passe actuel incorrect"})
                    try:
                        T.change_password(s["user"], new)
                    except T.CliError as e:
                        return self.send(200, {"error": str(e).lstrip("% ")})
                    s["must_change"] = False
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
            return self.send(500, {"error": str(e)})
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
