import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock
import uuid

import codex_chat_merge as core
import git_chat_sync as sync


class GitCommandTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.client = sync.GitStore("git@github.com:example/chats.git", Path(self.temp.name), "office",
                                    str(uuid.uuid4()), lambda _: None, network_timeout=600)

    def test_git_uses_terminal_ssh_configuration_and_network_timeout(self):
        with mock.patch.dict(os.environ, {"GIT_SSH_COMMAND": "custom-ssh -F test-config", "GIT_DIR": "other-repo"}), \
                mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
            self.client.git("push", "origin", "HEAD")
            kwargs = run.call_args.kwargs
            self.assertEqual(kwargs["env"]["GIT_SSH_COMMAND"], "custom-ssh -F test-config")
            self.assertNotIn("GIT_DIR", kwargs["env"])
            self.assertEqual(kwargs["timeout"], 600)
            self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
            self.client.git("status", "--porcelain")
            self.assertEqual(run.call_args.kwargs["timeout"], 300)

    def test_no_ssh_override_when_system_git_config_should_apply(self):
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
            self.client.git("fetch", "origin")
            self.assertNotIn("GIT_SSH_COMMAND", run.call_args.kwargs["env"])

    def test_push_timeout_identifies_command_and_retry_path(self):
        with mock.patch.object(subprocess, "run", side_effect=subprocess.TimeoutExpired("git push", 600)):
            with self.assertRaisesRegex(core.MergeError, "Git push.*仅重试推送"):
                self.client.git("push", "origin", "HEAD")
