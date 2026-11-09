# Historique

## 0.7.0 Test

- Ajoute les mises à jour depuis le logiciel : recherche, présentation de la
  nouvelle version, téléchargement puis assistant d'installation Windows.
- Conserve les réglages, invitations, circuits et sessions du canal Test.
- Bloque l'installation tant qu'une capture, une réception ou la fermeture
  d'un fichier de session sont encore en cours.
- Permet d'annuler le téléchargement et conserve l'application ouverte en cas d'erreur.
- Publie les installateurs Test vérifiés avec un lien de téléchargement public,
  sans compte GitHub nécessaire pour les utilisateurs.

## 0.6.0 Test

- Ajoute un essai complet sur un seul PC avec la vraie PS5 : capture locale,
  transmission HTTPS puis affichage ingénieur, sans injection simulée.
- Vérifie une invitation avant la capture, sans rafraîchir la flotte.
- Installe en une opération les tracés disponibles et signale les échecs séparément.
- Propose un circuit parmi les tracés installés après plusieurs positions ;
  les variantes ambiguës restent à choisir.
- Préserve les réglages et le profil du canal Test 0.5.
- Corrige l'inversion gauche-droite du tracé et des marqueurs par un miroir
  horizontal activé par défaut, réglable dans Équipe et affichage.
- Affiche les numéros de virage à partir des annotations des circuits :
  19 à Spa, 11 à Monza et d'autres tracés disposant de coordonnées.
- Réserve le port UDP à une seule capture et fiabilise la découverte automatique
  sans changer le protocole ni le décodeur de la réception déjà validée.

## 0.5.0 Test

- Relie la capture GT7 existante à un poste ingénieur de quatre voitures.
- Ajoute un transport HTTPS avec invitations par voiture et certificat épinglé.
- Remplace le prototype d'affichage par un mur des stands, un mode pilote et
  une démonstration clairement identifiée.
- Corrige le champ carburant de la flotte et sépare les stratégies par voiture.
- Ajoute un enregistrement CSV asynchrone, sans décimation des paquets reçus.
- Fiabilise la bibliothèque de circuits et les données périmées après perte de signal.
- Conserve l'interface V4 et isole l'installation de test de l'installation historique.
- Vérifie les démarrages de l'application portable et installée dans le CI Windows.

Validation matérielle et connexion entre domiciles restent à effectuer avec l'équipe.

Réception locale PS5 confirmée par l'utilisateur : compteur de paquets en hausse
et données visibles dans l'interface 0.5.0. La réception entre domiciles n'a pas
encore été validée avec l'équipe.
