"""One command: choose a folder, sign in, keep it synced."""

import argparse
import getpass
import hashlib
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading

from .api import AuthenticationError, Client, SyncError, normalize_url
from .storage import atomic_json, checked_path, mirror_lock, read_json
from .sync import Mirror


def prompt(message, secret=False):
    if not sys.stdin.isatty():
        raise SyncError("Setup requires a terminal. Run canvas-sync FOLDER interactively first, or set CANVAS_URL and CANVAS_TOKEN.")
    value = (getpass.getpass(message) if secret else input(message)).strip()
    if not value:
        raise SyncError("A value is required.")
    return value


def credentials(metadata, login=False):
    config = read_json(metadata / "config.json", {})
    url = normalize_url(os.environ.get("CANVAS_URL") or config.get("url") or prompt("Canvas URL: "))
    account_name = hashlib.sha256(url.encode()).hexdigest()[:24]
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    account_path = config_home / "canvas-sync" / "accounts" / f"{account_name}.json"
    saved = read_json(account_path, {})
    token = None if login else os.environ.get("CANVAS_TOKEN") or saved.get("token")
    if not token:
        print(f"Create an access token at {url}/profile/settings (Account → Settings → New Access Token).")
        token = prompt("Canvas access token (hidden): ", secret=True)
    if not isinstance(token, str) or not token.strip() or any(c.isspace() for c in token):
        raise SyncError("Invalid access token.")
    client = Client(url, token)
    try:
        user = client.one("/api/v1/users/self/profile")
        if not isinstance(user, dict) or "id" not in user:
            raise SyncError("Canvas did not return a user identity.")
    except BaseException:
        client.close()
        raise
    identity = {"url": url, "user_id": str(user["id"])}
    return client, identity, account_path, token


def systemd_quote(value, *, command=False):
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    if command:
        escaped = escaped.replace("$", "$$")
    return '"' + escaped + '"'


def install_service(root, interval):
    if sys.platform != "linux":
        raise SyncError("--background requires Linux with systemd. On macOS, run in a terminal or use a login job.")
    if any(c in str(root) or c in sys.executable for c in ("\n", "\r")):
        raise SyncError("Background service paths cannot contain newlines.")
    name = "canvas-sync-" + hashlib.sha256(str(root).encode()).hexdigest()[:12] + ".service"
    directory = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "systemd" / "user"
    directory.mkdir(parents=True, exist_ok=True)
    command = " ".join(systemd_quote(arg, command=True) for arg in (sys.executable, "-m", "canvas_mirror", str(root), "--interval", str(interval)))
    working_directory = str(Path(__file__).resolve().parent.parent).replace("%", "%%")
    content = (
        "[Unit]\nDescription=Canvas file mirror\n\n[Service]\nType=simple\n"
        f"ExecStart={command}\nWorkingDirectory={working_directory}\n"
        "Restart=on-failure\nRestartSec=30\nUMask=0077\nEnvironment=PYTHONUNBUFFERED=1\n"
        f"Environment={systemd_quote('XDG_CONFIG_HOME=' + str(directory.parent.parent))}\n"
        "\n[Install]\nWantedBy=default.target\n"
    )
    path = directory / name
    if path.is_symlink():
        raise SyncError("Refusing to overwrite a service symlink.")
    path.write_text(content, encoding="utf-8")
    try:
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True, capture_output=True)
        subprocess.run(["systemctl", "--user", "enable", "--now", name], check=True, capture_output=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise SyncError("Could not start the user service. Check that systemd --user is available, or run without --background.") from exc
    print(f"Automatic sync enabled for {root} (every {interval} seconds while logged in).")
    print(f"Status: systemctl --user status {name}")
    print(f"Logs: journalctl --user -u {name}")
    print(f"Stop and disable: systemctl --user disable --now {name}")


def positive_interval(value):
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("interval must be a whole number of seconds") from exc
    if number < 30:
        raise argparse.ArgumentTypeError("interval must be at least 30 seconds")
    return number


def parser():
    result = argparse.ArgumentParser(prog="canvas-sync", description="Automatically mirror Canvas course files to a normal folder.")
    result.add_argument("folder", type=Path, help="destination folder (created if needed)")
    mode = result.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="sync once and exit")
    mode.add_argument("--background", action="store_true", help="install and start a Linux user service")
    result.add_argument("--interval", type=positive_interval, default=300, metavar="SECONDS", help="time between syncs (default: 300; minimum: 30)")
    result.add_argument("--login", action="store_true", help="prompt for a replacement access token")
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    stopped = threading.Event()
    previous = {}
    client = None
    try:
        root = args.folder.expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        with mirror_lock(root) as metadata:
            client, identity, account_path, token = credentials(metadata, args.login)
            mirror = Mirror(root, metadata, identity)
            # Bind the folder before saving credentials or starting its service.
            mirror.save()
            atomic_json(checked_path(root, ".canvas-sync/config.json"), {"url": identity["url"]})
            if args.background or args.login or not os.environ.get("CANVAS_TOKEN"):
                atomic_json(account_path, {"token": token})
            if not args.background:
                for signum in (signal.SIGINT, signal.SIGTERM):
                    previous[signum] = signal.signal(signum, lambda *_: stopped.set())
                print(f"Mirroring to {root}", flush=True)
                while not stopped.is_set():
                    try:
                        counts = mirror.run(client)
                        if args.once:
                            return 1 if counts["errors"] or counts["protected"] else 0
                    except AuthenticationError:
                        raise
                    except (SyncError, OSError) as exc:
                        print(f"Sync failed: {exc}", file=sys.stderr, flush=True)
                        if args.once:
                            return 1
                    print(f"Next sync in {args.interval} seconds. Ctrl+C to stop.", flush=True)
                    stopped.wait(args.interval)
                return 0
        # The child must be able to acquire the mirror lock immediately.
        install_service(root, args.interval)
        return 0
    except (SyncError, OSError, EOFError) as exc:
        print(f"canvas-sync: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    finally:
        if client is not None:
            client.close()
        for signum, handler in previous.items():
            signal.signal(signum, handler)
