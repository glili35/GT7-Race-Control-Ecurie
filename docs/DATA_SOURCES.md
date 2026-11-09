# Données des circuits et des virages

Les fichiers embarqués proviennent du dépôt communautaire
[GT7 Datalogger Track Data](https://github.com/jbhoorasingh/gt7-datalogger-track-data),
au commit immuable
[`637aaa379e7b225e53034b9864b6f04f8bf2724e`](https://github.com/jbhoorasingh/gt7-datalogger-track-data/tree/637aaa379e7b225e53034b9864b6f04f8bf2724e).
Ils ne sont pas un plan officiel publié par Polyphony Digital ou par les circuits.

## Catalogue et annotations

`circuits_gt7/index.json` conserve les 122 configurations du catalogue source,
dont 32 disposent d'un relevé de bordures téléchargeable. Le fichier n'a pas été
réinterprété : son SHA256 est
`155c5db05a6f2a05bfa598d136e2405ff1556844edbf29d123a832a5aaab3bf0`.

`corners.json` associe les annotations à l'identifiant exact de chaque
configuration. Il contient les 162 ancres réellement publiées pour 12 tracés.
Chaque jeu conserve son URL de bundle à ce commit. Les numéros, noms et
coordonnées X/Z sont recopiés sans inventer les virages absents. Les
110 configurations sans annotation sont déclarées `missing`, ce qui empêche
de reprendre silencieusement une numérotation d'une autre variante ou d'un
ancien cache. Aucun numéro n'est transféré automatiquement à une variante inverse.

Sept jeux ont autant d'ancres que le nombre de virages déclaré par le catalogue :
Lago Maggiore Full Course (17), Monza (11), Daytona Road Course (12), Daytona
Tri-Oval (4), Mount Panorama (23), Sardegna Road Track A (15) et Suzuka East (9).
Le statut `documented` signifie que ces repères sont présents dans la source et
traçables. Il ne certifie pas une numérotation officielle.

Les cinq autres jeux portent le statut `partial`, avec une note explicite :

| Configuration | Ancres publiées | Virages du catalogue | Traitement |
| --- | ---: | ---: | --- |
| Lago Maggiore Centre | 7 | 8 | Aucun numéro ajouté. |
| Spa-Francorchamps | 19 | 21 | Les 19 repères correspondent au plan Spa déjà validé dans le projet ; la convention du catalogue diffère. |
| SUGO | 14 | 13 | Convention communautaire conservée, sans certification officielle. |
| Suzuka | 19 | 20 | Aucun numéro ajouté. |
| Tsukuba | 12 | 8 | Convention communautaire conservée, sans certification officielle. |

Le nombre du catalogue ne permet pas à lui seul de compléter, supprimer ou
renuméroter les annotations. Tous les points restent ceux de la source ; la
rotation et le miroir appliqués à l'affichage ne modifient pas ces coordonnées.

Les 162 ancres du relevé épinglé ont aussi été comparées aux points de bordure
de leur propre configuration : la distance maximale observée est de 11,5 m
(Spa). Le générateur refuse toute ancre située à plus de 50 m de ses propres
bordures. Ce contrôle détecte notamment une erreur de coordonnées ; il ne
certifie ni le numéro officiel ni l'exhaustivité des virages.

## Lignes de référence

`track_paths.json` fournit des lignes ordonnées en coordonnées GT7, issues de
[`signatures.json`](https://github.com/jbhoorasingh/gt7-datalogger-track-data/blob/637aaa379e7b225e53034b9864b6f04f8bf2724e/signatures.json).
Les relevés `edges` étant non ordonnés, ils ne servent pas à reconstruire ces
polylignes. Une ligne de référence n'indique ni la largeur de la piste ni ses
bordures.

La source contient 81 empreintes, dont 78 avec une ligne. Le filtre embarqué
retient 71 lignes directes et 33 variantes inverses explicitement liées par la
source, soit **104 configurations**. Une variante inverse reçoit les mêmes
coordonnées dans l'ordre opposé, avec son identifiant propre et un
`reverse_of_id` explicite. Ce lien ne donne aucun numéro de virage et ne prouve
pas à lui seul le sens réellement parcouru par un pilote.

Les neuf empreintes signalées par la source sont exclues du pack de lignes :

- Lago Maggiore Centre et East End : longueur différente du catalogue ; leurs variantes inverses sont également exclues.
- Barcelona National, Northern Isle, Spa 24h et Willow Streets : longueur différente du catalogue ; la variante inverse de Willow Streets est également exclue.
- Barcelona Rallycross, Red Bull Ring Short et Suzuka East : longueur provenant du catalogue, sans ligne ordonnée exploitable.

Nürburgring Endurance est aussi exclu : deux points voisins sont séparés de
246,8 m malgré un espacement annoncé de 20 m. Le générateur refuse tout saut
adjacent supérieur à 100 m, pour ne pas dessiner un raccord sans relevé.
Les lignes de Nürburgring 24h et de Yas Marina restent ouvertes : leur dernier
point n'est pas relié artificiellement au premier (écarts de 256,7 m et 128,4 m).
Une bordure réellement relevée et installée reste utilisable indépendamment de
ce filtre des lignes de référence.

Les 104 lignes et les 32 relevés de bordures couvrent ensemble 110
configurations distinctes. Les données ne constituent donc pas une couverture
intégrale de toutes les cartes ni de tous les virages de GT7.

## Licences et reconstruction

Les bordures, le catalogue et les annotations de virages de cette source sont
placés sous CC0 1.0. La notice originale complète est embarquée dans
`circuits_gt7/LICENSE-CC0.txt`.

Les lignes de référence utilisent les captures de
[`zetetos/gt-telemetry`](https://github.com/zetetos/gt-telemetry), sous licence MIT,
copyright 2024 vwhitteron. Cette provenance concerne **toutes** les lignes, y
compris celles dont le champ `provenance` est `survey` : le générateur source
emprunte leur ordre aux captures du dossier `vendor/`. Le texte MIT original est
conservé intégralement dans `circuits_gt7/LICENSE-MIT-reference-laps.txt` et doit
accompagner ces données lors de leur redistribution.

Le script local `scripts/build_circuit_data.py` reconstruit les fichiers sans
accès réseau. Le dossier source doit contenir l'index, les signatures, les
bundles `tracks/`, `LICENSE` et `vendor/gt-telemetry-LICENSE` de ce commit :

```sh
python scripts/build_circuit_data.py --source-dir SOURCE_LOCALE --output-dir circuits_gt7 --revision 637aaa379e7b225e53034b9864b6f04f8bf2724e
```

Le script conserve les identifiants exacts, refuse les coordonnées invalides,
les doublons et les ancres éloignées de leurs bordures, applique les exclusions
de qualité et recopie les notices. Les
fichiers téléchargés dans le profil d'un utilisateur ne sont jamais une entrée
de cette reconstruction.
