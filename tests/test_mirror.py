import copy
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests

from canvas_mirror.api import AuthenticationError, Client, SyncError, UnavailableError, normalize_url
from canvas_mirror.cli import main, install_service
from canvas_mirror.storage import mirror_lock
from canvas_mirror.sync import Mirror


class Response:
    def __init__(self, data=None, body=b"abc", status=200, links=None, headers=None, broken=False):
        self.data, self.body, self.status_code = data, body, status
        self.links, self.headers = links or {}, headers or {}
        self.ok = status < 400
        self.broken = broken

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        pass

    def json(self):
        return self.data

    def iter_content(self, chunk_size):
        yield self.body
        if self.broken:
            raise requests.ConnectionError("sensitive signed URL")


class FakeClient:
    def __init__(self):
        self.courses = [{"id": 1, "course_code": "CS101", "name": "Intro"}]
        self.folders = [{"id": 10, "name": "course files", "parent_folder_id": None},
                        {"id": 11, "name": "Lectures", "parent_folder_id": 10}]
        self.files = [{"id": 20, "folder_id": 11, "display_name": "week.pdf", "updated_at": "1", "size": 3, "url": "https://cdn.example/file"}]
        self.body = b"abc"
        self.broken = False
        self.failure = None
        self.downloads = 0

    def listing(self, path):
        if path == "/api/v1/courses":
            return copy.deepcopy(self.courses)
        if path.endswith("/folders"):
            return copy.deepcopy(self.folders)
        if self.failure:
            raise self.failure
        return copy.deepcopy(self.files)

    def get(self, url, download=False):
        self.downloads += 1
        return Response(body=self.body, broken=self.broken)


class MirrorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.metadata = self.root / ".canvas-sync"
        self.metadata.mkdir()
        self.identity = {"url": "https://canvas.example", "user_id": "1"}
        self.client = FakeClient()
        self.mirror = Mirror(self.root, self.metadata, self.identity, report=lambda _: None)
        self.dest = self.root / "CS101 [1]/Lectures/week.pdf"

    def run_sync(self):
        return self.mirror.run(self.client)

    def test_initial_incremental_update_and_restore_deleted_local_file(self):
        self.assertEqual(self.run_sync()["downloaded"], 1)
        self.assertEqual(self.dest.read_bytes(), b"abc")
        self.assertEqual(self.run_sync()["unchanged"], 1)
        self.assertEqual(self.client.downloads, 1)
        self.client.files[0]["updated_at"] = "2"
        self.client.body = b"xyz"
        self.run_sync()
        self.assertEqual(self.dest.read_bytes(), b"xyz")
        self.dest.unlink()
        self.run_sync()
        self.assertEqual(self.dest.read_bytes(), b"xyz")

    def test_local_edit_is_protected_even_with_same_size_and_mtime(self):
        self.run_sync()
        stat = self.dest.stat()
        self.dest.write_bytes(b"xyz")
        os.utime(self.dest, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        self.client.files[0]["updated_at"] = "2"
        self.assertEqual(self.run_sync()["protected"], 1)
        self.assertEqual(self.dest.read_bytes(), b"xyz")

    def test_preexisting_file_is_not_adopted_or_deleted(self):
        self.dest.parent.mkdir(parents=True)
        self.dest.write_bytes(b"mine")
        self.assertEqual(self.run_sync()["protected"], 1)
        self.client.files = []
        self.run_sync()
        self.assertEqual(self.dest.read_bytes(), b"mine")

    def test_remote_rename_and_delete(self):
        self.run_sync()
        self.client.files[0]["display_name"] = "new.pdf"
        self.run_sync()
        new = self.dest.with_name("new.pdf")
        self.assertFalse(self.dest.exists())
        self.assertEqual(new.read_bytes(), b"abc")
        self.client.files = []
        self.assertEqual(self.run_sync()["removed"], 1)
        self.assertFalse(new.exists())

    def test_remote_delete_preserves_edited_copy(self):
        self.run_sync()
        self.dest.write_bytes(b"my notes")
        self.client.files = []
        self.assertEqual(self.run_sync()["protected"], 1)
        self.assertEqual(self.dest.read_bytes(), b"my notes")

    def test_renamed_modified_file_is_reported(self):
        self.run_sync()
        self.dest.write_bytes(b"edited")
        self.client.files[0]["display_name"] = "renamed.pdf"
        self.assertEqual(self.run_sync()["protected"], 1)
        self.assertEqual(self.dest.read_bytes(), b"edited")

    def test_failed_listing_and_missing_course_preserve_files(self):
        self.run_sync()
        for failure in (SyncError("offline"), UnavailableError("denied")):
            self.client.failure = failure
            self.assertEqual(self.run_sync()["errors"], 1)
            self.assertTrue(self.dest.exists())
        self.client.courses = []
        self.run_sync()
        self.assertTrue(self.dest.exists())

    def test_authentication_failure_is_fatal(self):
        self.client.failure = AuthenticationError("expired")
        with self.assertRaises(AuthenticationError):
            self.run_sync()

    def test_download_interruption_and_wrong_size_preserve_old_copy(self):
        self.run_sync()
        self.client.files[0]["updated_at"] = "2"
        self.client.body = b"new"
        self.client.broken = True
        self.assertEqual(self.run_sync()["errors"], 1)
        self.assertEqual(self.dest.read_bytes(), b"abc")
        self.client.broken = False
        self.client.files[0]["size"] = 100
        self.assertEqual(self.run_sync()["errors"], 1)
        self.assertEqual(self.dest.read_bytes(), b"abc")
        self.assertEqual(list(self.dest.parent.glob(".canvas-*")), [])

    def test_local_edit_during_download_is_preserved(self):
        self.run_sync()
        self.client.files[0]["updated_at"] = "2"
        original = self.client.get
        def edit(*args, **kwargs):
            self.dest.write_bytes(b"edited while downloading")
            return original(*args, **kwargs)
        self.client.get = edit
        self.assertEqual(self.run_sync()["errors"], 1)
        self.assertEqual(self.dest.read_bytes(), b"edited while downloading")

    def test_locked_file_is_preserved(self):
        self.run_sync()
        self.client.files[0]["locked_for_user"] = True
        self.assertEqual(self.run_sync()["unavailable"], 1)
        self.assertTrue(self.dest.exists())

    def test_symlink_folder_cannot_escape_root(self):
        outside = self.root / "outside"
        outside.mkdir()
        (self.root / "CS101 [1]").symlink_to(outside, target_is_directory=True)
        self.assertEqual(self.run_sync()["errors"], 1)
        self.assertEqual(list(outside.iterdir()), [])

    def test_separate_roots_have_separate_state(self):
        self.run_sync()
        other = self.root / "other"
        other.mkdir()
        mirror = Mirror(other, other / ".canvas-sync", self.identity, report=lambda _: None)
        self.assertEqual(mirror.run(self.client)["downloaded"], 1)

    def test_corrupt_state_and_other_account_are_rejected(self):
        self.run_sync()
        with self.assertRaises(SyncError):
            Mirror(self.root, self.metadata, {"url": "https://other.example", "user_id": "1"})
        (self.metadata / "state.json").write_text("{broken")
        with self.assertRaises(SyncError):
            Mirror(self.root, self.metadata, self.identity)

    def test_duplicate_and_unsafe_names_have_unique_safe_paths(self):
        self.client.files.extend([
            {**self.client.files[0], "id": 21, "display_name": "WEEK.PDF"},
            {**self.client.files[0], "id": 22, "display_name": "../../escape"},
            {**self.client.files[0], "id": 23, "folder_id": 10, "display_name": "Lectures"},
        ])
        self.assertEqual(self.run_sync()["downloaded"], 4)
        paths = [entry["path"] for entry in self.mirror.state["files"].values()]
        self.assertEqual(len({p.casefold() for p in paths}), 4)
        self.assertTrue(all((self.root / p).is_file() for p in paths))

    def test_cycle_or_missing_parent_fails_before_writes(self):
        self.client.folders[1]["parent_folder_id"] = 11
        self.assertEqual(self.run_sync()["errors"], 1)
        self.assertFalse(self.dest.exists())

    def test_restart_reuses_state_without_download(self):
        self.run_sync()
        self.mirror = Mirror(self.root, self.metadata, self.identity, report=lambda _: None)
        self.assertEqual(self.run_sync()["unchanged"], 1)
        self.assertEqual(self.client.downloads, 1)

    def test_malformed_remote_metadata_preserves_existing_files(self):
        self.run_sync()
        self.client.files = [{"id": 99}]
        self.assertEqual(self.run_sync()["errors"], 1)
        self.assertTrue(self.dest.exists())

    def test_state_cannot_target_internal_metadata(self):
        self.run_sync()
        self.mirror.state["files"]["1:20"]["path"] = ".canvas-sync/config.json"
        self.mirror.save()
        with self.assertRaises(SyncError):
            Mirror(self.root, self.metadata, self.identity)

    def test_lock_released_after_exception(self):
        with self.assertRaisesRegex(RuntimeError, "test"):
            with mirror_lock(self.root):
                with self.assertRaises(SyncError):
                    with mirror_lock(self.root):
                        pass
                raise RuntimeError("test")
        with mirror_lock(self.root):
            pass


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.client = Client("https://canvas.example", "secret-token")
        self.addCleanup(self.client.close)
        self.client.session.get = Mock()

    def test_pagination_and_credentials(self):
        self.client.session.get.side_effect = [Response([{"id": 1}], links={"next": {"url": "?page=2"}}), Response([{"id": 2}])]
        self.assertEqual(self.client.listing("/api/v1/courses"), [{"id": 1}, {"id": 2}])
        self.assertEqual(self.client.session.get.call_args.args[0], "https://canvas.example/api/v1/courses?page=2")

    def test_offsite_download_does_not_receive_token(self):
        self.client.session.get.return_value = Response()
        self.client.get("https://cdn.example/download", download=True)
        self.assertEqual(self.client.session.get.call_args.kwargs["headers"], {})

    def test_redirect_does_not_leak_token(self):
        self.client.session.get.side_effect = [Response(status=302, headers={"Location": "https://cdn.example/download"}), Response()]
        self.client.get("https://canvas.example/file", download=True)
        calls = self.client.session.get.call_args_list
        self.assertIn("Authorization", calls[0].kwargs["headers"])
        self.assertNotIn("Authorization", calls[1].kwargs["headers"])

    def test_untrusted_pagination_and_insecure_download_rejected(self):
        self.client.session.get.return_value = Response([], links={"next": {"url": "https://evil.example"}})
        with self.assertRaises(SyncError):
            self.client.listing("/api/v1/courses")
        self.assertEqual(self.client.session.get.call_count, 1)
        with self.assertRaises(SyncError):
            self.client.get("http://cdn.example/file", download=True)

    def test_expired_token_and_errors_do_not_expose_urls(self):
        self.client.session.get.return_value = Response(status=401)
        with self.assertRaises(AuthenticationError):
            self.client.one("/api/v1/users/self/profile")
        self.client.session.get.side_effect = requests.ConnectionError("secret-token")
        with self.assertRaises(SyncError) as caught:
            self.client.one("/api/v1/users/self/profile")
        self.assertNotIn("secret-token", str(caught.exception))

    def test_pagination_loop_rejected(self):
        url = "https://canvas.example/api/v1/courses?per_page=100"
        self.client.session.get.return_value = Response([], links={"next": {"url": url}})
        with self.assertRaises(SyncError):
            self.client.listing("/api/v1/courses")

    def test_url_validation(self):
        self.assertEqual(normalize_url("canvas.example/"), "https://canvas.example")
        for url in ("http://canvas.example", "https://u:p@canvas.example", "https://canvas.example/courses", "https://canvas.example?token=x"):
            with self.assertRaises(SyncError):
                normalize_url(url)

    def test_retry_policy_is_bounded_and_read_only(self):
        retry = self.client.session.get_adapter("https://").max_retries
        self.assertEqual(retry.total, 4)
        self.assertEqual(retry.allowed_methods, {"GET"})
        self.assertIn(429, retry.status_forcelist)
        self.assertEqual(retry.get_retry_after(Response(headers={"Retry-After": "9999"})), 60)


class CliTests(unittest.TestCase):
    def test_invalid_credentials_are_not_saved(self):
        with tempfile.TemporaryDirectory() as temp:
            client = Mock()
            client.one.side_effect = AuthenticationError("bad token")
            with patch.dict(os.environ, {"XDG_CONFIG_HOME": temp, "CANVAS_URL": "https://canvas.example", "CANVAS_TOKEN": "bad"}, clear=True), patch("canvas_mirror.cli.Client", return_value=client), patch("sys.stderr", new_callable=io.StringIO):
                self.assertEqual(main([str(Path(temp) / "mirror"), "--once"]), 1)
            self.assertEqual(list(Path(temp).rglob("*.json")), [])
            client.close.assert_called_once()

    def test_watch_retries_and_waits_between_passes(self):
        with tempfile.TemporaryDirectory() as temp:
            client = Mock()
            client.one.return_value = {"id": 1}
            event = Mock()
            event.is_set.side_effect = [False, False, True]
            with patch.dict(os.environ, {"XDG_CONFIG_HOME": temp, "CANVAS_URL": "https://canvas.example", "CANVAS_TOKEN": "test"}, clear=True), patch("canvas_mirror.cli.Client", return_value=client), patch("canvas_mirror.cli.threading.Event", return_value=event), patch("canvas_mirror.cli.Mirror.run", side_effect=[SyncError("offline"), {}]) as sync, patch("sys.stdout", new_callable=io.StringIO), patch("sys.stderr", new_callable=io.StringIO):
                self.assertEqual(main([str(Path(temp) / "mirror"), "--interval", "30"]), 0)
                self.assertEqual(sync.call_count, 2)
                self.assertEqual(event.wait.call_count, 2)
                event.wait.assert_called_with(30)

    def test_background_releases_lock_before_starting_service(self):
        with tempfile.TemporaryDirectory() as temp:
            client = Mock()
            client.one.return_value = {"id": 1}
            def start(root, interval):
                with mirror_lock(root):
                    pass
            with patch.dict(os.environ, {"XDG_CONFIG_HOME": temp, "CANVAS_URL": "https://canvas.example", "CANVAS_TOKEN": "test"}, clear=True), patch("canvas_mirror.cli.Client", return_value=client), patch("canvas_mirror.cli.install_service", side_effect=start) as service:
                self.assertEqual(main([str(Path(temp) / "mirror"), "--background"]), 0)
                service.assert_called_once()

    def test_prompt_validate_persist_and_reuse(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "mirror"
            config = Path(temp) / "config"
            client = Mock()
            client.one.return_value = {"id": 1}
            client.listing.return_value = []
            with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(config)}, clear=True), patch("canvas_mirror.cli.Client", return_value=client), patch("sys.stdin.isatty", return_value=True), patch("builtins.input", return_value="https://canvas.example") as url_prompt, patch("getpass.getpass", return_value="token") as token_prompt, patch("sys.stdout", new_callable=io.StringIO):
                self.assertEqual(main([str(root), "--once"]), 0)
                self.assertEqual(main([str(root), "--once"]), 0)
                self.assertEqual(url_prompt.call_count, 1)
                self.assertEqual(token_prompt.call_count, 1)
            saved = list(config.rglob("*.json"))
            self.assertEqual(len(saved), 1)
            self.assertEqual(saved[0].stat().st_mode & 0o777, 0o600)
            self.assertNotIn("token", (root / ".canvas-sync/config.json").read_text())

    def test_noninteractive_missing_credentials_fails_without_prompt(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {}, clear=True), patch("sys.stdin.isatty", return_value=False), patch("sys.stderr", new_callable=io.StringIO) as error:
            self.assertEqual(main([temp, "--once"]), 1)
            self.assertIn("terminal", error.getvalue())

    def test_service_uses_argument_array_and_quotes_folder(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"XDG_CONFIG_HOME": temp}), patch("sys.platform", "linux"), patch("subprocess.run") as run, patch("sys.stdout", new_callable=io.StringIO):
            install_service(Path(temp) / 'my folder % $ "', 300)
            service = next(Path(temp).rglob("*.service")).read_text()
            self.assertIn("%%", service)
            self.assertIn("$$", service)
            self.assertIn('\\"', service)
            self.assertIn("WorkingDirectory=/", service)
            self.assertEqual(run.call_count, 2)
            self.assertIsInstance(run.call_args.args[0], list)
            self.assertNotIn("token", service)


if __name__ == "__main__":
    unittest.main()
