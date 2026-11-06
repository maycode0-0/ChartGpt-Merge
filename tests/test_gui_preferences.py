"""Exercise actual Tk widgets and restart persistence with temporary settings."""
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import codex_chat_merge as core
import git_chat_sync as sync
import chat_merge_gui as ui


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
        root.chat_app = ui.ChatMergeApp(root)
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
        root.mainloop()
        if errors:
            raise errors[0]

    def test_project_panel_is_inside_local_tab(self):
        from tkinter import ttk
        def check(root):
            widgets = list(self.widgets(root))
            tabs = [w for w in widgets if isinstance(w, ttk.Notebook)]
            self.assertEqual(len(tabs), 1)
            self.assertEqual([tabs[0].tab(tab, "text") for tab in tabs[0].tabs()], ["Git 仓库同步", "本地导出 / 合并", "连接与设置"])
            self.assertEqual(tabs[0].select(), str(root.chat_app.pages["git"]))
            tree = root.chat_app.project_tree
            panel = tree.master.master
            self.assertIsInstance(panel, ttk.LabelFrame)
            self.assertEqual(panel.cget("text"), "导出范围（按项目）")
            self.assertEqual(str(panel.master), tabs[0].tabs()[1])
            self.assertLessEqual(root.winfo_reqheight(), 820)
            self.assertLessEqual(root.winfo_reqwidth(), 1080)
        self.launch([check])

    def test_paths_git_and_conflict_persist_without_connecting(self):
        from tkinter import ttk
        expected = {"本机 .codex 目录": "D:/test/.codex", "另一台电脑的导出目录": "D:/other",
                    "工作 / 备份目录": "D:/backups",
                    "仓库地址": "git@github.com:example/chats.git", "本地克隆目录": "D:/git", "本机设备标识": "office"}
        def edit(root):
            for label, value in expected.items():
                entry = self.entry(root, label)
                # Also covers programmatic incoming/result updates from worker events.
                root.setvar(entry.cget("textvariable"), value)
            root.chat_app.values["result"].set("D:/merged")
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
            self.assertEqual(root.chat_app.values["result"].get(), "D:/merged")
        self.launch([restored])

    def test_project_choices_restore_refresh_and_reset_for_another_profile(self):
        from tkinter import ttk
        home = str(Path("D:/profile/.codex").resolve())
        sync.save_settings({"gui": {"home": home, "scope": "selected", "selection_home": home,
                                   "project_ids": ["project"], "thread_ids": ["old", "deleted"]}})
        groups = [{"id": "project", "name": "Example", "count": 2, "roots": [], "threads": [
            {"id": tid, "title": tid, "assignment": "database", "archived": False} for tid in ("old", "new")]}]
        def selected(root):
            tree = root.chat_app.project_tree
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
            tree = root.chat_app.project_tree
            self.assertTrue(str(tree.item("p0", "text")).startswith("☐"))
            self.assertEqual(sync.load_settings()["gui"]["project_ids"], [])
        with mock.patch.object(core, "project_catalog", return_value=groups):
            # Initial auto-refresh ends around 240 ms; allow its debounce to finish.
            self.launch([lambda root: None, selected, refresh, selected, change_home, cleared])

    def test_upload_checks_password_before_export(self):
        sync.save_settings({"url": "git@github.com:example/chats.git"})
        with mock.patch.object(ui.ChatMergeApp, "_error") as error, mock.patch.object(core, "export_snapshot") as export:
            def check(root):
                self.button(root, "备份并上传").invoke()
                self.assertFalse(root.chat_app.busy)
                export.assert_not_called()
                error.assert_called_once()
            self.launch([check])

    def test_retry_does_not_require_password_or_export(self):
        sync.save_settings({"url": "git@github.com:example/chats.git"})
        client = mock.Mock()
        client.retry_push.return_value = []
        with mock.patch.object(ui.ChatMergeApp, "_client", return_value=client), mock.patch.object(core, "export_snapshot") as export:
            def start(root):
                self.button(root, "仅重试推送").invoke()
            def check(root):
                client.retry_push.assert_called_once()
                export.assert_not_called()
                self.assertFalse(root.chat_app.busy)
            self.launch([start, check])

    def test_upload_connects_before_export_and_does_not_fetch_after_push(self):
        sync.save_settings({"url": "git@github.com:example/chats.git", "gui": {"work": str(self.path.parent / "backups")}})
        client = mock.Mock()
        client.connect.return_value = []
        client.local_snapshots.return_value = []
        calls = []
        client.connect.side_effect = lambda: calls.append("connect") or []
        client.upload_snapshot.side_effect = lambda *args: calls.append("upload")
        def export(*args, **kwargs):
            calls.append("export")
            return self.path.parent / "export"
        with mock.patch.object(ui.ChatMergeApp, "_client", return_value=client), mock.patch.object(core, "require_closed"), \
                mock.patch.object(sync, "aes_class"), mock.patch.object(core, "export_snapshot", side_effect=export):
            def start(root):
                root.chat_app.password.set("a-valid-test-password")
                self.button(root, "备份并上传").invoke()
            def check(root):
                self.assertEqual(calls, ["connect", "export", "upload"])
                client.connect.assert_called_once()
                client.local_snapshots.assert_called_once()
                self.assertFalse(root.chat_app.busy)
            self.launch([start, check])

    def test_download_previews_then_applies_only_after_confirmation(self):
        sync.save_settings({"url": "git@github.com:example/chats.git", "gui": {"work": str(self.path.parent / "work")}})
        client = mock.Mock()
        client.download.return_value = self.path.parent / "download"
        def merged(base, incoming, job, home, policy, log):
            core.write_json(Path(job) / "report.json", {"status": "ready", "target_home": home,
                            "new_threads": ["new-chat"], "updated_threads": [], "conflicts": []})
            return Path(job) / "merged"
        with mock.patch.object(ui.ChatMergeApp, "_client", return_value=client), \
                mock.patch.object(core, "require_closed"), mock.patch.object(core, "export_snapshot", return_value=self.path.parent / "base"), \
                mock.patch.object(core, "merge_snapshots", side_effect=merged), mock.patch.object(core, "apply_result") as apply, \
                mock.patch.object(ui.messagebox, "askyesno", return_value=True):
            def start(root):
                root.chat_app._show_remotes([{"device": "other", "snapshot_id": "20261106-120000-abc123", "encrypted": False}])
                self.button(root, "下载并预览合并").invoke()
            def review(root):
                app = root.chat_app
                self.assertEqual(app.tabs.select(), str(app.review))
                self.assertIn("新增 1", app.review_summary.get())
                apply.assert_not_called()
                self.button(root, "应用到本机").invoke()
            def check(root):
                apply.assert_called_once()
                self.assertTrue(str(apply.call_args.args[0]).endswith("merged"))
                self.assertEqual(root.chat_app.report["status"], "applied")
                self.assertEqual(str(self.button(root, "应用到本机").cget("state")), "disabled")
            self.launch([start, review, check])

    def test_conflicts_offer_remerge_from_retained_snapshots(self):
        from tkinter import ttk
        job = self.path.parent / "merge"
        core.write_json(job / "report.json", {"status": "conflicts_need_choice", "conflicts": [{"thread_id": "fork", "title": "分叉聊天"}]})
        with mock.patch.object(ui.ChatMergeApp, "_merge") as merge:
            def start(root):
                app = root.chat_app
                app._show_review(job)
                self.assertEqual(str(self.button(root, "应用到本机").cget("state")), "disabled")
                self.button(root, "重新选择冲突版本").invoke()
                radios = [w for w in self.widgets(root) if isinstance(w, ttk.Radiobutton) and w.cget("text") == "显示另一台版本"]
                radios[-1].invoke()
                self.button(root, "重新生成合并预览").invoke()
            def check(root):
                merge.assert_called_once()
                self.assertEqual(merge.call_args.args[:2], (job / "base", job / "incoming"))
                self.assertEqual(merge.call_args.args[3], "incoming")
            self.launch([start, check])

    def test_project_search_keeps_selection_and_narrow_export_scope(self):
        home = str(Path("D:/profile/.codex").resolve())
        groups = [{"id": "p", "name": "Project", "count": 2, "roots": [], "threads": [
            {"id": tid, "title": tid, "assignment": "database", "archived": False} for tid in ("one", "two")]}]
        def check(root):
            app = root.chat_app
            app.values["home"].set(home)
            app.selection_home, app.groups, app.loaded = home, groups, True
            app._render_projects()
            app.project_tree.item("p0", open=True)
            app.toggle_node("p0t0")
            app.search.set("two")
            self.assertEqual(app.checked_threads, {"one"})
            self.assertEqual(app.safe_export_arguments(), {"project_ids": [], "thread_ids": ["one"]})
            app.search.set("")
            self.assertTrue(app.project_tree.item("p0t0", "text").startswith("☑"))
        self.launch([check])

    def test_project_directory_dialog_prefills_local_path_and_remembers_edits(self):
        from tkinter import ttk
        job = self.path.parent / "merge"
        core.write_json(job / "report.json", {"status": "ready", "conflicts": []})
        source, target = r"D:\Office\Project", r"E:\Home\Project"
        def catalog(path):
            return [{"name": "Project", "roots": [source if Path(path).name == "incoming" else target]}]
        with mock.patch.object(core, "project_catalog", side_effect=catalog), mock.patch.object(ui.ChatMergeApp, "_merge") as merge:
            def edit(root):
                root.chat_app._show_review(job)
                self.button(root, "修改项目目录…").invoke()
                tree = next(w for w in self.widgets(root) if isinstance(w, ttk.Treeview) and tuple(w.cget("columns")) == ("source", "target"))
                self.assertEqual(tree.item("0", "values"), (source, target))
                dialog = tree.winfo_toplevel()
                entry = next(w for w in self.widgets(dialog) if isinstance(w, ttk.Entry))
                root.setvar(entry.cget("textvariable"), r"F:\Personal\Project")
                self.button(root, "按此目录重新合并").invoke()
            def check(root):
                merge.assert_called_once()
                self.assertEqual(merge.call_args.args[:2], (job / "base", job / "incoming"))
                self.assertEqual(root.chat_app.path_mappings, {source: r"F:\Personal\Project"})
            self.launch([edit, check])
        self.assertEqual(sync.load_settings()["gui"]["path_mappings"], {source: r"F:\Personal\Project"})


if __name__ == "__main__":
    unittest.main()
