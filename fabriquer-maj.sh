#!/bin/bash
# =============================================================================
#  TL-SwitchOS - fabrique un paquet de mise a jour signe
#
#     bash fabriquer-maj.sh "Nouveautes de la version (texte libre)"
#     bash fabriquer-maj.sh --majeure forky "TL-SwitchOS 2.0 sur Debian 14"
#                                     (mise a jour majeure : monte aussi Debian)
#
#  Resultat : TL-SwitchOS-<version>.tlsu (version lue dans tlswitch/tlswitch.py).
#  Sur le switch :  install add file usbflash0:TL-SwitchOS-2.0.0.tlsu activate
#             ou :  page Mise a jour de l'interface web (envoi du fichier)
#
#  La cle de signature est dans cles/ (creee au premier lancement). GARDE
#  cles/maj-privee.pem en lieu sur et HORS de GitHub : sans elle, plus de mise
#  a jour signee pour les switchs deja installes.
#  Variable optionnelle : TLS_VERSION_MIN=1.0.0 (version minimale requise)
# =============================================================================
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
PKG="$HERE/tlswitch"
TYPE=fonctionnalites; DEBIAN=""
if [ "${1:-}" = "--majeure" ]; then
  TYPE=majeure; DEBIAN="${2:?nom de code Debian cible, ex : forky}"; shift 2
fi
NOTES="${1:-}"
VERSION=$(python3 "$PKG/tlswitch.py" version)

# --- cle de signature ed25519 ---
cle() {
  install -d -m 700 "$HERE/cles"
  if [ ! -f "$HERE/cles/maj-privee.pem" ]; then
    openssl genpkey -algorithm ed25519 -out "$HERE/cles/maj-privee.pem"
    chmod 600 "$HERE/cles/maj-privee.pem"
    echo "==> Nouvelle cle de signature : $HERE/cles/maj-privee.pem (A SAUVEGARDER, jamais sur GitHub)"
  fi
  openssl pkey -in "$HERE/cles/maj-privee.pem" -pubout -out "$HERE/cles/maj-publique.pem"
  cp "$HERE/cles/maj-publique.pem" "$PKG/maj-publique.pem"
}
cle

echo "==> Paquet TL-SwitchOS $VERSION ($TYPE${DEBIAN:+, Debian $DEBIAN})"
W=$(mktemp -d); trap 'rm -rf "$W"' EXIT
python3 -m py_compile "$PKG/tlswitch.py" "$PKG/tlswitch_web.py"
tar czf "$W/paquet.tar.gz" --owner=0 --group=0 --exclude=__pycache__ --exclude='*.pyc' -C "$HERE" tlswitch
SUM=$(sha256sum "$W/paquet.tar.gz" | cut -d' ' -f1)
TLS_V="$VERSION" TLS_SUM="$SUM" TLS_NOTES="$NOTES" TLS_TYPE="$TYPE" TLS_DEB="$DEBIAN" python3 - > "$W/MANIFEST.json" <<'PY'
import json, os, time
print(json.dumps({"produit": "TL-SwitchOS", "version": os.environ["TLS_V"],
                  "version_min": os.environ.get("TLS_VERSION_MIN", "1.0.0"),
                  "date": time.strftime("%Y-%m-%d %H:%M"), "sha256": os.environ["TLS_SUM"],
                  "notes": os.environ.get("TLS_NOTES", ""), "type": os.environ["TLS_TYPE"],
                  "debian": os.environ.get("TLS_DEB", ""), "redemarrage": True}, indent=1, ensure_ascii=False))
PY
openssl pkeyutl -sign -inkey "$HERE/cles/maj-privee.pem" -rawin -in "$W/MANIFEST.json" -out "$W/MANIFEST.sig"
OUT="$HERE/TL-SwitchOS-$VERSION.tlsu"
tar cf "$OUT" --owner=0 --group=0 -C "$W" MANIFEST.json MANIFEST.sig paquet.tar.gz
sha256sum "$OUT" | tee "$OUT.sha256"
cat <<FIN

Paquet pret : $OUT
Sur le switch, au choix :
  - cle USB  : install add file usbflash0:$(basename "$OUT") activate
  - serveur  : install add file http://ton-pc:8000/$(basename "$OUT") activate
  - web      : menu Mise a jour > choisir le fichier
Le switch installe, verifie, puis REDEMARRE tout seul.
A publier sur GitHub : Releases > nouvelle release v$VERSION > joindre $(basename "$OUT")
Retour a la version precedente a tout moment : install rollback
FIN
