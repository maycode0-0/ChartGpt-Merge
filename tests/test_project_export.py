"""Project-scoped export must exclude rows, free pages, metadata and raw files."""
import json
from pathlib import Path
import tempfile
import unittest

import codex_chat_merge as m
from test_merge import make_home, put_thread, quiet


class ProjectExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "source"
        make_home(self.home, {"a1": ["selected one"], "a2": ["selected two"]}, project="server-a")
        with m.database(self.home / "state_5.sqlite", True) as c:
            c.execute("UPDATE projects SET name='Project A' WHERE id='server-a'")
            c.execute("UPDATE project_roots SET path=? WHERE project_id='server-a'", (r"D:\Projects\a",))
            for pid, name, path in (("server-b", "UNSELECTED_PRIVATE_PROJECT", r"D:\Projects\b"),
                                    ("server-empty", "Empty Project", None)):
                m.insert(c, "projects", dict(id=pid, name=name, metadata="{}", position=1, created_at_ms=1, updated_at_ms=1))
                if path:
                    m.insert(c, "project_roots", dict(project_id=pid, position=0, path=path))
                m.insert(c, "thread_sections", dict(id="section-" + pid, name=name))
        put_thread(self.home, "b1", ["UNSELECTED_PRIVATE_MESSAGE"], project="server-b")
        put_thread(self.home, "directory-a", ["directory inferred"], project="server-a")
        put_thread(self.home, "loose", ["UNSELECTED_LOOSE_MESSAGE"], project="server-a")
        with m.database(self.home / "state_5.sqlite", True) as c:
            c.execute("UPDATE threads SET project_id=NULL")
            c.execute("UPDATE threads SET cwd=? WHERE id IN ('a1','b1')", (r"D:\Projects\b",))
            c.execute("UPDATE threads SET cwd=? WHERE id IN ('a2','directory-a')", (r"D:\Projects\a\sub",))
            c.execute("UPDATE threads SET cwd=? WHERE id='loose'", (r"E:\Other",))
            c.execute("UPDATE threads SET archived=1 WHERE id='a2'")
            c.execute("UPDATE threads SET attached_thread_parent_id='b1' WHERE id='a1'")
            m.insert(c, "thread_spawn_edges", dict(parent_thread_id="b1", child_thread_id="a1", status="completed"))
            m.insert(c, "thread_spawn_edges", dict(parent_thread_id="a1", child_thread_id="a2", status="completed"))
            m.insert(c, "thread_attachments", dict(id="attachment-private", thread_id="b1", attachment_type="file",
                     identity_key="private", payload='{"data":"UNSELECTED_PRIVATE_ATTACHMENT"}', created_at=1))
        with m.database(self.home / "sqlite/codex.db", True) as c:
            for tid in ("b1", "directory-a", "loose"):
                m.insert(c, "local_thread_catalog", dict(host_id="local-a", thread_id=tid, display_title=tid,
                         source_created_at=1, source_updated_at=2, cwd=r"D:\Projects\b", source_kind="local", observation_sequence=1))
            c.execute("UPDATE automations SET prompt='UNSELECTED_AUTOMATION_SECRET'")
        m.write_json(self.home / m.GLOBAL, {
            "local-projects": {
                "legacy-a": {"id": "legacy-a", "name": "Project A", "rootPaths": [r"D:\Projects\a"]},
                "legacy-b": {"id": "legacy-b", "name": "UNSELECTED_PRIVATE_PROJECT", "rootPaths": [r"D:\Projects\b"]},
                "legacy-empty": {"id": "legacy-empty", "name": "Empty Project", "rootPaths": []}},
            "project-order": ["legacy-a", "legacy-b", "legacy-empty"],
            "app-server-project-id-by-legacy-project-id-by-host": {"local-a": {
                "legacy-a": "server-a", "legacy-b": "server-b", "legacy-empty": "server-empty"}},
            "thread-project-assignments": {"a1": {"projectKind": "local", "projectId": "legacy-a"},
                                           "a2": {"projectKind": "local", "projectId": "legacy-a"},
                                           "b1": {"projectKind": "local", "projectId": "legacy-b"}},
            "sidebar-project-thread-orders": {"local:legacy-a": {"threadIds": ["a1", "a2"]},
                                              "local:legacy-b": {"threadIds": ["b1"]}},
            "project-appearances": {"legacy-a": {"color": "green"}, "legacy-b": {"label": "UNSELECTED_PRIVATE_APPEARANCE"}},
            "thread-workspace-root-hints": {"a1": r"D:\Projects\a", "b1": "UNSELECTED_PRIVATE_ROOT"},
            "projectless-thread-ids": ["loose"], "private-machine-setting": "UNSELECTED_MACHINE_SETTING"})
        orphan = self.home / "sessions/orphan.jsonl"
        orphan.write_text('UNSELECTED_UNINDEXED_LOG\n', encoding="utf-8")

    def export(self, name="selected", **kwargs):
        return m.export_snapshot(self.home, self.root / name, quiet, _check_closed=False, **kwargs)

    def ids(self, output):
        with m.database(output / "state_5.sqlite") as c:
            return {r[0] for r in c.execute("SELECT id FROM threads")}

    def test_classification_bridges_explicit_membership_and_directory(self):
        catalog = {p["id"]: p for p in m.project_catalog(self.home)}
        a = catalog["project:server-a"]
        self.assertEqual({t["id"] for t in a["threads"]}, {"a1", "a2", "directory-a"})
        self.assertEqual(a["legacy_ids"], ["legacy-a"])
        self.assertEqual({t["id"] for t in catalog["unassigned"]["threads"]}, {"loose"})
        self.assertEqual(catalog["project:server-empty"]["count"], 0)
        self.assertEqual(sum(p["count"] for p in catalog.values()), 5)

    def test_project_export_contains_no_unselected_data(self):
        output = self.export(project_ids=["legacy-a"])
        self.assertEqual(self.ids(output), {"a1", "a2", "directory-a"})
        m.load_snapshot(output)
        for path in output.rglob("*"):
            if path.is_file():
                self.assertNotIn(b"UNSELECTED_", path.read_bytes(), path.name)
        self.assertFalse((output / "sessions/2026/11/06/rollout-b1.jsonl").exists())
        self.assertFalse((output / "sessions/orphan.jsonl").exists())
        with m.database(output / "state_5.sqlite") as c:
            self.assertEqual({r[0] for r in c.execute("SELECT id FROM projects")}, {"server-a"})
            self.assertEqual(c.execute("SELECT attached_thread_parent_id FROM threads WHERE id='a1'").fetchone()[0], None)
            self.assertEqual({tuple(r)[:2] for r in c.execute("SELECT * FROM thread_spawn_edges")}, {("a1", "a2")})
            self.assertEqual(c.execute("SELECT COUNT(*) FROM thread_attachments").fetchone()[0], 0)
        with m.database(output / "thread_history_1.sqlite") as c:
            self.assertEqual({r[0] for r in c.execute("SELECT DISTINCT thread_id FROM thread_items")}, {"a1", "a2", "directory-a"})
        with m.database(output / "sqlite/codex.db") as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM automations").fetchone()[0], 0)
            self.assertEqual({r[0] for r in c.execute("SELECT thread_id FROM local_thread_catalog")}, {"a1", "a2", "directory-a"})
        g = m.read_json(output / m.GLOBAL)
        self.assertEqual(set(g["local-projects"]), {"legacy-a"})
        self.assertEqual(g["project-order"], ["legacy-a"])
        self.assertEqual(g["app-server-project-id-by-legacy-project-id-by-host"], {"local-a": {"legacy-a": "server-a"}})
        self.assertEqual(set(g["sidebar-project-thread-orders"]), {"local:legacy-a"})

    def test_single_chat_and_project_union(self):
        single = self.export("one", thread_ids=["a1"])
        self.assertEqual(self.ids(single), {"a1"})
        mixed = self.export("mixed", project_ids=["Project A"], thread_ids=["b1"])
        self.assertEqual(self.ids(mixed), {"a1", "a2", "directory-a", "b1"})
        listing = m.read_json(mixed / m.PROJECT_INDEX)
        self.assertEqual({p["id"] for p in listing["projects"]}, {"project:server-a", "project:server-b"})

    def test_empty_project_is_kept_without_any_chat(self):
        output = self.export(project_ids=["legacy-empty"])
        self.assertEqual(self.ids(output), set())
        with m.database(output / "state_5.sqlite") as c:
            self.assertEqual({r[0] for r in c.execute("SELECT id FROM projects")}, {"server-empty"})
        self.assertEqual(m.read_json(output / m.PROJECT_INDEX)["projects"][0]["count"], 0)

    def test_empty_and_unknown_selection_never_become_full_export(self):
        for number, args in enumerate(({"thread_ids": []}, {"project_ids": []}, {"project_ids": ["missing"]}, {"thread_ids": ["missing"]})):
            with self.assertRaises(m.MergeError):
                self.export("invalid" + str(number), **args)
            self.assertFalse((self.root / ("invalid" + str(number)) / "manifest.json").exists())

    def test_deleted_private_rows_do_not_leak_in_fresh_database(self):
        with m.database(self.home / "thread_history_1.sqlite", True) as c:
            c.execute("PRAGMA secure_delete=OFF")
            m.insert(c, "thread_items", dict(thread_id="deleted", turn_id="t", item_id="i", rollout_ordinal=1,
                     created_at_ms=1, item_json="DELETED_PRIVATE_MARKER" * 300))
            c.execute("DELETE FROM thread_items WHERE thread_id='deleted'")
        self.assertIn(b"DELETED_PRIVATE_MARKER", (self.home / "thread_history_1.sqlite").read_bytes())
        output = self.export(project_ids=["legacy-a"])
        self.assertNotIn(b"DELETED_PRIVATE_MARKER", (output / "thread_history_1.sqlite").read_bytes())

    def test_database_only_selected_chat_still_exported(self):
        with m.database(self.home / "state_5.sqlite", True) as c:
            raw = c.execute("SELECT rollout_path FROM threads WHERE id='a2'").fetchone()[0]
            Path(raw).unlink()
            c.execute("UPDATE threads SET history_mode='paginated' WHERE id='a2'")
        output = self.export(thread_ids=["a2"])
        self.assertEqual(self.ids(output), {"a2"})
        with m.database(output / "thread_history_1.sqlite") as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM thread_items").fetchone()[0], 1)

    def test_selected_incoming_merges_without_deleting_local_projects(self):
        incoming = self.export(project_ids=["legacy-a"])
        target = self.root / "target"
        make_home(target, {"target-only": ["must remain"]}, project="target-project")
        base = m.export_snapshot(target, self.root / "base", quiet, _check_closed=False)
        merged = m.merge_snapshots(base, incoming, self.root / "merge", target, log=quiet)
        self.assertEqual(self.ids(merged), {"target-only", "a1", "a2", "directory-a"})
        receipt = m.apply_result(merged, target, self.root / "rollback", quiet, _check_closed=False)
        self.assertEqual(self.ids(target), {"target-only", "a1", "a2", "directory-a"})
        self.assertFalse((target / m.PROJECT_INDEX).exists())  # Catalog is backup metadata, not live app data.
        m.rollback(receipt, quiet, _check_closed=False)
        self.assertEqual(self.ids(target), {"target-only"})

    def test_partial_base_is_rejected(self):
        partial = self.export("partial", project_ids=["legacy-a"])
        incoming = self.export("full")
        with self.assertRaises(m.MergeError):
            m.merge_snapshots(partial, incoming, self.root / "merge", log=quiet)

    def test_duplicate_project_names_require_unique_identifier(self):
        g = m.read_json(self.home / m.GLOBAL)
        g["local-projects"]["legacy-b"]["name"] = "Project A"
        m.write_json(self.home / m.GLOBAL, g)
        with self.assertRaises(m.MergeError):
            self.export(project_ids=["Project A"])
        output = self.export("unique", project_ids=["project:server-a"])
        self.assertEqual(self.ids(output), {"a1", "a2", "directory-a"})

    def test_longest_root_wins_and_explicit_unassigned_is_not_inferred(self):
        g = m.read_json(self.home / m.GLOBAL)
        g["local-projects"]["legacy-nested"] = {"id": "legacy-nested", "name": "Nested", "rootPaths": [r"D:\Projects\a\sub"]}
        g["projectless-thread-ids"].append("a2")
        del g["thread-project-assignments"]["a2"]
        m.write_json(self.home / m.GLOBAL, g)
        groups = {p["id"]: p for p in m.project_catalog(self.home)}
        self.assertEqual({t["id"] for t in groups["legacy:legacy-nested"]["threads"]}, {"directory-a"})
        self.assertEqual({t["id"] for t in groups["unassigned"]["threads"]}, {"loose", "a2"})


if __name__ == "__main__":
    unittest.main()
