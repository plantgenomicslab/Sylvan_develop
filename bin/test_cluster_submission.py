"""Exercise scheduler rejection and empty-account handling without submitting jobs."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest

BIN=Path(__file__).resolve().parent

class ClusterSubmission(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        fake=self.root/'fake';fake.mkdir();self.record=self.root/'submitted.json'
        for name,body in [('sinfo','print("cpu|1-00:00:00")'),
                          ('sbatch','import os,json,sys; open(os.environ["SUBMISSION_RECORD"],"w").write(json.dumps(sys.argv[1:])); print("12345")')]:
            p=fake/name;p.write_text('#!'+sys.executable+'\n'+body+'\n');p.chmod(0o755)
        self.env=dict(os.environ,PATH=str(fake)+os.pathsep+os.environ['PATH'],SUBMISSION_RECORD=str(self.record))
        self.env.pop('SYLVAN_RUN_NAME',None)

    def tearDown(self):self.temp.cleanup()

    def call(self,limit):
        return subprocess.run([sys.executable,str(BIN/'cluster_submit.py'),'1','8g','4','stage','','cpu',limit,'out','err','job.sh'],env=self.env,text=True,capture_output=True)

    def test_excess_partition_time_fails_before_sbatch(self):
        p=self.call('2-00:00:00')
        self.assertNotEqual(p.returncode,0)
        self.assertFalse(self.record.exists())
        self.assertIn('PartitionTimeLimit',p.stderr)

    def test_equal_partition_limit_is_preserved(self):
        p=self.call('1-00:00:00');self.assertEqual(p.returncode,0,p.stderr)
        args=json.loads(self.record.read_text());self.assertEqual(args[args.index('-t')+1],'1-00:00:00');self.assertNotIn('-A',args)

    def test_entry_script_preserves_empty_account_as_an_argument(self):
        # Execute the actual entry script with a recorder for Snakemake, then
        # execute the cluster template it passed with realistic substituted data.
        fake=self.root/'fake';p=fake/'snakemake'
        p.write_text('#!'+sys.executable+'\nimport sys,json,os\nopen(os.environ["CAPTURE_SNAKEMAKE"],"w").write(json.dumps(sys.argv[1:]))\n');p.chmod(0o755)
        (self.root/'config').mkdir();(self.root/'config/config_annotate.yml').write_text('Input: {}\n')
        (self.root/'bin').mkdir();(self.root/'bin/cluster_submit.py').write_bytes((BIN/'cluster_submit.py').read_bytes())
        capture=self.root/'snakemake.json';env=dict(self.env,CAPTURE_SNAKEMAKE=str(capture))
        proc=subprocess.run(['bash',str(BIN/'annotate.sh')],cwd=self.root,env=env,capture_output=True,text=True)
        self.assertEqual(proc.returncode,0,proc.stderr)
        args=json.loads(capture.read_text());template=args[args.index('--cluster')+1]
        cluster=SimpleNamespace(nodes=1,memory='8g',ncpus=4,name='stage',account='',partition='cpu',time='1-00:00:00',output='out',error='err',extra_args='')
        proc=subprocess.run(['bash','-c',template.format(cluster=cluster)+' job.sh'],cwd=self.root,env=env,capture_output=True,text=True)
        self.assertEqual(proc.returncode,0,proc.stderr)
        args=json.loads(self.record.read_text());self.assertNotIn('-A',args)
        self.assertEqual(args[args.index('-p')+1],'cpu')

if __name__=='__main__':unittest.main()
