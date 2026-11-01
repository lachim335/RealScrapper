#!/usr/bin/env python3
"""Interactive launcher for the project scripts."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Callable


ROOT = Path(__file__).resolve().parent
SCRIPTS = {
    "1": ("Scraper — sběr inzerátů", ROOT / "scraper.py"),
    "2": ("Webový server", ROOT / "server.py"),
    "3": ("Pročištění databáze", ROOT / "cleanup.py"),
}
Input = Callable[[str], str]


def scraper_command(config: str, interval: str) -> list[str]:
    command = [sys.executable, str(ROOT / "scraper.py"), "--config", config]
    if interval:
        command.extend(("--interval", interval))
    return command


def server_command(host: str, port: str, database: str) -> list[str]:
    command = [
        sys.executable,
        str(ROOT / "server.py"),
        "--host",
        host,
        "--port",
        port,
    ]
    if database:
        command.extend(("--db", database))
    return command


def cleanup_command(database: str, apply: bool) -> list[str]:
    command = [sys.executable, str(ROOT / "cleanup.py"), "--db", database]
    if apply:
        command.append("--apply")
    return command


def ask(prompt: str, default: str, input_fn: Input = input) -> str:
    value = input_fn(f"{prompt} [{default}]: ").strip()
    return value or default


def ask_integer(
    prompt: str,
    default: str,
    minimum: int,
    maximum: int | None = None,
    input_fn: Input = input,
) -> str:
    while True:
        value = ask(prompt, default, input_fn)
        try:
            number = int(value)
        except ValueError:
            print("Zadej celé číslo.")
            continue
        if number < minimum or (maximum is not None and number > maximum):
            limit = (
                f"{minimum} až {maximum}"
                if maximum is not None
                else f"nejméně {minimum}"
            )
            print(f"Hodnota musí být {limit}.")
            continue
        return str(number)


def choose_script(input_fn: Input = input) -> str | None:
    while True:
        print("\nCo chceš spustit?")
        for key, (label, _) in SCRIPTS.items():
            print(f"  {key}) {label}")
        print("  4) Zobrazit nápovědu skriptu")
        print("  0) Konec")
        choice = input_fn("Volba: ").strip()
        if choice in {"0", *SCRIPTS.keys(), "4"}:
            return choice
        print("Neplatná volba.")


def build_interactive_command(
    choice: str, input_fn: Input = input
) -> list[str] | None:
    if choice == "1":
        config = ask("Konfigurační soubor", "config.json", input_fn)
        interval = ask("Interval opakování v sekundách; prázdné = jednou", "", input_fn)
        if interval:
            interval = ask_integer(
                "Interval opakování (min. 60 sekund)", interval, 60, input_fn=input_fn
            )
        return scraper_command(config, interval)

    if choice == "2":
        host = ask("Síťové rozhraní", "0.0.0.0", input_fn)
        port = ask_integer("Port", "8000", 1, 65535, input_fn)
        database = ask(
            "Databáze; prázdné = použít config.json", "", input_fn
        )
        return server_command(host, port, database)

    if choice == "3":
        database = ask("SQLite databáze", "data/reality.sqlite3", input_fn)
        apply = input_fn(
            "Provést změny? Vytvoří se záloha databáze [y/N]: "
        ).strip().lower() in {"y", "yes", "a", "ano"}
        return cleanup_command(database, apply)

    if choice == "4":
        print("Kterého skriptu zobrazit nápovědu?")
        for key, (label, _) in SCRIPTS.items():
            print(f"  {key}) {label}")
        selected = input_fn("Volba: ").strip()
        if selected not in SCRIPTS:
            print("Neplatná volba.")
            return None
        script = SCRIPTS[selected][1]
        return [sys.executable, str(script), "--help"]
    return None


def run_command(command: list[str]) -> int:
    print("\nSpouštím:", " ".join(command))
    try:
        result = subprocess.run(command, cwd=ROOT, check=False)
    except OSError as exc:
        print(f"Skript se nepodařilo spustit: {exc}")
        return 1
    if result.returncode:
        print(f"Skript skončil s návratovým kódem {result.returncode}.")
    return result.returncode


def main() -> int:
    print("RealScrapper — interaktivní spouštěč")
    try:
        while True:
            choice = choose_script()
            if choice == "0":
                return 0
            command = build_interactive_command(choice) if choice else None
            if command:
                run_command(command)
    except (EOFError, KeyboardInterrupt):
        print("\nUkončuji spouštěč.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
