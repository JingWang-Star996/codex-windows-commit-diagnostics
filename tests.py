"""Windows-only fixtures. All directory changes stay inside fresh temporary roots."""
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("diagnose", Path(__file__).with_name("diagnose.py"))
d = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = d
spec.loader.exec_module(d)


@unittest.skipUnless(sys.platform == "win32", "Windows integration suite")
class LayoutTests(unittest.TestCase):
    def setUp(self):
        import _winapi
        self.junction = _winapi.CreateJunction
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.p = self.root / "roam" / "Codex"
        self.q = self.root / "local" / "Packages" / d.FAMILY / "LocalCache" / "Roaming" / "Codex"
        self.r = self.q.with_name("retained")
        self.p.parent.mkdir()
        self.q.parent.mkdir(parents=True)

    def state(self):
        return d.inspect_layout(self.p, self.q)

    def fixture_folder(self, folder_id):
        return self.root / {d.ROAMING: "roam", d.LOCAL: "local"}[folder_id]

    def legacy(self):
        self.p.mkdir()
        self.junction(str(self.p), str(self.q))

    def test_original(self):
        self.legacy()
        self.assertEqual(self.state(), "match")

    def test_cli_plan_is_repeatable_and_read_only(self):
        self.legacy()
        sentinel = self.p / "fixture.txt"
        sentinel.write_bytes(b"original")
        identity = self.p.stat().st_ino
        outputs = []
        with patch.object(d, "registered_family", return_value=[d.FAMILY]), \
                patch.object(d, "known_folder", side_effect=self.fixture_folder):
            for _ in range(2):
                stream = io.StringIO()
                with redirect_stdout(stream):
                    self.assertEqual(d.main(["--plan"]), 0)
                outputs.append(json.loads(stream.getvalue()))
        self.assertEqual(outputs[0], outputs[1])
        self.assertEqual(outputs[0]["layout"], "match")
        self.assertEqual(outputs[0]["mode"], "plan")
        self.assertEqual(sentinel.read_bytes(), b"original")
        self.assertEqual(identity, self.p.stat().st_ino)
        self.assertEqual(list(self.q.parent.iterdir()), [self.q])

    def test_normal_cli_on_fixture(self):
        self.q.mkdir()
        stream = io.StringIO()
        with patch.object(d, "registered_family", return_value=[d.FAMILY]), \
                patch.object(d, "known_folder", side_effect=self.fixture_folder), \
                redirect_stdout(stream):
            self.assertEqual(d.main([]), 0)
        result = json.loads(stream.getvalue())
        self.assertEqual(result["layout"], "native")
        self.assertEqual(result["mode"], "inspect")

    def test_normal_private_directory(self):
        self.q.mkdir()
        self.assertEqual(self.state(), "native")
        self.assertNotIn("next", d.report(self.state(), True))

    def test_two_real_directories(self):
        self.p.mkdir()
        self.q.mkdir()
        self.assertEqual(self.state(), "indeterminate")

    def test_other_target(self):
        self.p.mkdir()
        other = self.root / "unrelated"
        other.mkdir()
        self.junction(str(other), str(self.q))
        self.assertEqual(self.state(), "indeterminate")

    def test_dangling_private_junction(self):
        self.legacy()
        self.p.rmdir()
        self.assertEqual(self.state(), "indeterminate")

    def test_wrong_public_type(self):
        self.p.write_bytes(b"file")
        self.assertEqual(self.state(), "indeterminate")

    def test_redirected_ancestor(self):
        displaced = self.root / "displaced"
        self.q.parent.rename(displaced)
        self.junction(str(displaced), str(self.q.parent))
        self.legacy()
        self.assertEqual(self.state(), "indeterminate")

    def test_reverse_junction(self):
        self.q.mkdir()
        self.junction(str(self.q), str(self.p))
        self.assertEqual(self.state(), "indeterminate")

    def test_partial_and_rollback(self):
        self.legacy()
        self.q.rename(self.r)
        self.assertEqual(self.state(), "indeterminate")
        self.r.rename(self.q)
        self.assertEqual(self.state(), "match")

    def test_symlink_or_unknown_reparse_tag_blocks_plan(self):
        self.q.mkdir()
        api = d.WindowsMetadata()
        private_id = d.inspect_path(api, self.q)[2][-1][0]
        real_info = d.info
        for tag in (0xA000000C, 0x9000001A):
            def synthetic_tag(api, handle, kind, length):
                if kind == 9 and real_info(api, handle, 18, 24) == private_id:
                    return d.struct.pack("<II", d.REPARSE | d.DIRECTORY, tag)
                return real_info(api, handle, kind, length)
            with self.subTest(tag=tag), patch.object(d, "info", side_effect=synthetic_tag):
                self.assertEqual(self.state(), "indeterminate")

    def test_drive_mappings_are_rejected_before_open(self):
        class MappedDrive:
            def device(self, name, target, length):
                target.value = r"\Device\Mup\example.invalid\share"
                return len(target.value)
        with patch.object(d, "WindowsMetadata", return_value=MappedDrive()):
            self.assertEqual(self.state(), "indeterminate")

    def test_changed_snapshot(self):
        self.legacy()
        query = d.inspect_path
        observations = []
        def changed(api, path):
            result = query(api, path)
            observations.append(path)
            if len(observations) == 4:
                return result[0], result[1], result[2][:-1]
            return result
        with patch.object(d, "inspect_path", side_effect=changed):
            self.assertEqual(self.state(), "indeterminate")

    def test_hostname_device_and_dotdot_are_rejected_before_io(self):
        for path in (Path(r"\\example.invalid\share\Codex"), Path(r"\\?\X:\Codex"),
                     self.root / ".." / "Codex"):
            with self.subTest(path=path), patch.object(d, "WindowsMetadata",
                                                       side_effect=AssertionError):
                self.assertEqual(d.inspect_layout(path, self.q), "indeterminate")

    def test_recreated_public_after_move(self):
        self.legacy()
        self.q.rename(self.r)
        self.p.rename(self.q)
        self.p.mkdir()
        (self.p / "new").write_bytes(b"new")
        self.assertEqual(self.state(), "indeterminate")
        self.assertEqual((self.p / "new").read_bytes(), b"new")
        self.assertTrue(self.q.is_dir())

    def test_full_manual_sequence_and_new_writes(self):
        self.legacy()
        data = self.p / "fixture"
        data.write_bytes(b"before")
        original_id = self.p.stat().st_ino
        self.q.rename(self.r)
        self.p.rename(self.q)
        self.assertEqual(self.state(), "native")
        self.assertEqual(self.q.stat().st_ino, original_id)
        (self.q / "fixture").write_bytes(b"after")
        self.q.rename(self.p)
        self.r.rename(self.q)
        self.assertEqual(self.state(), "match")
        self.assertEqual(data.read_bytes(), b"after")
        self.assertEqual(self.p.stat().st_ino, original_id)

    def test_non_target_identity_skips_profile_lookup(self):
        def forbidden(*unused):
            self.fail("unregistered or ambiguous app caused a filesystem probe")
        for packages in ([], ["Other.Codex"], [d.FAMILY, d.FAMILY]):
            with self.subTest(packages=packages):
                with patch.object(d, "registered_family", return_value=packages), \
                        patch.object(d, "known_folder", side_effect=forbidden):
                    self.assertEqual(d.observe(), "indeterminate")

    def test_fixed_package_role_paths(self):
        seen = []
        def capture(public, private):
            seen.extend([public, private])
            return "native"
        with patch.object(d, "registered_family", return_value=[d.FAMILY]), \
                patch.object(d, "known_folder", side_effect=self.fixture_folder), \
                patch.object(d, "inspect_layout", side_effect=capture):
            self.assertEqual(d.observe(), "native")
        self.assertEqual(seen, [self.root / "roam" / "Codex",
                                self.root / "local" / "Packages" / d.FAMILY /
                                "LocalCache" / "Roaming" / "Codex"])

    def test_undetermined_cli_hides_paths_and_plan(self):
        with patch.object(d, "observe", side_effect=PermissionError(str(self.root))):
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(d.main(["--plan"]), 2)
        payload = json.loads(output.getvalue())
        self.assertNotIn(str(self.root), output.getvalue())
        self.assertNotIn("next", payload)


if __name__ == "__main__":
    unittest.main(verbosity=2)
