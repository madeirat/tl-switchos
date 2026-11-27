#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TL-SwitchOS - gestion centrale par TL-Manager.

Le switch ecoute sur un port dedie (2507 par defaut) seulement si on l'a active
('central enable'). TL-Manager s'y connecte en HTTPS :
  1. sans certificat client, il lit l'identite du switch (/hello) et demande
     l'appairage (/pair) en envoyant son autorite et son certificat ;
  2. l'admin du switch tape 'central accept <code>' avec le code a 6 chiffres
     affiche par TL-Manager. Le code est calcule a partir des deux empreintes :
     un intermediaire qui s'interposerait obtiendrait un autre code ;
  3. ensuite seul ce TL-Manager (certificat client epingle) peut piloter le switch.

Ce module est charge de deux facons :
  - par la CLI : tlswitch.py appelle register(module) pour ajouter les commandes ;
  - par le service tlswitch-central : python3 tlswitch_central.py serve
"""

import hashlib
import hmac
import http.server
import io
import ipaddress
import json
import os
import re
import secrets
import socket
import socketserver
import ssl
import subprocess
import sys
import threading
import time

ETC = os.environ.get("TLSWITCH_ETC", "/etc/tlswitch")
DIR = os.path.join(ETC, "central")
CONF = os.path.join(DIR, "central.json")         # {"port": 2507, "allow": ["192.0.2.10"]}
KEY = os.path.join(DIR, "switch.key")
CRT = os.path.join(DIR, "switch.crt")
MGR = os.path.join(DIR, "manager.json")          # TL-Manager appaire
MGR_CA = os.path.join(DIR, "manager-ca.crt")
RUN = os.path.join(os.environ.get("TLSWITCH_RUN", "/run/tlswitch"), "central")
PENDING = os.path.join(RUN, "attente.json")      # demandes d'appairage (10 min)
PORT = 2507
PENDING_TTL = 600
REVERT = 120
UNIT = "tlswitch-central.service"


# ---------------------------------------------------------------------------
# Outils partages (CLI et service)
# ---------------------------------------------------------------------------

def read_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path, data):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)


def conf():
    c = {"port": PORT, "allow": []}
    c.update(read_json(CONF, {}))
    return c


def fp_pem(pem):
    return hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest()


def pair_code(switch_fp, manager_fp):
    """Code a 6 chiffres lie aux deux certificats (meme calcul cote TL-Manager)."""
    h = hashlib.sha256(("tl-central|%s|%s" % (switch_fp, manager_fp)).encode()).digest()
    return "%06d" % (int.from_bytes(h[:8], "big") % 1000000)


def fmt_fp(fp):
    return ":".join(fp[i:i + 2] for i in range(0, 32, 2)).upper() + "..."


def ensure_cert(hostname):
    if os.path.exists(KEY) and os.path.exists(CRT):
        return
    os.makedirs(DIR, mode=0o700, exist_ok=True)
    r = subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
                        "-nodes", "-days", "3650", "-subj", "/CN=%s/O=TL-SwitchOS central" % hostname,
                        "-addext", "basicConstraints=critical,CA:FALSE",
                        "-addext", "keyUsage=critical,digitalSignature",
                        "-addext", "extendedKeyUsage=serverAuth",
                        "-keyout", KEY + ".tmp", "-out", CRT + ".tmp"], capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError("openssl : " + r.stderr.strip()[-200:])
    os.chmod(KEY + ".tmp", 0o600)
    os.replace(KEY + ".tmp", KEY)
    os.replace(CRT + ".tmp", CRT)


def pending_list():
    now = time.time()
    return [p for p in read_json(PENDING, []) if now - p.get("ts", 0) < PENDING_TTL]


def systemctl(*args):
    return subprocess.run(["systemctl"] + list(args), capture_output=True, text=True)


# ---------------------------------------------------------------------------
# Commandes CLI (enregistrees dans la CLI de tlswitch.py)
# ---------------------------------------------------------------------------

def register(T):
    """Ajoute les commandes 'central ...' et 'show central' a la CLI du module T."""
    EXEC, CliError, priv15 = T.EXEC, T.CliError, T.priv15
    for k, v in (("central", "Gestion centrale (TL-Manager)"), ("accept", "Valider un appairage"),
                 ("allow", "Adresses autorisees")):
        T.KW_HELP.setdefault(k, v)

    @EXEC.add("show central", "Gestion centrale TL-Manager : etat, appairage")
    def x_show_central(s):
        c = conf()
        on = systemctl("is-active", UNIT).stdout.strip() == "active"
        s.say("Gestion centrale : %s (port %d)" % ("activee" if on else "desactivee", c["port"]))
        s.say("Adresses autorisees : %s" % (", ".join(c["allow"]) or "toutes"))
        if os.path.exists(CRT):
            s.say("Empreinte du switch : %s" % fmt_fp(fp_pem(open(CRT).read())))
        m = read_json(MGR, None)
        if m:
            last = read_json(os.path.join(RUN, "contact.json"), {})
            s.say("TL-Manager appaire : %s (%s) depuis le %s" % (m.get("nom", "?"), m.get("ip", "?"), m.get("depuis", "?")))
            s.say("  empreinte %s" % fmt_fp(m.get("fp", "")))
            if last.get("ts"):
                s.say("  dernier contact : %s depuis %s" % (time.strftime("%d/%m %H:%M:%S", time.localtime(last["ts"])),
                                                          last.get("ip", "?")))
        else:
            s.say("TL-Manager appaire : aucun")
        p = pending_list()
        if p:
            s.say("")
            s.say("Demandes d'appairage en attente (tape 'central accept <code>' avec le code affiche par TL-Manager) :")
            for d in p:
                s.say("  %s  %-16s %s" % (time.strftime("%H:%M:%S", time.localtime(d["ts"])), d["ip"], d.get("nom", "")))

    @EXEC.add("central enable", "Activer la gestion par TL-Manager (port 2507)")
    @priv15
    def x_central_enable(s):
        _enable(s, None)

    @EXEC.add("central enable port <port>", "Activer la gestion centrale sur un autre port")
    @priv15
    def x_central_enable_port(s, port):
        if not port.isdigit() or not 1024 <= int(port) <= 65535 or int(port) in (8443, 9100, 443):
            raise CliError("% Port invalide (1024-65535, hors ports deja utilises)")
        _enable(s, int(port))

    def _enable(s, port):
        c = conf()
        if port:
            c["port"] = port
        write_json(CONF, c)
        ensure_cert(s.m.hostname)
        r = systemctl("enable", "--now", UNIT)
        systemctl("restart", UNIT)
        if r.returncode:
            raise CliError("% Service non demarre : %s" % r.stderr.strip()[-200:])
        T.log("central : gestion centrale activee sur le port %d par %s" % (c["port"], s.user))
        s.say("Gestion centrale activee sur le port %d." % c["port"])
        s.say("Empreinte du switch : %s" % fmt_fp(fp_pem(open(CRT).read())))
        if not c["allow"]:
            s.say("% Conseil : limite l'acces avec 'central allow <adresse du TL-Manager>'")
        if not read_json(MGR, None):
            s.say("Ajoute maintenant le switch dans TL-Manager, puis tape 'central accept <code>'.")

    @EXEC.add("central disable", "Desactiver la gestion centrale (garde l'appairage)")
    @priv15
    def x_central_disable(s):
        systemctl("disable", "--now", UNIT)
        T.log("central : gestion centrale desactivee par %s" % s.user)
        s.say("Gestion centrale desactivee.")

    @EXEC.add("central allow <adresse>", "N'accepter que cette adresse (ou ce reseau) pour TL-Manager")
    @priv15
    def x_central_allow(s, addr):
        try:
            net = str(ipaddress.ip_network(addr, strict=False))
        except ValueError:
            raise CliError("% Adresse ou reseau invalide (ex : 192.0.2.10 ou 192.0.2.0/24)")
        c = conf()
        if net not in c["allow"]:
            c["allow"].append(net)
        write_json(CONF, c)
        systemctl("try-restart", UNIT)
        s.say("Adresses autorisees : %s" % ", ".join(c["allow"]))

    @EXEC.add("central allow any", "Accepter TL-Manager depuis toute adresse")
    @priv15
    def x_central_allow_any(s):
        c = conf()
        c["allow"] = []
        write_json(CONF, c)
        systemctl("try-restart", UNIT)
        s.say("Adresses autorisees : toutes")

    @EXEC.add("central accept <code>", "Valider l'appairage avec le code affiche par TL-Manager")
    @priv15
    def x_central_accept(s, code):
        try:
            d = accept(code, s.user, T.log)
        except ValueError as e:
            raise CliError("% " + str(e))
        s.say("TL-Manager %s (%s) appaire. Il gere maintenant ce switch." % (d.get("nom", ""), d["ip"]))

    @EXEC.add("central forget", "Oublier le TL-Manager appaire")
    @priv15
    def x_central_forget(s):
        for f in (MGR, MGR_CA):
            try:
                os.remove(f)
            except OSError:
                pass
        systemctl("try-restart", UNIT)
        T.log("central : appairage supprime par %s" % s.user)
        s.say("Appairage supprime. Le TL-Manager ne peut plus gerer ce switch.")



# ---------------------------------------------------------------------------
# API pour l'interface web du switch (tlswitch_web.py)
# ---------------------------------------------------------------------------

def status():
    """Etat de la gestion centrale, pour l'interface web (GET)."""
    c = conf()
    m = read_json(MGR, None)
    last = read_json(os.path.join(RUN, "contact.json"), {})
    return {
        "active": systemctl("is-active", UNIT).stdout.strip() == "active",
        "port": c["port"], "allow": c["allow"],
        "empreinte": fmt_fp(fp_pem(open(CRT).read())) if os.path.exists(CRT) else "",
        "manager": {"nom": m.get("nom"), "ip": m.get("ip"), "depuis": m.get("depuis"),
                    "empreinte": fmt_fp(m.get("fp", "")), "contact": last.get("ts")} if m else None,
        "attente": [{"ip": d["ip"], "nom": d.get("nom", ""), "ts": d["ts"]} for d in pending_list()],
    }


def accept(code, user, log=None):
    """Valide la demande d'appairage dont le code correspond. ValueError sinon."""
    code = str(code).strip()
    if not re.fullmatch(r"\d{6}", code):
        raise ValueError("Le code fait 6 chiffres")
    for d in pending_list():
        if hmac.compare_digest(d["code"], code):
            with open(MGR_CA + ".tmp", "w") as f:
                f.write(d["ca"])
            os.chmod(MGR_CA + ".tmp", 0o600)
            os.replace(MGR_CA + ".tmp", MGR_CA)
            write_json(MGR, {"nom": d.get("nom", "TL-Manager"), "ip": d["ip"], "fp": d["fp"],
                             "depuis": time.strftime("%d/%m/%Y %H:%M")})
            write_json(PENDING, [])
            systemctl("try-restart", UNIT)
            if log:
                log("central : TL-Manager %s (%s) appaire par %s" % (d.get("nom", ""), d["ip"], user))
            return d
    raise ValueError("Aucune demande avec ce code (expiree apres 10 min ? verifie avec 'show central')")


# ---------------------------------------------------------------------------
# Service (port 2507)
# ---------------------------------------------------------------------------

LOCK = threading.Lock()
CONSOLES = {}       # console -> (Session, derniere utilisation)
FAILS = {}


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "TL-SwitchOS-central"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def send(self, code, body, ctype="application/json"):
        data = (json.dumps(body) if ctype == "application/json" else body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def body(self, limit=1 << 20):
        n = int(self.headers.get("Content-Length") or 0)
        if n < 0 or n > limit:
            raise ValueError("requete trop grosse")
        d = json.loads(self.rfile.read(n) or b"{}") if n else {}
        if not isinstance(d, dict):
            raise ValueError("JSON attendu")
        return d

    def ip(self):
        a = self.client_address[0]
        return a[7:] if a.startswith("::ffff:") else a

    def manager(self):
        """Vrai si la connexion vient du TL-Manager appaire (certificat client epingle)."""
        m = read_json(MGR, None)
        if not m:
            return False
        try:
            der = self.connection.getpeercert(binary_form=True)
        except (AttributeError, ValueError):
            return False
        return bool(der) and hmac.compare_digest(hashlib.sha256(der).hexdigest(), m.get("fp", ""))

    # -- GET --------------------------------------------------------------
    def do_GET(self):
        T, W = self.server.T, self.server.W
        if self.path == "/central/v1/hello":
            m, _ = T.load_running()
            return self.send(200, {"produit": "TL-SwitchOS", "hostname": m.hostname, "version": T.VERSION,
                                   "appaire": bool(read_json(MGR, None)), "gere": self.manager()})
        if not self.manager():
            return self.send(403, {"error": "TL-Manager non appaire"})
        self.contact()
        try:
            if self.path == "/central/v1/etat":
                st = W.state(15)
                try:
                    st["lldp"] = T.lldp_rows()
                except Exception:
                    st["lldp"] = []
                try:
                    st["maj"] = T.maj_etat()
                except Exception:
                    st["maj"] = {}
                st["config_sha"] = hashlib.sha256(T.load_running()[0].render().encode()).hexdigest()
                return self.send(200, st)
            if self.path == "/central/v1/config":
                return self.send(200, T.load_running()[0].render(), "text/plain")
        except Exception as e:
            T.log("central : erreur %r" % e)
            return self.send(500, {"error": "erreur interne (voir show logging)"})
        self.send(404, {"error": "introuvable"})

    # -- POST -------------------------------------------------------------
    def do_POST(self):
        T = self.server.T
        try:
            if self.path == "/central/v1/pair":
                return self.pair()
            if not self.manager():
                return self.send(403, {"error": "TL-Manager non appaire"})
            self.contact()
            d = self.body(4 << 20)
            if self.path == "/central/v1/exec":
                return self.send(200, self.exec_line(d))
            if self.path == "/central/v1/config":
                return self.send(200, self.config(d))
            if self.path == "/central/v1/confirmer":
                if T.rollback_pending() is not None:
                    T.rollback_cancel()
                    T.log("central : modification confirmee (TL-Manager toujours joignable)")
                return self.send(200, {"ok": True})
            if self.path == "/central/v1/enregistrer":
                c = T.Session(T.load_running()[0], user="tl-manager")
                c.out = io.StringIO()
                T._write(c)
                return self.send(200, {"ok": True, "sortie": c.out.getvalue()})
            if self.path == "/central/v1/maj":
                st = T.maj_etat()
                if st.get("etat") == "en_cours":
                    return self.send(200, {"error": "une mise a jour est deja en cours"})
                T.log("central : mise a jour par le depot lancee par TL-Manager (%s)" % d.get("user", ""))
                T.maj_lancer_detache(["--depot"])
                return self.send(200, {"ok": True})
        except (ValueError, KeyError, TypeError) as e:
            return self.send(400, {"error": "requete invalide (%s)" % e})
        except Exception as e:
            T.log("central : erreur %r" % e)
            return self.send(500, {"error": "erreur interne (voir show logging)"})
        self.send(404, {"error": "introuvable"})

    def contact(self):
        try:
            write_json(os.path.join(RUN, "contact.json"), {"ts": time.time(), "ip": self.ip()})
        except OSError:
            pass

    def pair(self):
        T = self.server.T
        ip = self.ip()
        now = time.time()
        with LOCK:
            if len(FAILS) > 10000:               # memoire bornee
                FAILS.clear()
            n, t0 = FAILS.get(ip, (0, now))
            if now - t0 > 600:
                n, t0 = 0, now
            FAILS[ip] = (n + 1, t0)
        if n >= 10:
            return self.send(429, {"error": "trop de demandes, reessaie dans 10 minutes"})
        d = self.body(64 << 10)
        ca, cert = str(d.get("ca", "")), str(d.get("cert", ""))
        nom = re.sub(r"[^A-Za-z0-9 ._-]", "", str(d.get("nom", "TL-Manager")))[:40] or "TL-Manager"
        try:
            mfp = fp_pem(cert)
            fp_pem(ca)
        except (ValueError, ssl.SSLError):
            return self.send(400, {"error": "certificats invalides"})
        # le certificat client doit bien etre signe par l'autorite envoyee
        r = _verify(ca, cert)
        if not r:
            return self.send(400, {"error": "le certificat client n'est pas signe par l'autorite fournie"})
        code = pair_code(fp_pem(open(CRT).read()), mfp)
        with LOCK:
            lst = [p for p in pending_list() if p["ip"] != ip][-4:]
            lst.append({"ip": ip, "nom": nom, "ts": now, "fp": mfp, "ca": ca, "code": code})
            write_json(PENDING, lst)
        T.log("central : demande d'appairage de %s (%s), a valider par 'central accept <code>'" % (nom, ip))
        self.send(200, {"attente": True})

    def exec_line(self, d):
        T = self.server.T
        priv = 15 if d.get("priv") == 15 else 1
        key = "%s|%s" % (str(d.get("console", ""))[:64], priv)
        user = re.sub(r"[^A-Za-z0-9._-]", "", str(d.get("user", "")))[:32] or "tl-manager"
        with LOCK:
            now = time.time()
            for k in [k for k, v in CONSOLES.items() if now - v[1] > 1800]:
                CONSOLES.pop(k, None)
            c = CONSOLES.get(key, (None, 0))[0]
            if c is None:
                c = T.Session(T.load_running()[0], priv=priv, user="tl-manager:" + user)
                c.mode = "exec"
            CONSOLES[key] = (c, now)
        c.out = io.StringIO()
        c.armed = False
        line = str(d.get("ligne", ""))[:500].strip()
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
        if line and not line.endswith("?"):
            T.log("central : %s (TL-Manager) : %s" % (user, line if not T.SENSIBLE.search(line) else "<secret>"))
        return {"sortie": c.out.getvalue(), "prompt": c.prompt(),
                "rollback": T.rollback_pending() if getattr(c, "armed", False) else None}

    def config(self, d):
        T = self.server.T
        lines = d.get("lignes") or []
        if not isinstance(lines, list) or len(lines) > 2000:
            raise ValueError("liste de commandes invalide")
        s = T.Session(T.load_running()[0], priv=15, user="tl-manager")
        s.mode = "config"
        s.out = io.StringIO()
        errors, armed = [], False
        for line in lines:
            line = str(line).rstrip()
            if not line.strip() or line.strip().startswith("!"):
                continue
            try:
                T.execute(s, line.strip(), remote=True, auto_revert=REVERT)
                armed = armed or getattr(s, "armed", False)
            except T.CliError as e:
                errors.append("%s -> %s" % (line.strip(), str(e).lstrip("% ")))
            except (ValueError, KeyError) as e:
                errors.append("%s -> valeur invalide (%s)" % (line.strip(), e))
        T.log("central : %d commande(s) poussee(s) par TL-Manager (%s)" % (len(lines), d.get("user", "")))
        if d.get("enregistrer") and not errors:
            c = T.Session(T.load_running()[0], user="tl-manager")
            c.out = io.StringIO()
            T._write(c)
            s.say(c.out.getvalue().rstrip())
        return {"sortie": s.out.getvalue(), "erreurs": errors, "rollback": T.rollback_pending() if armed else None}


def _verify(ca, cert):
    import tempfile
    with tempfile.TemporaryDirectory() as t:
        a, b = os.path.join(t, "ca.pem"), os.path.join(t, "c.pem")
        open(a, "w").write(ca)
        open(b, "w").write(cert)
        r = subprocess.run(["openssl", "verify", "-purpose", "sslclient", "-CAfile", a, b],
                           capture_output=True, text=True, timeout=10)
        return r.returncode == 0


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    address_family = socket.AF_INET6

    def server_bind(self):
        try:
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        except OSError:
            pass
        super().server_bind()

    def get_request(self):
        while True:
            sock, addr = self.socket.accept()
            a = addr[0][7:] if addr[0].startswith("::ffff:") else addr[0]
            if allowed(a, self.allow):
                break
            sock.close()
        sock.settimeout(60)
        try:
            # poignee de main TLS faite dans le thread du client : un client lent ou muet
            # ne bloque plus les autres connexions
            return self.ctx().wrap_socket(sock, server_side=True, do_handshake_on_connect=False), addr
        except (ssl.SSLError, OSError):
            sock.close()
            raise OSError("poignee de main TLS refusee")

    def handle_error(self, request, client_address):
        pass

    def ctx(self):
        c = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        c.minimum_version = ssl.TLSVersion.TLSv1_2
        c.load_cert_chain(CRT, KEY)
        if os.path.exists(MGR_CA) and read_json(MGR, None):
            c.load_verify_locations(MGR_CA)
            c.verify_mode = ssl.CERT_OPTIONAL     # /hello et /pair sans certificat client
        return c


def allowed(ip, nets):
    if not nets:
        return True
    try:
        a = ipaddress.ip_address(ip)
        return any(a in ipaddress.ip_network(n, strict=False) for n in nets)
    except ValueError:
        return False


def serve():
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import tlswitch as T              # noqa: E402
    import tlswitch_web as W          # noqa: E402
    c = conf()
    ensure_cert(T.load_running()[0].hostname)
    os.makedirs(RUN, mode=0o700, exist_ok=True)
    try:
        srv = Server(("::", int(c["port"])), Handler)
    except OSError:
        Server.address_family = socket.AF_INET
        srv = Server(("0.0.0.0", int(c["port"])), Handler)
    srv.T, srv.W, srv.allow = T, W, c["allow"]
    T.log("central : service a l'ecoute sur le port %d" % c["port"])
    srv.serve_forever()


if __name__ == "__main__":
    if sys.argv[1:2] == ["serve"]:
        serve()
    else:
        print(__doc__)
