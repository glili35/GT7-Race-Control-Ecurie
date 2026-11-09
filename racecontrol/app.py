"""Interface pilote et mur des stands, sur le même moteur de télémétrie."""
from __future__ import annotations

import ipaddress
import json
import math
import os
from pathlib import Path
import queue
import socket
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

from racecontrol import __version__, demo as demo_data
from racecontrol.circuits import CircuitLibrary
from racecontrol.fleet import FleetMonitor
from racecontrol.runtime import PilotSession
from racecontrol.transport import EngineerHub, check_invitation
from racecontrol.local_trial import LocalTrial
from racecontrol.track_matcher import TrackMatcher

BG = "#0c1420"
PANEL = "#152233"
FG = "#ecf3fa"
MUTED = "#9db0c5"
FONT = "Segoe UI" if os.name == "nt" else "Helvetica"
COLORS = ("#38bdf8", "#c084fc", "#fbbf24", "#34d399")


def profile_dir() -> Path:
    override = os.environ.get("GT7RC_DATA_DIR")
    if override:
        return Path(override)
    base = Path(os.environ.get("APPDATA") or (Path.home() / ".config"))
    return base / "GT7-Race-Control-Ecurie-Test"


def number(value, decimals=0, suffix=""):
    try:
        value = float(value)
        return f"{value:.{decimals}f}{suffix}" if math.isfinite(value) else "—"
    except (TypeError, ValueError, OverflowError):
        return "—"


def lap_time(value):
    if value is None or value <= 0 or value == 0xFFFFFFFF:
        return "—:—.———"
    return f"{int(value) // 60000}:{(value % 60000) / 1000:06.3f}"


def tyre_color(value):
    if value is None:
        return "#64748b"
    if value < 60:
        return "#3498db"
    if value < 80:
        return "#34d399"
    if value < 100:
        return "#fbbf24"
    return "#fb7185"


def local_address():
    try:
        addresses = socket.gethostbyname_ex(socket.gethostname())[2]
        return next((a for a in addresses if not a.startswith("127.")), "127.0.0.1")
    except OSError:
        return "127.0.0.1"


class CarCard(tk.Frame):
    def __init__(self, parent, car):
        super().__init__(parent, bg=PANEL, padx=13, pady=10,
                         highlightthickness=1, highlightbackground="#25394e")
        self.car = car
        self.values = {}
        header = tk.Frame(self, bg=PANEL)
        header.pack(fill="x")
        tk.Label(header, text=f" {car:02d} ", bg=COLORS[car - 1], fg=BG,
                 font=(FONT, 13, "bold")).pack(side="left")
        self.name = tk.Label(header, text=f"PILOTE {car}", bg=PANEL, fg=FG,
                             font=(FONT, 11, "bold"))
        self.name.pack(side="left", padx=8)
        self.status = tk.Label(header, text="EN ATTENTE", bg=PANEL, fg=MUTED,
                               font=(FONT, 9))
        self.status.pack(side="right")
        line = tk.Frame(self, bg=PANEL)
        line.pack(fill="x", pady=(4, 0))
        self.speed = tk.Label(line, text="—", bg=PANEL, fg=FG,
                              font=(FONT, 30, "bold"))
        self.speed.pack(side="left")
        tk.Label(line, text="km/h", bg=PANEL, fg=MUTED, font=(FONT, 10)).pack(side="left", padx=5)
        self.gear = tk.Label(line, text="RAPPORT —", bg=PANEL, fg=MUTED,
                             font=(FONT, 11, "bold"))
        self.gear.pack(side="right")
        self.pedals = tk.Canvas(self, bg=PANEL, height=31, highlightthickness=0)
        self.pedals.pack(fill="x")
        self.race = tk.Label(self, text="TOUR —   ·   POSITION —", bg=PANEL, fg=MUTED,
                             anchor="w", font=(FONT, 10))
        self.race.pack(fill="x", pady=(3, 4))
        details = tk.Frame(self, bg=PANEL)
        details.pack(fill="x")
        self.tyres = tk.Canvas(details, bg=PANEL, width=110, height=78, highlightthickness=0)
        self.tyres.pack(side="right")
        metrics = tk.Frame(details, bg=PANEL)
        metrics.pack(side="left", fill="both", expand=True)
        for key, title in (("fuel", "Carburant"), ("consumption", "Conso / tour"), ("range", "Autonomie")):
            row = tk.Frame(metrics, bg=PANEL)
            row.pack(fill="x", pady=2)
            tk.Label(row, text=title, bg=PANEL, fg=MUTED,
                     font=(FONT, 9)).pack(side="left")
            value = tk.Label(row, text="—", bg=PANEL, fg=FG, font=(FONT, 10, "bold"))
            value.pack(side="right", padx=6)
            self.values[key] = value
        self.times = tk.Label(self, text="DERNIER —   ·   MEILLEUR —", bg=PANEL, fg=MUTED,
                             anchor="w", font=(FONT, 9))
        self.times.pack(fill="x", pady=(6, 2))
        self.alert = tk.Label(self, text="En attente de télémétrie", bg=PANEL, fg=MUTED,
                             anchor="w", font=(FONT, 9), wraplength=285)
        self.alert.pack(fill="x")

    def update_data(self, snapshot, name):
        s = snapshot
        self.name.config(text=name[:22])
        status = "EN DIRECT" if s.connected else ("SIGNAL PERDU" if s.has_data else "EN ATTENTE")
        self.status.config(text=status, fg="#34d399" if s.connected else ("#fb7185" if s.has_data else MUTED))
        self.speed.config(text=number(s.speed_kmh))
        gear = "R" if s.current_gear == 0 else number(s.current_gear)
        self.gear.config(text=f"RAPPORT {gear}")
        self.race.config(text=f"TOUR {number(s.lap)} / {number(s.total_laps) if s.total_laps else 'LIBRE'}"
                              f"   ·   POSITION {number(s.race_position)}")
        self.values["fuel"].config(text=number(s.fuel_percent, 1, " %"))
        self.values["consumption"].config(text=number(s.consumption_percent_per_lap, 2, " %"))
        self.values["range"].config(text=number(s.remaining_laps, 1, " tours"))
        self.times.config(text=f"DERNIER {lap_time(s.last_lap_ms)}   ·   MEILLEUR {lap_time(s.best_lap_ms)}")
        alert = " · ".join(s.alerts) if s.alerts else s.strategy_status
        self.alert.config(text=alert, fg="#fbbf24" if s.alerts else MUTED)
        self.pedals.delete("all")
        width = max(80, self.pedals.winfo_width() - 44)
        for y, label, value, color in ((3, "ACC", s.throttle_percent, "#34d399"),
                                       (18, "FRE", s.brake_percent, "#fb7185")):
            self.pedals.create_text(0, y + 5, anchor="w", text=label, fill=MUTED, font=(FONT, 8))
            self.pedals.create_rectangle(33, y, 33 + width, y + 8, fill="#23364b", outline="")
            if value is not None:
                self.pedals.create_rectangle(33, y, 33 + width * max(0, min(100, value)) / 100,
                                             y + 8, fill=color, outline="")
        self.tyres.delete("all")
        self.tyres.create_rectangle(43, 15, 68, 64, outline="#52687d", width=2)
        tyres = s.tyre_temperatures or (None,) * 4
        for i, (x, y) in enumerate(((22, 18), (88, 18), (22, 56), (88, 56))):
            self.tyres.create_rectangle(x - 14, y - 9, x + 14, y + 9,
                                        fill=tyre_color(tyres[i]), outline="")
            self.tyres.create_text(x, y, text=number(tyres[i]), fill=BG,
                                   font=(FONT, 8, "bold"))


class RaceControlApp:
    def __init__(self, root, *, demo=False):
        self.root = root
        self.data_dir = profile_dir()
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.config_file = self.data_dir / "settings.json"
        try:
            config = json.loads(self.config_file.read_text(encoding="utf-8"))
            self.settings = config if isinstance(config, dict) else {}
        except (OSError, ValueError):
            self.settings = {}
        self.fleet = FleetMonitor()
        self.library = CircuitLibrary(cache_dir=self.data_dir / "circuits")
        if self.library.erreur_catalogue:
            raise RuntimeError(self.library.erreur_catalogue)
        self.hub = None
        self.pilot = None
        self.local_trial = None
        self._bulk_cancel = threading.Event()
        self.matcher = None
        self._match_car = None
        self.demo = False
        self.demo_start = 0.
        self.closed = False
        self.busy = False
        self.catalog_busy = False
        self.events = queue.Queue(maxsize=200)
        self.actions = queue.SimpleQueue()
        self.points = []
        self.corners = ()
        self.transform = None
        self.layout_key = None
        self.legacy_windows = []
        root.title(f"GT7 Race Control Écurie · {__version__} Test")
        root.geometry("1440x940")
        root.minsize(1120, 800)
        root.configure(bg=BG)
        root.protocol("WM_DELETE_WINDOW", self.close)
        self._styles()
        self.names = [tk.StringVar(value=str(self.settings.get(f"driver_{i}", f"PILOTE {i}"))) for i in range(1, 5)]
        self.circuit = tk.StringVar(value=self.settings.get("circuit", ""))
        self.host = tk.StringVar(value=self.settings.get("host", local_address()))
        self.port = tk.StringVar(value=str(self.settings.get("port", 8765)))
        self.slot = tk.StringVar(value="1")
        self.ps5_ip = tk.StringVar(value=self.settings.get("ps5_ip", ""))
        self.invitation = tk.StringVar(value=self.settings.get("invitation", ""))
        local_car = str(self.settings.get("local_car", 1))
        self.local_car = tk.StringVar(value=local_car if local_car in ("1", "2", "3", "4") else "1")
        self.record = tk.BooleanVar(value=True)
        rotation = str(self.settings.get("rotation", "0"))
        self.rotation = tk.StringVar(value=rotation if rotation in ("0", "90", "180", "270") else "0")
        mirror = self.settings.get("mirror_horizontal", True)
        self.mirror_horizontal = tk.BooleanVar(value=mirror if type(mirror) is bool else True)
        show_corners = self.settings.get("show_corners", True)
        self.show_corners = tk.BooleanVar(value=show_corners if type(show_corners) is bool else True)
        self.hub_status = tk.StringVar(value="Réception fermée")
        self.pilot_status = tk.StringVar(value="Prêt à connecter ta PS5")
        self.link_status = tk.StringVar(value="Transmission ingénieur inactive")
        self.circuit_status = tk.StringVar(value="Sélectionne le circuit de la séance")
        self.recognition = tk.BooleanVar(value=False)
        self.recognition_status = tk.StringVar(value="Reconnaissance limitée aux tracés installés")
        self.suggested = tk.StringVar(value="")
        self.connection_status = tk.StringVar(value="Invitation non vérifiée")
        self.trial_status = tk.StringVar(value="Un seul PC suffit pour vérifier la chaîne avec ta PS5.")
        self.summary = tk.StringVar(value="0 / 4 voitures en direct")
        self._build()
        if self.circuit.get() in self.library.entrees:
            self.select_circuit()
        if demo:
            self.toggle_demo()
        self.root.after(100, self.tick)

    def _styles(self):
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("TFrame", background=BG)
        style.configure("TLabel", background=BG, foreground=FG, font=(FONT, 10))
        style.configure("TButton", background="#263c53", foreground=FG, padding=(12, 7), font=(FONT, 10))
        style.map("TButton", background=[("active", "#365673")])
        style.configure("TEntry", fieldbackground="#1d2d40", foreground=FG, insertcolor=FG, padding=5)
        style.configure("TCombobox", fieldbackground="#1d2d40", background="#263c53", foreground=FG, padding=4)
        style.map("TCombobox", fieldbackground=[("readonly", "#1d2d40")], foreground=[("readonly", FG)])
        style.configure("TNotebook", background=BG, borderwidth=0)
        style.configure("TNotebook.Tab", background="#17283a", foreground=MUTED, padding=(20, 10))
        style.map("TNotebook.Tab", background=[("selected", "#263c53")], foreground=[("selected", FG)])
        style.configure("TCheckbutton", background=BG, foreground=FG)

    def _build(self):
        top = tk.Frame(self.root, bg=BG, padx=22, pady=14)
        top.pack(fill="x")
        tk.Label(top, text="GT7  /  RACE CONTROL ÉCURIE", bg=BG, fg=FG,
                 font=(FONT, 21, "bold")).pack(side="left")
        ttk.Button(top, text="Démonstration", command=self.toggle_demo).pack(side="right")
        tk.Label(top, text=f"{__version__} TEST", bg=BG, fg=COLORS[0], font=(FONT, 10, "bold")).pack(side="right", padx=18)
        self.banner = tk.Label(self.root, text="", bg="#533b12", fg="#ffe2a3",
                               font=(FONT, 11, "bold"), pady=7)
        self.tabs = ttk.Notebook(self.root)
        self.tabs.pack(fill="both", expand=True, padx=16)
        engineer, pilot, settings, journal = (ttk.Frame(self.tabs, padding=12) for _ in range(4))
        for frame, title in ((engineer, "Poste ingénieur"), (pilot, "Poste pilote"),
                              (settings, "Équipe et affichage"), (journal, "Diagnostic")):
            self.tabs.add(frame, text=title)
        self._engineer_controls(engineer)
        self._dashboard(engineer)
        self._pilot_controls(pilot)
        self._settings_controls(settings)
        self.journal = tk.Text(journal, bg=PANEL, fg=MUTED, insertbackground=FG,
                               font=("Consolas", 10), relief="flat", state="disabled", wrap="word")
        self.journal.pack(fill="both", expand=True)
        footer = tk.Frame(self.root, bg=BG, padx=20, pady=9)
        footer.pack(fill="x")
        tk.Label(footer, textvariable=self.summary, bg=BG, fg=MUTED,
                 font=(FONT, 10)).pack(side="left")
        tk.Label(footer, text="Pneus : couleurs indicatives · Stratégie : estimation", bg=BG, fg=MUTED,
                 font=(FONT, 9)).pack(side="right")

    def _engineer_controls(self, parent):
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=(0, 8))
        ttk.Label(row, text="Adresse joignable par les pilotes").pack(side="left")
        ttk.Entry(row, textvariable=self.host, width=24).pack(side="left", padx=7)
        ttk.Label(row, text="Port").pack(side="left")
        ttk.Entry(row, textvariable=self.port, width=6).pack(side="left", padx=7)
        self.hub_button = ttk.Button(row, text="Ouvrir la réception", command=self.toggle_hub)
        self.hub_button.pack(side="left", padx=7)
        ttk.Label(row, textvariable=self.hub_status).pack(side="left", padx=8)
        invites = ttk.Frame(parent)
        invites.pack(fill="x", pady=(0, 9))
        ttk.Label(invites, text="Invitation pour la voiture").pack(side="left")
        ttk.Combobox(invites, textvariable=self.slot, values=(1, 2, 3, 4), width=3,
                     state="readonly").pack(side="left", padx=7)
        ttk.Button(invites, text="Copier l'invitation", command=self.copy_invitation).pack(side="left")
        ttk.Button(invites, text="Renouveler l'accès", command=self.rotate_invitation).pack(side="left", padx=7)
        ttk.Label(invites, text="À distance : cette adresse et ce port doivent être accessibles depuis Internet.",
                  foreground=MUTED).pack(side="left", padx=5)
        circuit_row = ttk.Frame(parent)
        circuit_row.pack(fill="x", pady=(0, 8))
        ttk.Label(circuit_row, text="Circuit").pack(side="left")
        self.circuit_combo = ttk.Combobox(circuit_row, textvariable=self.circuit,
                                         values=self.library.noms, width=39, state="readonly")
        self.circuit_combo.pack(side="left", padx=7)
        self.circuit_combo.bind("<<ComboboxSelected>>", lambda _: self.select_circuit())
        ttk.Button(circuit_row, text="Télécharger le tracé", command=self.download_circuit).pack(side="left")
        count = sum(self.library.est_disponible(n) for n in self.library.noms)
        ttk.Button(circuit_row, text=f"Tous les tracés ({count})", command=self.download_all_circuits).pack(side="left", padx=7)
        ttk.Label(circuit_row, textvariable=self.circuit_status, foreground=MUTED).pack(side="left", padx=10)
        recognition_row = ttk.Frame(parent)
        recognition_row.pack(fill="x", pady=(0, 8))
        ttk.Checkbutton(recognition_row, text="Reconnaître le circuit", variable=self.recognition,
                        command=self.toggle_matching).pack(side="left")
        self.suggestion_combo = ttk.Combobox(recognition_row, textvariable=self.suggested,
                                             values=(), width=29, state="readonly")
        self.suggestion_combo.pack(side="left", padx=8)
        ttk.Button(recognition_row, text="Appliquer", command=self.apply_suggestion).pack(side="left")
        ttk.Label(recognition_row, textvariable=self.recognition_status,
                  foreground=MUTED).pack(side="left", padx=10)

    def _dashboard(self, parent):
        self.body = tk.Frame(parent, bg=BG)
        self.body.pack(fill="both", expand=True)
        self.body.columnconfigure(0, weight=1, minsize=290)
        self.body.columnconfigure(1, weight=2, minsize=390)
        self.body.columnconfigure(2, weight=1, minsize=290)
        for row in (0, 1):
            self.body.rowconfigure(row, weight=1)
        self.cards = {i: CarCard(self.body, i) for i in range(1, 5)}
        map_frame = tk.Frame(self.body, bg="#101d2b", highlightthickness=1,
                             highlightbackground="#25394e")
        map_frame.grid(row=0, column=1, rowspan=2, sticky="nsew", padx=10, pady=4)
        map_header = tk.Frame(map_frame, bg="#101d2b", padx=12, pady=10)
        map_header.pack(fill="x")
        tk.Label(map_header, text="CARTE DE LA SÉANCE", bg="#101d2b", fg=MUTED,
                 font=(FONT, 11, "bold")).pack(side="left")
        tk.Checkbutton(map_header, text="Virages", variable=self.show_corners,
                       command=self.map_orientation_changed, bg="#101d2b", fg=MUTED,
                       activebackground="#101d2b", activeforeground=FG,
                       selectcolor=PANEL, font=(FONT, 9)).pack(side="right")
        self.canvas = tk.Canvas(map_frame, bg="#101d2b", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda _: self.draw_track())
        tk.Label(map_frame, text="Les quatre pilotes doivent rouler sur le circuit sélectionné.",
                 bg="#101d2b", fg=MUTED, font=(FONT, 9), wraplength=370,
                 pady=10).pack(fill="x")

    def _pilot_controls(self, parent):
        ttk.Label(parent, text="TA PS5 → TON PC → LE POSTE INGÉNIEUR", font=(FONT, 18, "bold")).pack(anchor="w", pady=(8, 20))
        ttk.Label(parent, text="Connecte ce PC au même réseau que ta console et lance GT7 sur la piste.").pack(anchor="w", pady=5)
        grid = ttk.Frame(parent)
        grid.pack(fill="x", pady=20)
        for row, (label, var) in enumerate((("Adresse IPv4 de la PS5", self.ps5_ip),
                                            ("Invitation reçue de l'ingénieur", self.invitation))):
            ttk.Label(grid, text=label).grid(row=row, column=0, sticky="w", pady=10)
            ttk.Entry(grid, textvariable=var, width=78, show="" if row == 0 else "•").grid(row=row, column=1, sticky="ew", padx=20)
        grid.columnconfigure(1, weight=1)
        ttk.Label(parent, text="Adresse PS5 vide : recherche sur le réseau local. Invitation vide : essai local sans transmission.",
                  foreground=MUTED).pack(anchor="w")
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=18)
        ttk.Label(row, text="Voiture dans l'aperçu local").pack(side="left")
        ttk.Combobox(row, textvariable=self.local_car, values=(1, 2, 3, 4), width=3,
                     state="readonly").pack(side="left", padx=12)
        ttk.Checkbutton(row, text="Enregistrer la télémétrie complète en CSV", variable=self.record).pack(side="left", padx=20)
        connection = ttk.Frame(parent)
        connection.pack(fill="x", pady=8)
        self.pilot_button = ttk.Button(connection, text="Connecter la PS5", command=self.toggle_pilot)
        self.pilot_button.pack(side="left")
        ttk.Button(connection, text="Vérifier l'invitation", command=self.test_connection).pack(side="left", padx=12)
        ttk.Label(parent, textvariable=self.connection_status, foreground=MUTED).pack(anchor="w", pady=4)
        ttk.Label(parent, textvariable=self.pilot_status, font=(FONT, 12)).pack(anchor="w", pady=15)
        ttk.Label(parent, textvariable=self.link_status, foreground=COLORS[0]).pack(anchor="w", pady=4)
        self.diagnostic = tk.StringVar(value="Aucun paquet reçu")
        ttk.Label(parent, textvariable=self.diagnostic, foreground=MUTED).pack(anchor="w", pady=4)
        ttk.Button(parent, text="Ouvrir le dossier des sessions", command=self.open_sessions).pack(anchor="w", pady=15)
        ttk.Separator(parent).pack(fill="x", pady=12)
        ttk.Label(parent, text="ESSAI COMPLET SUR UN SEUL PC", font=(FONT, 13, "bold")).pack(anchor="w", pady=6)
        ttk.Label(parent, text="Ta PS5 fournit les données. Elles traversent la liaison chiffrée avant d'arriver au mur des stands.",
                  foreground=MUTED, wraplength=950).pack(anchor="w", pady=5)
        self.trial_button = ttk.Button(parent, text="Démarrer l'essai complet", command=self.toggle_local_trial)
        self.trial_button.pack(anchor="w", pady=7)
        ttk.Label(parent, textvariable=self.trial_status, foreground=COLORS[0]).pack(anchor="w", pady=5)
        ttk.Label(parent, text="Les invitations sont propres à chaque voiture. Le numéro reçu par l'ingénieur est fixé par son invitation.",
                  foreground=MUTED, wraplength=950).pack(anchor="w", pady=12)

    def _settings_controls(self, parent):
        ttk.Label(parent, text="TON ÉQUIPE", font=(FONT, 18, "bold")).pack(anchor="w", pady=(10, 20))
        for i, name in enumerate(self.names, 1):
            row = ttk.Frame(parent)
            row.pack(fill="x", pady=6)
            ttk.Label(row, text=f"Voiture {i:02d}", width=15).pack(side="left")
            ttk.Entry(row, textvariable=name, width=30).pack(side="left")
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=20)
        ttk.Label(row, text="Rotation de la carte").pack(side="left")
        combo = ttk.Combobox(row, textvariable=self.rotation, values=("0", "90", "180", "270"), width=5, state="readonly")
        combo.pack(side="left", padx=15)
        combo.bind("<<ComboboxSelected>>", lambda _: self.draw_track())
        ttk.Checkbutton(row, text="Miroir horizontal du tracé et des voitures",
                        variable=self.mirror_horizontal,
                        command=self.map_orientation_changed).pack(side="left", padx=15)
        ttk.Label(parent, text="L'affichage s'adapte aux voitures connectées : une de chaque côté à deux, deux de chaque côté à quatre.",
                  foreground=MUTED).pack(anchor="w", pady=8)
        ttk.Button(parent, text="Enregistrer mes préférences", command=self.save_settings).pack(anchor="w", pady=12)
        ttk.Button(parent, text="Ouvrir l'interface historique V4", command=self.open_legacy).pack(anchor="w", pady=(35, 5))

    def post(self, kind, value):
        if self.closed:
            return
        if kind != "log":
            self.actions.put((kind, value))
            return
        try:
            self.events.put_nowait((kind, value))
        except queue.Full:
            pass

    def log(self, text):
        self.post("log", str(text))

    def pilot_message(self, text):
        self.log(text)
        self.post("pilot_message", text)

    def work(self, operation, success, *, catalog=False):
        if (catalog and self.catalog_busy) or (not catalog and self.busy):
            return
        if catalog:
            self.catalog_busy = True
        else:
            self.busy = True

        def run():
            try:
                result = operation()
                self.post("catalog_done" if catalog else "done", (success, result))
            except Exception as exc:
                self.post("catalog_failure" if catalog else "failure", str(exc))

        threading.Thread(target=run, name="GT7-Action", daemon=True).start()

    def toggle_hub(self):
        if self.busy:
            return
        if self.local_trial:
            self.toggle_local_trial()
            return
        if self.demo or (self.pilot and self.pilot.running):
            messagebox.showinfo("Réception", "Arrête la démonstration ou la connexion pilote avant d'ouvrir la réception.", parent=self.root)
            return
        if self.hub:
            hub = self.hub
            self.work(hub.stop, lambda _: self._hub_stopped())
            return
        try:
            port = int(self.port.get())
            if not 1024 <= port <= 65535:
                raise ValueError
        except ValueError:
            messagebox.showerror("Port", "Choisis un port compris entre 1024 et 65535.", parent=self.root)
            return
        self.hub_status.set("Ouverture…")

        def start():
            hub = EngineerHub(self.fleet.ingest, self.data_dir / "reception", port=port)
            hub.start()
            return hub

        def started(hub):
            self.hub = hub
            self.hub_button.config(text="Fermer la réception")
            self.hub_status.set(f"Réception chiffrée ouverte · port {hub.port}")
            self.log("Réception ingénieur ouverte. Une invitation distincte est disponible pour chaque voiture.")
            self.save_settings(quiet=True)

        self.work(start, started)

    def _hub_stopped(self):
        self.hub = None
        self.fleet.reset()
        self.hub_button.config(text="Ouvrir la réception")
        self.hub_status.set("Réception fermée")

    def copy_invitation(self):
        if self.local_trial:
            messagebox.showinfo("Essai local", "L'invitation de cet essai reste sur ce PC. Arrête l'essai pour préparer les invitations de l'équipe.", parent=self.root)
            return
        if not self.hub:
            messagebox.showinfo("Invitation", "Ouvre d'abord la réception ingénieur.", parent=self.root)
            return
        try:
            invite = self.hub.invitation(int(self.slot.get()), self.host.get().strip())
            self.root.clipboard_clear()
            self.root.clipboard_append(invite)
            self.hub_status.set(f"Invitation voiture {self.slot.get()} copiée")
        except ValueError as exc:
            messagebox.showerror("Invitation", str(exc), parent=self.root)

    def rotate_invitation(self):
        if self.local_trial:
            messagebox.showinfo("Essai local", "L'accès de l'essai local est géré automatiquement. Arrête l'essai pour gérer les invitations de l'équipe.", parent=self.root)
            return
        if not self.hub:
            return
        self.hub.rotate_token(int(self.slot.get()))
        self.copy_invitation()
        self.log(f"Accès renouvelé pour la voiture {self.slot.get()} ; utiliser sa nouvelle invitation.")

    def toggle_pilot(self):
        if self.busy:
            return
        if self.local_trial:
            self.toggle_local_trial()
            return
        if self.demo or self.hub:
            messagebox.showinfo("Poste pilote", "Arrête la démonstration ou ferme la réception ingénieur avant de connecter une PS5.", parent=self.root)
            return
        if self.pilot and self.pilot.running:
            self.work(self.pilot.stop, lambda _: self._pilot_stopped())
            return
        ip = self.ps5_ip.get().strip()
        if ip:
            try:
                ipaddress.IPv4Address(ip)
            except ValueError:
                messagebox.showerror("PS5", "Entre une adresse IPv4 valide, ou laisse le champ vide pour la recherche locale.", parent=self.root)
                return
        invitation = self.invitation.get().strip() or None
        car = int(self.local_car.get())
        record = self.record.get()
        self.fleet = FleetMonitor()
        fleet = self.fleet
        self.reset_recognition_history()
        self.pilot_status.set("Connexion en cours…")

        def start():
            pilot = PilotSession(fleet, self.data_dir, car_number=car, on_status=self.pilot_message)
            pilot.start(ip or None, invitation=invitation, record=record)
            return pilot

        def started(pilot):
            self.pilot = pilot
            self.pilot_button.config(text="Arrêter la connexion")
            self.pilot_status.set("À l'écoute de la PS5 · démarre le roulage dans GT7")
            if not invitation:
                self.link_status.set("Essai local · aucune transmission à l'ingénieur")
            self.save_settings(quiet=True)

        self.work(start, started)

    def _pilot_stopped(self):
        self.fleet.reset()
        self.pilot_button.config(text="Connecter la PS5")
        self.pilot_status.set("Connexion arrêtée · session CSV fermée")
        self.link_status.set("Transmission ingénieur arrêtée")

    def _validated_ps5_ip(self):
        value = self.ps5_ip.get().strip()
        if value:
            ipaddress.IPv4Address(value)
        return value or None

    def test_connection(self):
        if self.busy:
            return
        invitation = self.invitation.get().strip()
        self.connection_status.set("Vérification de la liaison chiffrée…")

        def checked(result):
            self.connection_status.set(result.message)
            self.log("Vérification de l'invitation : " + result.message)

        self.work(lambda: check_invitation(invitation), checked)

    def toggle_local_trial(self):
        if self.busy:
            return
        if self.local_trial:
            trial = self.local_trial

            def stopped(_):
                self.local_trial = None
                self.pilot = None
                self._hub_stopped()
                self._pilot_stopped()
                self.banner.pack_forget()
                self.trial_button.config(text="Démarrer l'essai complet")
                self.trial_status.set("Essai arrêté · les sessions CSV ont été fermées.")

            self.work(trial.stop, stopped)
            return
        if self.demo or self.hub or (self.pilot and self.pilot.running):
            messagebox.showinfo("Essai complet", "Arrête les connexions ou la démonstration avant de commencer l'essai complet.", parent=self.root)
            return
        try:
            ip = self._validated_ps5_ip()
        except ValueError:
            messagebox.showerror("PS5", "Entre une adresse IPv4 valide, ou laisse le champ vide pour la recherche locale.", parent=self.root)
            return
        self.fleet = FleetMonitor()
        fleet, car, record = self.fleet, int(self.local_car.get()), self.record.get()
        self.reset_recognition_history()
        self.trial_status.set("Ouverture de la capture et de la liaison chiffrée…")

        def start():
            trial = LocalTrial(fleet, self.data_dir, car_number=car, on_status=self.pilot_message)
            trial.start(ip, record=record)
            return trial

        def started(trial):
            self.local_trial = trial
            self.pilot, self.hub = trial.pilot, trial.hub
            self.trial_button.config(text="Arrêter l'essai complet")
            self.pilot_button.config(text="Arrêter l'essai complet")
            self.hub_button.config(text="Arrêter l'essai complet")
            self.hub_status.set("Essai sur ce PC · réception locale chiffrée")
            self.pilot_status.set("À l'écoute de ta PS5")
            self.trial_status.set("Lance le roulage : le mur des stands attend les données reçues par la liaison chiffrée.")
            self.banner.config(text="ESSAI LOCAL · VRAIE PS5 · TRANSMISSION CHIFFRÉE SUR CE PC")
            self.banner.pack(before=self.tabs, fill="x", padx=16, pady=(0, 8))
            self.save_settings(quiet=True)

        self.work(start, started)

    def toggle_demo(self):
        if self.busy or self.hub or (self.pilot and self.pilot.running):
            messagebox.showinfo("Démonstration", "Arrête les connexions en cours avant de changer de mode.", parent=self.root)
            return
        self.demo = not self.demo
        self.fleet = FleetMonitor()
        self.reset_recognition_history()
        self.demo_start = time.monotonic()
        if self.demo:
            self.banner.config(text="DÉMONSTRATION · QUATRE VOITURES SIMULÉES · AUCUNE PS5 CONNECTÉE")
            self.banner.pack(before=self.tabs, fill="x", padx=16, pady=(0, 8))
            self.points = demo_data.circuit_points()
            self.corners = ()
            self.circuit_status.set("Circuit fictif de démonstration")
        else:
            self.banner.pack_forget()
            self.select_circuit()
        self.circuit_combo.config(state="disabled" if self.demo else "readonly")
        self.layout_key = None
        self.draw_track()

    def select_circuit(self):
        if self.demo:
            return
        name = self.circuit.get()
        self.points = []
        self.corners = ()
        if name in self.library.entrees:
            self.points = self.library.charger_points(name)
            self.corners = self.library.charger_virages(name)
            if self.points:
                corner_info = f" · {len(self.corners)} virages repérés" if self.corners else " · virages non annotés"
                self.circuit_status.set("Tracé installé" + corner_info)
            elif self.library.est_disponible(name):
                self.circuit_status.set("Tracé disponible à télécharger")
            else:
                self.circuit_status.set("Bordures non disponibles dans la bibliothèque")
        else:
            self.circuit_status.set("Sélectionne le circuit de la séance")
        self.draw_track()

    def download_circuit(self):
        if self.demo or self.catalog_busy:
            return
        name = self.circuit.get()
        if name not in self.library.entrees or not self.library.est_disponible(name):
            messagebox.showinfo("Circuit", "Choisis un circuit dont le tracé est disponible.", parent=self.root)
            return
        self.circuit_status.set("Téléchargement en cours…")
        self.work(lambda: self.library.installer(name), lambda _: self._circuits_updated(), catalog=True)

    def _circuits_updated(self):
        self.select_circuit()
        self.matcher = None
        self.suggested.set("")
        self.suggestion_combo.config(values=())
        if self.recognition.get():
            self.toggle_matching()

    def download_all_circuits(self):
        if self.demo or self.catalog_busy:
            return
        self._bulk_cancel.clear()
        self.circuit_status.set("Installation des tracés disponibles…")

        def progress(done, total, name, status):
            self.post("circuit_progress", f"{done}/{total} · {status} · {name}")

        def finished(result):
            self._circuits_updated()
            total = len(result.installed) + len(result.already_present)
            self.circuit_status.set(f"{total} tracés prêts · {len(result.failed)} échecs")
            self.log(f"Circuits : {len(result.installed)} installés, {len(result.already_present)} déjà présents, {len(result.failed)} échecs.")
            for name in result.failed:
                self.log(f"Circuit non installé : {name}. Une nouvelle tentative reste possible.")

        self.work(lambda: self.library.installer_tous(progress=progress, cancel=self._bulk_cancel), finished, catalog=True)

    def toggle_matching(self):
        if not self.recognition.get():
            self.matcher = None
            self.suggested.set("")
            self.suggestion_combo.config(values=())
            self.recognition_status.set("Reconnaissance désactivée")
            return
        if self.demo:
            self.recognition_status.set("La reconnaissance utilise les données réelles, hors démonstration")
            return
        if self.busy:
            self.root.after(300, self.toggle_matching)
            return
        self.recognition_status.set("Lecture des tracés installés…")

        def ready(matcher):
            if self.recognition.get() and not self.demo:
                self.matcher = matcher
                self._match_car = None
                self.recognition_status.set("Roule quelques virages pour obtenir une proposition")

        self.work(lambda: TrackMatcher(self.library), ready)

    def apply_suggestion(self):
        suggestion = self.suggested.get()
        if not self.demo and suggestion in self.library.entrees:
            self.circuit.set(suggestion)
            self.select_circuit()
            self.log("Circuit choisi depuis la proposition de reconnaissance : " + suggestion)

    def reset_recognition_history(self):
        if self.matcher is not None:
            self.matcher.reset(keep_stable=False)
        self._match_car = None
        self.suggested.set("")
        self.suggestion_combo.config(values=())

    def update_recognition(self, cars):
        if self.demo or not self.recognition.get() or self.matcher is None:
            return
        source = next((s for s in cars if s.connected and s.position_x is not None and s.position_z is not None), None)
        if source is None:
            if self._match_car is not None:
                self.reset_recognition_history()
            self.recognition_status.set("En attente de coordonnées réelles")
            return
        if source.number != self._match_car:
            self.matcher.reset(keep_stable=False)
            self._match_car = source.number
        result = self.matcher.observe(source.position_x, source.position_z)
        self.recognition_status.set(result.message)
        candidates = result.candidates if result.status in ("candidate", "ambiguous") else ()
        self.suggestion_combo.config(values=candidates)
        if result.status == "candidate" and result.candidate:
            self.suggested.set(result.candidate)
        elif self.suggested.get() not in candidates:
            self.suggested.set("")

    def _rotate(self, x, z):
        angle = math.radians(float(self.rotation.get()))
        rotated_x = x * math.cos(angle) - z * math.sin(angle)
        rotated_z = x * math.sin(angle) + z * math.cos(angle)
        return (-rotated_x if self.mirror_horizontal.get() else rotated_x, rotated_z)

    def map_orientation_changed(self):
        self.draw_track()
        self.save_settings(quiet=True)

    def draw_track(self):
        if self.closed:
            return
        self.canvas.delete("track")
        self.canvas.delete("corners")
        self.canvas.delete("cars")
        self.transform = None
        w, h = self.canvas.winfo_width(), self.canvas.winfo_height()
        if not self.points or w < 20 or h < 20:
            self.canvas.create_text(max(140, w / 2), max(80, h / 2),
                                     text="Le tracé apparaîtra ici\naprès sélection et téléchargement.",
                                     fill=MUTED, justify="center", font=(FONT, 11), tags="track")
            return
        rotated = [self._rotate(x, z) for x, z, _ in self.points]
        xs, zs = zip(*rotated)
        xmin, xmax, zmin, zmax = min(xs), max(xs), min(zs), max(zs)
        scale = min((w - 54) / max(1, xmax - xmin), (h - 54) / max(1, zmax - zmin))
        cx, cz = (xmin + xmax) / 2, (zmin + zmax) / 2
        self.transform = lambda x, z: (w / 2 + (x - cx) * scale, h / 2 - (z - cz) * scale)
        stride = max(1, len(rotated) // 6000)
        for i in range(0, len(rotated), stride):
            px, pz = self.transform(*rotated[i])
            self.canvas.create_oval(px - 1, pz - 1, px + 1, pz + 1,
                                    fill="#66869d", outline="", tags="track")
        self.draw_corners(w, h)

    def draw_corners(self, width, height):
        if not self.show_corners.get() or self.transform is None:
            return
        placed = []
        radius = 10
        for corner in self.corners:
            x, y = self.transform(*self._rotate(corner.x, corner.z))
            if not (0 <= x <= width and 0 <= y <= height):
                continue
            choices = []
            for distance in (22, 36, 50):
                for angle in (0, 45, 90, 135, 180, 225, 270, 315):
                    a = math.radians(angle)
                    lx, ly = x + distance * math.cos(a), y + distance * math.sin(a)
                    if radius + 3 <= lx <= width - radius - 3 and radius + 3 <= ly <= height - radius - 3:
                        overlap = sum(max(0, 25 - math.hypot(lx - px, ly - py)) for px, py in placed)
                        choices.append((overlap * 100 + distance, lx, ly))
            if not choices:
                continue
            _, lx, ly = min(choices)
            placed.append((lx, ly))
            self.canvas.create_line(x, y, lx, ly, fill="#9aaebe", width=1,
                                    tags=("corners", f"turn-anchor-{corner.n}"))
            self.canvas.create_oval(lx - radius, ly - radius, lx + radius, ly + radius,
                                    fill="#e5edf4", outline="#152233", width=1,
                                    tags=("corners", f"turn-{corner.n}"))
            self.canvas.create_text(lx, ly, text=str(corner.n), fill="#152233",
                                    font=(FONT, 9, "bold"),
                                    tags=("corners", f"turn-label-{corner.n}"))

    def draw_cars(self, cars):
        self.canvas.delete("cars")
        if self.transform is None:
            return
        for s in cars:
            if not s.connected or s.position_x is None or s.position_z is None:
                continue
            x, y = self.transform(*self._rotate(s.position_x, s.position_z))
            if not (0 <= x <= self.canvas.winfo_width() and 0 <= y <= self.canvas.winfo_height()):
                continue
            self.canvas.create_oval(x - 13, y - 13, x + 13, y + 13,
                                    fill=COLORS[s.number - 1], outline=BG, width=2, tags="cars")
            self.canvas.create_text(x, y, text=str(s.number), fill=BG,
                                    font=(FONT, 10, "bold"), tags="cars")

    def update_layout(self, cars):
        shown = [s.number for s in cars if s.connected or s.has_data]
        if not shown:
            shown = [1, 2, 3, 4]
        key = tuple(shown)
        if self.layout_key == key:
            return
        self.layout_key = key
        for card in self.cards.values():
            card.grid_forget()
        positions = ((0, 0), (0, 2), (1, 0), (1, 2))
        for i, car in enumerate(shown):
            row, col = positions[i]
            self.cards[car].grid(row=row, column=col, sticky="nsew", pady=4)

    def tick(self):
        if self.closed:
            return
        for _ in range(100):
            try:
                kind, value = self.actions.get_nowait()
            except queue.Empty:
                try:
                    kind, value = self.events.get_nowait()
                except queue.Empty:
                    break
            if kind == "done":
                self.busy = False
                callback, result = value
                callback(result)
            elif kind == "catalog_done":
                self.catalog_busy = False
                callback, result = value
                callback(result)
            elif kind == "catalog_failure":
                self.catalog_busy = False
                self.circuit_status.set("Installation interrompue · voir Diagnostic")
                self.log("Installation de circuits interrompue : " + value)
                messagebox.showerror("Circuits", value, parent=self.root)
            elif kind == "failure":
                self.busy = False
                self.log("Action interrompue : " + value)
                self.hub_status.set("Action impossible · voir Diagnostic")
                self.pilot_status.set("Action impossible · voir Diagnostic")
                messagebox.showerror("Action impossible", value, parent=self.root)
            elif kind == "pilot_message":
                if value.startswith(("Télémétrie transmise", "Ingénieur injoignable", "Invitation refusée",
                                     "Certificat ingénieur", "Transmission arrêtée", "En attente des données",
                                     "En attente de données", "Mesure PS5 invalide")):
                    self.link_status.set(value)
            elif kind == "circuit_progress":
                self.circuit_status.set(value)
            elif kind == "log":
                self.journal.config(state="normal")
                self.journal.insert("end", time.strftime("%H:%M:%S") + "  " + value + "\n")
                if int(self.journal.index("end-1c").split(".")[0]) > 350:
                    self.journal.delete("1.0", "40.0")
                self.journal.see("end")
                self.journal.config(state="disabled")
        if self.demo:
            elapsed = time.monotonic() - self.demo_start
            for i in range(1, 5):
                self.fleet.ingest(i, demo_data.sample(i, elapsed))
        cars = self.fleet.snapshots()
        for s in cars:
            self.cards[s.number].update_data(s, self.names[s.number - 1].get())
        self.update_layout(cars)
        self.draw_cars(cars)
        self.update_recognition(cars)
        connected = sum(s.connected for s in cars)
        self.summary.set(f"{connected} / 4 voitures {'simulées' if self.demo else 'en direct'}")
        if self.pilot and self.pilot.running:
            d = self.pilot.diagnostics.snapshot()
            errors = d.packet_format_errors + d.decryption_errors + d.decode_errors
            self.diagnostic.set(f"{d.decoded_packets} paquets décodés · {d.packets_per_second:.1f} Hz · "
                                f"{errors} erreurs · {self.pilot.rows_written} lignes CSV")
            recording_error = getattr(self.pilot, "recording_error", None)
            rows_dropped = getattr(self.pilot, "rows_dropped", 0)
            if recording_error or rows_dropped:
                self.diagnostic.set(self.diagnostic.get() + " · CSV incomplet : voir Diagnostic")
            local_fleet = getattr(self.pilot, "fleet", self.fleet)
            local = local_fleet.snapshots()[self.pilot.car_number - 1]
            if local.has_data:
                self.pilot_status.set("PS5 en direct" if local.connected else "Signal PS5 interrompu · reconnexion en attente")
            if self.local_trial:
                remote = cars[self.pilot.car_number - 1]
                if local.connected and remote.connected:
                    self.trial_status.set("Chaîne locale complète en direct : PS5 → capture → liaison chiffrée → ingénieur")
                elif local.connected:
                    self.trial_status.set("PS5 reçue · en attente de la transmission chiffrée")
                elif local.has_data:
                    self.trial_status.set("Signal PS5 interrompu · aucune ancienne donnée présentée comme récente")
        self.root.after(100, self.tick)

    def save_settings(self, quiet=False):
        config = {"ps5_ip": self.ps5_ip.get().strip(), "host": self.host.get().strip(),
                  "port": self.port.get(), "circuit": self.circuit.get(),
                  "rotation": self.rotation.get(), "local_car": self.local_car.get(),
                  "mirror_horizontal": self.mirror_horizontal.get(),
                  "show_corners": self.show_corners.get(),
                  "invitation": self.invitation.get().strip()}
        config.update({f"driver_{i}": name.get()[:32] for i, name in enumerate(self.names, 1)})
        try:
            path = self.config_file.with_suffix(".tmp")
            with open(path, "w", encoding="utf-8", opener=lambda p, f: os.open(p, f, 0o600)) as stream:
                json.dump(config, stream, ensure_ascii=False, indent=2)
            path.replace(self.config_file)
            if not quiet:
                self.log("Préférences enregistrées.")
        except OSError:
            self.log("Impossible d'enregistrer les préférences.")

    def open_sessions(self):
        path = self.data_dir / "sessions"
        path.mkdir(parents=True, exist_ok=True)
        if sys_platform_windows():
            os.startfile(str(path))
        else:
            messagebox.showinfo("Sessions", str(path), parent=self.root)

    def open_legacy(self):
        if self.busy or self.catalog_busy or self.hub or self.demo or (self.pilot and self.pilot.running):
            messagebox.showinfo("Interface historique", "Arrête les connexions et la démonstration avant d'ouvrir l'ancienne interface.", parent=self.root)
            return
        from race_control_live_V4_INTERFACE import RaceControlLive
        window = tk.Toplevel(self.root)
        app = RaceControlLive(window)
        self.legacy_windows.append((window, app))
        self.root.withdraw()

        def return_to_app():
            app.fermer_application()
            self.root.deiconify()

        window.protocol("WM_DELETE_WINDOW", return_to_app)

    def close(self):
        if self.closed:
            return
        if self.busy or self.catalog_busy:
            self._bulk_cancel.set()
            self.root.after(200, self.close)
            return
        self.save_settings(quiet=True)
        self.closed = True
        for window, app in self.legacy_windows:
            try:
                if window.winfo_exists():
                    app.fermer_application()
            except tk.TclError:
                pass
        try:
            if self.local_trial:
                self.local_trial.stop()
            else:
                try:
                    if self.pilot:
                        self.pilot.stop()
                finally:
                    if self.hub:
                        self.hub.stop()
        finally:
            self.root.destroy()


def sys_platform_windows():
    return os.name == "nt"
