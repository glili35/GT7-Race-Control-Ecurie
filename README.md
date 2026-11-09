# GT7 Race Control Écurie — 0.6.0 Test

Un poste ingénieur pour réunir les télémétries de **quatre pilotes GT7**, chacun
avec sa PlayStation et son PC Windows. La même application propose un mode
pilote, un mur des stands et une démonstration clairement identifiée.

![Aperçu de l'interface avec quatre voitures et un circuit fictifs](docs/images/demo.png)

## Télécharger et lancer sur Windows

1. Ouvrir l'onglet **Actions** du dépôt et la dernière exécution verte
   **Vérifier et construire GT7 Race Control**, sur la branche `codex/race-control-0.5`.
2. Dans **Artifacts**, télécharger `GT7-Race-Control-Ecurie-Windows-0.6.0-test`.
3. Décompresser le téléchargement et lancer
   `GT7-Race-Control-Ecurie-Setup-0.6.0-test.exe`.
4. Ouvrir **GT7 Race Control Écurie (Test)** depuis le menu Démarrer.

Python et les bibliothèques sont inclus. L'installateur n'est pas signé.
Le canal Test utilise un dossier et un identifiant différents de la version
historique : il peut être essayé sans la désinstaller.
L'archive `Portable` permet aussi un lancement sans installation.

Le bouton **Démonstration** présente quatre voitures fictives et une carte fictive,
sans connexion PS5, transmission réseau ni enregistrement de session.
Un bandeau indique ce mode en permanence.

La version 0.6 met à jour le canal Test 0.5 en conservant son profil. La réception
locale d'une PS5 réelle a été confirmée par l'utilisateur avec la version 0.5.
Les nouvelles fonctions doivent encore être essayées avec son matériel.

## Premier essai avec une PS5

1. Sur le PC pilote, ouvrir **Poste pilote**.
2. Renseigner l'IPv4 de la PS5, sur le même réseau que le PC. Un champ vide
   active la recherche par diffusion UDP locale ; celle-ci dépend du réseau.
3. Laisser l'invitation vide pour commencer par un essai local.
4. Cocher l'enregistrement CSV, puis **Connecter la PS5**.
5. Dans GT7, rouler sur la piste. Le compteur doit augmenter et les données
   apparaître dans **Poste ingénieur**, qui sert aussi d'aperçu local.
6. Arrêter la connexion pour fermer le CSV. **Ouvrir le dossier des sessions**
   donne accès aux données enregistrées.

Autoriser l'application dans le pare-feu Windows sur le réseau privé si Windows
le demande. GT7 utilise UDP 33739 vers la PS5 et UDP 33740 en réception sur le PC.
Fermer les autres logiciels qui utilisent ces ports pendant l'essai.
L'état PS5 indique la réception de données valides ; l'état de transmission
indique séparément si le poste ingénieur reçoit les données.

## Vérifier toute la chaîne avec un seul PC

1. Ouvrir **Poste pilote** et renseigner l'IP de la PS5 comme pour l'essai précédent.
2. Arrêter une éventuelle connexion ou la démonstration.
3. Cliquer **Démarrer l'essai complet**, puis rouler dans GT7.
4. Vérifier que le statut affiche **Chaîne locale complète en direct**, puis
   retrouver la voiture dans **Poste ingénieur**.

L'application ouvre une réception chiffrée accessible uniquement sur ce PC et
crée son invitation automatiquement. Les données de la vraie PS5 traversent
le transport HTTPS avant d'alimenter le mur des stands : l'affichage ingénieur
ne reçoit pas de copie directe de la capture. Aucune donnée fictive n'est injectée.
L'invitation saisie pour l'équipe reste inchangée et les CSV gardent la fréquence
locale complète. Le bouton **Arrêter l'essai complet** ferme les deux côtés.

Cet essai vérifie la chaîne sur un seul ordinateur. Il ne valide pas l'accès
depuis un autre domicile, le routeur ou le pare-feu d'un autre PC.

## Connecter l'ingénieur et les quatre pilotes

Sur le poste ingénieur :

1. Renseigner une **adresse joignable par les pilotes** et le port, 8765 par défaut.
2. Cliquer **Ouvrir la réception**.
3. Choisir la voiture 1, puis **Copier l'invitation** et la transmettre au pilote 1.
4. Refaire l'opération avec les voitures 2, 3 et 4. Chaque invitation donne accès
   uniquement à sa voiture. **Renouveler l'accès** révoque son ancienne invitation.
5. Dans **Équipe et affichage**, nommer les pilotes. Sélectionner le circuit
   de la séance et télécharger son tracé s'il est disponible.

Sur chaque poste pilote, coller son invitation dans **Poste pilote**, renseigner
l'adresse locale de sa propre PS5, puis connecter. Le numéro distant vient de
l'invitation ; le sélecteur de voiture ne concerne que l'aperçu local.

Le bouton **Vérifier l'invitation** contrôle l'adresse, le certificat et l'accès
avant même le roulage. Il distingue invitation invalide/révoquée, certificat
différent et ingénieur injoignable, sans créer de fausse voiture connectée.
Les deux postes doivent utiliser 0.6 pour cette vérification ; le transport
de télémétrie existant reste compatible avec 0.5.

**Depuis plusieurs domiciles**, l'adresse de l'ingénieur doit être accessible
depuis Internet. Une adresse privée `192.168.*`, `10.*` ou `172.16–31.*` ne suffit
pas à elle seule. Une solution consiste à utiliser une adresse publique/DNS et
une redirection TCP du port choisi vers le PC ingénieur, avec le pare-feu adapté.
Avec un accès Internet sous CGNAT, il faut un relais hébergé ou un réseau privé
configuré séparément. Cette version ne provisionne aucun relais ni abonnement
et ne modifie pas le routeur automatiquement.

Les quatre flux voyagent en HTTPS. L'invitation contient l'empreinte du certificat
de l'ingénieur, vérifiée avant l'envoi du jeton d'accès. Les invitations et les
clés restent dans le profil local : ne pas les publier dans un ticket GitHub.

## Fonctions de cette version

- Mur des stands de quatre voitures, carte commune et numéros colorés.
- Disposition adaptée : à deux voitures, une de chaque côté ; à quatre, deux
  de chaque côté. Les voitures dont le signal est perdu restent identifiées.
- Vitesse, rapport, accélérateur vert, frein rouge, carburant en pourcentage,
  quatre températures de pneus, chronos, tour et position.
- Consommation moyenne et autonomie **indépendantes par voiture**, exprimées en
  pourcentage du réservoir. Premier tour partiel, ravitaillements et ruptures de
  séance sont exclus des estimations. Leur caractère provisoire est affiché.
- Réception locale et CSV à la fréquence des paquets, environ 60 Hz selon GT7.
  Transmission à l'ingénieur plafonnée à 10 Hz, sans bloquer la capture locale.
- Détection de perte de signal et masquage des mesures périmées.
- Catalogue embarqué de 122 configurations, dont 27 avec données de bordures
  dans ce catalogue ; bouton **Tous les tracés (27)** pour les installer ensemble.
  Les caches valides sont conservés et chaque échec est signalé séparément.
- Proposition du circuit à partir des coordonnées réelles, parmi les tracés
  installés. Les variantes géométriquement identiques restent à choisir.
- Noms des pilotes, rotation de carte et miroir horizontal enregistrables.
  Le miroir est activé par défaut pour corriger le sens du tracé GT7 ; le même
  changement s'applique aux voitures, sans modifier leurs coordonnées réelles.
- Numéros de virage affichés lorsque la bibliothèque fournit leurs coordonnées :
  notamment 19 à Spa et 11 à Monza. Le bouton **Virages** sur la carte permet
  de les masquer. Les annotations disponibles couvrent actuellement 10 tracés ;
  aucun numéro n'est créé pour les tracés non annotés.
- Accès à l'ancienne interface V4, conservée pour comparaison.

Les points de bordure ne sont pas reliés arbitrairement : les fichiers de la
bibliothèque ne garantissent pas leur ordre. Les quatre pilotes doivent rouler
sur le circuit sélectionné.

Les points d'ancrage des numéros subissent le même miroir et la même rotation
que le tracé et les voitures. Les pastilles sont décalées légèrement pour rester
lisibles et reliées à leur position par un trait.

![Spa : tracé en miroir et 19 virages annotés depuis les données du circuit](docs/images/spa-map.png)

Pour obtenir une proposition, installer les tracés, cocher **Reconnaître le
circuit**, puis parcourir plusieurs virages. La reconnaissance reste locale,
attend plusieurs positions espacées et n'invente pas les circuits absents.
Choisir la configuration proposée puis **Appliquer** ; en cas d'ambiguïté,
sélectionner la variante voulue dans la liste. Il n'y a pas de changement
automatique de carte sur une simple position.

Les couleurs des pneus sont indicatives. La stratégie carburant estime des
besoins sur les courses au nombre de tours ; elle ne décide pas d'un arrêt et ne
modélise pas encore la fin des courses chronométrées. Le déplacement libre des
widgets, les mises à jour automatiques et une publication stable restent à faire.

## Données et configuration

La nouvelle application stocke son profil dans
`%APPDATA%\GT7-Race-Control-Ecurie-Test` sous Windows. On y trouve les préférences,
les sessions CSV, les circuits téléchargés et l'identité de réception.
Le code source ne contient ni sessions personnelles ni adresse PS5 prédéfinie.
`GT7RC_DATA_DIR` permet de choisir un autre profil, notamment pour les tests.

L'ancienne interface V4 conserve son propre comportement de configuration.

## Développement

Python 3.12 et Tkinter sont utilisés pour le développement et la compilation.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
.\.venv\Scripts\python -m unittest discover -s tests -v
.\.venv\Scripts\python racecontrol_app.py
# Aperçu sans PS5 :
.\.venv\Scripts\python racecontrol_app.py --demo
```

Pour construire sous Windows : `./build_windows.ps1`, puis compiler `installer.iss`
avec Inno Setup 6. Le workflow GitHub effectue ces étapes automatiquement.

Les tests couvrent protocole, décodeur, stratégies, cache de circuits, cycle de
capture/CSV, transport TLS, reconnaissance et une chaîne UDP chiffrée → quatre
clients HTTPS → flotte ingénieur. Le contrôleur d'essai sur un seul PC est aussi
testé avec des sockets TLS réels. Ces tests utilisent des paquets synthétiques : **ce ne sont pas des
essais avec quatre PS5 réelles ni une validation Internet**.

La chaîne Windows vérifie aussi que l'EXE portable et l'EXE installé ouvrent
réellement l'interface et produisent un rapport de démarrage. La compilation
seule ne suffit pas à valider le logiciel en course.

Source du catalogue :
[GT7 Datalogger Track Data](https://github.com/jbhoorasingh/gt7-datalogger-track-data),
licence CC0 1.0 pour ces données. Le jeu et les marques appartiennent à leurs
propriétaires respectifs.
