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

## Contrôles automatisés

Le workflow Windows construit le commit de la branche, démarre l'exécutable
portable, installe le logiciel puis démarre l'exécutable installé. Chaque
démarrage doit fournir un rapport JSON positif avec la version attendue.

Les tests des protocoles, connexions et stratégies emploient des données
synthétiques. Les tests de reconnaissance utilisent aussi des géométries
synthétiques pour vérifier les cas ambigus et les mauvaises correspondances.

## Essais à poursuivre

- Version0.6 : essai complet sur un seul PC avec la vraie PS5.
- Téléchargement des tracés et proposition du circuit réellement parcouru.
- Réception depuis un autre PC, puis depuis un autre domicile.
- Quatre PS5 et validation des pertes/reprises de signal pendant une séance.
