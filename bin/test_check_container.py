"""Exercise bin/check_container.sh against a fake `singularity` (no image needed)."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

BIN = Path(__file__).resolve().parent
SCRIPT = BIN / 'check_container.sh'

FAKE_SINGULARITY = '''import json, os, sys
open(os.environ["FAKE_RECORD"], "w").write(json.dumps(sys.argv[1:]))
if os.environ.get("FAKE_ERROR"):
    sys.stderr.write(os.environ["FAKE_ERROR"] + "\\n")
sys.exit(int(os.environ.get("FAKE_RC", "0")))
'''


class CheckContainer(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        fake = self.root / 'fake'
        fake.mkdir()
        singularity = fake / 'singularity'
        singularity.write_text('#!' + sys.executable + '\n' + FAKE_SINGULARITY)
        singularity.chmod(0o755)
        self.record = self.root / 'argv.json'
        self.sif = self.root / 'sylvan.sif'
        self.sif.write_bytes(b'')
        self.env = dict(os.environ, PATH=str(fake) + os.pathsep + os.environ['PATH'],
                        FAKE_RECORD=str(self.record))
        self.env.pop('SYLVAN_SKIP_CONTAINER_CHECK', None)

    def tearDown(self):
        self.temp.cleanup()

    def run_check(self, *extra, **env):
        return subprocess.run(['bash', str(SCRIPT), str(self.sif), *extra], cwd=self.root,
                              env=dict(self.env, **env), text=True, capture_output=True)

    def test_passes_when_helixer_env_imports(self):
        p = self.run_check('--nv', '-B', '/data')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('check_container: OK', p.stderr)
        argv = json.loads(self.record.read_text())
        self.assertEqual(argv[:3], ['exec', '--home', str(self.root)])
        self.assertEqual(argv[3:6], ['--nv', '-B', '/data'])
        self.assertEqual(argv[6], str(self.sif))
        self.assertIn('import yaml, sklearn, scipy, sqlite3', argv[-1])

    def test_broken_helixer_image_fails_with_required_image(self):
        p = self.run_check(FAKE_RC='1', FAKE_ERROR="ModuleNotFoundError: No module named 'yaml'")
        self.assertEqual(p.returncode, 1)
        self.assertIn("No module named 'yaml'", p.stderr)
        self.assertIn('Sylvan container check failed', p.stderr)
        self.assertIn('singularity/Sylvan.def', p.stderr)
        self.assertIn('sha256sum', p.stderr)

    def test_missing_image_fails_before_singularity(self):
        self.sif.unlink()
        p = self.run_check()
        self.assertEqual(p.returncode, 1)
        self.assertIn('image file not found', p.stderr)
        self.assertFalse(self.record.exists())

    def test_explicit_skip_does_not_run_singularity(self):
        p = self.run_check(FAKE_RC='1', SYLVAN_SKIP_CONTAINER_CHECK='1')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('skipped', p.stderr)
        self.assertFalse(self.record.exists())


if __name__ == '__main__':
    unittest.main()
