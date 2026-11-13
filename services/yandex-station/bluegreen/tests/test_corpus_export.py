"""The corpus transport must materialize allowlisted regular files, including vendor hardlinks."""
from pathlib import Path
import importlib.util
import os
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

EXPORTER = Path(__file__).resolve().parents[1] / "prepare_corpus_bundle.py"
spec = importlib.util.spec_from_file_location("corpus_export_under_test", EXPORTER)
exporter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(exporter)


class CorpusArchiveTests(unittest.TestCase):
    def test_real_vendor_hardlinks_remain_regular_and_declared_symlinks_remain_links(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            vendor = root / "vendor"
            vendor.mkdir()
            (vendor / "a.d.cts").write_bytes(b"export interface Example {}\n")
            os.link(vendor / "a.d.cts", vendor / "a.d.mts")
            (vendor / "entry").symlink_to("a.d.cts")
            archive = root / "rootfs.tar.gz"
            actual_run = subprocess.run

            def container_tar(command, **kwargs):
                self.assertEqual(command[:5], ["docker", "exec", "-i", "candidate", "tar"])
                command = ["tar", *command[5:]]
                index = command.index("-C") + 1
                self.assertEqual(command[index], "/")
                command[index] = str(root)
                return actual_run(command, **kwargs)

            with patch.object(exporter.subprocess, "run", side_effect=container_tar):
                exporter.export_archive(
                    "candidate", ["/vendor/a.d.cts", "/vendor/a.d.mts", "/vendor/entry"], archive
                )
            with tarfile.open(archive, "r:gz") as packed:
                members = {member.name: member for member in packed.getmembers()}
                self.assertEqual(set(members), {"vendor/a.d.cts", "vendor/a.d.mts", "vendor/entry"})
                for name in ["vendor/a.d.cts", "vendor/a.d.mts"]:
                    self.assertTrue(members[name].isfile(), "declared regular path must contain bytes")
                    self.assertEqual(packed.extractfile(members[name]).read(), b"export interface Example {}\n")
                    self.assertEqual((members[name].mtime, members[name].uid, members[name].gid), (0, 0, 0))
                self.assertTrue(members["vendor/entry"].issym())
                self.assertEqual(members["vendor/entry"].linkname, "a.d.cts")


if __name__ == "__main__":
    unittest.main()
