import json
from pathlib import Path
import tempfile
import threading
import unittest

import git_chat_sync as sync


class SettingsTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "settings.json"

    def test_git_and_local_updates_preserve_each_other_and_exclude_secrets(self):
        cfg = sync.load_settings(self.path)
        sync.save_settings(cfg, self.path)
        gui = {"home": "D:/配置/.codex", "incoming": "D:/other", "work": "D:/backups",
               "result": "D:/merged", "conflict": "base", "scope": "selected",
               "selection_home": "D:/配置/.codex", "project_ids": ["p1"], "thread_ids": ["t1"],
               "path_mappings": {r"D:\Office\Project": r"E:\Home\Project"},
               "password": "nested-secret"}
        sync.save_settings({"gui": gui, "password": "outer-secret"}, self.path)
        sync.save_settings({"url": "git@github.com:example/chats.git", "encrypt": False}, self.path)
        loaded = sync.load_settings(self.path)
        self.assertEqual(loaded["gui"], {k: v for k, v in gui.items() if k != "password"})
        self.assertEqual(loaded["owner_id"], cfg["owner_id"])
        self.assertFalse(loaded["encrypt"])
        self.assertEqual(loaded["url"], "git@github.com:example/chats.git")
        self.assertNotIn("secret", self.path.read_text(encoding="utf-8"))

    def test_concurrent_worker_and_gui_save(self):
        sync.save_settings(sync.load_settings(self.path), self.path)
        barrier = threading.Barrier(2)
        errors = []
        def save(cfg):
            try:
                barrier.wait()
                sync.save_settings(cfg, self.path)
            except Exception as e:
                errors.append(e)
        workers = [threading.Thread(target=save, args=(cfg,)) for cfg in
                   ({"gui": {"work": "D:/backup"}}, {"device": "office"})]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        self.assertFalse(errors)
        cfg = sync.load_settings(self.path)
        self.assertEqual(cfg["gui"]["work"], "D:/backup")
        self.assertEqual(cfg["device"], "office")

    def test_damaged_or_invalid_settings_fall_back(self):
        for data in ("{broken", "[]", '{"owner_id": ""}', json.dumps({"owner_id": 7, "gui": {"scope": [], "thread_ids": [4, "ok"], "password": "secret"}})):
            self.path.write_text(data, encoding="utf-8")
            cfg = sync.load_settings(self.path)
            self.assertIsInstance(cfg["owner_id"], str)
            self.assertTrue(cfg["owner_id"])
            self.assertNotIn("scope", cfg["gui"])
            self.assertNotIn("password", cfg["gui"])

    def test_autosave_does_not_persist_credentials_in_url(self):
        sync.save_settings({"url": "git@github.com:example/chats.git"}, self.path)
        sync.save_settings({"url": "https://user:private-token@github.com/example/chats.git"}, self.path)
        self.assertNotIn("private-token", self.path.read_text(encoding="utf-8"))
        self.assertEqual(sync.load_settings(self.path)["url"], "git@github.com:example/chats.git")
