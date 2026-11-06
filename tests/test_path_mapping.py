import json
from pathlib import Path
import tempfile
import unittest

import codex_chat_merge as core
from test_merge import make_home, quiet


class PathMappingTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.local, self.foreign = self.root / "local", self.root / "foreign"
        self.source = r"C:\Work\demo"
        self.target = r"E:\Code\demo"
        make_home(self.local, {"local": ["local message"]}, project="local-project", local_host="local-host")
        make_home(self.foreign, {"remote": ["original path: " + self.source]}, project="remote-project", local_host="remote-host")
        self.set_paths(self.local, self.target)
        self.set_paths(self.foreign, self.source)

    def set_paths(self, home, root):
        with core.database(home / "state_5.sqlite", True) as db:
            db.execute("UPDATE project_roots SET path=?", (root,))
            db.execute("UPDATE projects SET metadata=?", (json.dumps({"rootPaths": [root], "note": root}),))
            db.execute("UPDATE threads SET cwd=?, sandbox_policy=?", (root + r"\SubFolder", json.dumps({"type": "workspace-write", "writable_roots": [root], "note": root})))
        with core.database(home / "sqlite/codex.db", True) as db:
            db.execute("UPDATE local_thread_catalog SET cwd=?", (root + r"\SubFolder",))
        g = core.read_json(home / core.GLOBAL)
        for project in g["local-projects"].values():
            project["rootPaths"] = [root]
        ids = g["thread-project-assignments"]
        g["thread-workspace-root-hints"] = {tid: [root] for tid in ids}
        g["thread-writable-roots"] = {tid: [root + r"\SubFolder"] for tid in ids}
        core.write_json(home / core.GLOBAL, g)

    def export(self):
        a = core.export_snapshot(self.local, self.root / "a", quiet, _check_closed=False)
        b = core.export_snapshot(self.foreign, self.root / "b", quiet, _check_closed=False)
        return a, b

    def test_mapping_merges_project_and_rewrites_incoming_metadata_only(self):
        a, b = self.export()
        before = (b / "manifest.json").read_bytes()
        result = core.merge_snapshots(a, b, self.root / "merge", self.local, log=quiet,
                                      path_mappings={self.source: self.target})
        with core.database(result / "state_5.sqlite") as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM projects").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT path FROM project_roots").fetchone()[0], self.target)
            remote = dict(db.execute("SELECT * FROM threads WHERE id='remote'").fetchone())
            self.assertEqual(remote["project_id"], "local-project")
            self.assertEqual(remote["cwd"], self.target + r"\SubFolder")
            policy = json.loads(remote["sandbox_policy"])
            self.assertEqual(policy["writable_roots"], [self.target])
            self.assertEqual(policy["note"], self.source)
            self.assertEqual(db.execute("SELECT cwd FROM threads WHERE id='local'").fetchone()[0], self.target + r"\SubFolder")
        with core.database(result / "sqlite/codex.db") as db:
            self.assertEqual(db.execute("SELECT cwd FROM local_thread_catalog WHERE thread_id='remote'").fetchone()[0], self.target + r"\SubFolder")
        g = core.read_json(result / core.GLOBAL)
        self.assertEqual(set(g["local-projects"]), {"local-project"})
        self.assertEqual(g["thread-workspace-root-hints"]["remote"], [self.target])
        self.assertEqual(g["thread-writable-roots"]["remote"], [self.target + r"\SubFolder"])
        self.assertEqual(g["thread-project-assignments"]["remote"]["projectId"], "local-project")
        manifest = core.load_snapshot(result)
        for rel in manifest["files"]:
            if rel.endswith("rollout-remote.jsonl"):
                self.assertEqual((result / rel).read_bytes(), (b / rel).read_bytes())
        self.assertEqual((b / "manifest.json").read_bytes(), before)
        self.assertEqual(core.read_json(result.parent / "report.json")["path_mappings"], {self.source: self.target})
        self.assertEqual(core.load_snapshot(result.parent / "incoming")["source_home"], str(self.foreign))
        receipt = core.apply_result(result, self.local, self.root / "rollback", quiet, _check_closed=False)
        with core.database(self.local / "state_5.sqlite") as db:
            self.assertEqual(db.execute("SELECT cwd FROM threads WHERE id='remote'").fetchone()[0], self.target + r"\SubFolder")
        core.rollback(receipt, quiet, _check_closed=False)
        with core.database(self.local / "state_5.sqlite") as db:
            self.assertIsNone(db.execute("SELECT id FROM threads WHERE id='remote'").fetchone())

    def test_new_project_metadata_and_database_only_chat_are_mapped(self):
        with core.database(self.foreign / "state_5.sqlite", True) as db:
            db.execute("UPDATE projects SET name='Other project'")
            rollout = Path(db.execute("SELECT rollout_path FROM threads WHERE id='remote'").fetchone()[0])
            db.execute("UPDATE threads SET history_mode='paginated'")
        rollout.unlink()
        g = core.read_json(self.foreign / core.GLOBAL)
        g["local-projects"]["remote-project"]["name"] = "Other project"
        core.write_json(self.foreign / core.GLOBAL, g)
        a, b = self.export()
        result = core.merge_snapshots(a, b, self.root / "merge", self.local, log=quiet, path_mappings={self.source: self.target})
        with core.database(result / "state_5.sqlite") as db:
            metadata = json.loads(db.execute("SELECT metadata FROM projects WHERE id='remote-project'").fetchone()[0])
            self.assertEqual(metadata["rootPaths"], [self.target])
            self.assertEqual(metadata["note"], self.source)
            self.assertEqual(db.execute("SELECT cwd FROM threads WHERE id='remote'").fetchone()[0], self.target + r"\SubFolder")
        core.load_snapshot(result)

    def test_longest_prefix_case_and_directory_boundaries(self):
        mapping = core.normalize_path_mappings({r"C:\Work": r"E:\Code", r"C:\Work\demo": r"F:\Special"})
        self.assertEqual(core.mapped_project_path("c:/WORK/demo/SubFolder", mapping), r"F:\Special\SubFolder")
        self.assertEqual(core.mapped_project_path(r"C:\Workshop\file", mapping), r"C:\Workshop\file")
        self.assertEqual(core.mapped_project_path(r"C:\Work\other", mapping), r"E:\Code\other")
        self.assertEqual(core.mapped_project_path("C:/Work/demo/İmage/中文", mapping), "F:\\Special\\İmage\\中文")
        self.assertEqual(core.mapped_project_path("See C:/Work/demo", mapping), "See C:/Work/demo")
        self.assertEqual(core.mapped_project_path(r"\\?\C:\Work\demo\SubFolder", mapping), r"F:\Special\SubFolder")
        mapping = core.normalize_path_mappings({r"\\server\share\work": r"D:\local"})
        self.assertEqual(core.mapped_project_path(r"\\SERVER\share\work\Folder", mapping), r"D:\local\Folder")
        mapping = core.normalize_path_mappings({r"C:\Work": r"E:\Code", self.source: self.source})
        self.assertEqual(core.mapped_project_path(self.source + r"\Keep", mapping), self.source + r"\Keep")

    def test_invalid_or_ambiguous_rules_are_rejected_before_output(self):
        a, b = self.export()
        for mapping in ({"relative": self.target}, {self.source: "relative"}, {self.source: ""},
                        {self.source: self.target, "c:/work/demo": r"F:\other"}):
            with self.assertRaises(core.MergeError):
                core.merge_snapshots(a, b, self.root / "invalid", path_mappings=mapping, log=quiet)
            self.assertFalse((self.root / "invalid").exists())

    def test_same_project_id_from_two_directories_can_be_reconciled(self):
        # Legacy IDs on two computers may coincide even when their roots differ.
        with core.database(self.foreign / "state_5.sqlite", True) as db:
            db.execute("UPDATE projects SET id='local-project'")
            db.execute("UPDATE project_roots SET project_id='local-project'")
            db.execute("UPDATE threads SET project_id='local-project'")
        g = core.read_json(self.foreign / core.GLOBAL)
        project = g["local-projects"].pop("remote-project")
        project["id"] = "local-project"
        g["local-projects"]["local-project"] = project
        g["thread-project-assignments"]["remote"]["projectId"] = "local-project"
        core.write_json(self.foreign / core.GLOBAL, g)
        a, b = self.export()
        result = core.merge_snapshots(a, b, self.root / "merge", self.local, log=quiet, path_mappings={self.source: self.target})
        self.assertEqual(set(core.read_json(result / core.GLOBAL)["local-projects"]), {"local-project"})
