#!/bin/bash
# Test des mises a jour sur un vrai noyau : 1.0.0 -> 2.0.0, paquets pieges, echec + retour auto, rollback.
SW=${1:-$(cd "$(dirname "$0")/.." && pwd)}; M=/tmp/mt; rm -rf $M; mkdir -p $M
ok(){ echo "OK    - $1"; }; ko(){ echo "ECHEC - $1"; FAIL=1; }
export TLSWITCH_PORTS="ge1 ge2 ge3 ge4" TLSWITCH_RUN=$M/run TLSWITCH_ETC=$M/etc TLSWITCH_VAR=$M/var TLSWITCH_LIB=$M/lib TLSWITCH_NO_DAEMON=1 TLSWITCH_NO_REBOOT=1 TLSWITCH_NO_APT=1
for i in 1 2 3 4; do ip link add ge$i type veth peer name p$i; ip link set p$i up; done
# depot de travail + cle
cp -r $SW $M/depot; rm -rf $M/depot/cles $M/depot/tlswitch/maj-publique.pem $M/depot/*.tlsu*
sed -i 's/^VERSION = "[^"]*"/VERSION = "1.0.0"/' $M/depot/tlswitch/tlswitch.py   # les tests partent toujours de 1.0.0
(cd $M/depot && bash fabriquer-maj.sh "test" >/dev/null 2>&1)   # cree la cle (paquet 1.0.0 jete)
rm -f $M/depot/*.tlsu*
# switch installe en 1.0.0
cp -r $M/depot/tlswitch $M/lib; mkdir -p $M/etc; cp $M/depot/cles/maj-publique.pem $M/etc/maj.pub
cat > $M/etc/startup.conf <<'C'
hostname SW-TEST
vlan 20
 name SERVEURS
interface Gi1/0/1
 switchport mode trunk
 switchport trunk allowed vlan 1,20
interface Gi1/0/2
 switchport access vlan 20
interface Vlan1
 ip address 192.0.2.10 255.255.255.0
C
python3 $M/lib/tlswitch.py boot; sleep 1
AVANT=$(bridge vlan show | sort | md5sum)
T(){ python3 $M/lib/tlswitch.py "$@"; }
[ "$(T version)" = 1.0.0 ] && ok "switch demarre en 1.0.0" || ko "version initiale"

mk(){ # mk VERSION [patch-python]  -> fabrique un paquet signe
  rm -rf $M/v; cp -r $M/depot $M/v
  sed -i "s/^VERSION = .*/VERSION = \"$1\"/" $M/v/tlswitch/tlswitch.py
  [ -n "${2:-}" ] && python3 -c "$2" $M/v/tlswitch/tlswitch.py
  (cd $M/v && bash fabriquer-maj.sh "Version $1 de test" >/dev/null 2>&1); cp $M/v/TL-SwitchOS-$1.tlsu $M/
}
mk 2.0.0
# 1. paquet modifie apres signature
mkdir $M/x; tar xf $M/TL-SwitchOS-2.0.0.tlsu -C $M/x; echo pirate >> $M/x/MANIFEST.json; tar cf $M/piege1.tlsu -C $M/x .
T upgrade $M/piege1.tlsu 2>&1 | grep -q "signature INVALIDE" && ok "manifeste modifie -> refuse" || ko "manifeste modifie accepte"
# 2. contenu remplace (manifeste intact)
rm -rf $M/x; mkdir $M/x; tar xf $M/TL-SwitchOS-2.0.0.tlsu -C $M/x; echo abc | gzip > $M/x/paquet.tar.gz; tar cf $M/piege2.tlsu -C $M/x .
T upgrade $M/piege2.tlsu 2>&1 | grep -q "altere" && ok "contenu remplace -> refuse" || ko "contenu remplace accepte"
# 3. signe avec une autre cle
rm -rf $M/autre; cp -r $M/depot $M/autre; rm -rf $M/autre/cles; sed -i 's/^VERSION = .*/VERSION = "2.0.0"/' $M/autre/tlswitch/tlswitch.py
(cd $M/autre && bash fabriquer-maj.sh x >/dev/null 2>&1)
T upgrade $M/autre/TL-SwitchOS-2.0.0.tlsu 2>&1 | grep -q "signature INVALIDE" && ok "autre cle -> refuse" || ko "autre cle acceptee"
# 4. fichier quelconque
echo bonjour > $M/faux.tlsu; T upgrade $M/faux.tlsu 2>&1 | grep -q "pas un paquet" && ok "fichier quelconque -> refuse" || ko "fichier quelconque"
[ "$(T version)" = 1.0.0 ] && ok "toujours en 1.0.0 apres les paquets pieges" || ko "version modifiee par un paquet piege"

echo "== mise a niveau 1.0.0 -> 2.0.0"
T upgrade --verifier $M/TL-SwitchOS-2.0.0.tlsu | grep -q '"action": "mise a niveau"' && ok "verification seule" || ko "verification"
T upgrade $M/TL-SwitchOS-2.0.0.tlsu; RC=$?
[ $RC = 0 ] && [ "$(T version)" = 2.0.0 ] && ok "2.0.0 active" || ko "mise a niveau (rc=$RC)"
[ "$(bridge vlan show | sort | md5sum)" = "$AVANT" ] && ok "VLAN du pont identiques apres la mise a jour" || ko "pont modifie"
grep -q '^VERSION = "1.0.0"' $M/var/precedente/tlswitch.py && ok "1.0.0 sauvegardee" || ko "sauvegarde"
grep -q '"etat": "redemarrage"' $M/var/maj.json && ok "redemarrage demande apres la mise a jour" || ko "etat redemarrage"
T boot; grep -q '"etat": "ok"' $M/var/maj.json && grep -q "Redemarrage reussi" $M/var/maj.json && ok "au redemarrage : mise a jour confirmee" || ko "confirmation au boot"
T upgrade $M/TL-SwitchOS-2.0.0.tlsu 2>&1 | grep -q "deja installee" && ok "reinstallation sans force -> refusee" || ko "reinstallation"

echo "== 3.0.0 cassee : l'application de la config echoue"
mk 3.0.0 "import sys; p=sys.argv[1]; s=open(p).read(); s=s.replace('    if cmd in (\"apply\", \"check\"):', '    if cmd == \"apply\":\n        sys.exit(\"panne simulee\")\n    if cmd in (\"apply\", \"check\"):',1); open(p,'w').write(s)"
T upgrade $M/TL-SwitchOS-3.0.0.tlsu; RC=$?
[ $RC = 3 ] && [ "$(T version)" = 2.0.0 ] && ok "3.0.0 cassee -> retour automatique en 2.0.0" || ko "retour auto (rc=$RC, version $(T version))"
[ "$(bridge vlan show | sort | md5sum)" = "$AVANT" ] && ok "pont intact apres l'echec" || ko "pont modifie apres echec"
grep -q "ANNULEE" $M/etc/historique-maj && ok "historique : mise a jour annulee notee" || ko "historique"

echo "== 3.0.0 avec une erreur de syntaxe"
rm -f $M/TL-SwitchOS-3.0.1.tlsu; mk 3.0.1 "import sys; p=sys.argv[1]; open(p,'a').write('\ndef (:\n')" 2>/dev/null
[ ! -f $M/TL-SwitchOS-3.0.1.tlsu ] && ok "fabriquer-maj refuse un code avec erreur de syntaxe" || ko "paquet syntaxe fabrique"
# meme chose cote switch : paquet signe fabrique a la main
rm -rf $M/s; mkdir -p $M/s/w; cp -r $M/depot/tlswitch $M/s/; sed -i 's/^VERSION = .*/VERSION = "3.0.2"/' $M/s/tlswitch/tlswitch.py; echo 'def (:' >> $M/s/tlswitch/tlswitch.py
tar czf $M/s/w/paquet.tar.gz -C $M/s tlswitch; SUM=$(sha256sum $M/s/w/paquet.tar.gz | cut -d' ' -f1)
printf '{"produit":"TL-SwitchOS","version":"3.0.2","version_min":"1.0.0","sha256":"%s"}' $SUM > $M/s/w/MANIFEST.json
openssl pkeyutl -sign -inkey $M/depot/cles/maj-privee.pem -rawin -in $M/s/w/MANIFEST.json -out $M/s/w/MANIFEST.sig
tar cf $M/s.tlsu -C $M/s/w MANIFEST.json MANIFEST.sig paquet.tar.gz
T upgrade $M/s.tlsu 2>&1 | grep -q "contient une erreur" && [ "$(T version)" = 2.0.0 ] && ok "switch : erreur de syntaxe detectee avant installation" || ko "syntaxe cote switch"
grep -q '"1.0.0"' $M/var/precedente/tlswitch.py && ok "la precedente reste 1.0.0 apres les echecs" || ko "precedente ecrasee"

echo "== install rollback (2.0.0 -> 1.0.0)"
T upgrade --retour; [ "$(T version)" = 1.0.0 ] && grep -q '"2.0.0"' $M/var/precedente/tlswitch.py && ok "retour en 1.0.0, 2.0.0 gardee" || ko "rollback"
T upgrade $M/TL-SwitchOS-2.0.0.tlsu >/dev/null && [ "$(T version)" = 2.0.0 ] && ok "re-mise a niveau en 2.0.0" || ko "re-mise a niveau"
mk 1.5.0; T upgrade $M/TL-SwitchOS-1.5.0.tlsu 2>&1 | grep -q "deja installee ou plus recente" && ok "version plus ancienne refusee sans force" || ko "downgrade"

echo "== secours : la version attendue n'a pas demarre"
python3 -c "import json;d=json.load(open('$M/var/maj.json'));d['etat']='redemarrage';d['attendue']='9.9.9';json.dump(d,open('$M/var/maj.json','w'))"
T boot; grep -q '"etat": "annulee"' $M/var/maj.json && ok "au redemarrage : echec detecte et signale" || ko "detection echec au boot"

echo "== mise a jour MAJEURE (Debian simule)"
mkmaj(){ rm -rf $M/v; cp -r $M/depot $M/v; sed -i "s/^VERSION = .*/VERSION = \"$1\"/" $M/v/tlswitch/tlswitch.py
  printf '#!/bin/bash\necho "faux apt : Debian -> $TLS_CODENAME"\nexit %s\n' "$2" > $M/v/tlswitch/os-upgrade.sh
  (cd $M/v && bash fabriquer-maj.sh --majeure forky "Debian 14" >/dev/null 2>&1); cp $M/v/TL-SwitchOS-$1.tlsu $M/; }
mkmaj 4.0.0 0
T upgrade --verifier $M/TL-SwitchOS-4.0.0.tlsu | grep -q '"type": "majeure"' && ok "paquet majeur reconnu" || ko "type majeure"
T upgrade $M/TL-SwitchOS-4.0.0.tlsu > $M/maj4.log 2>&1; RC=$?
[ $RC = 0 ] && [ "$(T version)" = 4.0.0 ] && grep -q "faux apt : Debian -> forky" $M/maj4.log && ok "majeure : TL-SwitchOS 4.0.0 + script Debian execute" || { ko "majeure (rc=$RC)"; tail -5 $M/maj4.log; }
ls $M/var/sauvegarde-*.tar.gz >/dev/null 2>&1 && ok "configuration sauvegardee avant Debian" || ko "sauvegarde config"
T boot >/dev/null 2>&1
mkmaj 5.0.0 1
T upgrade $M/TL-SwitchOS-5.0.0.tlsu > $M/maj5.log 2>&1; RC=$?
[ $RC = 3 ] && grep -q '"etat": "echec"' $M/var/maj.json && ! grep -q "Redemarrage du switch" $M/maj5.log && ok "majeure : echec Debian -> signale, pas de redemarrage" || { ko "echec majeure (rc=$RC)"; tail -5 $M/maj5.log; }

echo "== CLI"
printf 'show install summary\nshow version\ndir flash:\n' | T cli 2>&1 | grep -E 'Version|Previous|->|Directory|tlsu' | head -12
cat $M/etc/historique-maj
[ -z "$FAIL" ] && echo "TOUS LES TESTS MAJ OK" || echo "DES TESTS ONT ECHOUE"
