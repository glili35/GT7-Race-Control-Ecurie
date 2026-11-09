# Validations du projet

## Essai utilisateur — version 0.5.0 Test

L'utilisateur confirme : le compteur de paquets augmente et les données
apparaissent après connexion à sa PS5.

Ce résultat valide la réception et l'affichage local dans cette configuration.
Il ne valide pas quatre consoles simultanées, une connexion depuis un autre
domicile ou la précision des prévisions de stratégie.

L'utilisateur dispose actuellement d'un seul PC pour les essais. La version0.6
ajoute donc un mode permettant de faire traverser aux données de sa vraie PS5
la capture, la liaison chiffrée et le mur des stands sur ce même ordinateur.

## Essai utilisateur — version 0.6.0 Test

L'utilisateur confirme que l'essai complet fonctionne sur son PC. Cette
validation rapportée porte sur la PS5, la capture et la transmission chiffrée
vers le mur des stands sur un seul ordinateur. Elle ne valide pas les quatre
consoles ni la réception depuis un autre domicile.

## Mises à jour — version 0.7.0 Test

Le nouveau mécanisme recherche uniquement les publications du canal Test du
projet. La vérification et le téléchargement n'exécutent aucun programme.
L'installation est demandée par un bouton et reste interdite pendant une
capture ou une fermeture de CSV. Le fichier est vérifié une nouvelle fois
juste avant l'ouverture de l'assistant Windows.

Le CI a vérifié le téléchargement public de 0.7 avec le vrai client, sans
authentification, et les démarrages des EXE portable et installé sous Windows.
Le premier essai utilisateur de passage 0.7 → 0.8 révèle ensuite un refus
erroné après le téléchargement : « L'installateur a changé pendant sa
vérification ». La publication et le téléchargement étaient donc validés,
mais le lancement depuis le logiciel restait à corriger.

## Relais et états de séance — version 0.8.0 Test

Les tests utilisent un vrai serveur WebSocket local, quatre clients pilotes et
un client ingénieur. Ils vérifient l'isolation des voitures et des salles, les
invitations, la rotation des accès, les reconnexions, les séquences retardées et
la péremption des mesures. Les connexions et challenges seuls ne rafraîchissent
pas la flotte. Un essai WSS refuse un certificat inconnu puis accepte une
autorité de test explicitement reconnue, avec vérification du nom du serveur.

L'essai local vérifie la capture, le chiffrement, le relais puis l'ingénieur sans
copie directe vers la flotte distante. Les tests utilisent une capture contrôlée
et des profils temporaires ; ils ne constituent pas un essai avec une PS5 réelle.
L'interface Tk couvre aussi le démarrage et l'arrêt du relais, la copie et la
rotation des invitations, ainsi que les blocages pendant une mise à jour.

Les tests de séance vérifient les pauses, chargements, retours hors piste,
ravitaillements progressifs et horodatages CSV en présence d'une écriture retardée.
La pause conserve les mesures visibles sans apprendre de nouveaux tours ; le
chargement et le hors-piste masquent les valeurs et les marqueurs de carte.

Aucun relais public n'est déployé. L'essai **Relais local** avec la vraie PS5,
puis la connexion entre deux domiciles, restent à effectuer avec l'équipe.

## Contrôle de l'installateur — version 0.8.1 Test

Le diagnostic correspond au comportement de CPython 3.12 sous Windows :
`lstat(path).st_ctime_ns` représente la création du fichier, tandis que
`fstat(fd).st_ctime_ns` représente sa dernière modification de métadonnées.
Comparer directement ces deux valeurs peut donc refuser un fichier intact.
La taille, l'identité et l'empreinte restent à contrôler ; les dates de changement
doivent être comparées entre observations provenant de la même API.

Le correctif est préparé pour vérifier notamment une écriture après création,
suivie du renommage utilisé à la fin du téléchargement. Le CI Windows passe
l'installateur réellement construit par `launch_installer` avant son unique
installation silencieuse, puis contrôle le démarrage de l'EXE installé. Les
résultats du CI de 0.8.1 et du prochain essai utilisateur restent à confirmer.
Les démarrages portable/installé de 0.8 et le téléchargement public déjà
réussis ne valident pas à eux seuls cette étape du lanceur de mise à jour.

Le passage manuel unique à 0.8.1 est nécessaire depuis 0.7 ou 0.8, puisque
leur lanceur contient le défaut. Le chemin et l'identifiant du canal Test
restent identiques pour conserver le profil existant.

## Contrôles automatisés

Le workflow Windows construit le commit de la branche, démarre l'exécutable
portable, installe le logiciel puis démarre l'exécutable installé. Chaque
démarrage doit fournir un rapport JSON positif avec la version attendue.
Pour 0.8.1, le contrôle vérifie également la présence du runtime Python embarqué
dans `_internal`, puis limite le PATH aux répertoires Windows système avant le
lancement des deux EXE. Le PATH initial est restauré après chaque essai. Ce test
n'enlève pas le Python installé sur le runner ; il vérifie que les démarrages
n'ont pas besoin d'un interpréteur Python accessible dans PATH. Son résultat
doit être confirmé par le prochain CI Windows.

Les tests des protocoles, connexions et stratégies emploient des données
synthétiques. Les tests de reconnaissance utilisent aussi des géométries
synthétiques pour vérifier les cas ambigus et les mauvaises correspondances.

## Essais à poursuivre

- Installation manuelle de 0.8.1 et conservation des réglages du canal Test.
- Lancement d'une mise à jour suivante depuis le logiciel corrigé.
- Essai Relais local avec la vraie PS5, incluant pause, chargement et retour piste.
- Proposition du circuit réellement parcouru.
- Réception depuis un autre PC, puis depuis un autre domicile.
- Quatre PS5 et validation des pertes/reprises de signal pendant une séance.
