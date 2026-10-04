"""File reconciliation; never writes to Canvas."""

from collections import Counter
import os
from pathlib import Path
import tempfile

import requests

from .api import AuthenticationError, SyncError
from .storage import atomic_json, checked_path, digest, read_json, safe_name


def file_paths(course, folders, files):
    """Build paths from folder IDs, disambiguating files and folders together."""
    course_id = int(course["id"])
    base = Path(f"{safe_name(course.get('course_code') or course.get('name') or 'Course')} [{course_id}]")
    by_id = {int(f["id"]): f for f in folders}
    names = {}
    used = {}
    items = [("folder", f) for f in folders if f.get("parent_folder_id") is not None]
    items += [("file", f) for f in files]
    for kind, item in sorted(items, key=lambda pair: (pair[0], int(pair[1]["id"]))):
        parent = item.get("parent_folder_id") if kind == "folder" else item["folder_id"]
        original = safe_name(item.get("display_name") or item.get("name") or item.get("filename") or "unnamed")
        name = original
        siblings = used.setdefault(parent, set())
        count = 0
        while name.casefold() in siblings:
            count += 1
            suffix = Path(original).suffix if kind == "file" else ""
            stem = original[:-len(suffix)] if suffix else original
            name = f"{stem} [{kind}-{int(item['id'])}-{count}]{suffix}"
        siblings.add(name.casefold())
        names[kind, int(item["id"])] = name

    def folder_path(folder_id):
        parts = []
        seen = set()
        while folder_id is not None:
            if folder_id in seen or folder_id not in by_id:
                raise SyncError("Canvas returned an incomplete or cyclic folder hierarchy.")
            seen.add(folder_id)
            folder = by_id[folder_id]
            parent = folder.get("parent_folder_id")
            if parent is not None:
                parts.append(names["folder", folder_id])
            folder_id = parent
        return base.joinpath(*reversed(parts))

    return {str(int(f["id"])): folder_path(f["folder_id"]) / names["file", int(f["id"])] for f in files}


class Mirror:
    def __init__(self, root, metadata, identity, report=print):
        self.root = root
        self.state_path = checked_path(root, ".canvas-sync/state.json")
        self.state = read_json(self.state_path, {"version": 1, "identity": identity, "files": {}})
        if self.state.get("version") != 1 or not isinstance(self.state.get("files"), dict):
            raise SyncError("Unsupported mirror metadata; restore a valid state file.")
        if self.state.get("identity") != identity:
            raise SyncError("This folder belongs to a different Canvas account. Choose another folder.")
        for key, entry in self.state["files"].items():
            if not isinstance(entry, dict) or not all(k in entry for k in ("path", "sha256", "stamp", "course")):
                raise SyncError("Invalid file record in mirror metadata.")
            if not isinstance(entry["path"], str) or Path(entry["path"]).parts[:1] == (".canvas-sync",):
                raise SyncError("Invalid file path in mirror metadata.")
            checked_path(root, entry["path"])
        self.report = report

    def save(self):
        atomic_json(self.state_path, self.state)

    def fetch(self, client, info, destination, expected_hash):
        destination.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".canvas-", dir=destination.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                try:
                    with client.get(info["url"], download=True) as response:
                        size = 0
                        for chunk in response.iter_content(chunk_size=1024 * 1024):
                            stream.write(chunk)
                            size += len(chunk)
                        if info.get("size") is not None and size != int(info["size"]):
                            raise SyncError("Incomplete download; the existing file was preserved.")
                except requests.RequestException as exc:
                    raise SyncError("Download interrupted; the existing file was preserved.") from exc
                stream.flush()
                os.fsync(stream.fileno())
            checked_path(self.root, destination.relative_to(self.root))
            if digest(destination) != expected_hash:
                raise SyncError("File changed during download; local changes were preserved.")
            sha256 = digest(Path(temporary))
            os.replace(temporary, destination)
            return sha256
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def remove(self, key, counts):
        entry = self.state["files"][key]
        path = checked_path(self.root, entry["path"])
        actual = digest(path)
        if actual is not None and actual != entry["sha256"]:
            counts["protected"] += 1
            self.report(f"Kept local changes: {entry['path']}")
            return
        if actual is not None:
            path.unlink()
            self.report(f"Removed: {entry['path']}")
            counts["removed"] += 1
        del self.state["files"][key]
        self.save()
        self.clean_parents(path.parent)

    def clean_parents(self, directory):
        while directory != self.root:
            try:
                directory.rmdir()
            except OSError:
                break
            directory = directory.parent

    def course(self, client, course, counts):
        course_id = str(int(course["id"]))
        folders = client.listing(f"/api/v1/courses/{course_id}/folders")
        files = client.listing(f"/api/v1/courses/{course_id}/files")
        paths = file_paths(course, folders, files)
        seen = set()
        for info in files:
            key = f"{course_id}:{int(info['id'])}"
            seen.add(key)
            if info.get("locked_for_user") or info.get("hidden_for_user"):
                counts["unavailable"] += 1
                continue
            relative = str(paths[str(int(info["id"]))])
            destination = checked_path(self.root, relative)
            known = self.state["files"].get(key)
            current = digest(destination)
            same_path = known and known["path"] == relative
            if current is not None and (not same_path or current != known["sha256"]):
                self.report(f"Kept existing/local file: {relative}")
                counts["protected"] += 1
                continue
            stamp = [info.get("updated_at"), info.get("modified_at"), info.get("size")]
            if same_path and current is not None and known["stamp"] == stamp:
                counts["unchanged"] += 1
                continue
            # Keep a modified old path on a remote rename, and report the conflict.
            if known and not same_path:
                old = checked_path(self.root, known["path"])
                old_hash = digest(old)
                if old_hash is not None and old_hash != known["sha256"]:
                    self.report(f"Kept local changes: {known['path']}")
                    counts["protected"] += 1
                    continue
            sha256 = self.fetch(client, info, destination, current)
            if known and not same_path:
                old = checked_path(self.root, known["path"])
                if digest(old) == known["sha256"]:
                    old.unlink()
                    self.clean_parents(old.parent)
            self.state["files"][key] = {"course": course_id, "path": relative, "stamp": stamp, "sha256": sha256}
            self.save()
            self.report(f"Downloaded: {relative}")
            counts["downloaded"] += 1
        # Reconcile only after both complete listings and every download succeeded.
        for key, entry in list(self.state["files"].items()):
            if entry["course"] == course_id and key not in seen:
                self.remove(key, counts)

    def run(self, client):
        counts = Counter()
        courses = client.listing("/api/v1/courses")
        for course in courses:
            if course.get("access_restricted_by_date") or "name" not in course:
                counts["unavailable"] += 1
                continue
            try:
                self.course(client, course, counts)
            except AuthenticationError:
                raise
            except (SyncError, OSError) as exc:
                counts["errors"] += 1
                self.report(f"Course {course.get('id')}: {exc}")
            except (KeyError, TypeError, ValueError):
                counts["errors"] += 1
                self.report(f"Course {course.get('id')}: invalid file or folder metadata from Canvas.")
        self.save()
        self.report(
            f"{counts['downloaded']} downloaded, {counts['unchanged']} current, "
            f"{counts['removed']} removed, {counts['protected']} protected, "
            f"{counts['unavailable']} unavailable, {counts['errors']} errors"
        )
        return counts
