"""Point d'entrée commun du logiciel et de l'installateur Windows."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
import traceback

from racecontrol import __version__


def main(argv=None):
    parser = argparse.ArgumentParser(description="GT7 Race Control Écurie")
    parser.add_argument("--demo", action="store_true", help="Afficher quatre voitures simulées")
    parser.add_argument("--smoke-test", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--smoke-report", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    temporary = None
    root = None
    report = {"ok": False, "version": __version__}
    code = 1
    try:
        if args.smoke_test and not os.environ.get("GT7RC_DATA_DIR"):
            temporary = tempfile.TemporaryDirectory(prefix="gt7rc-smoke-")
            os.environ["GT7RC_DATA_DIR"] = temporary.name
        import tkinter as tk
        from racecontrol.app import RaceControlApp
        root = tk.Tk()
        app = RaceControlApp(root, demo=args.demo or args.smoke_test)
        callback_errors = []

        def callback_error(exc_type, exc, tb):
            callback_errors.append(str(exc))
            app.log("Erreur d'interface : " + exc_type.__name__)
            if not args.smoke_test:
                from tkinter import messagebox
                messagebox.showerror("Erreur d'interface", str(exc), parent=root)
            else:
                root.after_idle(root.destroy)

        root.report_callback_exception = callback_error
        if args.smoke_test:
            root.after(1200, app.close)
        root.mainloop()
        if callback_errors:
            raise RuntimeError("Échec d'un événement graphique : " + callback_errors[0])
        report["ok"] = True
        code = 0
    except Exception as exc:
        report["error"] = str(exc)
        if not args.smoke_test:
            try:
                import tkinter as tk
                from tkinter import messagebox
                if root is None:
                    root = tk.Tk()
                    root.withdraw()
                messagebox.showerror("Démarrage impossible", str(exc), parent=root)
            except Exception:
                pass
        traceback.print_exc()
    finally:
        if root is not None:
            try:
                root.destroy()
            except Exception:
                pass
        if args.smoke_report:
            args.smoke_report.parent.mkdir(parents=True, exist_ok=True)
            args.smoke_report.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
        if temporary is not None:
            temporary.cleanup()
    return code


if __name__ == "__main__":
    sys.exit(main())
