"""One command: choose a folder, sign in, keep it synced."""

import argparse
import getpass
import hashlib
import os
from pathlib import Path
import signal
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
    result.add_argument("--once", action="store_true", help="sync once and exit")
    result.add_argument("--interval", type=positive_interval, default=300, metavar="SECONDS", help="time between syncs (default: 300; minimum: 30)")
    result.add_argument("--login", action="store_true", help="prompt for a replacement access token")
    return result


class Stopped(BaseException):
    def __init__(self, signum):
        super().__init__(signum)
        self.signum = signum


def stop(signum, _frame):
    # Raise rather than set a flag, so a pass that is mid-download stops too.
    # Downloads go to temporary files, so nothing is left half-written.
    raise Stopped(signum)


def main(argv=None):
    args = parser().parse_args(argv)
    pause = threading.Event()
    previous = {}
    client = None
    try:
        root = args.folder.expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        with mirror_lock(root) as metadata:
            client, identity, account_path, token = credentials(metadata, args.login)
            mirror = Mirror(root, metadata, identity)
            # Bind the folder before saving credentials.
            mirror.save()
            atomic_json(checked_path(root, ".canvas-sync/config.json"), {"url": identity["url"]})
            if args.login or not os.environ.get("CANVAS_TOKEN"):
                atomic_json(account_path, {"token": token})
            for signum in (signal.SIGINT, signal.SIGTERM):
                previous[signum] = signal.signal(signum, stop)
            print(f"Mirroring to {root}", flush=True)
            while True:
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
                pause.wait(args.interval)
    except (SyncError, OSError, EOFError) as exc:
        print(f"canvas-sync: {exc}", file=sys.stderr)
        return 1
    except Stopped as exc:
        # SIGTERM is how systemd stops the service; that is not a failure.
        return 130 if exc.signum == signal.SIGINT else 0
    except KeyboardInterrupt:
        return 130
    finally:
        if client is not None:
            client.close()
        for signum, handler in previous.items():
            signal.signal(signum, handler)
