
"""
GT7 Race Control Ecurie
Moteur de strategie carburant en temps reel - V1.

Ce module ne gere ni le reseau ni l'interface.
Il recoit simplement les mesures de telemetrie.
"""

from collections import deque
from dataclasses import dataclass
import math


@dataclass
class EtatCarburant:
    carburant_l: float | None
    capacite_l: float | None
    consommation_l_tour: float | None
    autonomie_tours: float | None
    tours_mesures: int
    tours_restants: int | None
    carburant_necessaire_l: float | None
    message: str


class StrategieCarburant:

    def __init__(self, nombre_tours_moyenne=5):
        self.consommations = deque(
            maxlen=nombre_tours_moyenne
        )

        self.tour_actuel = None
        self.carburant_debut_tour = None
        self.carburant_precedent = None

        self.tour_debut_observe = False
        self.tour_avec_ravitaillement = False

        self.nombre_tours_mesures = 0

    @staticmethod
    def valeur(valeur):
        try:
            resultat = float(valeur)
            return resultat if math.isfinite(resultat) else None
        except (TypeError, ValueError):
            return None

    def mettre_a_jour(self, telemetry):
        carburant = self.valeur(
            getattr(telemetry, "fuel_level", None)
        )

        capacite = self.valeur(
            getattr(telemetry, "fuel_capacity", None)
        )

        tour = getattr(
            telemetry, "current_lap", None
        )

        total_tours = getattr(
            telemetry, "total_laps", None
        )

        if carburant is None or carburant < 0:
            return self.etat(
                None, capacite, tour, total_tours
            )

        try:
            tour = int(tour)
        except (TypeError, ValueError):
            tour = None
        # Detecter le ravitaillement AVANT le passage de ligne.
        ravitaillement_detecte = (
            self.carburant_precedent is not None
            and carburant - self.carburant_precedent > 0.5
        )

        if ravitaillement_detecte:
            self.tour_avec_ravitaillement = True
            self.consommations.clear()
            self.nombre_tours_mesures = 0

        # Initialisation : le premier tour est potentiellement
        # deja commence. Il ne sera pas mesure.
        if self.tour_actuel is None:
            self.tour_actuel = tour
            self.carburant_debut_tour = carburant
            self.tour_debut_observe = False

        elif tour is not None and tour != self.tour_actuel:

            # Passage normal au tour suivant.
            if (
                self.tour_actuel is not None
                and tour == self.tour_actuel + 1
            ):
                if (
                    self.tour_debut_observe
                    and not self.tour_avec_ravitaillement
                    and self.carburant_debut_tour is not None
                ):
                    consommation = (
                        self.carburant_debut_tour
                        - carburant
                    )

                    if consommation > 0.01:
                        self.consommations.append(
                            consommation
                        )
                        self.nombre_tours_mesures += 1

                self.tour_debut_observe = True

            else:
                # Saut de tour ou nouvelle session :
                # ne pas reutiliser l'historique precedent.
                self.consommations.clear()
                self.nombre_tours_mesures = 0
                self.tour_debut_observe = False

            self.tour_actuel = tour
            self.carburant_debut_tour = carburant
            self.tour_avec_ravitaillement = ravitaillement_detecte

        self.carburant_precedent = carburant

        return self.etat(
            carburant,
            capacite,
            tour,
            total_tours,
        )

    def etat(
        self,
        carburant,
        capacite,
        tour,
        total_tours,
    ):
        moyenne = None
        autonomie = None
        restants = None
        necessaire = None

        if self.consommations:
            moyenne = (
                sum(self.consommations)
                / len(self.consommations)
            )

            if carburant is not None and moyenne > 0:
                autonomie = carburant / moyenne

        try:
            tour = int(tour)
            total_tours = int(total_tours)

            if total_tours > 0 and tour > 0:
                # Le tour actuel est encore a parcourir
                # au moins partiellement.
                restants = max(
                    0,
                    total_tours - tour + 1,
                )
        except (TypeError, ValueError):
            pass

        if moyenne is not None and restants is not None:
            necessaire = moyenne * restants

        if moyenne is None:
            message = "CALCUL EN ATTENTE DE TOURS COMPLETS"

        elif self.nombre_tours_mesures < 2:
            message = "ESTIMATION PROVISOIRE"

        elif (
            necessaire is not None
            and carburant is not None
            and necessaire > carburant
        ):
            message = "ALERTE : CARBURANT INSUFFISANT"

        else:
            message = "STRATEGIE CARBURANT ACTIVE"

        return EtatCarburant(
            carburant_l=carburant,
            capacite_l=capacite,
            consommation_l_tour=moyenne,
            autonomie_tours=autonomie,
            tours_mesures=self.nombre_tours_mesures,
            tours_restants=restants,
            carburant_necessaire_l=necessaire,
            message=message,
        )
