"""Construct the actual Tk widget tree without displaying a window or syncing."""
import unittest
from unittest import mock
import uuid

import codex_chat_merge as core
import git_chat_sync as sync


class GuiTests(unittest.TestCase):
    def test_local_and_git_tabs_build(self):
        import tkinter as tk
        from tkinter import ttk
        try:
            root = tk.Tk()
        except tk.TclError as e:
            self.skipTest("No graphical Tk environment: " + str(e))
        root.withdraw()
        errors = []
        def descendants(widget):
            for child in widget.winfo_children():
                yield child
                yield from descendants(child)
        def check_and_close():
            try:
                widgets = list(descendants(root))
                tabs = [w for w in widgets if isinstance(w, ttk.Notebook)]
                self.assertEqual(len(tabs), 1)
                self.assertEqual(len(tabs[0].tabs()), 3)
                labels = {str(w.cget("text")) for w in widgets if isinstance(w, (ttk.Label, ttk.Button))}
                self.assertTrue({"仓库地址", "连接 / 拉取", "导出并推送", "拉取并合并"} <= labels)
                self.assertTrue({"读取 / 刷新项目", "全选项目", "清空选择"} <= labels)
                self.assertEqual(len([w for w in widgets if isinstance(w, ttk.Treeview)]), 1)
                passwords = [w for w in widgets if isinstance(w, ttk.Entry) and str(w.cget("show")) == "*"]
                self.assertEqual(len(passwords), 1)
            except Exception as e:
                errors.append(e)
            finally:
                root.destroy()
        cfg = {"owner_id": str(uuid.uuid4()), "device": "test-device", "url": "", "repo": "C:/example/repo", "encrypt": True}
        root.after(40, check_and_close)
        with mock.patch.object(tk, "Tk", return_value=root), mock.patch.object(sync, "load_settings", return_value=cfg):
            core.gui()
        if errors:
            raise errors[0]


if __name__ == "__main__":
    unittest.main()
