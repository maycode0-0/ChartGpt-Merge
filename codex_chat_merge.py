"""Offline Codex chat merger for Windows. Python 3.10+.

Internal Codex formats are not a public import API. Never edit a live profile.
Run without arguments for the Chinese desktop interface.
"""
from __future__ import annotations

import argparse
import contextlib
import csv
import hashlib
import io
import json
import ntpath
import os
from pathlib import Path, PurePosixPath
import queue
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import tempfile
import uuid

FORMAT = "codex-chat-snapshot-v1"
GLOBAL = ".codex-global-state.json"
INDEX = "session_index.jsonl"
PROJECT_INDEX = "projects.json"
SNAPSHOT_METADATA = {PROJECT_INDEX}
CHAT_KEYS = {
    "local-projects", "project-order", "projectless-thread-ids",
    "thread-projectless-output-directories", "thread-workspace-root-hints",
    "sidebar-project-thread-orders", "thread-writable-roots", "project-appearances",
    "thread-project-assignments", "thread-project-membership-host-ids",
    "app-server-project-id-by-legacy-project-id-by-host", "electron-thread-read-state-v1",
}
STATE_CHILDREN = {
    "thread_dynamic_tools", "thread_attachments", "guardian_review_feedback",
    "thread_read_receipts",
}
HISTORY_TABLES = {
    "thread_turns", "thread_items", "thread_history_projection_state",
    "thread_realtime_items",
}
APP_FILES = ("sqlite/codex.db", "sqlite/codex-dev.db")
_locks = threading.local()


class MergeError(Exception):
    pass


def stamp():
    return time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def q(name):
    return '"' + name.replace('"', '""') + '"'


@contextlib.contextmanager
def database(path, writable=False):
    path = Path(path).resolve()
    conn = sqlite3.connect(str(path) if writable else path.as_uri() + "?mode=ro", uri=not writable)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA trusted_schema=OFF")
    if not writable:
        conn.execute("PRAGMA query_only=ON")
    try:
        yield conn
        if writable:
            conn.commit()
    except BaseException:
        if writable:
            conn.rollback()
        raise
    finally:
        conn.close()


def tables(c):
    return {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def columns(c, table):
    return [r[1] for r in c.execute("PRAGMA table_info(" + q(table) + ")")]


def rows(c, table, where="", params=()):
    return [dict(r) for r in c.execute("SELECT * FROM " + q(table) + where, params)]


def insert(c, table, row):
    names = list(row)
    c.execute("INSERT INTO " + q(table) + " (" + ",".join(map(q, names)) + ") VALUES ("
              + ",".join("?" for _ in names) + ")", [row[n] for n in names])


def schema(c):
    objects = [tuple(r) for r in c.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type,name")]
    migrations = []
    for name in ("_sqlx_migrations", "codex_schema_migrations"):
        if name in tables(c):
            cols = [x for x in ("version", "id", "checksum", "success") if x in columns(c, name)]
            migrations += [tuple(r) for r in c.execute(
                "SELECT " + ",".join(map(q, cols)) + " FROM " + q(name) + " ORDER BY 1")]
    return objects, migrations


def check_database(path):
    with database(path) as c:
        if [r[0] for r in c.execute("PRAGMA integrity_check")] != ["ok"]:
            raise MergeError(f"数据库损坏：{path}")
        if c.execute("PRAGMA foreign_key_check").fetchone():
            raise MergeError(f"数据库外键关系不完整：{path}")


def sqlite_copy(src, dst):
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    with database(src) as source, database(dst, True) as target:
        source.backup(target, pages=256, sleep=0.01)
    with database(dst, True) as c:
        c.execute("PRAGMA journal_mode=DELETE")
    check_database(dst)


def allowed(rel):
    p = PurePosixPath(rel)
    if "\\" in rel or p.is_absolute() or ".." in p.parts or ":" in rel:
        return False
    if rel in {GLOBAL, INDEX} | SNAPSHOT_METADATA | set(APP_FILES):
        return True
    if rel.endswith(("-wal", "-shm")):
        return allowed(rel[:-4]) and rel[:-4].endswith((".sqlite", ".db"))
    if len(p.parts) == 1:
        import re
        return bool(re.fullmatch(r"(?:state|thread_history)_\d+\.sqlite", rel))
    return p.parts[0] in {"sessions", "archived_sessions"}


def safe_path(root, rel):
    if not allowed(rel):
        raise MergeError(f"不允许的数据路径：{rel}")
    root = Path(root).resolve()
    result = root.joinpath(*PurePosixPath(rel).parts)
    # Reject symlinks / Windows junctions rather than following them into another directory.
    for part in [result, *result.parents]:
        if part == root.parent:
            break
        if part.exists():
            attrs = getattr(part.lstat(), "st_file_attributes", 0)
            if part.is_symlink() or attrs & 0x400:
                raise MergeError(f"不支持链接或目录联接：{part}")
        if part == root:
            break
    if not result.resolve().is_relative_to(root):
        raise MergeError("路径越过数据目录")
    return result


def disjoint(a, b):
    a, b = Path(a).resolve(), Path(b).resolve()
    if a.is_relative_to(b) or b.is_relative_to(a):
        raise MergeError("输入、输出及备份目录必须互不包含")


def require_closed():
    if os.name != "nt":
        return
    try:
        result = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True,
                                creationflags=0x08000000, check=True)
    except (OSError, subprocess.SubprocessError) as e:
        raise MergeError("无法读取 Windows 进程列表，出于数据完整性考虑已停止。"
                         "请在普通 Windows 桌面环境中运行工具，并确认 tasklist 可用。") from e
    text = result.stdout.decode("mbcs", errors="replace")
    active = {r[0].lower() for r in csv.reader(io.StringIO(text)) if r}
    found = active & {"codex.exe", "chatgpt.exe", "codex-code-mode-host.exe"}
    if found:
        raise MergeError("请先完全退出 Codex / ChatGPT（包括托盘和后台进程），再执行。检测到："
                         + ", ".join(sorted(found)))


@contextlib.contextmanager
def profile_lock(home):
    """OS lock released on process exit, including a crash; reentrant per thread."""
    identity = os.path.normcase(str(Path(home).resolve()))
    held = getattr(_locks, "held", set())
    if identity in held:
        yield
        return
    directory = Path(tempfile.gettempdir()) / "CodexChatMergeLocks"
    directory.mkdir(exist_ok=True)
    name = hashlib.sha256(identity.encode("utf-8")).hexdigest() + ".lock"
    with (directory / name).open("a+b") as f:
        f.seek(0, os.SEEK_END)
        if f.tell() == 0:
            f.write(b"0")
            f.flush()
        f.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            raise MergeError("另一个合并工具正在处理此数据目录，请等待完成") from e
        _locks.held = held | {identity}
        try:
            yield
        finally:
            _locks.held = held
            f.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def discover(home):
    result = {}
    for kind in ("state", "thread_history"):
        choices = list(Path(home).glob(kind + "_[0-9]*.sqlite"))
        if len(choices) != 1:
            raise MergeError(f"需要且只支持一份 {kind}_数字.sqlite，发现 {len(choices)} 份。"
                             "请使用相同版本 Codex 的完整数据目录。")
        result[kind] = choices[0].name
    return result


def inventory(root, paths):
    return {r: {"sha256": digest(safe_path(root, r)), "size": safe_path(root, r).stat().st_size}
            for r in sorted(paths)}


def load_snapshot(root):
    root = Path(root).resolve()
    manifest = read_json(root / "manifest.json")
    if manifest.get("format") != FORMAT:
        raise MergeError("请选择本工具导出的快照目录（内有 manifest.json）")
    if not isinstance(manifest.get("files"), dict):
        raise MergeError("快照缺少文件清单")
    names = set()
    for rel, meta in manifest["files"].items():
        if rel.casefold() in names:
            raise MergeError("快照包含 Windows 下重名的路径")
        names.add(rel.casefold())
        p = safe_path(root, rel)
        if not p.is_file() or p.stat().st_size != meta["size"] or digest(p) != meta["sha256"]:
            raise MergeError(f"快照缺失或校验失败：{rel}，请重新复制/下载")
    dbs = discover(root)
    if any(name not in manifest["files"] for name in dbs.values()):
        raise MergeError("快照数据库未列入校验清单")
    if GLOBAL not in manifest["files"]:
        raise MergeError("快照缺少项目状态")
    for rel in manifest["files"]:
        if rel.endswith((".sqlite", ".db")):
            check_database(root / rel)
    return manifest


def normalized_root(value):
    value = str(value).replace("/", "\\")
    if value.startswith("\\\\?\\UNC\\"):
        value = "\\\\" + value[8:]
    elif value.startswith("\\\\?\\"):
        value = value[4:]
    return ntpath.normcase(ntpath.normpath(value))


def project_catalog(home, *, state_path=None, global_state=None):
    """Read metadata only; prefer explicit UI membership, then server IDs, then cwd."""
    home = Path(home).resolve()
    g = global_state if global_state is not None else (read_json(home / GLOBAL) if (home / GLOBAL).exists() else {})
    legacy = g.get("local-projects", {})
    if not isinstance(legacy, dict):
        raise MergeError("尚未支持的 local-projects 数据格式")
    with database(state_path or home / discover(home)["state"]) as c:
        server = {r["id"]: r for r in rows(c, "projects")}
        roots = {pid: [r[0] for r in c.execute("SELECT path FROM project_roots WHERE project_id=? ORDER BY position", (pid,))]
                 for pid in server}
        threads = rows(c, "threads")
    groups, aliases = {}, {}
    for pid, p in server.items():
        key = "project:" + pid
        groups[key] = {"id": key, "name": p["name"], "roots": roots[pid],
                       "server_ids": [pid], "legacy_ids": [], "threads": []}
        aliases[pid] = key
    bridges = {}
    for mapping in g.get("app-server-project-id-by-legacy-project-id-by-host", {}).values():
        if isinstance(mapping, dict):
            for old, new in mapping.items():
                if new in server:
                    bridges.setdefault(old, set()).add(new)
    for pid, p in legacy.items():
        candidates = bridges.get(pid, set())
        if pid in server:
            candidates = {pid}
        if not candidates and p.get("rootPaths"):
            wanted = {normalized_root(x) for x in p["rootPaths"]}
            candidates = {sid for sid, v in server.items() if v["name"] == p.get("name")
                          and {normalized_root(x) for x in roots[sid]} == wanted}
        key = "project:" + next(iter(candidates)) if len(candidates) == 1 else "legacy:" + pid
        if key not in groups:
            groups[key] = {"id": key, "name": p.get("name") or pid, "roots": list(p.get("rootPaths", [])),
                           "server_ids": [], "legacy_ids": [], "threads": []}
        group = groups[key]
        group["legacy_ids"].append(pid)
        group["name"] = p.get("name") or group["name"]
        group["roots"] = list(dict.fromkeys(group["roots"] + list(p.get("rootPaths", []))))
        aliases[pid] = key
    unassigned = {"id": "unassigned", "name": "未分组聊天", "roots": [],
                  "server_ids": [], "legacy_ids": [], "threads": []}
    assignments = g.get("thread-project-assignments", {})
    for row in threads:
        tid = row["id"]
        assignment = assignments.get(tid, {})
        explicit = assignment.get("projectId") if isinstance(assignment, dict) else None
        force_unassigned = tid in g.get("projectless-thread-ids", []) and not explicit
        key = aliases.get(explicit) if explicit else None if force_unassigned else aliases.get(row.get("project_id"))
        method = "assignment" if explicit in aliases else "database" if key else "unassigned"
        # Explicit assignment to an unknown project must not be reassigned by cwd.
        if explicit and key is None:
            key = "legacy:" + explicit
            groups.setdefault(key, {"id": key, "name": "未识别项目 " + explicit[:8], "roots": [],
                                    "server_ids": [], "legacy_ids": [explicit], "threads": []})
            method = "assignment"
        if key is None and not force_unassigned and row.get("cwd"):
            cwd = normalized_root(row["cwd"])
            matches = []
            for gid, group in groups.items():
                for root in group["roots"]:
                    normalized = normalized_root(root)
                    try:
                        if ntpath.commonpath([cwd, normalized]) == normalized:
                            matches.append((len(normalized), gid))
                    except ValueError:
                        continue
            if matches:
                longest = max(n for n, _ in matches)
                choices = {gid for n, gid in matches if n == longest}
                if len(choices) == 1:
                    key, method = next(iter(choices)), "directory"
        group = groups[key] if key else unassigned
        group["threads"].append({"id": tid, "title": row.get("title") or row.get("name") or tid,
                                 "archived": bool(row.get("archived")), "assignment": method})
    output = sorted(groups.values(), key=lambda p: (p["name"].casefold(), p["id"]))
    if unassigned["threads"]:
        output.append(unassigned)
    for group in output:
        group["threads"].sort(key=lambda t: (t["title"].casefold(), t["id"]))
        group["count"] = len(group["threads"])
    return output


def select_export(catalog, project_ids=None, thread_ids=None):
    all_threads = {t["id"] for p in catalog for t in p["threads"]}
    if project_ids is None and thread_ids is None:
        return all_threads, {p["id"] for p in catalog}, {"mode": "all"}
    selected = set(thread_ids or [])
    unknown = selected - all_threads
    if unknown:
        raise MergeError("选中的聊天已不存在，请刷新项目列表后重试")
    groups = set()
    for token in project_ids or []:
        candidates = [p for p in catalog if p["id"] == token]
        if not candidates:
            candidates = [p for p in catalog if token in [p["name"], *p["legacy_ids"], *p["server_ids"]]]
        if len(candidates) != 1:
            raise MergeError("项目名称或标识无效或重名，请使用列表中的唯一项目标识：" + token)
        p = candidates[0]
        groups.add(p["id"])
        selected.update(t["id"] for t in p["threads"])
    groups.update(p["id"] for p in catalog if any(t["id"] in selected for t in p["threads"]))
    if not selected and not groups:
        raise MergeError("请至少勾选一个项目或聊天；空选择不会导出全部内容")
    return selected, groups, {"mode": "selected", "project_ids": sorted(groups), "thread_ids": sorted(selected)}


def filter_global(g, selected, kept_projects):
    project_ids = {pid for p in kept_projects for pid in p["legacy_ids"] + p["server_ids"]}
    result = {}
    for key in CHAT_KEYS:
        value = g.get(key)
        if value is None:
            continue
        if key in {"local-projects", "project-appearances"}:
            result[key] = {k: v for k, v in value.items() if k in project_ids or k.rsplit(":", 1)[-1] in project_ids}
        elif key == "project-order":
            result[key] = [x for x in value if x in project_ids]
        elif key == "projectless-thread-ids":
            result[key] = [x for x in value if x in selected]
        elif key == "app-server-project-id-by-legacy-project-id-by-host":
            maps = {host: {old: new for old, new in mapping.items() if old in project_ids and new in project_ids}
                    for host, mapping in value.items() if isinstance(mapping, dict)}
            result[key] = {host: mapping for host, mapping in maps.items() if mapping}
        elif key == "sidebar-project-thread-orders":
            orders = {}
            for k, v in value.items():
                if not isinstance(v, dict) or not isinstance(v.get("threadIds"), list):
                    raise MergeError("尚未支持的侧边栏项目排序格式，无法安全按项目导出")
                ids = [tid for tid in v["threadIds"] if tid in selected]
                if ids or k in project_ids or k.rsplit(":", 1)[-1] in project_ids:
                    orders[k] = dict(v, threadIds=ids)
            result[key] = orders
        elif isinstance(value, dict):
            result[key] = {tid: v for tid, v in value.items() if tid in selected}
        else:
            raise MergeError("尚未支持的项目导出字段格式：" + key)
    return result


def subset_database(source, destination, rules, transforms=None):
    """Create fresh pages containing only selected rows; no deleted private cells."""
    transforms = transforms or {}
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with database(source) as src, database(destination, True) as dst:
        objects = [tuple(r) for r in src.execute("SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' ORDER BY type,name")]
        for kind, name, sql in objects:
            if kind == "table":
                dst.execute(sql)
        for table, rule in rules.items():
            if table not in tables(src):
                continue
            if rule is None:
                queries = [("SELECT * FROM " + q(table), ())]
                keep = None
            else:
                column, values, *extra = rule
                values = sorted(values)
                keep = extra[0] if extra else None
                queries = [("SELECT * FROM " + q(table) + " WHERE " + q(column) + " IN (" + ",".join("?" for _ in values[i:i+400]) + ")",
                            values[i:i+400]) for i in range(0, len(values), 400)]
            for query, params in queries:
                for raw in src.execute(query, params):
                    row = dict(raw)
                    if keep is not None and not keep(row):
                        continue
                    if table in transforms:
                        row = transforms[table](row)
                    insert(dst, table, row)
        # Insert before creating triggers so nothing rewrites historical row values.
        for kind, name, sql in objects:
            if kind != "table":
                dst.execute(sql)
    check_database(destination)


def write_title_index(state_path, destination):
    import datetime
    with database(state_path) as c, Path(destination).open("w", encoding="utf-8", newline="\n") as f:
        for row in c.execute("SELECT id,title,updated_at FROM threads ORDER BY updated_at,id"):
            dt = datetime.datetime.fromtimestamp(row[2], datetime.timezone.utc).isoformat().replace("+00:00", "Z")
            f.write(json.dumps({"id": row[0], "thread_name": row[1], "updated_at": dt}, ensure_ascii=False) + "\n")


def export_snapshot(home, output, log=print, *, project_ids=None, thread_ids=None, _check_closed=True):
    with profile_lock(home):
        return _export_snapshot(home, output, log, project_ids=project_ids, thread_ids=thread_ids, _check_closed=_check_closed)


def _export_snapshot(home, output, log=print, *, project_ids=None, thread_ids=None, _check_closed=True):
    home, output = Path(home).resolve(), Path(output).resolve()
    disjoint(home, output)
    if output.exists():
        raise MergeError("导出目录已存在，请选择一个新目录")
    if _check_closed:
        require_closed()
    dbs = discover(home)
    output.mkdir(parents=True)
    try:
        if project_ids is not None or thread_ids is not None:
            return export_selected(home, output, dbs, project_ids, thread_ids, log, _check_closed=_check_closed)
        paths = []
        for rel in list(dbs.values()) + [x for x in APP_FILES if (home / x).is_file()]:
            log("备份数据库：" + rel)
            sqlite_copy(safe_path(home, rel), output / rel)
            paths.append(rel)
        for folder in ("sessions", "archived_sessions"):
            directory = home / folder
            if directory.exists():
                safe_path(home, folder + "/.path-check")
                for p in directory.rglob("*"):
                    rel = p.relative_to(home).as_posix()
                    safe_path(home, rel)
                    if p.is_file():
                        dst = output / rel
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        before = (p.stat().st_size, p.stat().st_mtime_ns)
                        shutil.copy2(p, dst)
                        if before != (p.stat().st_size, p.stat().st_mtime_ns):
                            raise MergeError("导出期间会话文件改变，请关闭 Codex 后重试")
                        paths.append(rel)
                        if len(paths) % 100 == 0:
                            log(f"已复制 {len(paths)} 个聊天文件……")
        g = read_json(home / GLOBAL) if (home / GLOBAL).exists() else {}
        write_json(output / GLOBAL, {k: v for k, v in g.items() if k in CHAT_KEYS})
        paths.append(GLOBAL)
        if (home / INDEX).exists():
            shutil.copy2(home / INDEX, output / INDEX)
            paths.append(INDEX)
        write_json(output / PROJECT_INDEX, {"format": "codex-project-catalog-v1", "selection": {"mode": "all"},
                                          "projects": project_catalog(home, state_path=output / dbs["state"], global_state=g)})
        paths.append(PROJECT_INDEX)
        if _check_closed:
            require_closed()
        log("计算导出文件的 SHA-256 校验清单……")
        manifest = {"format": FORMAT, "kind": "export", "source_home": str(home),
                    "created_at": stamp(), "databases": dbs, "files": inventory(output, paths)}
        write_json(output / "manifest.json", manifest)
        log(f"导出完成：{output}（{len(paths)} 个文件）")
        return output
    except BaseException:
        write_json(output / "FAILED.json", {"error": "导出未完成，请勿使用此目录"})
        raise


def export_selected(home, output, dbs, project_ids, thread_ids, log, *, _check_closed=True):
    g = read_json(home / GLOBAL) if (home / GLOBAL).exists() else {}
    paths = []
    with tempfile.TemporaryDirectory(prefix="codex-project-export-", dir=output.parent) as tmp:
        state_copy = Path(tmp) / dbs["state"]
        sqlite_copy(home / dbs["state"], state_copy)
        catalog = project_catalog(home, state_path=state_copy, global_state=g)
        selected, groups, selection = select_export(catalog, project_ids, thread_ids)
        kept = [dict(p, threads=[t for t in p["threads"] if t["id"] in selected],
                     count=sum(t["id"] in selected for t in p["threads"])) for p in catalog if p["id"] in groups]
        server_ids = {pid for p in kept for pid in p["server_ids"]}
        with database(state_copy) as s:
            supported(s, "state")
            thread_rows = [r for r in rows(s, "threads") if r["id"] in selected]
        server_ids.update(r["project_id"] for r in thread_rows if r.get("project_id"))
        sections = {r["thread_section_id"] for r in thread_rows if r.get("thread_section_id")}
        rules = {"_sqlx_migrations": None, "threads": ("id", selected),
                 "projects": ("id", server_ids), "project_roots": ("project_id", server_ids),
                 "project_idempotency_keys": ("project_id", server_ids), "thread_sections": ("id", sections),
                 "thread_spawn_edges": ("child_thread_id", selected, lambda r: r["parent_thread_id"] in selected)}
        rules.update({table: ("thread_id", selected) for table in STATE_CHILDREN})
        def parent_reference(row):
            if row.get("attached_thread_parent_id") not in selected:
                row["attached_thread_parent_id"] = None
            return row
        log(f"按项目导出：{len(kept)} 个分组、{len(selected)} 条聊天；重建专用数据库……")
        subset_database(state_copy, output / dbs["state"], rules, {"threads": parent_reference})
        paths.append(dbs["state"])
        with database(home / dbs["thread_history"]) as h:
            supported(h, "history")
        subset_database(home / dbs["thread_history"], output / dbs["thread_history"],
                        {"_sqlx_migrations": None, **{t: ("thread_id", selected) for t in HISTORY_TABLES}})
        paths.append(dbs["thread_history"])
        for rel in APP_FILES:
            if not (home / rel).is_file():
                continue
            with database(home / rel) as a:
                local_hosts = {r[0] for r in a.execute("SELECT host_id FROM local_thread_catalog_hosts WHERE host_kind='local'")}
                known = {"codex_schema_migrations", "local_thread_catalog_hosts", "local_thread_catalog_metadata",
                         "local_thread_catalog", "thread_timeline_ledger", "inbox_items", "automations", "automation_runs",
                         "local_app_server_feature_enablement", "local_thread_catalog_sync_state", "local_thread_catalog_scan_checkpoints",
                         "local_thread_catalog_scan_entries", "live_visualization_suggestions"}
                if tables(a) - known:
                    raise MergeError("应用数据库新增未支持的表，无法安全按项目导出")
            subset_database(home / rel, output / rel, {
                "codex_schema_migrations": None, "local_thread_catalog_metadata": None,
                "local_thread_catalog_hosts": ("host_id", local_hosts),
                "local_thread_catalog": ("thread_id", selected, lambda r: r["host_id"] in local_hosts),
                "thread_timeline_ledger": ("thread_id", selected, lambda r: r["host_id"] in local_hosts)})
            paths.append(rel)
        seen = set()
        for row in thread_rows:
            rel = rollout_relative(row, {"source_home": str(home)})
            if rel in seen:
                raise MergeError("多个聊天引用同一个日志，无法保证按项目导出的范围")
            seen.add(rel)
            src = safe_path(home, rel)
            if src.exists():
                dst = output / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                before = (src.stat().st_size, src.stat().st_mtime_ns)
                shutil.copy2(src, dst)
                if before != (src.stat().st_size, src.stat().st_mtime_ns):
                    raise MergeError("导出期间聊天日志改变，请退出 Codex 后重试")
                paths.append(rel)
        write_json(output / GLOBAL, filter_global(g, selected, kept))
        write_title_index(output / dbs["state"], output / INDEX)
        write_json(output / PROJECT_INDEX, {"format": "codex-project-catalog-v1", "selection": selection, "projects": kept})
        paths += [GLOBAL, INDEX, PROJECT_INDEX]
        if _check_closed:
            require_closed()
        manifest = {"format": FORMAT, "kind": "export", "source_home": str(home),
                    "created_at": stamp(), "databases": dbs, "selection": selection, "files": inventory(output, paths)}
        write_json(output / "manifest.json", manifest)
    log(f"项目导出完成：{output}；未选项目的数据库记录及未索引日志未加入快照。")
    return output


def copy_snapshot(src, dst, manifest):
    Path(dst).mkdir(parents=True, exist_ok=False)
    for rel in manifest["files"]:
        p = safe_path(dst, rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(safe_path(src, rel), p)
    write_json(Path(dst) / "manifest.json", manifest)


def rollout_relative(row, manifest):
    value, home = row["rollout_path"], manifest["source_home"]
    if "\\" in value or ntpath.splitdrive(value)[0]:
        def normal_windows(p):
            p = p.replace("/", "\\")
            if p.startswith("\\\\?\\UNC\\"):
                return "\\\\" + p[8:]
            if p.startswith("\\\\?\\"):
                return p[4:]
            return p
        try:
            rel = ntpath.relpath(normal_windows(value), normal_windows(home)).replace("\\", "/")
        except ValueError as e:
            raise MergeError("会话路径与快照来源目录不符") from e
    else:
        p = Path(value)
        rel = p.relative_to(Path(home)).as_posix() if p.is_absolute() else p.as_posix()
    if not allowed(rel) or PurePosixPath(rel).parts[0] not in {"sessions", "archived_sessions"}:
        raise MergeError("会话路径不在 sessions / archived_sessions 下")
    return rel


def prefix_relation(a, b):
    """equal / base / incoming / conflict, without loading large conversations."""
    sa, sb = a.stat().st_size, b.stat().st_size
    small = min(sa, sb)
    with a.open("rb") as fa, b.open("rb") as fb:
        remaining = small
        last = b""
        while remaining:
            n = min(1024 * 1024, remaining)
            ca, cb = fa.read(n), fb.read(n)
            if ca != cb:
                return "conflict"
            last = ca[-1:]
            remaining -= len(ca)
        if last != b"\n":
            return "conflict"
    return "equal" if sa == sb else "base" if sa > sb else "incoming"


def history_fingerprint(c, thread_id):
    h = hashlib.sha256()
    count = 0
    for t in sorted(HISTORY_TABLES & tables(c)):
        cols = columns(c, t)
        h.update(t.encode())
        for row in c.execute("SELECT * FROM " + q(t) + " WHERE thread_id=? ORDER BY "
                             + ",".join(map(q, cols)), (thread_id,)):
            h.update(repr(tuple(row)).encode("utf-8"))
            count += 1
    return h.digest(), count


def projected_relation(base, incoming, thread_id):
    """Compare database-only histories by primary key and full row content.

    Projection cursors refer to pruned raw files and are not message content.
    A changed existing item is ambiguous, so require an explicit conflict choice.
    """
    base_only = incoming_only = False
    for table in sorted((HISTORY_TABLES - {"thread_history_projection_state"}) & tables(base)):
        info = list(base.execute("PRAGMA table_info(" + q(table) + ")"))
        primary = [r[1] for r in sorted(info, key=lambda r: r[5]) if r[5]]
        cols = [r[1] for r in info]
        key_indices = [cols.index(name) for name in primary]
        query = "SELECT * FROM " + q(table) + " WHERE thread_id=? ORDER BY " + ",".join(map(q, primary))
        aa, bb = iter(base.execute(query, (thread_id,))), iter(incoming.execute(query, (thread_id,)))
        a, b = next(aa, None), next(bb, None)
        while a is not None or b is not None:
            if a is None:
                incoming_only = True
                b = next(bb, None)
            elif b is None:
                base_only = True
                a = next(aa, None)
            else:
                ka, kb = tuple(a[n] for n in key_indices), tuple(b[n] for n in key_indices)
                if ka == kb:
                    if tuple(a) != tuple(b):
                        return "conflict"
                    a, b = next(aa, None), next(bb, None)
                elif ka < kb:
                    base_only = True
                    a = next(aa, None)
                else:
                    incoming_only = True
                    b = next(bb, None)
            if base_only and incoming_only:
                return "conflict"
    return "base" if base_only else "incoming" if incoming_only else "equal"


def has_projected_messages(history, tid):
    for table in ("thread_items", "thread_realtime_items"):
        if table in tables(history) and history.execute(
                "SELECT 1 FROM " + q(table) + " WHERE thread_id=? LIMIT 1", (tid,)).fetchone():
            return True
    return False


def supported(c, kind):
    for t in tables(c):
        cols = columns(c, t)
        linked = any(n == "thread_id" or n.endswith("_thread_id") for n in cols)
        known = (STATE_CHILDREN | {"threads", "thread_spawn_edges", "rollout_migration_state"}
                 if kind == "state" else HISTORY_TABLES)
        if linked and t not in known:
            raise MergeError(f"发现尚未支持的聊天关联表：{t}，为避免遗漏已停止")
    if kind == "state" and not {"threads", "projects", "project_roots", "thread_sections"} <= tables(c):
        raise MergeError("当前工具只支持含 projects / project_roots / thread_sections 的数据库版本")


def project_signature(c, row):
    roots = [r[0] for r in c.execute("SELECT path FROM project_roots WHERE project_id=? ORDER BY position",
                                   (row["id"],))]
    return (row["name"], tuple(sorted(ntpath.normcase(ntpath.normpath(x)) for x in roots)))


def merge_groups(dst, src, report):
    maps = {"projects": {}, "thread_sections": {}}
    for table in maps:
        existing = {r["id"]: r for r in rows(dst, table)}
        for row in rows(src, table):
            old = row["id"]
            if table == "projects":
                sig = project_signature(src, row)
                matches = [k for k, v in existing.items() if project_signature(dst, v) == sig]
                # Rootless projects with identical names may still be distinct.
                if not sig[1]:
                    matches = [k for k in matches if k == old]
            else:
                matches = [k for k, v in existing.items() if v["name"] == row["name"]]
            if matches:
                new = old if old in matches else sorted(matches)[0]
            else:
                new = old
                if old in existing:
                    new = str(uuid.uuid5(uuid.NAMESPACE_URL, "codex-chat-merge:" + table + repr(row)))
                row = dict(row, id=new)
                if new not in existing:
                    insert(dst, table, row)
                    existing[new] = row
                    if table == "projects":
                        for root in rows(src, "project_roots", " WHERE project_id=?", (old,)):
                            insert(dst, "project_roots", dict(root, project_id=new))
            maps[table][old] = new
        report[table + "_mapped"] = sum(k != v for k, v in maps[table].items())
    return maps


def remap(value, mapping):
    if isinstance(value, str):
        return mapping.get(value, value)
    if isinstance(value, list):
        return [remap(v, mapping) for v in value]
    if isinstance(value, dict):
        return {mapping.get(k, k): remap(v, mapping) for k, v in value.items()}
    return value


def union(a, b):
    if isinstance(a, dict) and isinstance(b, dict):
        result = dict(a)
        for k, v in b.items():
            result[k] = union(result[k], v) if k in result else v
        return result
    if isinstance(a, list) and isinstance(b, list):
        result = list(a)
        for v in b:
            if v not in result:
                result.append(v)
        return result
    return a


def merge_global(base, incoming, chosen, maps, host_map):
    mapping = dict(maps["projects"])
    # Legacy UI project identifiers can differ from server project identifiers.
    bp, ip = base.get("local-projects", {}), incoming.get("local-projects", {})
    if not isinstance(bp, dict) or not isinstance(ip, dict):
        raise MergeError("尚未支持的 local-projects 数据格式")
    for key, p in ip.items():
        matches = [k for k, b in bp.items() if b.get("name") == p.get("name")
                   and b.get("rootPaths") == p.get("rootPaths") and b.get("rootPaths")]
        if matches:
            mapping[key] = key if key in matches else sorted(matches)[0]
        elif key in bp and bp[key] != p:
            raise MergeError("相同项目 ID 对应不同项目，请先在两边核对项目分组")
    mapping.update(host_map)
    incoming = remap(incoming, mapping)
    result = union(base, incoming)
    for k in ("thread-project-assignments", "thread-workspace-root-hints",
              "thread-projectless-output-directories", "thread-writable-roots",
              "thread-project-membership-host-ids", "electron-thread-read-state-v1"):
        if isinstance(result.get(k), dict) and isinstance(incoming.get(k), dict):
            for tid in chosen:
                if tid in incoming[k]:
                    result[k][tid] = incoming[k][tid]
    # A thread assigned to a project should not also remain in the projectless list.
    assignments = result.get("thread-project-assignments", {})
    if isinstance(assignments, dict) and isinstance(result.get("projectless-thread-ids"), list):
        result["projectless-thread-ids"] = [t for t in result["projectless-thread-ids"]
                                          if t not in assignments]
    return {k: v for k, v in result.items() if k in CHAT_KEYS}


def merge_app_catalog(dst_path, src_path, chosen, maps):
    host_map = {}
    with database(dst_path, True) as dst, database(src_path) as src:
        if schema(dst) != schema(src):
            raise MergeError("应用索引数据库版本不一致，请将两边 Codex 更新到相同版本后重新导出")
        required = {"local_thread_catalog", "local_thread_catalog_hosts", "thread_timeline_ledger"}
        if not required <= tables(dst):
            raise MergeError("尚未支持的应用聊天索引格式")
        for c in (dst, src):
            unknown = [t for t in tables(c) if t.startswith("local_thread_catalog") and t not in {
                "local_thread_catalog", "local_thread_catalog_hosts", "local_thread_catalog_metadata",
                "local_thread_catalog_sync_state", "local_thread_catalog_scan_entries",
                "local_thread_catalog_scan_checkpoints"}]
            if unknown:
                raise MergeError("应用索引新增了未支持的表：" + ",".join(unknown))
        bh = [r[0] for r in dst.execute("SELECT host_id FROM local_thread_catalog_hosts WHERE host_kind='local'")]
        ih = [r[0] for r in src.execute("SELECT host_id FROM local_thread_catalog_hosts WHERE host_kind='local'")]
        if len(bh) != 1 or len(ih) != 1:
            raise MergeError("每份应用索引必须恰有一个本机 host，无法安全判断目标主机")
        host_map[ih[0]] = bh[0]
        for tid in chosen:
            for t in ("local_thread_catalog", "thread_timeline_ledger", "local_thread_catalog_scan_entries"):
                if t not in tables(dst):
                    continue
                dst.execute("DELETE FROM " + q(t) + " WHERE host_id=? AND thread_id=?", (bh[0], tid))
                for row in rows(src, t, " WHERE host_id=? AND thread_id=?", (ih[0], tid)):
                    row["host_id"] = bh[0]
                    if row.get("project_id"):
                        row["project_id"] = maps["projects"].get(row["project_id"], row["project_id"])
                    insert(dst, t, row)
        # Force a new local scan; do not import another machine's cursor or automations.
        for t in ("local_thread_catalog_sync_state", "local_thread_catalog_scan_checkpoints"):
            if t in tables(dst):
                dst.execute("DELETE FROM " + q(t) + " WHERE host_id=?", (bh[0],))
        if "local_thread_catalog_metadata" in tables(dst):
            dst.execute("UPDATE local_thread_catalog_metadata SET catalog_revision=catalog_revision+1")
    return host_map


def validate_rollout(path, thread_id, history):
    ordinal = -1
    count = 0
    with path.open("rb") as f:
        for n, line in enumerate(f, 1):
            count += 1
            if not line.endswith(b"\n"):
                raise MergeError(f"会话 {thread_id} 第 {n} 行未完整写入")
            try:
                item = json.loads(line)
            except (ValueError, UnicodeError) as e:
                raise MergeError(f"会话 {thread_id} 第 {n} 行 JSON 损坏") from e
            if n == 1:
                p = item.get("payload", {})
                if item.get("type") != "session_meta" or thread_id not in {p.get("id"), p.get("session_id")}:
                    raise MergeError(f"会话文件与数据库 ID 不匹配：{thread_id}")
            if isinstance(item.get("ordinal"), int):
                if item["ordinal"] <= ordinal:
                    raise MergeError(f"会话 {thread_id} 的消息序号重复或倒退")
                ordinal = item["ordinal"]
        if count == 0:
            raise MergeError("空会话文件")
    if "thread_history_projection_state" in tables(history):
        row = history.execute("SELECT next_rollout_byte_offset FROM thread_history_projection_state WHERE thread_id=?",
                              (thread_id,)).fetchone()
        if row and not 0 <= row[0] <= path.stat().st_size:
            raise MergeError(f"聊天历史与会话文件读取位置不匹配：{thread_id}")


def merge_snapshots(base, incoming, output, target_home=None, conflict="stop", log=print):
    base, incoming, output = map(lambda p: Path(p).resolve(), (base, incoming, output))
    for p in (base, incoming):
        disjoint(p, output)
    disjoint(base, incoming)
    if output.exists():
        raise MergeError("合并输出目录已存在，请选择一个新目录")
    if conflict not in {"stop", "base", "incoming"}:
        raise MergeError("未知冲突策略")
    log("校验两份快照，较大的历史数据库需要一些时间……")
    bm, im = load_snapshot(base), load_snapshot(incoming)
    if bm.get("selection", {}).get("mode") == "selected":
        raise MergeError("本机基准必须使用全部聊天快照；按项目导出的快照只能作为另一台电脑的来源，以保留本机其他项目")
    target_home = str(Path(target_home or bm["source_home"]).resolve())
    if bm["databases"] != im["databases"]:
        raise MergeError("数据库版本不同，请将两台电脑的 Codex 更新到相同版本后重新导出")
    state, history = bm["databases"]["state"], bm["databases"]["thread_history"]
    for rel in (state, history):
        with database(base / rel) as b, database(incoming / rel) as i:
            if schema(b) != schema(i):
                raise MergeError(f"数据库结构或迁移版本不同：{rel}")
            supported(b, "state" if rel == state else "history")
    output.mkdir(parents=True)
    report = {"status": "planning", "target_home": target_home, "conflict_policy": conflict,
              "new_threads": [], "updated_threads": [], "kept_threads": [], "conflicts": [],
              "database_only_threads": [], "index_only_threads": [],
              "warnings": ["只迁移本地聊天；不抓取 ChatGPT 云端记录。",
                           "图片、音视频及聊天引用的外部文件未自动打包；原始路径仍保留。",
                           "界面最终效果需在目标 Codex 版本中实际恢复验证。"]}
    write_json(output / "report.json", report)
    log("保留双方快照作为恢复副本……")
    copy_snapshot(base, output / "base", bm)
    copy_snapshot(incoming, output / "incoming", im)
    # Work only on retained copies, never on the caller's input directories.
    base, incoming = output / "base", output / "incoming"
    chosen, rels = set(), {}
    with database(base / state) as b, database(incoming / state) as i, \
            database(base / history) as bh, database(incoming / history) as ih:
        br = {r["id"]: r for r in rows(b, "threads")}
        ir = {r["id"]: r for r in rows(i, "threads")}
        log(f"逐项比较来源的 {len(ir)} 个聊天……")
        for number, (tid, row) in enumerate(ir.items(), 1):
            if number % 100 == 0:
                log(f"已比较 {number} / {len(ir)} 个聊天……")
            rel = rollout_relative(row, im)
            if rel not in im["files"]:
                if row.get("history_mode") != "paginated":
                    raise MergeError(f"来源聊天 {tid} 缺少日志，且不是支持数据库历史的 paginated 格式。")
                report["database_only_threads"].append(tid)
                if not has_projected_messages(ih, tid):
                    report["index_only_threads"].append(tid)
            rels[tid] = rel
            if tid not in br:
                chosen.add(tid)
                report["new_threads"].append(tid)
                continue
            brel = rollout_relative(br[tid], bm)
            if brel not in bm["files"] or rel not in im["files"]:
                if brel not in bm["files"] and br[tid].get("history_mode") != "paginated":
                    raise MergeError(f"本机聊天 {tid} 缺少日志，无法安全比较旧格式聊天")
                relation = projected_relation(bh, ih, tid)
                if relation == "equal":
                    relation = "incoming" if rel in im["files"] and brel not in bm["files"] else "base"
            else:
                relation = prefix_relation(base / brel, incoming / rel)
            if relation == "equal":
                hf, hc = history_fingerprint(bh, tid)
                sf, sc = history_fingerprint(ih, tid)
                if hf != sf:
                    relation = "incoming" if hc == 0 and sc > 0 else "base" if sc == 0 else "conflict"
            if relation == "conflict":
                report["conflicts"].append({"thread_id": tid, "title": row.get("title", ""),
                                            "choice": conflict, "reason": "消息日志分叉，或同一日志的历史投影不同"})
                relation = conflict
            if relation == "incoming":
                chosen.add(tid)
                report["updated_threads"].append(tid)
            else:
                report["kept_threads"].append(tid)
        if report["conflicts"] and conflict == "stop":
            report["status"] = "conflicts_need_choice"
            write_json(output / "report.json", report)
            raise MergeError(f"发现 {len(report['conflicts'])} 个冲突。已保留两份备份和 report.json；"
                             "未生成可安装结果。查看报告后，改用 base / incoming 策略重新合并。")
    result = output / "merged"
    copy_snapshot(base, result, bm)
    (result / "manifest.json").unlink()
    paths = set(bm["files"])
    log(f"导入 {len(chosen)} 个新增或更新的聊天索引和项目关联……")
    with database(result / state, True) as dst, database(incoming / state) as src:
        maps = merge_groups(dst, src, report)
        for tid in chosen:
            row = dict(ir[tid])
            row["project_id"] = maps["projects"].get(row.get("project_id"), row.get("project_id"))
            row["thread_section_id"] = maps["thread_sections"].get(row.get("thread_section_id"), row.get("thread_section_id"))
            row["rollout_path"] = str(Path(target_home).joinpath(*PurePosixPath(rels[tid]).parts))
            for table in STATE_CHILDREN & tables(dst):
                dst.execute("DELETE FROM " + q(table) + " WHERE thread_id=?", (tid,))
            dst.execute("DELETE FROM threads WHERE id=?", (tid,))
            insert(dst, "threads", row)
            for table in STATE_CHILDREN & tables(dst):
                for child in rows(src, table, " WHERE thread_id=?", (tid,)):
                    if "id" in child:
                        found = dst.execute("SELECT thread_id FROM " + q(table) + " WHERE id=?", (child["id"],)).fetchone()
                        if found:
                            child["id"] = str(uuid.uuid5(uuid.NAMESPACE_URL, table + tid + child["id"]))
                    insert(dst, table, child)
            if "thread_spawn_edges" in tables(dst):
                dst.execute("DELETE FROM thread_spawn_edges WHERE child_thread_id=?", (tid,))
                for edge in rows(src, "thread_spawn_edges", " WHERE child_thread_id=?", (tid,)):
                    insert(dst, "thread_spawn_edges", edge)
        # Rebase all existing thread paths to the selected destination profile.
        for tid, row in br.items():
            if tid not in chosen:
                rel = rollout_relative(row, bm)
                dst.execute("UPDATE threads SET rollout_path=? WHERE id=?",
                            (str(Path(target_home).joinpath(*PurePosixPath(rel).parts)), tid))
    log("合并消息历史数据库，保留原始消息内容及偏移量……")
    with database(result / history, True) as dst, database(incoming / history) as src:
        for tid in chosen:
            # Projection deletion has a realtime cleanup trigger, so delete it first.
            order = sorted(HISTORY_TABLES & tables(dst), key=lambda t: t != "thread_history_projection_state")
            for table in order:
                dst.execute("DELETE FROM " + q(table) + " WHERE thread_id=?", (tid,))
            for table in order:
                for row in src.execute("SELECT * FROM " + q(table) + " WHERE thread_id=?", (tid,)):
                    insert(dst, table, dict(row))
    # First verify all incoming files against their projection and avoid path collisions.
    with database(incoming / history) as ih:
        owner = {rollout_relative(r, bm).casefold(): tid for tid, r in br.items() if tid not in chosen}
        for tid in chosen:
            rel = rels[tid]
            if rel.casefold() in owner and owner[rel.casefold()] != tid:
                raise MergeError("两个聊天引用同一会话文件路径，已停止")
            owner[rel.casefold()] = tid
            if rel in im["files"]:
                validate_rollout(incoming / rel, tid, ih)
                dst = safe_path(result, rel)
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(incoming / rel, dst)
                paths.add(rel)
            elif rel in paths:
                # A database-only winner must not be paired with an obsolete raw log.
                (result / rel).unlink()
                paths.remove(rel)
            if tid in br:
                old = rollout_relative(br[tid], bm)
                if old != rel and old in paths:
                    (result / old).unlink()
                    paths.remove(old)
        # Preserve unindexed source rollouts/other session assets; report them for inspection.
        indexed = {rollout_relative(r, im) for r in ir.values()}
        orphan_count = 0
        for rel in im["files"]:
            if PurePosixPath(rel).parts[0] not in {"sessions", "archived_sessions"} or rel in indexed:
                continue
            if rel in paths and digest(result / rel) != im["files"][rel]["sha256"]:
                report["warnings"].append("未索引的同名文件有差异，仅在 incoming 备份中保留：" + rel)
            elif rel not in paths:
                p = result / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(incoming / rel, p)
                paths.add(rel)
                orphan_count += 1
        report["unindexed_files_copied"] = orphan_count
    host_map = {}
    log("更新本机聊天目录、项目分组和标题索引……")
    for rel in APP_FILES:
        if (rel in bm["files"]) != (rel in im["files"]):
            raise MergeError("两边应用数据库清单不同，请使用相同版本和安装渠道导出")
        if rel in bm["files"]:
            host_map.update(merge_app_catalog(result / rel, incoming / rel, chosen, maps))
    write_json(result / GLOBAL, merge_global(read_json(base / GLOBAL), read_json(incoming / GLOBAL),
                                           chosen, maps, host_map))
    # Rebuild the small title index from the authoritative merged state, not by line concatenation.
    write_title_index(result / state, result / INDEX)
    write_json(result / PROJECT_INDEX, {"format": "codex-project-catalog-v1", "selection": {"mode": "all"},
                                       "projects": project_catalog(result)})
    with database(result / state) as c:
        report["total_threads"] = c.execute("SELECT COUNT(*) FROM threads").fetchone()[0]
    paths.update({INDEX, PROJECT_INDEX})
    log("验证合并数据库并计算完整校验清单……")
    for rel in paths:
        if rel.endswith((".sqlite", ".db")):
            check_database(result / rel)
    manifest = {"format": FORMAT, "kind": "merged", "source_home": target_home,
                "target_home": target_home, "created_at": stamp(), "databases": bm["databases"],
                "base_files": bm["files"], "files": inventory(result, paths)}
    write_json(result / "manifest.json", manifest)
    report["status"] = "ready"
    write_json(output / "report.json", report)
    log(f"合并完成：共 {report['total_threads']} 个聊天，新增 {len(report['new_threads'])}，"
        f"更新 {len(report['updated_threads'])}，冲突 {len(report['conflicts'])}。")
    log("尚未写入 Codex。结果：" + str(result))
    return result


def current_chat_files(home):
    home = Path(home)
    dbs = discover(home)
    paths = list(dbs.values()) + [x for x in APP_FILES if (home / x).exists()]
    paths += [x for x in (GLOBAL, INDEX) if (home / x).exists()]
    for folder in ("sessions", "archived_sessions"):
        if (home / folder).exists():
            for p in (home / folder).rglob("*"):
                rel = p.relative_to(home).as_posix()
                safe_path(home, rel)
                if p.is_file():
                    paths.append(rel)
    return paths


def check_base_unchanged(home, manifest):
    """Do not lose chats created between export and apply."""
    paths = set(current_chat_files(home))
    expected = {r: meta for r, meta in manifest["base_files"].items() if r not in SNAPSHOT_METADATA}
    if paths != set(expected):
        raise MergeError("目标聊天文件清单已变化。请重新导出本机并重新合并。")
    for rel in paths:
        p = safe_path(home, rel)
        if rel == GLOBAL:
            g = read_json(p)
            filtered = {k: v for k, v in g.items() if k in CHAT_KEYS}
            # Hash the same formatting used by write_json during export.
            content = (json.dumps(filtered, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
            if hashlib.sha256(content).hexdigest() != expected[rel]["sha256"]:
                raise MergeError("本机项目分组已变化，请重新导出并合并")
        elif rel.endswith((".sqlite", ".db")):
            # SQLite backup changes the file header; compare logical schema and rows instead.
            continue
        elif digest(p) != expected[rel]["sha256"]:
            raise MergeError("本机聊天已变化，请重新导出并合并：" + rel)


def logical_digest(path):
    h = hashlib.sha256()
    with database(path) as c:
        h.update(repr(schema(c)).encode())
        for table in sorted(tables(c)):
            if table.startswith("sqlite_"):
                continue
            h.update(table.encode())
            # ORDER BY every column is deterministic, including composite keys and WITHOUT ROWID.
            cols = columns(c, table)
            for row in c.execute("SELECT * FROM " + q(table) + " ORDER BY " + ",".join(map(q, cols))):
                h.update(repr(tuple(row)).encode("utf-8"))
    return h.hexdigest()


def atomic_copy(src, dst):
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".chat-merge-" + uuid.uuid4().hex + ".tmp")
    try:
        shutil.copy2(src, tmp)
        # Windows FlushFileBuffers requires a handle opened with write access.
        with tmp.open("r+b") as f:
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, dst)
    finally:
        if tmp.exists():
            tmp.unlink()


def rollback(receipt_path, log=print, *, _check_closed=True):
    receipt = read_json(receipt_path)
    with profile_lock(receipt["home"]):
        return _rollback(receipt_path, log, _check_closed=_check_closed)


def _rollback(receipt_path, log=print, *, _check_closed=True):
    receipt_path = Path(receipt_path).resolve()
    receipt = read_json(receipt_path)
    if receipt.get("format") != "codex-chat-apply-v1":
        raise MergeError("无效的回滚凭据")
    if receipt["status"] == "rolled_back":
        log("该操作已经回滚")
        return
    home, work = Path(receipt["home"]).resolve(), receipt_path.parent
    disjoint(home, work)
    if _check_closed:
        require_closed()
    log("校验回滚凭据和备份……")
    if receipt["status"] == "applied":
        expected = receipt.get("expected_after", {})
        present = set(current_chat_files(home))
        if present != set(expected):
            raise MergeError("应用后聊天文件清单已有变化，不能自动回滚；请保留备份重新合并")
        for rel, sha in expected.items():
            if digest(safe_path(home, rel)) != sha:
                raise MergeError("应用后记录已有变化，不能自动回滚：" + rel)
            if rel.endswith((".sqlite", ".db")):
                if any(Path(str(home / rel) + s).exists() for s in ("-wal", "-shm")):
                    raise MergeError("应用后数据库已重新打开并产生边车文件，不能自动回滚：" + rel)
    # Preflight everything before changing anything. Refuse to overwrite later conversations.
    for op in receipt["operations"]:
        p = safe_path(home, op["path"])
        now = digest(p) if p.exists() else None
        if now not in {op["before"], op["after"]}:
            raise MergeError("安装后文件又有变化，自动回滚会覆盖新记录，已停止：" + op["path"])
        if op["before"] is not None:
            old = safe_path(work / "old", op["path"])
            if not old.is_file() or digest(old) != op["before"]:
                raise MergeError("回滚备份校验失败：" + op["path"])
    for op in reversed(receipt["operations"]):
        p = safe_path(home, op["path"])
        if op["before"] is None:
            if p.exists():
                p.unlink()
        else:
            atomic_copy(work / "old" / op["path"], p)
    receipt["status"] = "rolled_back"
    write_json(receipt_path, receipt)
    log("已回滚：" + str(receipt_path))


def apply_result(result, home, backup_dir, log=print, *, _check_closed=True):
    with profile_lock(home):
        return _apply_result(result, home, backup_dir, log, _check_closed=_check_closed)


def _apply_result(result, home, backup_dir, log=print, *, _check_closed=True):
    result, home, backup_dir = map(lambda x: Path(x).resolve(), (result, home, backup_dir))
    disjoint(home, result)
    disjoint(home, backup_dir)
    disjoint(result, backup_dir)
    if backup_dir.exists():
        raise MergeError("回滚备份目录必须是新目录")
    log("校验合并结果和目标聊天状态……")
    m = load_snapshot(result)
    if m.get("kind") != "merged" or "base_files" not in m:
        raise MergeError("只允许应用已经完成合并的 merged 目录")
    if home != Path(m["target_home"]).resolve():
        raise MergeError("目标目录与合并时指定的目录不同，请重新合并")
    if _check_closed:
        require_closed()
    check_base_unchanged(home, m)
    # The preserved base is a prerequisite for stale-state protection.
    base = result.parent / "base"
    bm = load_snapshot(base)
    if bm["files"] != m["base_files"]:
        raise MergeError("合并结果与本机原始快照不匹配")
    for rel in m["base_files"]:
        if rel.endswith((".sqlite", ".db")):
            if logical_digest(home / rel) != logical_digest(base / rel):
                raise MergeError("目标数据库在导出后有变化，请重新导出并合并：" + rel)
    backup_dir.mkdir(parents=True)
    log("准备回滚备份及安装暂存文件……")
    receipt_path = backup_dir / "receipt.json"
    receipt = {"format": "codex-chat-apply-v1", "status": "preparing", "home": str(home),
               "created_at": stamp(), "operations": []}
    try:
        desired = set(m["files"]) - SNAPSHOT_METADATA
        # Remove superseded active/archived locations, not unrelated files.
        all_paths = desired | (set(m["base_files"]) - SNAPSHOT_METADATA)
        # Remove old WAL/SHM before installing a consolidated database. Back them
        # up too: a previous unclean exit can leave committed data in the WAL.
        sidecars = set()
        for rel in m["base_files"]:
            if rel.endswith((".sqlite", ".db")):
                for suffix in ("-wal", "-shm"):
                    if safe_path(home, rel + suffix).exists():
                        sidecars.add(rel + suffix)
        for rel in sorted(sidecars) + sorted(all_paths):
            p = safe_path(home, rel)
            new = safe_path(result, rel) if rel in desired else None
            if rel == GLOBAL:
                current = read_json(p) if p.exists() else {}
                for key in CHAT_KEYS:
                    current.pop(key, None)
                current.update(read_json(new))
                new = backup_dir / "staged-global.json"
                write_json(new, current)
            before = digest(p) if p.exists() else None
            after = digest(new) if new is not None else None
            if before == after:
                continue
            if before is not None:
                old = safe_path(backup_dir / "old", rel)
                old.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(p, old)
                if digest(old) != before:
                    raise MergeError("创建回滚备份失败")
            if new is not None:
                staged = safe_path(backup_dir / "new", rel)
                staged.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(new, staged)
                if digest(staged) != after:
                    raise MergeError("创建安装暂存文件失败")
            receipt["operations"].append({"path": rel, "before": before, "after": after})
        receipt["expected_after"] = {r: meta["sha256"] for r, meta in m["files"].items() if r not in SNAPSHOT_METADATA}
        for op in receipt["operations"]:
            if op["after"] is None:
                receipt["expected_after"].pop(op["path"], None)
            else:
                receipt["expected_after"][op["path"]] = op["after"]
        write_json(receipt_path, receipt)
        if _check_closed:
            require_closed()
        # Check again after staging, before the first replacement.
        for op in receipt["operations"]:
            p = safe_path(home, op["path"])
            if (digest(p) if p.exists() else None) != op["before"]:
                raise MergeError("准备安装期间文件改变，已停止")
        receipt["status"] = "applying"
        write_json(receipt_path, receipt)
        log(f"开始应用 {len(receipt['operations'])} 项文件变更，请勿启动 Codex……")
        for op in receipt["operations"]:
            p = safe_path(home, op["path"])
            if op["after"] is None:
                if p.exists():
                    p.unlink()
            else:
                atomic_copy(backup_dir / "new" / op["path"], p)
        receipt["status"] = "applied"
        write_json(receipt_path, receipt)
        log("安装完成。启动 Codex 检查聊天。回滚凭据：" + str(receipt_path))
        return receipt_path
    except BaseException:
        if receipt.get("status") == "applying":
            try:
                rollback(receipt_path, log, _check_closed=_check_closed)
            except Exception as e:
                log("自动回滚未完成，请保留回滚目录并运行 rollback：" + str(e))
        raise


def gui():
    from chat_merge_gui import launch
    launch()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Windows Codex 本地聊天导出、合并、应用和回滚；无参数打开窗口")
    sub = parser.add_subparsers(dest="command")
    export = sub.add_parser("export", help="完全退出 Codex 后导出聊天快照")
    export.add_argument("--home", default=str(Path.home() / ".codex"))
    export.add_argument("--out", required=True)
    export.add_argument("--project", dest="project_ids", action="append", help="按项目 ID 或唯一名称导出；可重复指定")
    export.add_argument("--thread", dest="thread_ids", action="append", help="额外选中聊天 ID；可重复指定")
    listing = sub.add_parser("list-projects", help="只读列出项目标识、名称和聊天数量")
    listing.add_argument("--home", default=str(Path.home() / ".codex"))
    merge = sub.add_parser("merge", help="合并两份快照到一个新目录")
    merge.add_argument("--base", required=True)
    merge.add_argument("--incoming", required=True)
    merge.add_argument("--out", required=True)
    merge.add_argument("--target-home")
    merge.add_argument("--conflict", choices=["stop", "base", "incoming"], default="stop")
    apply = sub.add_parser("apply", help="应用结果；必须保留合并目录旁的 base 目录")
    apply.add_argument("--result", required=True)
    apply.add_argument("--home", default=str(Path.home() / ".codex"))
    apply.add_argument("--backup", required=True)
    back = sub.add_parser("rollback", help="使用 receipt.json 回滚")
    back.add_argument("--receipt", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "export":
            export_snapshot(args.home, args.out, project_ids=args.project_ids, thread_ids=args.thread_ids)
        elif args.command == "list-projects":
            for project in project_catalog(args.home):
                print(f"{project['id']}\t{project['name']}\t{project['count']} 条聊天")
        elif args.command == "merge":
            merge_snapshots(args.base, args.incoming, args.out, args.target_home, args.conflict)
        elif args.command == "apply":
            apply_result(args.result, args.home, args.backup)
        elif args.command == "rollback":
            rollback(args.receipt)
        else:
            gui()
        return 0
    except Exception as e:
        print("操作未完成：" + str(e), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
