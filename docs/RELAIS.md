# Serveur relais optionnel — 0.8 Test

Le relais permet aux quatre postes pilotes et au poste ingénieur d'ouvrir
uniquement des connexions sortantes. Aucun serveur distant n'est provisionné par
l'application. Pour vérifier le parcours sans hébergement, utiliser **Relais local**
dans l'essai complet du poste pilote.

## Installation sur un serveur choisi par l'équipe

Utiliser Python 3.12 et les dépendances du dépôt dans un environnement virtuel.
Créer un répertoire privé pour les données et un fichier privé contenant un secret
aléatoire de 32 octets encodé en base64 URL sans remplissage. Depuis le répertoire
du projet, ce script prépare un dossier local `relay-private` et génère ce secret
sans l'afficher :

```python
from pathlib import Path
from racecontrol.relay_protocol import new_secret

directory = Path("relay-private")
directory.mkdir(mode=0o700)
with (directory / "bootstrap.txt").open("x", encoding="ascii") as stream:
    (directory / "bootstrap.txt").chmod(0o600)
    stream.write(new_secret())
```

Sur un hébergement Linux, lancer le service avec un compte dédié, puis placer
un frontal HTTPS devant son écoute locale :

```sh
python -m racecontrol.relay_server --data-dir relay-private/state --bootstrap-file relay-private/bootstrap.txt --host 127.0.0.1 --port 8766
```

Le frontal doit accepter les connexions WebSocket sur `/relay/v1`, conserver ce
chemin, transmettre vers `127.0.0.1:8766` et utiliser un certificat reconnu pour
son nom DNS. L'adresse dans l'application sera par exemple
`wss://relais.exemple.fr/relay/v1`. Le port 8766 doit rester accessible uniquement
sur le serveur lorsque le frontal est utilisé. Le frontal ne doit pas journaliser
les corps de messages WebSocket.

Le serveur peut aussi terminer TLS lui-même avec `--certfile` et `--keyfile`.
Le renouvellement du certificat et le redémarrage du service sont alors à gérer
par l'administrateur. Les clients vérifient le certificat et le nom du serveur ;
il n'existe pas d'option permettant d'ignorer cette vérification.

Lorsque le serveur Python termine TLS, les tickets de reprise TLS 1.3 sont
désactivés pour éviter un blocage initial observé dans les essais du transport
synchrone. TLS 1.3 reste activé, avec vérification du certificat et du nom côté
client. Un [signalement CPython](https://github.com/python/cpython/issues/137583)
décrit un symptôme proche de concurrence entre lecture et écriture SSL ; il
concerne un autre runtime et sert de contexte technique au contournement.

## Connexion de l'équipe

Transmettre au responsable ingénieur l'adresse WSS et, de manière privée, le
contenu du fichier `bootstrap.txt`. Le champ **Code d'accès ingénieur** permet
la création initiale d'une salle. Le profil ingénieur conserve ensuite son accès
propre à cette salle, ainsi que quatre clés de chiffrement différentes. Les
invitations peuvent être copiées aux pilotes depuis l'application.

Sauvegarder séparément les données privées du serveur et le profil de l'ingénieur.
Les premières conservent les accès des salles ; le second contient aussi les clés
nécessaires au déchiffrement. Restaurer uniquement le serveur ne restitue pas les
clés de l'ingénieur. Ne pas publier ces fichiers, les invitations ni le code d'accès
dans les journaux, captures ou tickets du projet.

## Comportement et limites

Le serveur transmet des mesures chiffrées et ne conserve aucun historique de
télémétrie. Il connaît les identifiants de salles et de voitures, les jetons
d'accès et les informations réseau des connexions, mais pas les clés des mesures.
Les accès restent dans `rooms.json` sous le répertoire de données privé.

Un poste ingénieur et un pilote par voiture peuvent être connectés à une même
salle. Le serveur limite les connexions à 64 et les salles à 16. Les files sont
bornées et le transport est plafonné à 10 mesures par seconde par pilote ; les CSV
locaux gardent leur fréquence de réception. Les nouvelles connexions ne reçoivent
aucune mesure mémorisée. Les challenges, séquences et sessions empêchent une
ancienne mesure de rafraîchir artificiellement le mur des stands.

`ws://127.0.0.1:PORT/relay/v1` et la boucle IPv6 sont admis uniquement pour un essai
sur le même PC. Une adresse WS de réseau local ou Internet est refusée. Le mode
direct HTTPS reste disponible et conserve son format d'invitation existant.

Les contrôles automatisés utilisent des sockets locaux et des données synthétiques.
Ils ne constituent pas une validation depuis plusieurs domiciles ou avec quatre
PS5 réelles.
