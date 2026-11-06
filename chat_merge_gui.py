"""Task-oriented Windows interface for local exports and Git chat backups."""
from pathlib import Path
import os
import queue
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import codex_chat_merge as core
import git_chat_sync as sync


class ChatMergeApp:
    def __init__(self, root):
        self.root = root
        cfg = sync.load_settings()
        saved = cfg.get("gui", {})
        self.owner_id = cfg["owner_id"]
        defaults = {"home": str(Path.home() / ".codex"), "incoming": "",
                    "work": str(Path.home() / "CodexChatBackups"), "result": ""}
        self.values = {k: tk.StringVar(value=saved.get(k, v)) for k, v in defaults.items()}
        self.git_values = {k: tk.StringVar(value=cfg[k]) for k in ("url", "repo", "device")}
        self.encrypted = tk.BooleanVar(value=cfg["encrypt"])
        self.timeout = tk.StringVar(value=str(cfg.get("network_timeout", 1800) // 60))
        self.password = tk.StringVar()
        self.choice = tk.StringVar(value=saved.get("conflict", "stop"))
        self.scope = tk.StringVar(value=saved.get("scope", "all"))
        self.scope_summary = tk.StringVar()
        self.connection = tk.StringVar(value="尚未刷新远端备份")
        self.backup_summary = tk.StringVar(value="备份当前选择的聊天，自动导出、加密并上传。")
        self.project_hint = tk.StringVar(value="点击「读取 / 刷新项目」查看本机聊天。")
        self.status = tk.StringVar(value="就绪 · 先确认连接设置，再备份或导入聊天")
        self.search = tk.StringVar()
        self.show_log = tk.BooleanVar(value=False)
        self.checked_threads = set(saved.get("thread_ids", []))
        self.checked_projects = set(saved.get("project_ids", []))
        self.selection_home = saved.get("selection_home", "")
        self.groups, self.nodes, self.remote_items = [], {}, {}
        self.loaded = False
        self.busy = False
        self.job = None
        self.report = {}
        self.path_mappings = saved.get("path_mappings", {})
        self.events = queue.Queue()
        self.buttons, self.editors = [], []
        self.pending_save = self.poll_id = self.startup_id = None
        self.started = 0
        self.task = self.stage = ""
        self._style()
        self._build()
        self._update_scope(save=False)
        for var in [*self.values.values(), *self.git_values.values(), self.scope, self.choice, self.encrypted, self.timeout]:
            var.trace_add("write", self._changed)
        self.search.trace_add("write", lambda *_: self._render_projects())
        self.scope.trace_add("write", lambda *_: self._update_scope(save=False))
        self.values["home"].trace_add("write", lambda *_: self._home_changed())
        self.tabs.bind("<<NotebookTabChanged>>", self._changed)
        self.tabs.select(self.pages.get(saved.get("page", "git"), self.pages["git"]))
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.poll_id = root.after(100, self._poll)
        if self.scope.get() == "selected":
            self.startup_id = root.after(150, self.refresh_projects)
        result = self.values["result"].get()
        if result and (Path(result).parent / "report.json").is_file():
            try:
                self._show_review(Path(result).parent, select=False)
            except (OSError, ValueError):
                pass
        self._update_buttons()
        self.root.update_idletasks()

    def _style(self):
        self.root.title("Codex 聊天同步 · Git 与本地备份")
        self.root.geometry("1080x820")
        self.root.minsize(940, 800)
        self.root.configure(background="#f3f5f9")
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure(".", font=("Microsoft YaHei UI", 10), background="#f3f5f9", foreground="#17243b")
        style.configure("TNotebook", borderwidth=0)
        style.configure("TNotebook.Tab", padding=(20, 8))
        style.map("TNotebook.Tab", background=[("selected", "#ffffff")], foreground=[("selected", "#205cbe")])
        style.configure("Card.TLabelframe", background="#ffffff", bordercolor="#dce2ec", relief="solid")
        style.configure("Card.TLabelframe.Label", font=("Microsoft YaHei UI", 11, "bold"), background="#ffffff")
        for name in ("Card.TFrame", "Card.TLabel", "Card.TRadiobutton", "Card.TCheckbutton"):
            style.configure(name, background="#ffffff")
        style.configure("Muted.TLabel", foreground="#58677d", background="#ffffff")
        style.configure("TButton", padding=(12, 7))
        style.configure("Primary.TButton", background="#2563c4", foreground="#ffffff", borderwidth=0)
        style.map("Primary.TButton", background=[("disabled", "#c3cee0"), ("active", "#194b98")], foreground=[("disabled", "#ffffff")])
        style.configure("Treeview", rowheight=28, background="#ffffff", fieldbackground="#ffffff", borderwidth=0)
        style.configure("Treeview.Heading", padding=(6, 7), background="#eef2f8")
        style.map("Treeview", background=[("selected", "#dceafe")], foreground=[("selected", "#163c73")])
        style.configure("Status.TLabel", foreground="#325a88")

    def _card(self, parent, title):
        return ttk.LabelFrame(parent, text=title, padding=10, style="Card.TLabelframe")

    def _label(self, parent, text=None, variable=None, muted=False, wrap=870, **pack):
        label = ttk.Label(parent, text=text, textvariable=variable,
                          style="Muted.TLabel" if muted else "Card.TLabel", wraplength=wrap)
        label.pack(anchor="w", **pack)
        return label

    def _button(self, parent, text, command, primary=False, enabled=None, **pack):
        button = ttk.Button(parent, text=text, command=command, style="Primary.TButton" if primary else "TButton")
        button.pack(**pack)
        self.buttons.append((button, enabled or (lambda: True)))
        return button

    def _field(self, parent, label, variable, browse=False, password=False):
        row = ttk.Frame(parent, style="Card.TFrame")
        row.pack(fill="x", pady=4)
        ttk.Label(row, text=label, width=22, style="Card.TLabel").pack(side="left")
        entry = ttk.Entry(row, textvariable=variable, show="*" if password else "")
        entry.pack(side="left", fill="x", expand=True)
        self.editors.append(entry)
        if browse:
            self._button(row, "选择", lambda: self._browse(variable), side="left", padx=(8, 0))
        return entry

    def _build(self):
        outer = ttk.Frame(self.root, padding=14)
        outer.pack(fill="both", expand=True)
        header = ttk.Frame(outer)
        header.pack(fill="x", pady=(0, 10))
        ttk.Label(header, text="Codex 聊天同步", font=("Microsoft YaHei UI", 20, "bold")).pack(side="left")
        ttk.Label(header, text="两台电脑，接着聊", foreground="#58677d").pack(side="left", padx=18, pady=(9, 0))
        self.tabs = ttk.Notebook(outer)
        self.tabs.pack(fill="both", expand=True)
        self.pages = {key: ttk.Frame(self.tabs, padding=10) for key in ("git", "local", "settings")}
        for key, label in (("git", "Git 仓库同步"), ("local", "本地导出 / 合并"), ("settings", "连接与设置")):
            self.tabs.add(self.pages[key], text=label)
        self._build_git(self.pages["git"])
        self._build_local(self.pages["local"])
        self._build_settings(self.pages["settings"])
        self.review = self._card(self.tabs, "合并预览 · 确认后才会写入本机")
        self.review_summary = tk.StringVar()
        self._label(self.review, variable=self.review_summary)
        row = ttk.Frame(self.review, style="Card.TFrame")
        row.pack(fill="x", pady=(8, 0))
        self._button(row, "应用到本机", self.apply_click, primary=True,
                     enabled=lambda: self.report.get("status") == "ready", side="left")
        self._button(row, "查看报告", lambda: self.open_path(self.job / "report.json"), enabled=lambda: bool(self.job), side="left", padx=8)
        self._button(row, "打开备份目录", lambda: self.open_path(self.job), enabled=lambda: bool(self.job), side="left")
        self._button(row, "重新选择冲突版本", self.resolve_conflict,
                     enabled=lambda: bool(self.report.get("conflicts")), side="right")
        self._button(self.review, "修改项目目录…", self.edit_project_directories,
                     enabled=lambda: bool(self.job) and self.report.get("status") != "applied",
                     anchor="w", pady=(10, 0))
        self.review_details = tk.Text(self.review, height=12, wrap="word", state="disabled", relief="flat",
                                      background="#ffffff", font=("Microsoft YaHei UI", 10))
        self.review_details.pack(fill="both", expand=True, pady=(18, 0))
        footer = self.footer = ttk.Frame(outer)
        footer.pack(fill="x", pady=(12, 0))
        ttk.Label(footer, textvariable=self.status, style="Status.TLabel", wraplength=820).pack(side="left", fill="x", expand=True)
        ttk.Checkbutton(footer, text="操作日志", variable=self.show_log, command=self._toggle_log).pack(side="right")
        self.progress = ttk.Progressbar(outer, mode="indeterminate")
        self.progress.pack(fill="x", pady=(6, 0))
        self.log_frame = ttk.Frame(outer)
        self.log_text = tk.Text(self.log_frame, height=5, wrap="word", state="disabled", font=("Consolas", 9), relief="flat")
        scroll = ttk.Scrollbar(self.log_frame, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scroll.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

    def _build_git(self, page):
        connection = self._card(page, "同步位置")
        connection.pack(fill="x")
        row = ttk.Frame(connection, style="Card.TFrame")
        row.pack(fill="x")
        ttk.Label(row, textvariable=self.connection, style="Card.TLabel", wraplength=640).pack(side="left", fill="x", expand=True)
        self._button(row, "连接设置", lambda: self.tabs.select(self.pages["settings"]), side="right")
        self._button(row, "刷新远端备份", self.connect_click, side="right", padx=8)
        self._label(connection, variable=self.git_values["url"], muted=True, pady=(6, 0))
        selection = ttk.Frame(page)
        selection.pack(fill="x", pady=8)
        ttk.Label(selection, textvariable=self.scope_summary).pack(side="left", fill="x", expand=True)
        self._button(selection, "选择项目 / 聊天", lambda: self.tabs.select(self.pages["local"]), side="right")
        auth = self._card(page, "本次备份密码")
        auth.pack(fill="x", pady=(0, 8))
        self._field(auth, "备份密码：", self.password, password=True)
        row = ttk.Frame(auth, style="Card.TFrame")
        row.pack(fill="x")
        box = ttk.Checkbutton(row, text="加密上传", variable=self.encrypted, style="Card.TCheckbutton")
        box.pack(side="left")
        self.editors.append(box)
        ttk.Label(row, text="两台电脑使用相同密码；仅本次使用，不保存。", style="Muted.TLabel").pack(side="left", padx=14)
        actions = ttk.Frame(page)
        actions.pack(fill="both", expand=True)
        actions.columnconfigure((0, 1), weight=1, uniform="action")
        actions.rowconfigure(0, weight=1)
        upload = self._card(actions, "备份这台电脑 → Git")
        upload.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        self._label(upload, "结束工作时：退出 Codex，再备份并上传。", muted=True, wrap=390)
        self._label(upload, variable=self.backup_summary, wrap=390, pady=(16, 12))
        self._button(upload, "备份并上传", self.upload_click, primary=True, fill="x", pady=(4, 8))
        self._button(upload, "仅重试推送", self.retry_push_click, fill="x")
        self._label(upload, "推送失败时直接重试，使用已提交的分卷。", muted=True, wrap=390, pady=(8, 0))
        self._button(upload, "上传已有导出…", self.upload_existing, anchor="w", pady=(12, 0))
        download = self._card(actions, "Git → 导入到这台电脑")
        download.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        self._label(download, "开始工作时：刷新备份，选另一台电脑的记录。", muted=True, wrap=390)
        self.remote_tree = ttk.Treeview(download, columns=("date", "lock"), show="tree headings", height=4, selectmode="browse")
        self.remote_tree.heading("#0", text="设备")
        self.remote_tree.heading("date", text="备份时间")
        self.remote_tree.heading("lock", text="加密")
        self.remote_tree.column("#0", width=150, minwidth=100)
        self.remote_tree.column("date", width=146, stretch=False)
        self.remote_tree.column("lock", width=52, stretch=False)
        self.remote_tree.pack(fill="both", expand=True, pady=(12, 8))
        self.remote_tree.bind("<<TreeviewSelect>>", lambda _: self._update_buttons())
        self._button(download, "下载并预览合并", self.download_click, primary=True,
                     enabled=lambda: bool(self.remote_tree.selection()), fill="x")
        self._label(download, "预览后再确认应用，现有聊天会先完整备份。", muted=True, wrap=390, pady=(8, 0))

    def _build_local(self, page):
        scope = self._card(page, "导出范围（按项目）")
        scope.pack(fill="both", expand=True)
        row = ttk.Frame(scope, style="Card.TFrame")
        row.pack(fill="x")
        for label, value in (("全部聊天", "all"), ("按项目 / 聊天选择", "selected")):
            radio = ttk.Radiobutton(row, text=label, variable=self.scope, value=value, style="Card.TRadiobutton")
            radio.pack(side="left", padx=(0, 14))
            self.editors.append(radio)
        self._button(row, "读取 / 刷新项目", self.refresh_projects, side="right")
        search = ttk.Frame(scope, style="Card.TFrame")
        search.pack(fill="x", pady=(10, 8))
        ttk.Label(search, text="筛选项目或聊天", style="Card.TLabel").pack(side="left", padx=(0, 10))
        ttk.Entry(search, textvariable=self.search).pack(side="left", fill="x", expand=True)
        tree_frame = ttk.Frame(scope, style="Card.TFrame")
        tree_frame.pack(fill="both", expand=True)
        self.project_tree = ttk.Treeview(tree_frame, columns=("count", "method"), show="tree headings", height=5)
        for column, text, width in (("#0", "项目 / 聊天（点击勾选，展开可选单条）", 370),
                                    ("count", "数量 / 状态", 100), ("method", "项目目录 / 归属", 340)):
            self.project_tree.heading(column, text=text)
            self.project_tree.column(column, width=width, minwidth=80)
        scroll = ttk.Scrollbar(tree_frame, orient="vertical", command=self.project_tree.yview)
        self.project_tree.configure(yscrollcommand=scroll.set)
        self.project_tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.project_tree.bind("<ButtonRelease-1>", self._project_click)
        self.project_tree.bind("<space>", lambda _: (self.toggle_node(self.project_tree.focus()), "break")[-1])
        row = ttk.Frame(scope, style="Card.TFrame")
        row.pack(fill="x", pady=(8, 0))
        self._button(row, "全选项目", self.select_all, enabled=lambda: self.loaded, side="left")
        self._button(row, "清空选择", self.clear_selection, side="left", padx=8)
        ttk.Label(row, textvariable=self.scope_summary, style="Card.TLabel").pack(side="right")
        self._label(scope, variable=self.project_hint, muted=True, pady=(8, 0))
        row = ttk.Frame(page)
        row.pack(fill="x", pady=(10, 0))
        self._button(row, "导出到本地文件夹", self.export_click, primary=True, side="left")
        self._button(row, "去 Git 备份并上传 →", lambda: self.tabs.select(self.pages["git"]), side="left", padx=8)
        local = self._card(page, "从本地文件夹导入 · 先合并预览，再应用")
        local.pack(fill="x", pady=(12, 0))
        self._field(local, "另一台电脑的导出目录", self.values["incoming"], browse=True)
        row = ttk.Frame(local, style="Card.TFrame")
        row.pack(fill="x", pady=(6, 0))
        self._button(row, "预览本地合并", self.merge_click, side="left")
        self._button(row, "打开已有合并结果…", self.load_existing_result, side="left", padx=8)
        self._button(row, "回滚…", self.rollback_click, side="right")

    def _build_settings(self, page):
        local = self._card(page, "本机路径")
        local.pack(fill="x")
        self._field(local, "本机 .codex 目录", self.values["home"], browse=True)
        self._field(local, "工作 / 备份目录", self.values["work"], browse=True)
        self._label(local, "备份文件、下载和回滚副本统一保存在工作目录；导出和写入前请退出 Codex。", muted=True, pady=(6, 0))
        git = self._card(page, "Git 连接")
        git.pack(fill="x", pady=12)
        for key, label in (("url", "仓库地址"), ("repo", "本地克隆目录"), ("device", "本机设备标识")):
            self._field(git, label, self.git_values[key], browse=key == "repo")
        self._field(git, "网络超时（分钟）", self.timeout)
        self._label(git, "使用独立的聊天备份私库。沿用系统 Git / SSH 登录配置，两台电脑设备标识应不同。", muted=True, pady=(6, 0))
        row = ttk.Frame(git, style="Card.TFrame")
        row.pack(fill="x", pady=(10, 0))
        self._button(row, "连接并检查仓库", self.connect_click, primary=True, side="left")
        self._button(row, "打开仓库目录", lambda: self.open_path(Path(self.git_values["repo"].get())), side="left", padx=8)
        conflicts = self._card(page, "同一聊天在两台电脑分别续聊时")
        conflicts.pack(fill="x")
        row = ttk.Frame(conflicts, style="Card.TFrame")
        row.pack(fill="x")
        for label, value in (("停止并生成报告", "stop"), ("显示本机版本", "base"), ("显示另一台版本", "incoming")):
            radio = ttk.Radiobutton(row, text=label, variable=self.choice, value=value, style="Card.TRadiobutton")
            radio.pack(side="left", padx=(0, 18))
            self.editors.append(radio)
        self._label(conflicts, "双方原始版本始终保留；选择显示版本不会拼接分叉消息。", muted=True, pady=(8, 0))
        ttk.Label(page, text="更改自动保存到本机。备份密码不保存。", foreground="#58677d").pack(anchor="w", pady=(12, 0))

    def _browse(self, variable):
        path = filedialog.askdirectory(initialdir=variable.get() if Path(variable.get()).is_dir() else None)
        if path:
            variable.set(path)

    def _changed(self, *_):
        if self.pending_save:
            self.root.after_cancel(self.pending_save)
        self.pending_save = self.root.after(500, self.save_preferences)

    def save_preferences(self):
        if self.pending_save:
            self.root.after_cancel(self.pending_save)
            self.pending_save = None
        cfg = {k: v.get().strip() for k, v in self.git_values.items()}
        cfg.update(owner_id=self.owner_id, encrypt=self.encrypted.get())
        try:
            minutes = int(self.timeout.get())
            if 1 <= minutes <= 120:
                cfg["network_timeout"] = minutes * 60
        except ValueError:
            pass
        page = next((k for k, v in self.pages.items() if str(v) == self.tabs.select()), "git")
        cfg["gui"] = {k: v.get().strip() for k, v in self.values.items()}
        cfg["gui"].update(conflict=self.choice.get(), scope=self.scope.get(), selection_home=self.selection_home,
                          project_ids=sorted(self.checked_projects), thread_ids=sorted(self.checked_threads), page=page,
                          path_mappings=self.path_mappings)
        try:
            sync.save_settings(cfg)
            return True
        except OSError as e:
            self.log("无法保存设置：" + str(e))
            return False

    def _home_changed(self):
        self.loaded = False
        self._reset_review()
        self.project_hint.set("聊天目录已变更，请重新读取项目列表。")
        self._update_buttons()

    def _update_scope(self, save=True):
        self.scope_summary.set("导出范围：全部聊天" if self.scope.get() == "all" else
                               f"已选 {len(self.checked_threads)} 条聊天 · {len(self.checked_projects)} 个完整项目")
        if save:
            self._changed()

    def _update_buttons(self):
        for button, enabled in self.buttons:
            button.configure(state="normal" if not self.busy and enabled() else "disabled")
        for editor in self.editors:
            editor.configure(state="disabled" if self.busy else "normal")

    def log(self, message):
        self.events.put(("log", str(message)))

    def run(self, title, action):
        if self.busy:
            return
        self.save_preferences()
        self.busy = True
        self.started, self.task, self.stage = time.monotonic(), title, "正在准备"
        self.progress.start(15)
        self._update_buttons()
        def worker():
            try:
                action()
                self.events.put(("done", title + "完成"))
            except Exception as e:
                self.events.put(("error", str(e)))
            finally:
                self.events.put(("idle", None))
        threading.Thread(target=worker, daemon=False).start()

    def _poll(self):
        while not self.events.empty():
            kind, value = self.events.get()
            if kind == "projects":
                home, groups = value
                if home != self.selection_home:
                    self.checked_threads.clear()
                    self.checked_projects.clear()
                self.selection_home, self.groups, self.loaded = home, groups, True
                self.checked_projects.intersection_update(p["id"] for p in groups)
                self.checked_threads.intersection_update(t["id"] for p in groups for t in p["threads"])
                self.checked_threads.update(t["id"] for p in groups if p["id"] in self.checked_projects for t in p["threads"])
                self.project_hint.set(f"{len(groups)} 个分组 · {sum(p['count'] for p in groups)} 条聊天。项目选择同时用于本地导出和 Git 上传。")
                self._render_projects()
                self._update_scope()
            elif kind == "remotes":
                self._show_remotes(value)
            elif kind == "incoming":
                self.values["incoming"].set(str(value))
            elif kind == "review":
                self._show_review(value)
            elif kind == "exported":
                self.backup_summary.set("最近导出：" + Path(value).name)
            elif kind == "applied":
                self.report["status"] = "applied"
                self.values["result"].set("")
                self.review_summary.set("已应用到本机，可以启动 Codex 检查并继续聊天。")
                self.log("回滚凭据：" + str(value))
            elif kind == "idle":
                self.busy = False
                self.progress.stop()
                self._update_buttons()
            else:
                self.log_text.configure(state="normal")
                self.log_text.insert("end", time.strftime("%H:%M:%S ") + value + "\n")
                self.log_text.see("end")
                self.log_text.configure(state="disabled")
                if kind == "log":
                    self.stage = value.splitlines()[0][:110]
                else:
                    self.status.set(value.splitlines()[0][:180])
                    if kind == "error":
                        self.show_log.set(True)
                        self._toggle_log()
        if self.busy:
            elapsed = int(time.monotonic() - self.started)
            self.status.set(f"{self.task} · {elapsed // 60:02d}:{elapsed % 60:02d} · {self.stage}")
        self.poll_id = self.root.after(100, self._poll)

    def _toggle_log(self):
        if self.show_log.get():
            self.log_frame.pack(fill="x", pady=(8, 0))
        else:
            self.log_frame.pack_forget()

    def _error(self, text):
        messagebox.showerror("请先完成这一步", text, parent=self.root)

    def _cfg(self):
        cfg = {k: v.get().strip() for k, v in self.git_values.items()}
        try:
            sync.validate_url(cfg["url"])
            sync.identifier(cfg["device"])
            cfg["network_timeout"] = int(self.timeout.get()) * 60
            if not 60 <= cfg["network_timeout"] <= 7200:
                raise ValueError()
            cfg["owner_id"] = self.owner_id
            core.disjoint(cfg["repo"], self.values["home"].get())
            return cfg
        except (core.MergeError, ValueError) as e:
            self.tabs.select(self.pages["settings"])
            self._error(str(e) if isinstance(e, core.MergeError) else "网络超时请填写 1 至 120 分钟")

    def _client(self, cfg):
        return sync.GitStore(cfg["url"], cfg["repo"], cfg["device"], cfg["owner_id"], self.log,
                             network_timeout=cfg["network_timeout"])

    def connect_click(self):
        cfg = self._cfg()
        if cfg:
            self.run("刷新远端备份", lambda: self.events.put(("remotes", self._client(cfg).connect())))

    def _show_remotes(self, items):
        old = self.remote_tree.selection()
        old_key = self.remote_items.get(old[0], {}) if old else {}
        self.remote_tree.delete(*self.remote_tree.get_children())
        self.remote_items = {}
        preferred = None
        for number, item in enumerate(items):
            node = "remote" + str(number)
            self.remote_items[node] = item
            sid = item["snapshot_id"]
            date = sid[:8] + " " + sid[9:11] + ":" + sid[11:13] if len(sid) >= 15 else sid
            mine = item["device"] == self.git_values["device"].get()
            self.remote_tree.insert("", "end", iid=node, text=item["device"] + ("（本机）" if mine else ""),
                                    values=(date, "是" if item["encrypted"] else "否"))
            if preferred is None and not mine:
                preferred = node
            if item == old_key:
                preferred = node
        if preferred:
            self.remote_tree.selection_set(preferred)
        self.connection.set(f"已连接 · {len(items)} 台设备的最新备份" if items else "已连接 · 尚无备份，可以上传第一份")
        self._update_buttons()

    def safe_export_arguments(self):
        if self.scope.get() == "all":
            return {}
        home = str(Path(self.values["home"].get().strip()).resolve())
        if not self.loaded or self.selection_home != home:
            self.tabs.select(self.pages["local"])
            self._error("请先读取 / 刷新项目，确认要备份的聊天。")
            return None
        if not self.checked_threads and not self.checked_projects:
            self.tabs.select(self.pages["local"])
            self._error("请选择至少一个项目或一条聊天，或者切换到全部聊天。")
            return None
        covered = {t["id"] for p in self.groups if p["id"] in self.checked_projects for t in p["threads"]}
        return {"project_ids": sorted(self.checked_projects), "thread_ids": sorted(self.checked_threads - covered)}

    def refresh_projects(self):
        self.startup_id = None
        home = self.values["home"].get().strip()
        self.run("读取本机项目", lambda: self.events.put(("projects", (str(Path(home).resolve()), core.project_catalog(home)))))

    def _render_projects(self):
        expanded = {self.nodes[n][1]["id"] for n in self.project_tree.get_children() if self.project_tree.item(n, "open")}
        self.project_tree.delete(*self.project_tree.get_children())
        self.nodes = {}
        query = self.search.get().strip().casefold()
        for number, group in enumerate(self.groups):
            matches = query in (group["name"] + " " + " ".join(group["roots"])).casefold()
            threads = [t for t in group["threads"] if matches or query in t["title"].casefold()]
            if query and not matches and not threads:
                continue
            node = "p" + str(number)
            ids = {t["id"] for t in group["threads"]}
            mark = "☑" if group["id"] in self.checked_projects or (ids and ids <= self.checked_threads) else "▣" if ids & self.checked_threads else "☐"
            self.project_tree.insert("", "end", iid=node, text=mark + " " + group["name"], open=bool(query) or group["id"] in expanded,
                                     values=(group["count"], " / ".join(group["roots"]) or group["id"]))
            self.nodes[node] = ("project", group, None)
            for number, thread in enumerate(threads):
                child = node + "t" + str(number)
                self.project_tree.insert(node, "end", iid=child, text=("☑ " if thread["id"] in self.checked_threads else "☐ ") + thread["title"],
                                         values=("已归档" if thread["archived"] else "聊天", "目录推断" if thread["assignment"] == "directory" else "明确归属"))
                self.nodes[child] = ("thread", group, thread)

    def _project_click(self, event):
        if self.project_tree.identify_element(event.x, event.y) != "Treeitem.indicator":
            self.toggle_node(self.project_tree.identify_row(event.y))

    def toggle_node(self, node):
        if self.busy or not self.loaded or node not in self.nodes:
            return
        kind, group, thread = self.nodes[node]
        if kind == "project":
            ids = {t["id"] for t in group["threads"]}
            if group["id"] in self.checked_projects or (ids and ids <= self.checked_threads):
                self.checked_projects.discard(group["id"])
                self.checked_threads.difference_update(ids)
            else:
                self.checked_projects.add(group["id"])
                self.checked_threads.update(ids)
        else:
            self.checked_projects.discard(group["id"])
            if thread["id"] in self.checked_threads:
                self.checked_threads.remove(thread["id"])
            else:
                self.checked_threads.add(thread["id"])
        self.scope.set("selected")
        self._render_projects()
        self._update_scope()

    def select_all(self):
        self.checked_projects.update(p["id"] for p in self.groups)
        self.checked_threads.update(t["id"] for p in self.groups for t in p["threads"])
        self.scope.set("selected")
        self._render_projects()
        self._update_scope()

    def clear_selection(self):
        self.checked_projects.clear()
        self.checked_threads.clear()
        self.scope.set("selected")
        self._render_projects()
        self._update_scope()

    def _capture(self):
        return {k: v.get().strip() for k, v in self.values.items()}

    def _export(self, values, selected, repo=None):
        destination = Path(values["work"]) / ("export-" + core.stamp())
        if repo:
            core.disjoint(repo, destination)
        result = core.export_snapshot(values["home"], destination, self.log, **selected)
        self.events.put(("exported", result))
        return result

    def export_click(self):
        selected, values = self.safe_export_arguments(), self._capture()
        if selected is not None:
            self.run("导出本机聊天", lambda: self._export(values, selected))

    def _upload_password(self):
        if self.encrypted.get():
            if len(self.password.get()) < 12:
                self.tabs.select(self.pages["git"])
                self._error("请先填写至少 12 个字符的备份密码。")
                return False, None
            return True, self.password.get()
        return messagebox.askyesno("未加密上传", "聊天将以未加密形式上传到所填仓库，是否继续？", parent=self.root), None

    def upload_click(self):
        cfg, values = self._cfg(), self._capture()
        if cfg is None:
            return
        selected = self.safe_export_arguments()
        if selected is None:
            return
        valid, password = self._upload_password()
        if not valid:
            return
        def action():
            core.require_closed()
            if password is not None:
                sync.aes_class()
            client = self._client(cfg)
            self.events.put(("remotes", client.connect()))
            exported = self._export(values, selected, cfg["repo"])
            client.upload_snapshot(exported, password)
            self.events.put(("remotes", client.local_snapshots()))
        self.run("备份并上传", action)

    def upload_existing(self):
        cfg = self._cfg()
        if cfg is None:
            return
        path = filedialog.askdirectory(title="选择包含 manifest.json 的导出目录")
        if not path:
            return
        valid, password = self._upload_password()
        if not valid:
            return
        def action():
            client = self._client(cfg)
            client.upload_snapshot(path, password)
            self.events.put(("remotes", client.local_snapshots()))
        self.run("上传已有备份", action)

    def retry_push_click(self):
        cfg = self._cfg()
        if cfg:
            self.run("重试推送", lambda: self.events.put(("remotes", self._client(cfg).retry_push())))

    def _merge(self, base, incoming, values, policy):
        job = Path(values["work"]) / ("merge-" + core.stamp())
        try:
            options = {"path_mappings": dict(self.path_mappings)} if self.path_mappings else {}
            result = core.merge_snapshots(base, incoming, job, values["home"], policy, self.log, **options)
        except Exception:
            if (job / "report.json").exists():
                self.events.put(("review", job))
            raise
        self.events.put(("review", Path(result).parent))

    def _reset_review(self):
        self.report = {}
        self.job = None
        self.values["result"].set("")
        if str(self.review) in self.tabs.tabs():
            self.tabs.forget(self.review)
        self._update_buttons()

    def merge_click(self):
        values, policy = self._capture(), self.choice.get()
        if not values["incoming"]:
            self._error("请选择另一台电脑的导出目录，目录中应包含 manifest.json。")
            return
        self._reset_review()
        def action():
            base = core.export_snapshot(values["home"], Path(values["work"]) / ("local-" + core.stamp()), self.log)
            self._merge(base, values["incoming"], values, policy)
        self.run("预览本地合并", action)

    def download_click(self):
        cfg = self._cfg()
        selected = self.remote_tree.selection()
        item = self.remote_items.get(selected[0]) if selected else None
        if cfg is None or item is None:
            return
        password, values, policy = self.password.get(), self._capture(), self.choice.get()
        if item["encrypted"] and not password:
            self._error("所选备份已加密，请填写备份时使用的密码。")
            return
        self._reset_review()
        def action():
            core.require_closed()
            client = self._client(cfg)
            destination = Path(values["work"]) / ("download-" + core.stamp())
            incoming = client.download(item["device"], item["snapshot_id"], destination, password or None)
            self.events.put(("incoming", incoming))
            base_path = Path(values["work"]) / ("local-" + core.stamp())
            core.disjoint(cfg["repo"], base_path)
            base = core.export_snapshot(values["home"], base_path, self.log)
            self._merge(base, incoming, values, policy)
        self.run("下载并预览合并", action)

    def _show_review(self, job, select=True):
        self.job = Path(job)
        self.report = core.read_json(self.job / "report.json")
        ready = self.report.get("status") == "ready"
        self.values["result"].set(str(self.job / "merged") if ready else "")
        summary = f"新增 {len(self.report.get('new_threads', []))} · 更新 {len(self.report.get('updated_threads', []))} · 分叉 {len(self.report.get('conflicts', []))}"
        self.review_summary.set(summary + ("　尚未写入，确认后点击「应用到本机」。" if ready else "　合并未完成，请查看报告；分叉时可重新选择显示版本。"))
        details = [f"目标聊天目录：{self.report.get('target_home', '')}", f"双方备份与报告：{self.job}", "",
                   f"保留聊天：{len(self.report.get('kept_threads', []))}", ""]
        if self.report.get("path_mappings"):
            details.append("项目目录映射（只处理来源电脑的目录）：")
            details.extend(f"• {source} → {target}" for source, target in self.report["path_mappings"].items())
            details.append("")
        if self.report.get("conflicts"):
            details.append("分别续聊的聊天：")
            for conflict in self.report["conflicts"]:
                details.append("• " + (conflict.get("title") or conflict["thread_id"]))
                details.append("  " + conflict.get("reason", ""))
        details.extend(["", *self.report.get("warnings", [])])
        self.review_details.configure(state="normal")
        self.review_details.delete("1.0", "end")
        self.review_details.insert("end", "\n".join(details))
        self.review_details.configure(state="disabled")
        if str(self.review) not in self.tabs.tabs():
            self.tabs.add(self.review, text="合并预览")
        if select:
            self.tabs.select(self.review)
        self._update_buttons()

    def load_existing_result(self):
        path = filedialog.askdirectory(title="选择已有的 merged 目录")
        if path:
            try:
                self._show_review(Path(path).parent)
            except (OSError, ValueError) as e:
                self._error("无法读取合并报告：" + str(e))

    def edit_project_directories(self):
        previous = self.job
        if not previous:
            return
        try:
            source_groups = core.project_catalog(previous / "incoming")
            local_groups = core.project_catalog(previous / "base")
            entries, seen = [], set()
            existing = dict(self.path_mappings)
            existing.update(self.report.get("path_mappings", {}))
            existing = core.normalize_path_mappings(existing)
            for project in source_groups:
                candidates = [p for p in local_groups if p["name"] == project["name"] and len(p["roots"]) == 1]
                for source in project["roots"]:
                    key = core.normalized_root(source)
                    if key in seen:
                        continue
                    seen.add(key)
                    target = core.mapped_project_path(source, existing)
                    if target == source and len(project["roots"]) == 1 and len(candidates) == 1:
                        target = candidates[0]["roots"][0]
                    entries.append((project["name"], source, target))
            # Chats without project roots can still have a working directory.
            if not entries:
                with core.database(previous / "incoming" / core.discover(previous / "incoming")["state"]) as db:
                    for row in db.execute("SELECT DISTINCT cwd FROM threads ORDER BY cwd"):
                        if row[0]:
                            entries.append(("聊天工作目录", row[0], core.mapped_project_path(row[0], existing)))
        except (OSError, ValueError, core.MergeError) as e:
            self._error("无法读取快照中的项目目录：" + str(e))
            return
        if not entries:
            self._error("来源快照中没有可修改的项目目录或聊天工作目录。")
            return
        dialog = tk.Toplevel(self.root)
        dialog.title("修改项目目录")
        dialog.geometry("940x540")
        dialog.minsize(800, 440)
        dialog.transient(self.root)
        dialog.grab_set()
        frame = ttk.Frame(dialog, padding=18)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="把来源电脑的项目目录映射到本机。子目录随之转换，项目文件不会移动。",
                  wraplength=850).pack(anchor="w")
        ttk.Label(frame, text="同名且唯一的本机项目会预填目录；确认后重新生成合并预览。",
                  foreground="#58677d").pack(anchor="w", pady=(6, 12))
        table_frame = ttk.Frame(frame)
        table_frame.pack(fill="both", expand=True)
        tree = ttk.Treeview(table_frame, columns=("source", "target"), show="tree headings", selectmode="browse", height=6)
        for column, label, width in (("#0", "项目", 160), ("source", "来源电脑目录", 340), ("target", "本机目录", 340)):
            tree.heading(column, text=label)
            tree.column(column, width=width, minwidth=100)
        scroll = ttk.Scrollbar(table_frame, command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        for number, (name, source, target) in enumerate(entries):
            tree.insert("", "end", iid=str(number), text=name, values=(source, target))
        target_var, current = tk.StringVar(), [None]
        def select(_=None):
            selected = tree.selection()
            current[0] = selected[0] if selected else None
            if current[0] is not None:
                target_var.set(tree.item(current[0], "values")[1])
        def update(*_):
            if current[0] is not None:
                source = tree.item(current[0], "values")[0]
                tree.item(current[0], values=(source, target_var.get()))
        target_var.trace_add("write", update)
        tree.bind("<<TreeviewSelect>>", select)
        row = ttk.Frame(frame)
        row.pack(fill="x", pady=(14, 8))
        ttk.Label(row, text="选中项目的本机目录").pack(side="left", padx=(0, 10))
        ttk.Entry(row, textvariable=target_var).pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="选择文件夹", command=lambda: self._browse(target_var)).pack(side="left", padx=8)
        def keep_original():
            if current[0] is not None:
                target_var.set(tree.item(current[0], "values")[0])
        ttk.Button(row, text="保持原目录", command=keep_original).pack(side="left")
        row = ttk.Frame(frame)
        row.pack(fill="x", pady=(10, 0))
        def confirm():
            mappings = dict(self.path_mappings)
            for node in tree.get_children():
                source, target = tree.item(node, "values")
                mappings = {k: v for k, v in mappings.items() if core.normalized_root(k) != core.normalized_root(source)}
                mappings[source] = target
            try:
                mappings = core.normalize_path_mappings(mappings)
            except core.MergeError as e:
                messagebox.showerror("目录不完整", str(e), parent=dialog)
                return
            self.path_mappings = mappings
            self._changed()
            values, policy = self._capture(), self.choice.get()
            dialog.destroy()
            self._reset_review()
            self.run("按本机目录重新合并", lambda: self._merge(previous / "base", previous / "incoming", values, policy))
        ttk.Button(row, text="取消", command=dialog.destroy).pack(side="right")
        ttk.Button(row, text="按此目录重新合并", command=confirm, style="Primary.TButton").pack(side="right", padx=8)
        tree.selection_set("0")
        select()

    def resolve_conflict(self):
        dialog = tk.Toplevel(self.root)
        dialog.title("选择分叉聊天的显示版本")
        dialog.transient(self.root)
        dialog.grab_set()
        frame = ttk.Frame(dialog, padding=24)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="双方快照都会保留。请选择在 Codex 中显示哪一版：").pack(anchor="w", pady=(0, 12))
        policy = tk.StringVar(value=self.choice.get() if self.choice.get() != "stop" else "base")
        for label, value in (("显示本机版本", "base"), ("显示另一台版本", "incoming")):
            ttk.Radiobutton(frame, text=label, value=value, variable=policy).pack(anchor="w", pady=6)
        def confirm():
            self.choice.set(policy.get())
            previous, values, chosen = self.job, self._capture(), policy.get()
            dialog.destroy()
            self._reset_review()
            self.run("重新预览合并", lambda: self._merge(previous / "base", previous / "incoming", values, chosen))
        ttk.Button(frame, text="重新生成合并预览", command=confirm, style="Primary.TButton").pack(fill="x", pady=(12, 0))

    def apply_click(self):
        if self.report.get("status") != "ready":
            return
        values = self._capture()
        if not messagebox.askyesno("应用合并结果", "将写入：" + values["home"] + "\n\n请退出 Codex。工具会先建立可回滚备份，确认应用？", parent=self.root):
            return
        def action():
            receipt = core.apply_result(values["result"], values["home"], Path(values["work"]) / ("rollback-" + core.stamp()), self.log)
            self.events.put(("applied", receipt))
        self.run("应用到本机", action)

    def rollback_click(self):
        path = filedialog.askopenfilename(title="选择回滚目录中的 receipt.json", filetypes=[("回滚凭据", "receipt.json")])
        if path and messagebox.askyesno("回滚", "恢复到此次应用前的记录？请先退出 Codex。", parent=self.root):
            self.run("回滚聊天", lambda: core.rollback(path, self.log))

    def open_path(self, path):
        if not path or not Path(path).exists():
            self._error("目录或文件尚未生成。")
        elif os.name == "nt":
            os.startfile(str(Path(path).resolve()))

    def close(self):
        if self.busy:
            messagebox.showinfo("正在处理", "请等待操作完成后关闭，避免中断备份或恢复。", parent=self.root)
            return
        if not self.save_preferences():
            messagebox.showwarning("设置未保存", "无法保存本次选择，请检查配置目录的写入权限。", parent=self.root)
        for timer in (self.poll_id, self.startup_id):
            if timer:
                self.root.after_cancel(timer)
        self.root.destroy()


def launch():
    root = tk.Tk()
    ChatMergeApp(root)
    root.mainloop()
