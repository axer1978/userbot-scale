"""Run several copies of the assistant side by side, one per Telegram account.

    python main.py                     the default instance (data next to main.py)
    python main.py --instance work     instances/work/ — its own .env, database,
                                       config.json and panel port
    python main.py --list              show the instances that exist

Each instance is a separate process with a separate login, so two accounts
never share a session (which would make both answer the same chats twice).
Resolution happens by setting DATA_DIR / ADMIN_PORT in the environment before
the rest of the app reads them, so nothing else needs to know about instances.
"""

from __future__ import annotations

import argparse
import os
import re
import socket
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
INSTANCES_DIR = BASE_DIR / "instances"
DEFAULT_PORT = 8787
# Named instances start here; the default keeps 8787 so existing bookmarks work.
FIRST_NAMED_PORT = 8788
PORT_FILE = "port"

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,39}$")


def _port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            return True
    return False


def _saved_port(folder: Path) -> int | None:
    try:
        return int((folder / PORT_FILE).read_text().strip())
    except (OSError, ValueError):
        return None


def _taken_ports() -> set[int]:
    """Ports other instances have claimed, so a new one never reuses them."""
    taken = {DEFAULT_PORT}
    if INSTANCES_DIR.is_dir():
        for folder in INSTANCES_DIR.iterdir():
            port = _saved_port(folder)
            if port:
                taken.add(port)
    return taken


def _pick_port() -> int:
    taken = _taken_ports()
    port = FIRST_NAMED_PORT
    while port in taken or _port_in_use(port):
        port += 1
    return port


def list_instances() -> list[tuple[str, int | None, bool]]:
    """(name, port, signed_in) for every instance folder, default first."""
    rows = [("default", int(os.getenv("ADMIN_PORT") or DEFAULT_PORT), (BASE_DIR / ".env").exists())]
    if INSTANCES_DIR.is_dir():
        for folder in sorted(INSTANCES_DIR.iterdir()):
            if folder.is_dir():
                rows.append((folder.name, _saved_port(folder), (folder / ".env").exists()))
    return rows


def resolve(argv: list[str] | None = None) -> str:
    """Parse the command line and export DATA_DIR / ADMIN_PORT for this instance.

    Returns the instance name ("" for the default). Exits with a clear message
    on a bad name or a port already in use.
    """
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="Telegram AI assistant. Run once per Telegram account.",
    )
    parser.add_argument(
        "-i", "--instance", metavar="NAME", default=os.getenv("INSTANCE", ""),
        help="run a separate copy with its own login, data and port (created on first use)",
    )
    parser.add_argument("-p", "--port", type=int, help="panel port (remembered per instance)")
    parser.add_argument("--no-browser", action="store_true", help="do not open the panel in a browser")
    parser.add_argument("--list", action="store_true", help="show existing instances and exit")
    args = parser.parse_args(argv)

    if args.list:
        for name, port, signed_in in list_instances():
            state = "signed in" if signed_in else "not signed in yet"
            print(f"  {name:20} port {port or '?':<6} {state}")
        sys.exit(0)

    if args.no_browser:
        os.environ["NO_BROWSER"] = "1"

    name = args.instance.strip()
    if not name:
        if args.port:
            os.environ["ADMIN_PORT"] = str(args.port)
        port = int(os.getenv("ADMIN_PORT") or DEFAULT_PORT)
        if (os.getenv("ADMIN_HOST") or "127.0.0.1") in ("127.0.0.1", "localhost") and _port_in_use(port):
            sys.exit(
                f"Port {port} is already in use. Is the assistant running already?\n"
                f"Running the same account twice makes it answer every chat twice.\n"
                f"For a second Telegram account use:  python main.py --instance NAME"
            )
        return ""

    if not _NAME.match(name):
        sys.exit(f"Instance name {name!r} may only use letters, digits, '.', '_' and '-'.")

    folder = INSTANCES_DIR / name
    folder.mkdir(parents=True, exist_ok=True)
    os.environ["DATA_DIR"] = str(folder)
    os.environ["INSTANCE"] = name

    port = args.port or _saved_port(folder) or _pick_port()
    if _port_in_use(port):
        sys.exit(
            f"Port {port} is already in use. Is the '{name}' instance running already?\n"
            f"Running the same account twice makes it answer every chat twice."
        )
    (folder / PORT_FILE).write_text(f"{port}\n")
    os.environ["ADMIN_PORT"] = str(port)
    return name
