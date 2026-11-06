import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import codex_chat_merge as core
import git_chat_sync as sync
from test_merge import make_home, quiet


class GitSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        make_home(self.home, {"chat": ["人工聊天消息"]})
        self.snapshot = core.export_snapshot(self.home, self.root / "export", quiet, _check_closed=False)
        self.owner = str(uuid.uuid4())

    def store(self, device="office", clone="clone", owner=None):
        remote = self.root / "remote.git"
        if not remote.exists():
            subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
        return sync.GitStore(str(remote), self.root / clone, device, owner or self.owner, quiet)

    def test_encrypted_roundtrip_and_wrong_password(self):
        try:
            sync.aes_class()
        except core.MergeError:
            self.skipTest("optional cryptography not installed")
        folder = self.root / "packed"
        header = sync.pack_snapshot(self.snapshot, folder, "office", "a-strong-test-password", part_size=1024, log=quiet)
        self.assertGreater(len(header["parts"]), 1)
        self.assertTrue(all(p["size"] <= 1040 for p in header["parts"]))
        result = sync.unpack_snapshot(folder, self.root / "unpacked", "a-strong-test-password", quiet)
        self.assertEqual(core.read_json(self.snapshot / "manifest.json"), core.read_json(result / "manifest.json"))
        with self.assertRaises(core.MergeError):
            sync.unpack_snapshot(folder, self.root / "wrong", "different-password", quiet)
        self.assertFalse((self.root / "wrong").exists())

    def test_plaintext_roundtrip_and_tampered_part(self):
        folder = self.root / "packed"
        sync.pack_snapshot(self.snapshot, folder, "office", part_size=1024, log=quiet)
        sync.unpack_snapshot(folder, self.root / "unpacked", log=quiet)
        part = folder / "part-000001.bin"
        data = bytearray(part.read_bytes())
        data[0] ^= 1
        part.write_bytes(data)
        with self.assertRaises(core.MergeError):
            sync.unpack_snapshot(folder, self.root / "tampered", log=quiet)

    def test_two_computers_push_pull_download_and_merge(self):
        office = self.store()
        self.assertEqual(office.connect(), [])
        first = office.upload_snapshot(self.snapshot)
        home = self.store("home", "home-clone", str(uuid.uuid4()))
        listed = home.connect()
        self.assertEqual([(i["device"], i["snapshot_id"]) for i in listed], [("office", first["snapshot_id"])])
        downloaded = home.download("office", first["snapshot_id"], self.root / "download")
        local_home = self.root / "other-home"
        make_home(local_home, {"other": ["家里的聊天"]})
        local = core.export_snapshot(local_home, self.root / "local-export", quiet, _check_closed=False)
        merged = core.merge_snapshots(local, downloaded, self.root / "merge", local_home, log=quiet)
        with core.database(merged / "state_5.sqlite") as c:
            self.assertEqual({r[0] for r in c.execute("SELECT id FROM threads")}, {"chat", "other"})
        home.upload_snapshot(local)
        self.assertEqual({i["device"] for i in office.connect()}, {"office", "home"})
        self.assertEqual(office.git("status", "--porcelain").stdout.strip(), "")

    def test_dirty_clone_and_remote_mismatch_rejected(self):
        office = self.store()
        office.connect()
        (office.repo / "do-not-touch.txt").write_text("my work", encoding="utf-8")
        with self.assertRaises(core.MergeError):
            office.connect()
        self.assertEqual((office.repo / "do-not-touch.txt").read_text(), "my work")
        other = sync.GitStore(str(self.root / "another.git"), office.repo, "office", self.owner, quiet)
        with self.assertRaises(core.MergeError):
            other.connect()

    def test_device_identity_collision_rejected(self):
        office = self.store()
        office.upload_snapshot(self.snapshot)
        impostor = self.store("office", "other-clone", str(uuid.uuid4()))
        with self.assertRaises(core.MergeError):
            impostor.upload_snapshot(self.snapshot)

    def test_encryption_policy_prevents_plaintext_downgrade(self):
        try:
            sync.aes_class()
        except core.MergeError:
            self.skipTest("optional cryptography not installed")
        office = self.store()
        office.upload_snapshot(self.snapshot, "a-strong-test-password")
        home = self.store("home", "home-clone", str(uuid.uuid4()))
        with self.assertRaises(core.MergeError):
            home.upload_snapshot(self.snapshot)
        item = home.connect()[0]
        home.download(item["device"], item["snapshot_id"], self.root / "download", "a-strong-test-password")

    def test_failed_commit_restores_clean_worktree(self):
        office = self.store()
        original = office.git
        def fail_commit(*args, **kwargs):
            if "commit" in args:
                raise core.MergeError("simulated commit failure")
            return original(*args, **kwargs)
        with mock.patch.object(office, "git", side_effect=fail_commit):
            with self.assertRaises(core.MergeError):
                office.upload_snapshot(self.snapshot)
        self.assertEqual(office.git("status", "--porcelain").stdout.strip(), "")
        self.assertFalse((office.repo / sync.MARKER).exists())
        office.upload_snapshot(self.snapshot)  # Retry succeeds without manual cleanup.

    def test_failed_push_preserves_commit_for_retry(self):
        office = self.store()
        original = office.git
        def fail_push(*args, **kwargs):
            if args and args[0] == "push":
                return subprocess.CompletedProcess(args, 1, "", "simulated network failure")
            return original(*args, **kwargs)
        with mock.patch.object(office, "git", side_effect=fail_push):
            with self.assertRaises(core.MergeError):
                office.upload_snapshot(self.snapshot)
        self.assertEqual(office.git("status", "--porcelain").stdout.strip(), "")
        self.assertEqual(office.git("rev-parse", "--verify", "HEAD").returncode, 0)
        office.upload_snapshot(self.snapshot)
        home = self.store("home", "home-clone", str(uuid.uuid4()))
        self.assertEqual(len(home.connect()), 1)

    def test_concurrent_pushes_rebase_separate_device_commits(self):
        office = self.store()
        office.upload_snapshot(self.snapshot)
        home = self.store("home", "home-clone", str(uuid.uuid4()))
        home.connect()
        original = home.git
        raced = [False]
        def competing_push(*args, **kwargs):
            if args and args[0] == "push" and not raced[0]:
                raced[0] = True
                office.upload_snapshot(self.snapshot)
            return original(*args, **kwargs)
        with mock.patch.object(home, "git", side_effect=competing_push):
            home.upload_snapshot(self.snapshot)
        self.assertTrue(raced[0])
        self.assertEqual({x["device"] for x in office.connect()}, {"office", "home"})

    def test_first_push_race_between_two_unborn_clones(self):
        office = self.store()
        home = self.store("home", "home-clone", str(uuid.uuid4()))
        home.connect()
        original = home.git
        raced = [False]
        def competing_push(*args, **kwargs):
            if args and args[0] == "push" and not raced[0]:
                raced[0] = True
                office.upload_snapshot(self.snapshot)
            return original(*args, **kwargs)
        with mock.patch.object(home, "git", side_effect=competing_push):
            home.upload_snapshot(self.snapshot)
        self.assertEqual({x["device"] for x in office.connect()}, {"office", "home"})

    def test_settings_never_store_password(self):
        path = self.root / "settings.json"
        cfg = sync.load_settings(path)
        cfg.update(password="secret", url="https://github.com/example/private.git")
        sync.save_settings(cfg, path)
        self.assertNotIn("secret", path.read_text(encoding="utf-8"))
        self.assertEqual(sync.load_settings(path)["owner_id"], cfg["owner_id"])

    def test_project_scoped_snapshot_survives_git_transport(self):
        from test_merge import put_thread
        put_thread(self.home, "not-selected", ["excluded message"])
        selected = core.export_snapshot(self.home, self.root / "selected", quiet,
                                        thread_ids=["chat"], _check_closed=False)
        office = self.store()
        header = office.upload_snapshot(selected)
        home = self.store("home", "home-clone", str(uuid.uuid4()))
        restored = home.download("office", header["snapshot_id"], self.root / "restored")
        with core.database(restored / "state_5.sqlite") as c:
            self.assertEqual({r[0] for r in c.execute("SELECT id FROM threads")}, {"chat"})
        self.assertEqual(core.read_json(restored / "manifest.json")["selection"]["mode"], "selected")
        self.assertTrue((restored / core.PROJECT_INDEX).exists())

    def test_credential_urls_and_external_helpers_rejected(self):
        for address in ("https://token@github.com/a/b.git", "https://a:secret@github.com/a/b.git",
                        "ext::danger", "-u", "https://github.com/a/b.git?token=secret"):
            with self.assertRaises(core.MergeError):
                sync.validate_url(address)
        self.assertEqual(sync.validate_url("git@github.com:example/private.git"), "git@github.com:example/private.git")


if __name__ == "__main__":
    unittest.main()
