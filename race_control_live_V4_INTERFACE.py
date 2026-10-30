
"""
GT7 Race Control Ecurie
Poste de controle ingenieur de piste - Version 1

Interface Windows en direct avec Tkinter.

Fonctionnalites :
- Connexion PS5
- Tour et position
- Vitesse et regime moteur
- Carburant
- Temperatures des pneus
- Dernier et meilleur tour
- Surveillance de la connexion
- Enregistrement CSV

Aucun module existant n'est modifie.
"""

import queue
import threading
import time
import tkinter as tk
import math
import json
import os
import ipaddress
from pathlib import Path

from tkinter import ttk, messagebox, simpledialog

from racecontrol.telemetry import (
    GT7Receiver,
    TelemetryDiagnostics,
)

from racecontrol.telemetry.csv_recorder import (
    GT7CSVRecorder,
)

from strategie_carburant_V2 import StrategieCarburant
from racecontrol.circuits import CircuitLibrary


CONFIG_DIR = Path(os.environ.get("APPDATA") or (Path.home() / ".config")) / "GT7-Race-Control-Ecurie"
CONFIG_FILE = CONFIG_DIR / "settings.json"


def demander_ip_ps5(parent):
    """Charge l'IP locale, ou la demande lors de la premiere connexion."""
    try:
        adresse = json.loads(CONFIG_FILE.read_text(encoding="utf-8")).get("ps5_ip", "")
        ipaddress.IPv4Address(adresse)
        return adresse
    except (OSError, ValueError, TypeError, KeyError):
        pass

    while True:
        adresse = simpledialog.askstring(
            "Connexion PS5", "Adresse IPv4 de ta PS5 (ex. 192.168.1.50) :",
            parent=parent,
        )
        if adresse is None:
            return None
        adresse = adresse.strip()
        try:
            ipaddress.IPv4Address(adresse)
        except ipaddress.AddressValueError:
            messagebox.showerror("Adresse invalide", "Entre une adresse IPv4 valide.", parent=parent)
            continue
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            CONFIG_FILE.write_text(json.dumps({"ps5_ip": adresse}, indent=2), encoding="utf-8")
        except OSError as exc:
            messagebox.showwarning("Configuration", f"Impossible d'enregistrer l'adresse : {exc}", parent=parent)
        return adresse

LISTEN_PORT = 33740
HEARTBEAT_PORT = 33739

FREQUENCE_AFFICHAGE_MS = 200
DELAI_CONNEXION_PERDUE = 3.0


def afficher_nombre(valeur, decimales=1, unite=""):
    if valeur is None:
        return "--"

    try:
        return f"{float(valeur):.{decimales}f}{unite}"
    except (TypeError, ValueError):
        return "--"


def afficher_entier(valeur):
    if valeur is None:
        return "--"

    try:
        return str(int(valeur))
    except (TypeError, ValueError):
        return "--"


def afficher_temps(ms):
    if ms is None:
        return "--:--.---"

    try:
        ms = int(ms)
    except (TypeError, ValueError):
        return "--:--.---"

    if ms <= 0 or ms == 0xFFFFFFFF:
        return "--:--.---"

    minutes = ms // 60000
    secondes = (ms % 60000) / 1000

    return f"{minutes}:{secondes:06.3f}"

# V4 - Seuils provisoires pour tester les couleurs
# A calibrer avec les donnees GT7 avant utilisation en course.
def couleur_temperature_pneu(temperature):
    try:
        temperature = float(temperature)
    except (TypeError, ValueError):
        return "#64748b"

    if temperature < 60:
        return "#3498db"  # Bleu - froid
    elif temperature < 80:
        return "#2ecc71"  # Vert - plage provisoire
    elif temperature < 100:
        return "#f39c12"  # Orange - chaud
    else:
        return "#e74c3c"  # Rouge - tres chaud

class RaceControlLive:

    def __init__(self, root):
        self.root = root

        self.root.title(
            "GT7 Race Control Ecurie - Ingenieur de piste"
        )

        self.root.geometry("1250x900")
        self.root.minsize(1000, 750)

        self.root.configure(bg="#101820")

        self.file_evenements = queue.Queue()

        self.receiver = None
        self.recorder = None
        self.diagnostics = None

        # Nouveau moteur de strategie carburant V2
        self.strategie_carburant = StrategieCarburant()
        self.etat_carburant = None

        self.running = False
        self.last_packet_time = None
        self.latest_telemetry = None

        self.lock = threading.Lock()

        self.variables = {}

        self.creer_interface()

        self.root.protocol(
            "WM_DELETE_WINDOW",
            self.fermer_application,
        )

        self.root.after(
            FREQUENCE_AFFICHAGE_MS,
            self.actualiser_interface,
        )

    def creer_variable(self, nom, valeur="--"):
        variable = tk.StringVar(value=valeur)
        self.variables[nom] = variable
        return variable

    def creer_carte(self, parent, titre, variable, colonne, ligne):
        cadre = tk.Frame(
            parent,
            bg="#1d2a36",
            padx=18,
            pady=14,
        )

        cadre.grid(
            row=ligne,
            column=colonne,
            padx=7,
            pady=7,
            sticky="nsew",
        )

        tk.Label(
            cadre,
            text=titre,
            font=("Segoe UI", 11, "bold"),
            fg="#a8b8c7",
            bg="#1d2a36",
        ).pack(anchor="w")

        tk.Label(
            cadre,
            textvariable=variable,
            font=("Segoe UI", 23, "bold"),
            fg="white",
            bg="#1d2a36",
        ).pack(
            anchor="w",
            pady=(8, 0),
        )

    def creer_interface(self):
        entete = tk.Frame(
            self.root,
            bg="#101820",
            padx=22,
            pady=16,
        )

        entete.pack(fill="x")

        tk.Label(
            entete,
            text="GT7 RACE CONTROL ECURIE",
            font=("Segoe UI", 23, "bold"),
            fg="white",
            bg="#101820",
        ).pack(anchor="w")

        tk.Label(
            entete,
            text="POSTE INGENIEUR DE PISTE - TELEMETRIE LIVE",
            font=("Segoe UI", 10),
            fg="#8fa9bd",
            bg="#101820",
        ).pack(anchor="w")

        barre = tk.Frame(
            self.root,
            bg="#17232e",
            padx=20,
            pady=12,
        )

        barre.pack(fill="x")

        self.statut = self.creer_variable(
            "statut",
            "ARRETE",
        )

        self.label_statut = tk.Label(
            barre,
            textvariable=self.statut,
            font=("Segoe UI", 12, "bold"),
            fg="#ffcc66",
            bg="#17232e",
        )

        self.label_statut.pack(side="left")

        self.bouton_demarrer = ttk.Button(
            barre,
            text="DEMARRER LIVE",
            command=self.demarrer,
        )

        self.bouton_demarrer.pack(
            side="right",
            padx=5,
        )

        self.bouton_arreter = ttk.Button(
            barre,
            text="ARRETER",
            command=self.arreter,
            state="disabled",
        )

        self.bouton_arreter.pack(
            side="right",
            padx=5,
        )

        panneau = tk.Frame(
            self.root,
            bg="#101820",
            padx=14,
            pady=12,
        )

        # Anciennes cartes conservees pour la compatibilite telemetrie.
        # Elles restent masquees pendant la construction de la nouvelle interface.
        # panneau.pack(
        #     fill="both",
        #     expand=True,
        # )

        for colonne in range(4):
            panneau.grid_columnconfigure(
                colonne,
                weight=1,
                uniform="cartes",
            )

        for ligne in range(5):
            panneau.grid_rowconfigure(
                ligne,
                weight=1,
                minsize=65,
            )

        cartes = [
            ("TOUR ACTUEL", "tour"),
            ("POSITION COURSE", "position"),
            ("VITESSE", "vitesse"),
            ("REGIME MOTEUR", "rpm"),
            ("CARBURANT", "carburant"),
            ("DERNIER TOUR", "dernier"),
            ("MEILLEUR TOUR", "meilleur"),
            ("TEMPERATURE EAU", "eau"),
            ("PNEU AVANT GAUCHE", "pneu_ag"),
            ("PNEU AVANT DROIT", "pneu_ad"),
            ("PNEU ARRIERE GAUCHE", "pneu_rg"),
            ("PNEU ARRIERE DROIT", "pneu_rd"),

            # Nouveaux indicateurs strategie carburant V2
            ("CONSOMMATION MOYENNE", "conso_moyenne"),
            ("AUTONOMIE ESTIMEE", "autonomie"),
            ("TOURS CARBURANT MESURES", "tours_carburant"),
            ("FIABILITE ESTIMATION", "fiabilite_carburant"),
            ("CARBURANT NECESSAIRE", "carburant_necessaire"),
        ]

        for indice, (titre, nom) in enumerate(cartes):
            variable = self.creer_variable(nom)

            self.creer_carte(
                panneau,
                titre,
                variable,
                indice % 4,
                indice // 4,
            )

        # V4 INTERFACE - Structure provisoire du poste ingenieur
        self.zone_ingenieur = tk.Frame(
            self.root,
            bg="#101820",
            padx=14,
            pady=8,
        )
        self.zone_ingenieur.pack(
            fill="both",
            expand=True,
        )

        self.zone_ingenieur.grid_rowconfigure(
            0,
            weight=1,
            minsize=350,
        )

        for colonne, titre in enumerate(

            ("VOITURES GAUCHE", "CARTE DU CIRCUIT", "VOITURES DROITE")
        ):
            self.zone_ingenieur.grid_columnconfigure(
                colonne,
                weight=2 if colonne == 1 else 1,
            )            
            cadre_zone = tk.Frame(
                self.zone_ingenieur,
                bg="#1d2a36",
                padx=10,
                pady=10,
            )
            cadre_zone.grid(
                row=0,
                column=colonne,
                padx=5,
                sticky="nsew",
            )

            if colonne == 0:
                self.panneau_voitures_gauche = cadre_zone
            elif colonne == 2:
                self.panneau_voitures_droite = cadre_zone

            if colonne == 0:
                carte_voiture = tk.Frame(
                    cadre_zone,
                    bg="#263746",
                    padx=12,
                    pady=12,
                )
                carte_voiture.pack(
                    side="bottom",
                    fill="x",
                    pady=8,
                )

                tk.Label(
                    carte_voiture,
                    text="VOITURE 01",
                    font=("Segoe UI", 13, "bold"),
                    fg="#38bdf8",
                    bg="#263746",
                ).pack(anchor="w")

                self.vitesse_voiture_01 = tk.StringVar(
                    value="VITESSE : -- km/h"
                )

                self.accelerateur_voiture_01 = tk.StringVar(
                    value="ACCELERATEUR : -- %"
                )

                self.frein_voiture_01 = tk.StringVar(
                    value="FREIN : -- %"
                )

                self.carburant_voiture_01 = tk.StringVar(
                    value="CARBURANT : -- %"
                )

                for titre_indicateur in (
                    "VITESSE : -- km/h",
                    "ACCELERATEUR : -- %",
                    "FREIN : -- %",
                    "CARBURANT : -- %",
                ):
                    tk.Label(
                        carte_voiture,
                        textvariable=(
                            self.vitesse_voiture_01
                            if titre_indicateur.startswith("VITESSE")
                            else self.accelerateur_voiture_01
                            if titre_indicateur.startswith("ACCELERATEUR")
                            else self.frein_voiture_01
                            if titre_indicateur.startswith("FREIN")
                            else self.carburant_voiture_01
                        ),

                        text="",

                        font=("Segoe UI", 11),
                        fg="white",
                        bg="#263746",
                    ).pack(anchor="w", pady=4)

                    if titre_indicateur.startswith("ACCELERATEUR"):

                        style_jauges = ttk.Style()
                        style_jauges.theme_use("clam")

                        style_jauges.configure(
                            "Accelerateur.Horizontal.TProgressbar",
                            background="#22c55e",
                        )
                        self.jauge_accelerateur_01 = ttk.Progressbar(
                            carte_voiture,
                            orient="horizontal",
                            mode="determinate",
                            maximum=100,
                            value=0,
                            style="Accelerateur.Horizontal.TProgressbar",
                        )
                        self.jauge_accelerateur_01.pack(
                            fill="x",
                            pady=(0, 8),
                        )

                    if titre_indicateur.startswith("FREIN"):
                        style_jauges.configure(
                            "Frein.Horizontal.TProgressbar",
                            background="#ef4444",
                        )
                        self.jauge_frein_01 = ttk.Progressbar(
                            carte_voiture,
                            orient="horizontal",
                            mode="determinate",
                            maximum=100,
                            value=0,
                            style="Frein.Horizontal.TProgressbar",
                        )
                        self.jauge_frein_01.pack(
                            fill="x",
                            pady=(0, 8),
                        )

                self.panneau_pneus_voiture_01 = tk.Frame(
                    carte_voiture,
                    bg="#263746",
                    pady=8,
                )
                self.panneau_pneus_voiture_01.pack(
                    fill="x",
                )

                tk.Label(
                    self.panneau_pneus_voiture_01,
                    text="PNEUS",
                    font=("Segoe UI", 11, "bold"),
                    fg="#38bdf8",
                    bg="#263746",
                ).pack(anchor="w")

                self.canvas_pneus_01 = tk.Canvas(
                    self.panneau_pneus_voiture_01,
                    width=230,
                    height=125,
                    bg="#263746",
                    highlightthickness=0,
                )
                self.canvas_pneus_01.pack()

                for x, y, nom in (
                    (10, 15, "AVG"),
                    (135, 15, "AVD"),
                    (10, 75, "ARG"),
                    (135, 75, "ARD"),
                ):
                    self.canvas_pneus_01.create_rectangle(
                        x, y, x + 85, y + 38,
                        fill="#64748b",
                        outline="white",
                        width=2,
                        tags=("couleur_" + nom,),
                    )

                    self.canvas_pneus_01.create_text(
                        x + 42,
                        y + 19,
                        text=nom,
                        fill="white",
                        font=("Segoe UI", 10, "bold"),
                        tags=("pneu_" + nom,),
                    )

            tk.Label(
                cadre_zone,
                text=titre,
                bg="#1d2a36",
                fg="white",
                font=("Segoe UI", 11, "bold"),
            ).pack(anchor="n", pady=(0, 10))

            if colonne == 1:
                self.canvas_circuit = tk.Canvas(
                    cadre_zone,
                    bg="#14202b",
                    highlightthickness=0,
                )
                self.canvas_circuit.pack(
                    fill="both",
                    expand=True,
                )

                self.texte_circuit = self.canvas_circuit.create_text(
                    0,
                    0,
                    text="",
                    fill="#8fa9bd",
                    font=("Segoe UI", 15, "bold"),
                    justify="center",
                )

                self.contour_circuit = self.canvas_circuit.create_rectangle(
                    0,
                    0,
                    0,
                    0,
                    outline="#38bdf8",
                    width=2,
                    dash=(8, 5),
                )

                self.trace_circuit = self.canvas_circuit.create_oval(
                    0,
                    0,
                    0,
                    0,
                    outline="#94a3b8",
                    width=12,
                )
                self.canvas_circuit.itemconfigure(
                    self.trace_circuit, state="hidden"
                )

                self.bibliotheque_circuits = CircuitLibrary()
                self.points_monza_gauche = []
                self.points_monza_droite = []
                self.circuit_choisi = tk.StringVar(value="Autodromo Nazionale Monza")
                self.circuit_selecteur = ttk.Combobox(
                    cadre_zone, textvariable=self.circuit_choisi,
                    values=self.bibliotheque_circuits.noms,
                    state="readonly", width=37,
                )
                self.circuit_selecteur.pack(before=self.canvas_circuit, fill="x", padx=8, pady=(0, 4))
                self.circuit_selecteur.bind("<<ComboboxSelected>>", self.changer_circuit)
                self.bouton_circuit = ttk.Button(
                    cadre_zone, text="Installer le tracé sélectionné",
                    command=self.installer_circuit_selectionne,
                )
                self.bouton_circuit.pack(before=self.canvas_circuit, pady=(0, 5))
                self.bouton_tous_circuits = ttk.Button(
                    cadre_zone, text="Installer tous les tracés disponibles (27)",
                    command=self.installer_tous_circuits,
                )
                self.bouton_tous_circuits.pack(before=self.canvas_circuit, pady=(0, 5))
                self.charger_circuit_selectionne()

                self.trace_reel_gt7 = self.canvas_circuit.create_line(
                    0, 0, 0, 0,
                    fill="#00e5ff",
                    width=3,
                    smooth=False,
                    state="hidden",
                )

                self.canvas_circuit.after(
                    500, self.dessiner_bordures_monza
                )

                self.marqueur_voiture_01 = self.canvas_circuit.create_oval(
                    0, 0, 28, 28,
                    fill="#22c55e",
                    outline="white",
                    width=2,
                )

                self.numero_marqueur_01 = self.canvas_circuit.create_text(
                    14,
                    14,
                    text="01",
                    fill="white",
                    font=("Segoe UI", 10, "bold"),
                )

                self.canvas_circuit.bind(
                    "<Configure>",
                    lambda event: (
                        self.canvas_circuit.coords(
                            self.texte_circuit,
                            event.width / 2,
                            event.height / 2,
                        ),
                        self.canvas_circuit.coords(
                            self.contour_circuit,
                            20,
                            20,
                            max(21, event.width - 20),
                            max(21, event.height - 20),
                        ),
                        self.canvas_circuit.coords(
                            self.trace_circuit,
                            60,
                            60,
                            max(61, event.width - 60),
                            max(61, event.height - 60),
                        ),
                        self.dessiner_bordures_monza(),
                        self.positionner_voiture_01_sur_monza(),
                    ),
                )

                # Déplacement exclusivement fondé sur la télémétrie GT7.
        # V4 - Dessin de la voiture et des quatre pneus
        panneau_pneus = tk.Frame(
            self.root,
            bg="#17232e",
            padx=10,
            pady=5,
        )
        # panneau_pneus.pack(fill="x")
        tk.Label(
            panneau_pneus,
            text="TEMPERATURES DES PNEUS - VUE DU DESSUS",
            font=("Segoe UI", 11, "bold"),
            fg="white",
            bg="#17232e",
        ).pack()

        self.canvas_pneus = tk.Canvas(
            panneau_pneus,
            width=420,
            height=170,
            bg="#17232e",
            highlightthickness=0,
        )
        self.canvas_pneus.pack()

        # Carrosserie
        self.canvas_pneus.create_rectangle(
            180, 15, 240, 155,
            fill="#34495e",
            outline="#a8b8c7",
            width=2,
        )

        # Pare-brise
        self.canvas_pneus.create_rectangle(
            190, 40, 230, 75,
            fill="#5dade2",
            outline="",
        )

        # Quatre pneus
        for x, y, nom in [
            (80, 35, "AVG"),
            (245, 35, "AVD"),
            (80, 110, "ARG"),
            (245, 110, "ARD"),
        ]:
            self.canvas_pneus.create_rectangle(
                x, y, x + 95, y + 42,
                fill="#64748b",
                outline="white",
                width=2,
                tags=("couleur_" + nom,),
            )

            self.canvas_pneus.create_text(
                x + 47,
                y + 21,
                text=nom,
                fill="white",
                font=("Segoe UI", 10, "bold"),
                justify="center",
                tags=("pneu_" + nom,),
            )

        bas = tk.Frame(
            self.root,
            bg="#17232e",
            padx=20,
            pady=12,
        )

        bas.pack(fill="x")

        self.alerte = self.creer_variable(
            "alerte",
            "En attente du demarrage.",
        )

        self.label_alerte = tk.Label(
            bas,
            textvariable=self.alerte,
            font=("Segoe UI", 11, "bold"),
            fg="#ffcc66",
            bg="#17232e",
            anchor="w",
        )

        self.label_alerte.pack(fill="x")

        self.informations = self.creer_variable(
            "informations",
            "PS5 : à configurer | UDP : 33740",
        )

        tk.Label(
            bas,
            textvariable=self.informations,
            font=("Segoe UI", 9),
            fg="#a8b8c7",
            bg="#17232e",
            anchor="w",
        ).pack(
            fill="x",
            pady=(6, 0),
        )

    def on_telemetry(self, telemetry):
        """
        Cette fonction est appelee par le recepteur.

        Aucun composant Tkinter n'est modifie ici.
        """

        with self.lock:
            self.latest_telemetry = telemetry
            self.last_packet_time = time.monotonic()

            # Calcul de la strategie carburant
            # a chaque paquet recu de la PS5.
            self.etat_carburant = (
                self.strategie_carburant.mettre_a_jour(telemetry)
            )

            # Conservation de l'enregistrement CSV.
            if self.recorder is not None:
                self.recorder.record(telemetry)

    def on_status(self, message):
        self.file_evenements.put(
            ("statut", str(message))
        )

    def demarrer(self):
        if self.running:
            return

        ps5_ip = demander_ip_ps5(self.root if hasattr(self, "root") else self.canvas_circuit.winfo_toplevel())
        if ps5_ip is None:
            return

        self.ps5_ip_active = ps5_ip
        self.informations.set(f"PS5 : {ps5_ip} | UDP : {LISTEN_PORT}")
        try:
            # Nouvelle session : remettre la strategie a zero.
            with self.lock:
                self.strategie_carburant = StrategieCarburant()
                self.etat_carburant = None
                self.latest_telemetry = None
                self.last_packet_time = None

            # Reinitialiser les indicateurs carburant.
            self.variables["conso_moyenne"].set("--")
            self.variables["autonomie"].set("--")
            self.variables["tours_carburant"].set("0")
            self.variables["fiabilite_carburant"].set("EN ATTENTE")

            self.diagnostics = TelemetryDiagnostics()

            self.recorder = GT7CSVRecorder(
                output_directory="sessions"
            )

            self.receiver = GT7Receiver(
                ps5_ip=ps5_ip,
                listen_ip="0.0.0.0",
                listen_port=LISTEN_PORT,
                heartbeat_port=HEARTBEAT_PORT,
                heartbeat_interval=1.6,
                heartbeat=b"A",
                on_telemetry=self.on_telemetry,
                on_status=self.on_status,
                diagnostics=self.diagnostics,
            )

            self.running = True

            self.receiver.start()

            self.bouton_demarrer.config(
                state="disabled"
            )

            self.bouton_arreter.config(
                state="normal"
            )

            self.statut.set(
                "CONNEXION EN COURS..."
            )

            self.alerte.set(
                "Attente des donnees GT7."
            )

        except Exception as erreur:
            self.running = False

            if self.receiver is not None:
                try:
                    self.receiver.stop()
                except Exception:
                    pass

            if self.recorder is not None:
                self.recorder.close()

            self.receiver = None
            self.recorder = None

            messagebox.showerror(
                "Erreur de demarrage",
                str(erreur),
            )

    def arreter(self):
        if not self.running:
            return

        self.running = False

        self.bouton_arreter.config(
            state="disabled"
        )

        self.statut.set(
            "ARRET EN COURS..."
        )

        try:
            if self.receiver is not None:
                self.receiver.stop()

            with self.lock:
                if self.recorder is not None:
                    self.recorder.close()
                    self.recorder = None

        except Exception as erreur:
            messagebox.showerror(
                "Erreur d'arret",
                str(erreur),
            )

        finally:
            self.receiver = None

            self.bouton_demarrer.config(
                state="normal"
            )

            self.statut.set(
                "ARRETE"
            )

            self.label_statut.config(
                fg="#ffcc66"
            )

            self.alerte.set(
                "Reception arretee."
            )

    def actualiser_donnees(self, telemetry):
        tour = getattr(
            telemetry,
            "current_lap",
            None,
        )

        total = getattr(
            telemetry,
            "total_laps",
            None,
        )

        if total == 0:
            total_affiche = "Libre"
        else:
            total_affiche = afficher_entier(total)

        self.variables["tour"].set(
            f"{afficher_entier(tour)} / {total_affiche}"
        )

        position = getattr(
            telemetry,
            "race_position",
            None,
        )

        total_positions = getattr(
            telemetry,
            "race_position_total",
            None,
        )

        self.variables["position"].set(
            f"P{afficher_entier(position)}"
            f" / {afficher_entier(total_positions)}"
        )

        self.variables["vitesse"].set(
            afficher_nombre(
                getattr(telemetry, "speed_kmh", None),
                0,
                " km/h",
            )
        )

        self.vitesse_reelle_voiture_01 = (
            getattr(telemetry, "speed_kmh", 0) or 0
        )
        self.rapport_voiture_01 = getattr(telemetry, "current_gear", None)
        self.position_gt7_voiture_01 = (
            getattr(telemetry, "position_x", None),
            getattr(telemetry, "position_z", None),
        )
        self.enregistrer_point_circuit()
        self.dessiner_trace_reel_gt7()
        self.positionner_voiture_01_sur_monza()

        self.rotation_roues_voiture_01 = getattr(
            telemetry, "wheel_angular_speeds", None
        )
        print(
            "RAPPORT :",
            getattr(telemetry, "current_gear", None),
            "ROUES :",
            getattr(telemetry, "wheel_angular_speeds", None),
        )
        self.vitesse_voiture_01.set(
            "VITESSE : " + afficher_nombre(
                getattr(telemetry, "speed_kmh", None),
                0,
                " km/h",
            )
        )

        self.accelerateur_voiture_01.set(
            "ACCELERATEUR : " + afficher_nombre(
                getattr(telemetry, "throttle_percent", None),
                0,
                " %",
            )
        )

        self.frein_voiture_01.set(
            "FREIN : " + afficher_nombre(
                getattr(telemetry, "brake_percent", None),
                0,
                " %",
            )
        )

        valeur_frein = getattr(
            telemetry, "brake_percent", 0
        )

        self.jauge_frein_01["value"] = max(
            0,
            min(100, valeur_frein or 0),
        )

        valeur_accelerateur = getattr(
            telemetry, "throttle_percent", 0
        )

        self.jauge_accelerateur_01["value"] = max(
            0,
            min(100, valeur_accelerateur or 0),
        )

        self.variables["rpm"].set(
            afficher_nombre(
                getattr(telemetry, "rpm", None),
                0,
                " tr/min",
            )
        )

        carburant = getattr(
            telemetry,
            "fuel_level",
            None,
        )

        if carburant is None:
            carburant = getattr(
                telemetry,
                "fuel",
                None,
            )

        capacite = getattr(
            telemetry,
            "fuel_capacity",
            None,
        )

        try:
            if carburant is not None and capacite is not None:
                carburant_pourcent = (
                    float(carburant) / float(capacite)
                ) * 100 if float(capacite) > 0 else None
            else:
                carburant_pourcent = None
        except (TypeError, ValueError, ZeroDivisionError):
            carburant_pourcent = None

        self.variables["carburant"].set(
            afficher_nombre(
                carburant_pourcent,
                1,
                " %",
            )
        )

        self.carburant_voiture_01.set(
            "CARBURANT : " + afficher_nombre(
                carburant_pourcent,
                1,
                " %",
            )
        )

        self.variables["dernier"].set(
            afficher_temps(
                getattr(
                    telemetry,
                    "last_lap_ms",
                    None,
                )
            )
        )

        self.variables["meilleur"].set(
            afficher_temps(
                getattr(
                    telemetry,
                    "best_lap_ms",
                    None,
                )
            )
        )

        self.variables["eau"].set(
            afficher_nombre(
                getattr(
                    telemetry,
                    "water_temperature",
                    None,
                ),
                1,
                " °C",
            )
        )

        pneus = getattr(
            telemetry,
            "tyre_temperatures",
            None,
        )

        noms = [
            "pneu_ag",
            "pneu_ad",
            "pneu_rg",
            "pneu_rd",
        ]

        for indice, nom in enumerate(noms):
            valeur = None

            if pneus is not None:
                try:
                    valeur = pneus[indice]
                except (IndexError, TypeError):
                    pass

            self.variables[nom].set(
                afficher_nombre(
                    valeur,
                    1,
                    " °C",
                )
            )

            noms_dessin = {
                "pneu_ag": "AVG",
                "pneu_ad": "AVD",
                "pneu_rg": "ARG",
                "pneu_rd": "ARD",
            }

            nom_dessin = noms_dessin[nom]

            temperature_affichee = afficher_nombre(
                valeur,
                1,
                " °C",
            )

            self.canvas_pneus.itemconfigure(
                "pneu_" + nom_dessin,
                text=nom_dessin + "\n" + temperature_affichee,
            )

            self.canvas_pneus.itemconfigure(
                "couleur_" + nom_dessin,
                fill=couleur_temperature_pneu(valeur),
            )

            self.canvas_pneus_01.itemconfigure(
                "pneu_" + nom_dessin,
                text=nom_dessin + "\n" + temperature_affichee,
            )

            self.canvas_pneus_01.itemconfigure(
                "couleur_" + nom_dessin,
                fill=couleur_temperature_pneu(valeur),
            )

    def charger_circuit_selectionne(self):
        """Charge les points locaux du circuit choisi sans bloquer l'interface."""
        gauche, droite = self.bibliotheque_circuits.charger(self.circuit_choisi.get())
        self.points_monza_gauche = gauche
        self.points_monza_droite = droite
        if hasattr(self, "canvas_circuit"):
            self.dessiner_bordures_monza()

    def changer_circuit(self, event=None):
        self.charger_circuit_selectionne()

    def installer_circuit_selectionne(self):
        """Télécharge le circuit demandé uniquement après action explicite."""
        nom = self.circuit_choisi.get()
        if not self.bibliotheque_circuits.est_disponible(nom):
            messagebox.showinfo("Circuit", "Le tracé géométrique de ce circuit n'est pas encore disponible.")
            return
        if self.bibliotheque_circuits.est_installe(nom):
            self.charger_circuit_selectionne()
            return
        self.bouton_circuit.configure(state="disabled")
        def travail():
            try:
                self.bibliotheque_circuits.installer(nom)
                self.root.after(0, self.charger_circuit_selectionne)
            except Exception as exc:
                self.root.after(0, lambda msg=str(exc): messagebox.showerror("Circuit", msg))
            finally:
                self.root.after(0, lambda: self.bouton_circuit.configure(state="normal"))
        threading.Thread(target=travail, daemon=True).start()

    def installer_tous_circuits(self):
        """Installe en arrière-plan tous les tracés géométriques disponibles."""
        if not messagebox.askyesno("Circuits GT7", "Télécharger les 27 tracés disponibles depuis le projet GT7 Datalogger ?"):
            return
        self.bouton_tous_circuits.configure(state="disabled")
        def travail():
            echecs = []
            succes = 0
            for nom in self.bibliotheque_circuits.noms:
                if not self.bibliotheque_circuits.est_disponible(nom):
                    continue
                try:
                    self.bibliotheque_circuits.installer(nom)
                    succes += 1
                except Exception as exc:
                    echecs.append(f"{nom} : {exc}")
            def terminer():
                self.bouton_tous_circuits.configure(state="normal")
                self.charger_circuit_selectionne()
                messagebox.showinfo("Circuits GT7", f"{succes} tracés installés. {len(echecs)} échecs." + ("\n" + "\n".join(echecs[:3]) if echecs else ""))
            self.root.after(0, terminer)
        threading.Thread(target=travail, daemon=True).start()

    def positionner_voiture_01_sur_monza(self):
        """Positionne VOITURE 01 selon ses coordonnées GT7."""

        position = getattr(self, "position_gt7_voiture_01", None)

        if position is None or None in position:
            return

        points = (
            self.points_monza_gauche
            + self.points_monza_droite
        )

        if not points:
            return

        largeur = self.canvas_circuit.winfo_width()
        hauteur = self.canvas_circuit.winfo_height()

        x_min = min(x for x, z in points)
        x_max = max(x for x, z in points)
        z_min = min(z for x, z in points)
        z_max = max(z for x, z in points)

        echelle = min(
            max(1, largeur - 60) / max(1, x_max - x_min),
            max(1, hauteur - 60) / max(1, z_max - z_min),
        )

        centre_x = (x_min + x_max) / 2
        centre_z = (z_min + z_max) / 2

        x, z = position

        px = largeur / 2 + (x - centre_x) * echelle
        py = hauteur / 2 - (z - centre_z) * echelle

        self.canvas_circuit.coords(
            self.marqueur_voiture_01,
            px - 14, py - 14,
            px + 14, py + 14,
        )

        self.canvas_circuit.coords(
            self.numero_marqueur_01,
            px, py,
        )

    def dessiner_bordures_monza(self):
        """Affiche les bordures fixes du circuit sélectionné."""
        self.canvas_circuit.delete("bordures_monza")

        points = (
            self.points_monza_gauche
            + self.points_monza_droite
        )

        if not points:
            return

        largeur = self.canvas_circuit.winfo_width()
        hauteur = self.canvas_circuit.winfo_height()

        x_min = min(x for x, z in points)
        x_max = max(x for x, z in points)
        z_min = min(z for x, z in points)
        z_max = max(z for x, z in points)

        echelle = min(
            max(1, largeur - 60) / max(1, x_max - x_min),
            max(1, hauteur - 60) / max(1, z_max - z_min),
        )

        centre_x = (x_min + x_max) / 2
        centre_z = (z_min + z_max) / 2

        self.canvas_circuit.delete("bordures_monza")
        for bordure, couleur in (
            (self.points_monza_gauche, "#00e5ff"),
            (self.points_monza_droite, "#ffcc00"),
        ):
            for x, z in bordure:
                px = largeur / 2 + (x - centre_x) * echelle
                py = hauteur / 2 - (z - centre_z) * echelle

                self.canvas_circuit.create_oval(
                    px - 1, py - 1,
                    px + 1, py + 1,
                    fill=couleur,
                    outline="",
                    tags="bordures_monza",
                )

    def dessiner_trace_reel_gt7(self):
        """Dessine la trajectoire enregistrée sur le Canvas."""

        points = getattr(self, "points_circuit_gt7", [])

        if len(points) < 2:
            return

        coordonnees = []

        for x, z in points:
            canvas_x, canvas_y = self.convertir_coordonnees_circuit(x, z)
            coordonnees.extend((canvas_x, canvas_y))

        self.canvas_circuit.coords(
            self.trace_reel_gt7,
            *coordonnees,
        )

        self.canvas_circuit.itemconfigure(
            self.trace_reel_gt7,
            state="normal",
        )

    def convertir_coordonnees_circuit(self, x, z):
        """Convertit une position GT7 en coordonnées du Canvas."""

        largeur = self.canvas_circuit.winfo_width()
        hauteur = self.canvas_circuit.winfo_height()

        points = getattr(self, "points_circuit_gt7", [])

        if not points:
            return largeur / 2, hauteur / 2

        x_min = min(p[0] for p in points)
        x_max = max(p[0] for p in points)
        z_min = min(p[1] for p in points)
        z_max = max(p[1] for p in points)

        amplitude_x = max(1, x_max - x_min)
        amplitude_z = max(1, z_max - z_min)

        echelle = min(
            max(1, largeur - 120) / amplitude_x,
            max(1, hauteur - 120) / amplitude_z,
        )

        centre_x = (x_min + x_max) / 2
        centre_z = (z_min + z_max) / 2

        canvas_x = largeur / 2 + (x - centre_x) * echelle
        canvas_y = hauteur / 2 - (z - centre_z) * echelle

        return canvas_x, canvas_y

    def enregistrer_point_circuit(self):
        """Enregistre une position réelle de VOITURE 01."""

        position = getattr(self, "position_gt7_voiture_01", None)

        if position is None or None in position:
            return

        if not hasattr(self, "points_circuit_gt7"):
            self.points_circuit_gt7 = []

        if self.points_circuit_gt7:
            dernier_x, dernier_z = self.points_circuit_gt7[-1]
            x, z = position

            distance = math.hypot(x - dernier_x, z - dernier_z)

            if distance < 2.0:
                return

        self.points_circuit_gt7.append(position)

    def positionner_voiture_01(self, angle):
        """Positionne VOITURE 01 sur le circuit ovale provisoire."""

        self.angle_voiture_01 = angle

        largeur = self.canvas_circuit.winfo_width()
        hauteur = self.canvas_circuit.winfo_height()

        rayon_x = max(1, (largeur - 120) / 2)
        rayon_y = max(1, (hauteur - 120) / 2)

        centre_x = largeur / 2
        centre_y = hauteur / 2

        x = centre_x + rayon_x * math.cos(angle)
        y = centre_y + rayon_y * math.sin(angle)

        self.canvas_circuit.coords(
            self.marqueur_voiture_01,
            x - 14,
            y - 14,
            x + 14,
            y + 14,
        )

        self.canvas_circuit.coords(
            self.numero_marqueur_01,
            x,
            y,
        )
    def tester_position_voiture_01(self):
        """Test provisoire du positionnement sur la piste."""
        self.positionner_voiture_01(math.pi)
    def animer_voiture_01(self):
        """Fait avancer VOITURE 01 sur le circuit provisoire."""

        vitesse = max(0, float(getattr(self, "vitesse_reelle_voiture_01", 0)))

        roues = getattr(self, "rotation_roues_voiture_01", None)
        rotation = sum(roues) / len(roues) if roues else 0

        if rotation > 0.5:
            sens = -1
        elif rotation < -0.5:
            sens = 1
        else:
            sens = 0

        nouvel_angle = self.angle_voiture_01 + sens * vitesse * 0.00015
        self.positionner_voiture_01(nouvel_angle)

        self.canvas_circuit.after(
            50, self.animer_voiture_01
        )

    def actualiser_interface(self):
        """
        Cette methode tourne dans le thread Tkinter.
        """

        try:
            while True:
                evenement, valeur = (
                    self.file_evenements.get_nowait()
                )

                if evenement == "statut":
                    self.informations.set(
                        f"PS5 : {getattr(self, 'ps5_ip_active', 'non configurée')} | {valeur}"
                    )

        except queue.Empty:
            pass

        with self.lock:
            telemetry = self.latest_telemetry
            dernier_paquet = self.last_packet_time
            etat_carburant = self.etat_carburant

        if self.running:
            if dernier_paquet is None:
                self.statut.set(
                    "EN ATTENTE DE LA PS5"
                )

                self.label_statut.config(
                    fg="#ffcc66"
                )

            elif (
                time.monotonic() - dernier_paquet
                > DELAI_CONNEXION_PERDUE
            ):
                self.statut.set(
                    "TELEMETRIE PERDUE"
                )

                self.label_statut.config(
                    fg="#ff6666"
                )

                self.alerte.set(
                    "ALERTE : aucun paquet recu depuis 3 s."
                )

            
            else:
                self.statut.set(
                    "PS5 CONNECTEE - LIVE"
                )

                self.label_statut.config(
                    fg="#66dd99"
                )

                self.alerte.set(
                    "Telemetrie recue normalement."
                )

                if telemetry is not None:
                    self.actualiser_donnees(telemetry)

                    if etat_carburant is not None:

                        capacite = etat_carburant.capacite_l
                        consommation = etat_carburant.consommation_l_tour

                        if (
                            consommation is not None
                            and capacite is not None
                            and capacite > 0
                        ):
                            consommation_pourcent = (
                                consommation / capacite
                            ) * 100
                        else:
                            consommation_pourcent = None

                        self.variables["conso_moyenne"].set(
                            afficher_nombre(
                                consommation_pourcent,
                                2,
                                " %/tour",
                            )
                        )

                        self.variables["autonomie"].set(
                            afficher_nombre(
                                etat_carburant.autonomie_tours,
                                1,
                                " tours",
                            )
                        )

                        self.variables["tours_carburant"].set(
                            str(etat_carburant.tours_mesures)
                        )

                        if etat_carburant.tours_mesures == 0:
                            fiabilite = "EN ATTENTE"
                        elif etat_carburant.tours_mesures < 3:
                            fiabilite = "PROVISOIRE"
                        else:
                            fiabilite = "A CONFIRMER"

                        self.variables["fiabilite_carburant"].set(
                            fiabilite
                        )

                        necessaire = etat_carburant.carburant_necessaire_l
                        capacite = etat_carburant.capacite_l

                        if (
                            necessaire is not None
                            and capacite is not None
                            and capacite > 0
                        ):
                            necessaire_pourcent = (
                                necessaire / capacite
                            ) * 100
                        else:
                            necessaire_pourcent = None

                        self.variables["carburant_necessaire"].set(
                            afficher_nombre(
                                necessaire_pourcent,
                                1,
                                " %",
                            )
                        )

        # Toujours relancer l'actualisation de la fenetre.
        self.root.after(
            FREQUENCE_AFFICHAGE_MS,
            self.actualiser_interface,
        )



    def fermer_application(self):
        if self.running:
            self.arreter()

        self.root.destroy()


def main():
    root = tk.Tk()

    RaceControlLive(root)

    root.mainloop()


if __name__ == "__main__":
    main()
