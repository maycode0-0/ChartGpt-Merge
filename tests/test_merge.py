"""Synthetic records against schema-only fixtures from the installed Codex build."""
import contextlib
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest import mock
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import codex_chat_merge as m


FIXTURES = Path(__file__).parent


def quiet(*args):
    pass


def make_home(home, threads, project="project-a", local_host="local-a"):
    home.mkdir(parents=True)
    (home / "sqlite").mkdir()
    for name, script in (("state_5.sqlite", "state.sql"),
                         ("thread_history_1.sqlite", "history.sql"),
                         ("sqlite/codex.db", "app.sql")):
        with sqlite3.connect(home / name) as c:
            c.executescript((FIXTURES / script).read_text(encoding="utf-8"))
    with m.database(home / "state_5.sqlite", True) as c:
        c.execute("INSERT INTO projects VALUES (?,?,?,?,?,?)", (project, "项目", "{}", 0, 1000, 1000))
        c.execute("INSERT INTO project_roots VALUES (?,?,?)", (project, 0, r"D:\Projects\test"))
        c.execute("INSERT INTO thread_sections VALUES (?,?,?)", ("section-" + project, "分组", None))
    for tid, messages in threads.items():
        put_thread(home, tid, messages, project)
    with m.database(home / "sqlite/codex.db", True) as c:
        c.execute("INSERT INTO local_thread_catalog_hosts VALUES (?,?)", (local_host, "local"))
        c.execute("INSERT INTO local_thread_catalog_metadata VALUES (1,1)")
        for tid in threads:
            m.insert(c, "local_thread_catalog", dict(host_id=local_host, thread_id=tid,
                     display_title="标题-" + tid, source_created_at=1, source_updated_at=2,
                     cwd=r"D:\Projects\test", source_kind="local", observation_sequence=1,
                     project_id=project))
            m.insert(c, "thread_timeline_ledger", dict(host_id=local_host, thread_id=tid,
                     sequence=0, record_id="record-" + tid, payload_json='{"text":"记录"}'))
        # Incoming scheduled work must never become active on the other machine.
        m.insert(c, "automations", dict(id="auto-" + local_host, name="自动化", prompt="仅本机", created_at=1, updated_at=2))
    m.write_json(home / m.GLOBAL, {
        "local-projects": {project: {"id": project, "name": "项目", "rootPaths": [r"D:\Projects\test"]}},
        "project-order": [project],
        "thread-project-assignments": {t: {"projectKind": "local", "projectId": project} for t in threads},
        "thread-project-membership-host-ids": {t: local_host for t in threads},
        "electron-persisted-atom-state": {"private-machine-setting": "preserve-me"},
        "installation_id": "DO-NOT-SYNC",
    })
    (home / m.INDEX).write_text("", encoding="utf-8")
    (home / "auth.json").write_text('{"secret":"excluded"}', encoding="utf-8")


def put_thread(home, tid, messages, project="project-a", archived=False):
    folder = "archived_sessions" if archived else "sessions"
    rel = f"{folder}/2026/11/06/rollout-{tid}.jsonl"
    rollout = home / rel
    rollout.parent.mkdir(parents=True, exist_ok=True)
    items = [{"type": "session_meta", "ordinal": 0, "payload": {"id": tid, "session_id": tid}}]
    items += [{"type": "event_msg", "ordinal": n + 1, "payload": {"message": msg}}
              for n, msg in enumerate(messages)]
    data = b"".join((json.dumps(o, ensure_ascii=False) + "\n").encode() for o in items)
    rollout.write_bytes(data)
    with m.database(home / "state_5.sqlite", True) as c:
        c.execute("DELETE FROM threads WHERE id=?", (tid,))
        m.insert(c, "threads", dict(id=tid, rollout_path=str(rollout), created_at=1, updated_at=2,
                 source="cli", model_provider="openai", cwd=r"D:\Projects\test", title="标题-" + tid,
                 sandbox_policy="{}", approval_mode="on-request", project_id=project,
                 thread_section_id="section-" + project, preview="示例", archived=int(archived)))
        c.execute("DELETE FROM thread_dynamic_tools WHERE thread_id=?", (tid,))
        m.insert(c, "thread_dynamic_tools", dict(thread_id=tid, position=0, name="tool", description="test", input_schema="{}"))
    with m.database(home / "thread_history_1.sqlite", True) as c:
        for table in m.HISTORY_TABLES:
            c.execute("DELETE FROM " + m.q(table) + " WHERE thread_id=?", (tid,))
        m.insert(c, "thread_history_projection_state", dict(thread_id=tid,
                 next_rollout_byte_offset=len(data), next_rollout_ordinal=len(items)))
        for n, message in enumerate(messages):
            m.insert(c, "thread_turns", dict(thread_id=tid, turn_id="turn-" + str(n), rollout_ordinal=n + 1,
                     status="completed", rollout_byte_offset=0, rollout_end_byte_offset=len(data)))
            m.insert(c, "thread_items", dict(thread_id=tid, turn_id="turn-" + str(n), item_id="item-" + str(n),
                     rollout_ordinal=n + 1, created_at_ms=1000, item_json=json.dumps({"text": message}), item_type="userMessage"))


class MergeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.a, self.b = self.root / "home-a", self.root / "home-b"

    def export(self, home, name):
        return m.export_snapshot(home, self.root / name, quiet, _check_closed=False)

    def merge(self, a, b, policy="stop", name="job"):
        return m.merge_snapshots(a, b, self.root / name, self.a, policy, quiet)

    def read_ids(self, result):
        with m.database(result / "state_5.sqlite") as c:
            return {r[0] for r in c.execute("SELECT id FROM threads")}

    def test_distinct_threads_projects_hosts_and_private_settings(self):
        make_home(self.a, {"a": ["甲"]})
        make_home(self.b, {"b": ["乙"]}, project="project-b", local_host="local-b")
        a, b = self.export(self.a, "a"), self.export(self.b, "b")
        self.assertNotIn("installation_id", m.read_json(a / m.GLOBAL))
        self.assertFalse((a / "auth.json").exists())
        result = self.merge(a, b)
        self.assertEqual(self.read_ids(result), {"a", "b"})
        with m.database(result / "state_5.sqlite") as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM projects").fetchone()[0], 1)
            self.assertEqual(c.execute("SELECT project_id FROM threads WHERE id='b'").fetchone()[0], "project-a")
            self.assertEqual(c.execute("SELECT COUNT(*) FROM thread_dynamic_tools").fetchone()[0], 2)
        with m.database(result / "sqlite/codex.db") as c:
            self.assertEqual({r[0] for r in c.execute("SELECT host_id FROM local_thread_catalog")}, {"local-a"})
            self.assertEqual({r[0] for r in c.execute("SELECT thread_id FROM local_thread_catalog")}, {"a", "b"})
            self.assertEqual({r[0] for r in c.execute("SELECT id FROM automations")}, {"auto-local-a"})
        g = m.read_json(result / m.GLOBAL)
        self.assertEqual(g["thread-project-assignments"]["b"]["projectId"], "project-a")
        self.assertEqual(g["thread-project-membership-host-ids"]["b"], "local-a")
        receipt = m.apply_result(result, self.a, self.root / "undo", quiet, _check_closed=False)
        self.assertEqual(self.read_ids(self.a), {"a", "b"})
        self.assertEqual(m.read_json(self.a / m.GLOBAL)["installation_id"], "DO-NOT-SYNC")
        m.rollback(receipt, quiet, _check_closed=False)
        self.assertEqual(self.read_ids(self.a), {"a"})
        self.assertFalse((self.a / "sessions/2026/11/06/rollout-b.jsonl").exists())

    def test_append_preserves_exact_bytes_and_projection(self):
        make_home(self.a, {"shared": ["第一条"]})
        make_home(self.b, {"shared": ["第一条", "后续"]})
        a, b = self.export(self.a, "a"), self.export(self.b, "b")
        result = self.merge(a, b)
        rel = "sessions/2026/11/06/rollout-shared.jsonl"
        self.assertEqual((result / rel).read_bytes(), (b / rel).read_bytes())
        with m.database(result / "thread_history_1.sqlite") as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM thread_items").fetchone()[0], 2)
            self.assertEqual(c.execute("SELECT next_rollout_byte_offset FROM thread_history_projection_state").fetchone()[0], (result / rel).stat().st_size)

    def test_base_longer_keeps_complete_history(self):
        make_home(self.a, {"shared": ["一", "二"]})
        make_home(self.b, {"shared": ["一"]})
        result = self.merge(self.export(self.a, "a"), self.export(self.b, "b"))
        with m.database(result / "thread_history_1.sqlite") as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM thread_items").fetchone()[0], 2)

    def test_fork_stops_and_retains_both_snapshots(self):
        make_home(self.a, {"shared": ["共同", "甲续聊"]})
        make_home(self.b, {"shared": ["共同", "乙续聊"]})
        a, b = self.export(self.a, "a"), self.export(self.b, "b")
        with self.assertRaises(m.MergeError):
            self.merge(a, b)
        report = m.read_json(self.root / "job/report.json")
        self.assertEqual(report["status"], "conflicts_need_choice")
        self.assertFalse((self.root / "job/merged/manifest.json").exists())
        m.load_snapshot(self.root / "job/base")
        m.load_snapshot(self.root / "job/incoming")
        result = self.merge(a, b, "incoming", "chosen")
        with m.database(result / "thread_history_1.sqlite") as c:
            text = c.execute("SELECT item_json FROM thread_items ORDER BY rollout_ordinal DESC LIMIT 1").fetchone()[0]
            self.assertEqual(json.loads(text)["text"], "乙续聊")
        result2 = self.merge(a, b, "base", "chosen-base")
        with m.database(result2 / "thread_history_1.sqlite") as c:
            text = c.execute("SELECT item_json FROM thread_items ORDER BY rollout_ordinal DESC LIMIT 1").fetchone()[0]
            self.assertEqual(json.loads(text)["text"], "甲续聊")

    def test_tampered_snapshot_rejected(self):
        make_home(self.a, {"a": ["甲"]})
        a = self.export(self.a, "a")
        (a / "sessions/2026/11/06/rollout-a.jsonl").write_text("changed", encoding="utf-8")
        with self.assertRaises(m.MergeError):
            m.load_snapshot(a)

    def test_schema_drift_rejected_before_output(self):
        make_home(self.a, {"a": ["甲"]})
        make_home(self.b, {"b": ["乙"]})
        with m.database(self.b / "state_5.sqlite", True) as c:
            c.execute("ALTER TABLE threads ADD COLUMN future_field TEXT")
        with self.assertRaises(m.MergeError):
            self.merge(self.export(self.a, "a"), self.export(self.b, "b"))
        self.assertFalse((self.root / "job").exists())

    def test_known_app_migrations_do_not_prevent_merge_or_change_target_schema(self):
        app = "sqlite/codex.db"
        make_home(self.a, {"a": ["older schema"]})
        make_home(self.b, {"b": ["newer schema"]}, local_host="local-b")
        with m.database(self.a / app, True) as c:
            c.execute("DROP TABLE active_chat_turns")
        with m.database(self.b / app, True) as c:
            c.execute("ALTER TABLE automations ADD COLUMN remote_host TEXT")
            c.execute("UPDATE automations SET remote_host='local-only-host'")
            c.execute("INSERT INTO active_chat_turns VALUES ('profile','account','user','b','launch',1)")
            c.executemany("INSERT INTO codex_schema_migrations (id,checksum,applied_at) VALUES (?,'sample',1)", [
                ("20261028065000_create_active_chat_turns",),
                ("20261029223800_add_automation_remote_host",)])
        old, new = self.export(self.a, "old"), self.export(self.b, "new")
        for target, source in ((old, new), (new, old)):
            with self.subTest(target=target.name):
                result = m.merge_snapshots(target, source, self.root / ("job-" + target.name), log=quiet)
                self.assertEqual(self.read_ids(result), {"a", "b"})
                with m.database(result / app) as c, m.database(target / app) as original:
                    self.assertEqual(m.schema(c), m.schema(original))
                    for table in ("active_chat_turns", "automations"):
                        if table in m.tables(original):
                            self.assertEqual(m.rows(c, table), m.rows(original, table))
                    self.assertEqual({r[0] for r in c.execute("SELECT thread_id FROM local_thread_catalog")}, {"a", "b"})

    def test_unknown_app_drift_reports_database_table_and_migration(self):
        make_home(self.a, {"a": ["first"]})
        make_home(self.b, {"b": ["second"]})
        with m.database(self.b / "sqlite/codex.db", True) as c:
            c.execute("ALTER TABLE local_thread_catalog ADD COLUMN future_field TEXT")
            c.execute("INSERT INTO codex_schema_migrations (id,checksum,applied_at) VALUES ('future_step','sample',1)")
        with self.assertRaisesRegex(m.MergeError, r"codex\.db.*local_thread_catalog.*future_step"):
            self.merge(self.export(self.a, "a"), self.export(self.b, "b"))
        self.assertFalse((self.root / "job/merged/manifest.json").exists())

    def test_stale_local_database_rejected(self):
        make_home(self.a, {"a": ["甲"]})
        make_home(self.b, {"b": ["乙"]})
        result = self.merge(self.export(self.a, "a"), self.export(self.b, "b"))
        with m.database(self.a / "state_5.sqlite", True) as c:
            c.execute("UPDATE threads SET title='新标题' WHERE id='a'")
        with self.assertRaises(m.MergeError):
            m.apply_result(result, self.a, self.root / "undo", quiet, _check_closed=False)
        self.assertEqual(self.read_ids(self.a), {"a"})

    def test_new_local_chat_after_export_rejected(self):
        make_home(self.a, {"a": ["甲"]})
        make_home(self.b, {"b": ["乙"]})
        result = self.merge(self.export(self.a, "a"), self.export(self.b, "b"))
        put_thread(self.a, "later", ["新消息"])
        with self.assertRaises(m.MergeError):
            m.apply_result(result, self.a, self.root / "undo", quiet, _check_closed=False)

    def test_mid_apply_failure_automatically_rolls_back(self):
        make_home(self.a, {"a": ["甲"]})
        make_home(self.b, {"b": ["乙"]})
        result = self.merge(self.export(self.a, "a"), self.export(self.b, "b"))
        before = {p: m.digest(self.a / p) for p in m.current_chat_files(self.a)}
        original = m.atomic_copy
        count = [0]
        def fail_once(src, dst):
            count[0] += 1
            if count[0] == 3:
                raise OSError("simulated disk error")
            return original(src, dst)
        with mock.patch.object(m, "atomic_copy", side_effect=fail_once):
            with self.assertRaises(OSError):
                m.apply_result(result, self.a, self.root / "undo", quiet, _check_closed=False)
        self.assertEqual(before, {p: m.digest(self.a / p) for p in m.current_chat_files(self.a)})
        self.assertEqual(m.read_json(self.root / "undo/receipt.json")["status"], "rolled_back")

    def test_rollback_refuses_to_overwrite_later_changes(self):
        make_home(self.a, {"a": ["甲"]})
        make_home(self.b, {"b": ["乙"]})
        result = self.merge(self.export(self.a, "a"), self.export(self.b, "b"))
        receipt = m.apply_result(result, self.a, self.root / "undo", quiet, _check_closed=False)
        with m.database(self.a / "state_5.sqlite", True) as c:
            c.execute("UPDATE threads SET title='后来变化' WHERE id='a'")
        with self.assertRaises(m.MergeError):
            m.rollback(receipt, quiet, _check_closed=False)

    def test_malicious_manifest_paths_rejected(self):
        for path in ("../auth.json", "sessions/../../config.toml", "C:/secrets", "/tmp/x", "sessions\\..\\x"):
            self.assertFalse(m.allowed(path), path)

    def test_incomplete_log_rejected(self):
        make_home(self.a, {"a": ["甲"]})
        make_home(self.b, {"b": ["乙"]})
        p = self.b / "sessions/2026/11/06/rollout-b.jsonl"
        p.write_bytes(p.read_bytes()[:-1])
        with self.assertRaises(m.MergeError):
            self.merge(self.export(self.a, "a"), self.export(self.b, "b"))
        self.assertFalse((self.root / "job/merged/manifest.json").exists())

    def test_archived_thread_and_repeated_merge_are_idempotent(self):
        make_home(self.a, {"a": ["甲"]})
        make_home(self.b, {"b": ["乙"]})
        put_thread(self.b, "b", ["乙", "已归档"], archived=True)
        # Remove superseded old active rollout from the synthetic source fixture.
        (self.b / "sessions/2026/11/06/rollout-b.jsonl").unlink()
        a, b = self.export(self.a, "a"), self.export(self.b, "b")
        result = self.merge(a, b)
        with m.database(result / "state_5.sqlite") as c:
            self.assertEqual(c.execute("SELECT archived FROM threads WHERE id='b'").fetchone()[0], 1)
        repeated = self.merge(result, b, name="repeated")
        self.assertEqual(self.read_ids(repeated), {"a", "b"})
        report = m.read_json(repeated.parent / "report.json")
        self.assertEqual(report["new_threads"], [])
        self.assertEqual(report["conflicts"], [])

    def test_export_reads_committed_wal(self):
        make_home(self.a, {"a": ["甲"]})
        writer = sqlite3.connect(self.a / "state_5.sqlite")
        self.addCleanup(writer.close)
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("UPDATE threads SET title='WAL 中已提交标题' WHERE id='a'")
        writer.commit()
        a = self.export(self.a, "a")
        with m.database(a / "state_5.sqlite") as c:
            self.assertEqual(c.execute("SELECT title FROM threads WHERE id='a'").fetchone()[0], "WAL 中已提交标题")
        self.assertFalse((a / "state_5.sqlite-wal").exists())

    def remove_rollout(self, home, tid):
        with m.database(home / "state_5.sqlite", True) as c:
            row = c.execute("SELECT rollout_path FROM threads WHERE id=?", (tid,)).fetchone()
            Path(row[0]).unlink()
            c.execute("UPDATE threads SET history_mode='paginated' WHERE id=?", (tid,))

    def test_database_only_unique_chat_is_imported(self):
        make_home(self.a, {"a": ["甲"]})
        make_home(self.b, {"b": ["历史消息"]})
        self.remove_rollout(self.b, "b")
        result = self.merge(self.export(self.a, "a"), self.export(self.b, "b"))
        self.assertEqual(self.read_ids(result), {"a", "b"})
        with m.database(result / "thread_history_1.sqlite") as c:
            body = c.execute("SELECT item_json FROM thread_items WHERE thread_id='b'").fetchone()[0]
            self.assertEqual(json.loads(body)["text"], "历史消息")
        report = m.read_json(result.parent / "report.json")
        self.assertEqual(report["database_only_threads"], ["b"])
        self.assertEqual(report["index_only_threads"], [])

    def test_database_only_extension_and_fork(self):
        make_home(self.a, {"shared": ["共同"]})
        make_home(self.b, {"shared": ["共同", "新增"]})
        self.remove_rollout(self.a, "shared")
        self.remove_rollout(self.b, "shared")
        # In the fixture each earlier turn end offset points to the whole log.
        # Use NULL (valid for these columns) so prior completed turns stay unchanged.
        for home in (self.a, self.b):
            with m.database(home / "thread_history_1.sqlite", True) as c:
                c.execute("UPDATE thread_turns SET rollout_end_byte_offset=NULL")
        result = self.merge(self.export(self.a, "a"), self.export(self.b, "b"))
        with m.database(result / "thread_history_1.sqlite") as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM thread_items").fetchone()[0], 2)
        with m.database(self.b / "thread_history_1.sqlite", True) as c:
            c.execute("UPDATE thread_items SET item_json='{}' WHERE item_id='item-0'")
        with self.assertRaises(m.MergeError):
            self.merge(self.export(self.a, "a2"), self.export(self.b, "b2"), name="fork")

    def test_database_only_winner_removes_obsolete_raw_file(self):
        make_home(self.a, {"shared": ["共同"]})
        make_home(self.b, {"shared": ["共同", "新增"]})
        self.remove_rollout(self.b, "shared")
        for home in (self.a, self.b):
            with m.database(home / "thread_history_1.sqlite", True) as c:
                c.execute("UPDATE thread_turns SET rollout_end_byte_offset=NULL")
        result = self.merge(self.export(self.a, "a"), self.export(self.b, "b"))
        self.assertFalse((result / "sessions/2026/11/06/rollout-shared.jsonl").exists())
        receipt = m.apply_result(result, self.a, self.root / "undo", quiet, _check_closed=False)
        self.assertFalse((self.a / "sessions/2026/11/06/rollout-shared.jsonl").exists())
        m.rollback(receipt, quiet, _check_closed=False)
        self.assertTrue((self.a / "sessions/2026/11/06/rollout-shared.jsonl").exists())

    def test_windows_extended_path(self):
        row = {"rollout_path": "\\\\?\\C:\\Users\\Autumn\\.codex\\sessions\\test.jsonl"}
        manifest = {"source_home": r"C:\Users\Autumn\.codex"}
        self.assertEqual(m.rollout_relative(row, manifest), "sessions/test.jsonl")
        row = {"rollout_path": "\\\\?\\UNC\\server\\share\\.codex\\sessions\\test.jsonl"}
        self.assertEqual(m.rollout_relative(row, {"source_home": r"\\server\share\.codex"}), "sessions/test.jsonl")

    def test_rollback_rejects_edits_to_previously_unchanged_rollout(self):
        make_home(self.a, {"a": ["甲"]})
        make_home(self.b, {"b": ["乙"]})
        result = self.merge(self.export(self.a, "a"), self.export(self.b, "b"))
        receipt = m.apply_result(result, self.a, self.root / "undo", quiet, _check_closed=False)
        p = self.a / "sessions/2026/11/06/rollout-a.jsonl"
        with p.open("ab") as f:
            f.write(b'{}\n')
        with self.assertRaises(m.MergeError):
            m.rollback(receipt, quiet, _check_closed=False)

    def test_profile_lock_blocks_other_thread(self):
        import threading
        errors = []
        def other():
            try:
                with m.profile_lock(self.a):
                    pass
            except m.MergeError as e:
                errors.append(str(e))
        with m.profile_lock(self.a):
            with m.profile_lock(self.a):
                pass
            t = threading.Thread(target=other)
            t.start()
            t.join()
        self.assertEqual(len(errors), 1)

    def test_apply_and_rollback_with_committed_crash_wal(self):
        make_home(self.a, {"a": ["甲"]})
        make_home(self.b, {"b": ["乙"]})
        # A crashed process leaves a committed WAL with no live SQLite connection.
        subprocess.run([sys.executable, "-c",
            "import sqlite3,sys,os; c=sqlite3.connect(sys.argv[1]); "
            "c.execute('PRAGMA journal_mode=WAL'); c.execute('PRAGMA wal_autocheckpoint=0'); "
            "c.execute(\"UPDATE threads SET title='crash-wal-title' WHERE id='a'\"); "
            "c.commit(); os._exit(0)", str(self.a / "state_5.sqlite")], check=True)
        self.assertGreater((self.a / "state_5.sqlite-wal").stat().st_size, 0)
        result = self.merge(self.export(self.a, "a"), self.export(self.b, "b"))
        receipt = m.apply_result(result, self.a, self.root / "undo", quiet, _check_closed=False)
        self.assertFalse((self.a / "state_5.sqlite-wal").exists())
        m.rollback(receipt, quiet, _check_closed=False)
        with m.database(self.a / "state_5.sqlite") as c:
            self.assertEqual(c.execute("SELECT title FROM threads WHERE id='a'").fetchone()[0], "crash-wal-title")
        self.assertEqual(self.read_ids(self.a), {"a"})

    def test_running_app_blocks_export_before_output_created(self):
        if sys.platform != "win32":
            self.skipTest("Windows process names")
        make_home(self.a, {"a": ["甲"]})
        result = subprocess.CompletedProcess([], 0, b'"ChatGPT.exe","123","Console","1","1 K"\r\n', b"")
        with mock.patch.object(m.subprocess, "run", return_value=result):
            with self.assertRaises(m.MergeError):
                m.export_snapshot(self.a, self.root / "blocked", quiet)
        self.assertFalse((self.root / "blocked").exists())


if __name__ == "__main__":
    unittest.main()
