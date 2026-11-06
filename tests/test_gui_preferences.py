"""Exercise actual Tk widgets and restart persistence with temporary settings."""
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import codex_chat_merge as core
import git_chat_sync as sync


class GuiPreferencesTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "settings.json"
        patcher = mock.patch.object(sync, "settings_path", return_value=self.path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def widgets(self, parent):
        for child in parent.winfo_children():
            yield child
            yield from self.widgets(child)

    def button(self, root, label):
        from tkinter import ttk
        return next(w for w in self.widgets(root) if isinstance(w, ttk.Button) and w.cget("text") == label)

    def entry(self, root, label):
        from tkinter import ttk
        widget = next(w for w in self.widgets(root) if isinstance(w, ttk.Label) and w.cget("text") == label)
        return next(w for w in widget.master.winfo_children() if isinstance(w, ttk.Entry))

    def launch(self, steps):
        import tkinter as tk
        try:
            root = tk.Tk()
        except tk.TclError as e:
            self.skipTest("No graphical Tk environment: " + str(e))
        root.withdraw()
        root.update_idletasks()
        errors = []
        def close():
            root.tk.call(root.protocol("WM_DELETE_WINDOW"))
        def invoke(index):
            try:
                steps[index](root)
                if index + 1 == len(steps):
                    close()
                else:
                    root.after(700, lambda: invoke(index + 1))
            except Exception as e:
                errors.append(e)
                close()
        root.after(350, lambda: invoke(0))
        with mock.patch.object(tk, "Tk", return_value=root):
            core.gui()
        if errors:
            raise errors[0]

    def test_project_panel_is_inside_local_tab(self):
        from tkinter import ttk
        def check(root):
            widgets = list(self.widgets(root))
            tabs = [w for w in widgets if isinstance(w, ttk.Notebook)]
            self.assertEqual(len(tabs), 1)
            self.assertEqual([tabs[0].tab(tab, "text") for tab in tabs[0].tabs()], ["本地导出 / 合并", "Git 仓库同步"])
            tree = next(w for w in widgets if isinstance(w, ttk.Treeview))
            panel = tree.master.master
            self.assertIsInstance(panel, ttk.LabelFrame)
            self.assertEqual(panel.cget("text"), "导出范围（按项目）")
            self.assertEqual(str(panel.master), tabs[0].tabs()[0])
            self.assertLessEqual(root.winfo_reqheight(), 800)
        self.launch([check])

    def test_paths_git_and_conflict_persist_without_connecting(self):
        from tkinter import ttk
        expected = {"本机 .codex 目录": "D:/test/.codex", "另一台电脑的导出目录": "D:/other",
                    "工作 / 备份目录": "D:/backups", "合并结果 merged 目录": "D:/merged",
                    "仓库地址": "git@github.com:example/chats.git", "本地克隆目录": "D:/git", "本机设备标识": "office"}
        def edit(root):
            for label, value in expected.items():
                entry = self.entry(root, label)
                # Also covers programmatic incoming/result updates from worker events.
                root.setvar(entry.cget("textvariable"), value)
            next(w for w in self.widgets(root) if isinstance(w, ttk.Radiobutton) and w.cget("text") == "显示本机版本").invoke()
            self.entry(root, "备份密码：").insert(0, "never-save-this-password")
        def saved(root):
            cfg = sync.load_settings()
            self.assertEqual(cfg["gui"]["result"], "D:/merged")
            self.assertEqual(cfg["gui"]["conflict"], "base")
            self.assertNotIn("never-save", self.path.read_text(encoding="utf-8"))
        self.launch([edit, saved])
        def restored(root):
            for label, value in expected.items():
                self.assertEqual(self.entry(root, label).get(), value)
            self.assertEqual(self.entry(root, "备份密码：").get(), "")
        self.launch([restored])

    def test_project_choices_restore_refresh_and_reset_for_another_profile(self):
        from tkinter import ttk
        home = str(Path("D:/profile/.codex").resolve())
        sync.save_settings({"gui": {"home": home, "scope": "selected", "selection_home": home,
                                   "project_ids": ["project"], "thread_ids": ["old", "deleted"]}})
        groups = [{"id": "project", "name": "Example", "count": 2, "roots": [], "threads": [
            {"id": tid, "title": tid, "assignment": "database", "archived": False} for tid in ("old", "new")]}]
        def selected(root):
            tree = next(w for w in self.widgets(root) if isinstance(w, ttk.Treeview))
            self.assertTrue(str(tree.item("p0", "text")).startswith("☑"))
            self.assertTrue(str(tree.item("p0t1", "text")).startswith("☑"))
            cfg = sync.load_settings()["gui"]
            self.assertEqual(cfg["thread_ids"], ["new", "old"])
            self.assertEqual(cfg["project_ids"], ["project"])
        def refresh(root):
            self.button(root, "读取 / 刷新项目").invoke()
        def change_home(root):
            entry = self.entry(root, "本机 .codex 目录")
            root.setvar(entry.cget("textvariable"), "D:/another/.codex")
            refresh(root)
        def cleared(root):
            tree = next(w for w in self.widgets(root) if isinstance(w, ttk.Treeview))
            self.assertTrue(str(tree.item("p0", "text")).startswith("☐"))
            self.assertEqual(sync.load_settings()["gui"]["project_ids"], [])
        with mock.patch.object(core, "project_catalog", return_value=groups):
            # Initial auto-refresh ends around 240 ms; allow its debounce to finish.
            self.launch([lambda root: None, selected, refresh, selected, change_home, cleared])


if __name__ == "__main__":
    unittest.main()
