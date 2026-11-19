"""Immutable release staging contracts; only disposable local fixtures."""
import hashlib
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

HERE = Path(__file__).parent


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.make_writable)
        self.dest = self.root / 'install'
        self.reference = self.root / 'current-chatgpt_pm.py'
        shutil.copyfile(HERE / 'chatgpt_pm.py', self.reference)

    def make_writable(self):
        for path in self.root.rglob('*'):
            path.chmod(0o700 if path.is_dir() else 0o600)

    def invoke(self, args, script=None):
        result = subprocess.run(['bash', str(script or HERE / 'install-claude-primary.sh'), *args],
                                text=True, capture_output=True)
        for name in set(re.findall(r'^log=(/tmp/aisis-claude-install-\S+)', result.stderr, re.M)):
            Path(name).unlink(missing_ok=True)  # only paths emitted by this exact owned invocation
        return result

    def test_release_is_immutable_exact_and_stdout_contains_only_its_path(self):
        before = self.reference.read_bytes()
        result = self.invoke(['claude-primary-fixture', str(self.dest), str(self.reference)])
        self.assertEqual(result.returncode, 0, result.stderr)
        release = self.dest / 'releases' / 'claude-primary-fixture'
        self.assertEqual(result.stdout, str(release) + '\n')
        self.assertEqual(self.reference.read_bytes(), before)
        self.assertEqual(release.stat().st_mode & 0o777, 0o500)
        for line in (release / 'MANIFEST').read_text().splitlines():
            expected, name = line.split('  ', 1)
            path = release / name
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), expected)
            self.assertEqual(path.stat().st_mode & 0o777, 0o400)

    def test_invalid_reference_or_arguments_refuse_without_installing(self):
        bad = self.root / 'mismatch.py'
        bad.write_text('different fallback client\n')
        cases = [
            ['claude-primary-fixture', str(self.dest)],
            ['claude-primary-fixture', str(self.dest), 'relative.py'],
            ['claude-primary-fixture', str(self.dest), str(bad)],
            ['claude-primary-fixture', str(self.dest), str(self.reference), 'extra'],
        ]
        for args in cases:
            with self.subTest(args=args):
                result = self.invoke(args)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.dest.exists())

    def test_failed_copy_removes_owned_staging_and_preserves_current_client(self):
        broken = self.root / 'incomplete-source'
        broken.mkdir()
        for name in ('install-claude-primary.sh', 'claude_primary.py', 'chatgpt_pm.py'):
            shutil.copyfile(HERE / name, broken / name)
        before = self.reference.read_bytes()
        result = self.invoke(['claude-primary-fixture', str(self.dest), str(self.reference)],
                             broken / 'install-claude-primary.sh')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.reference.read_bytes(), before)
        self.assertFalse((self.dest / 'releases' / 'claude-primary-fixture').exists())
        self.assertEqual(list((self.dest / 'releases').iterdir()), [])


if __name__ == '__main__':
    unittest.main()
