"""Exercise scheduler rejection and empty-account handling without submitting jobs."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from snakemake.utils import format as snakemake_format
import unittest

BIN=Path(__file__).resolve().parent

class ClusterSubmission(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        fake=self.root/'fake';fake.mkdir();(fake/'python3').symlink_to(sys.executable);self.record=self.root/'submitted.json'
        for name,body in [('sinfo','print("cpu|1-00:00:00")'),
                          ('sbatch','import os,json,sys; open(os.environ["SUBMISSION_RECORD"],"w").write(json.dumps(sys.argv[1:])); print("12345")')]:
            p=fake/name;p.write_text('#!'+sys.executable+'\n'+body+'\n');p.chmod(0o755)
        self.env=dict(os.environ,PATH=str(fake)+os.pathsep+os.environ['PATH'],SUBMISSION_RECORD=str(self.record))
        self.env.pop('SYLVAN_RUN_NAME',None)

    def tearDown(self):self.temp.cleanup()

    def call(self,limit,*extra):
        return subprocess.run([sys.executable,str(BIN/'cluster_submit.py'),'1','8g','4','stage','','cpu',limit,'out','err',*extra,'job.sh'],env=self.env,text=True,capture_output=True)

    def test_excess_partition_time_fails_before_sbatch(self):
        p=self.call('2-00:00:00')
        self.assertNotEqual(p.returncode,0)
        self.assertFalse(self.record.exists())
        self.assertIn('PartitionTimeLimit',p.stderr)

    def test_equal_partition_limit_is_preserved(self):
        p=self.call('1-00:00:00');self.assertEqual(p.returncode,0,p.stderr)
        args=json.loads(self.record.read_text());self.assertEqual(args[args.index('-t')+1],'1-00:00:00');self.assertNotIn('-A',args)

    def test_attached_override_is_checked_in_both_directions(self):
        proc = self.call('2-00:00:00', '-t1-00:00:00')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.record.unlink()
        proc = self.call('1-00:00:00', '-t2-00:00:00')
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertFalse(self.record.exists())

    def test_invalid_overrides_fail_before_sbatch(self):
        for token in ['0x3C', '1_440', '72h']:
            with self.subTest(token=token):
                proc = self.call('60', '--time=' + token)
                self.assertEqual(proc.returncode, 2, proc.stderr)
                self.assertFalse(self.record.exists())
                self.assertIn('Invalid Slurm time', proc.stderr)

    def test_serialized_and_legacy_extra_values_stay_intact(self):
        for extra in [('--comment', 'a b'), ('--extra-args-string', '--comment "a b"')]:
            proc = self.call('60', *extra)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            args = json.loads(self.record.read_text())
            self.assertEqual(args[args.index('--comment') + 1], 'a b')
            self.assertEqual(args[-1], 'job.sh')
        proc = self.call('60', '--comment', '')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        args = json.loads(self.record.read_text())
        self.assertEqual(args[args.index('--comment') + 1], '')

    def test_entry_script_preserves_empty_account_as_an_argument(self):
        # Execute the actual entry script with a recorder for Snakemake, then
        # execute the cluster template it passed with realistic substituted data.
        fake=self.root/'fake';p=fake/'snakemake'
        p.write_text('#!'+sys.executable+'\nimport sys,json,os\nopen(os.environ["CAPTURE_SNAKEMAKE"],"w").write(json.dumps(sys.argv[1:]))\n');p.chmod(0o755)
        (self.root/'config').mkdir();(self.root/'config/config_annotate.yml').write_text('Input: {}\n')
        (self.root/'bin').symlink_to(BIN, target_is_directory=True)
        capture=self.root/'snakemake.json';env=dict(self.env,CAPTURE_SNAKEMAKE=str(capture))
        proc=subprocess.run(['bash',str(BIN/'annotate.sh')],cwd=self.root,env=env,capture_output=True,text=True)
        self.assertEqual(proc.returncode,0,proc.stderr)
        args=json.loads(capture.read_text());template=args[args.index('--cluster')+1]
        cluster=SimpleNamespace(nodes=1,memory='8g',ncpus=4,name='stage',account='',partition='cpu',time='1-00:00:00',output='out',error='err',extra_args='')
        proc=subprocess.run(['bash','-c',snakemake_format(template,cluster=cluster)+' job.sh'],cwd=self.root,env=env,capture_output=True,text=True)
        self.assertEqual(proc.returncode,0,proc.stderr)
        args=json.loads(self.record.read_text());self.assertNotIn('-A',args)
        self.assertEqual(args[args.index('-p')+1],'cpu')

if __name__=='__main__':unittest.main()
