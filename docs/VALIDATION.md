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

La validation réelle du téléchargement public doit être effectuée après la
publication par le CI. L'utilisateur doit installer 0.7 une première fois
avant de pouvoir utiliser ce bouton pour une version ultérieure.

## Contrôles automatisés

Le workflow Windows construit le commit de la branche, démarre l'exécutable
portable, installe le logiciel puis démarre l'exécutable installé. Chaque
démarrage doit fournir un rapport JSON positif avec la version attendue.

Les tests des protocoles, connexions et stratégies emploient des données
synthétiques. Les tests de reconnaissance utilisent aussi des géométries
synthétiques pour vérifier les cas ambigus et les mauvaises correspondances.

## Essais à poursuivre

- Version0.7 : ouverture du panneau de mises à jour et conservation des réglages.
- Installation d'une version ultérieure par ce mécanisme.
- Proposition du circuit réellement parcouru.
- Réception depuis un autre PC, puis depuis un autre domicile.
- Quatre PS5 et validation des pertes/reprises de signal pendant une séance.
