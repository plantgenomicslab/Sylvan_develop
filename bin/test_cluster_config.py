"""Resource contracts exercised without contacting a scheduler."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml

BIN = Path(__file__).resolve().parent
sys.path.insert(0, str(BIN))
import cluster_submit


class TimeAndOptions(unittest.TestCase):
    def test_slurm_units_and_unlimited(self):
        for value, expected in [('60', 3600), ('1:30', 90), ('72:00:00', 259200),
                                ('259200', 15552000), ('1-2', 93600),
                                ('1-2:30', 95400), ('1-2:30:01', 95401), ('0', None)]:
            with self.subTest(value=value):
                self.assertEqual(cluster_submit.time_seconds(value), expected)

    def test_non_slurm_numeric_lexemes_fail(self):
        for value in ['0x3C', '1_440', '+60', '-60', '1.5', '72h', '14d', '', '1:2:3:4']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                cluster_submit.time_seconds(value)

    def test_effective_options_skip_value_tokens_and_accept_attached_short(self):
        argv = ['sbatch', '-t', '14-00:00:00', '-t1-00:00:00',
                '--comment', '-t99', '--job-name', '--partition=wrong', '-pcpu7']
        self.assertEqual(cluster_submit.effective_option(argv, '-t', '--time', ''), '1-00:00:00')
        self.assertEqual(cluster_submit.effective_option(argv, '-p', '--partition', ''), 'cpu7')


class Generator(unittest.TestCase):
    def generate(self, source, *args, sinfo_limit="14-00:00:00"):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            inp, out = root/'source.yml', root/'generated.yml'
            inp.write_text(source)
            fake = root/'sinfo'
            call_log = root/'sinfo.called'
            fake.write_text('#!' + sys.executable + '\nfrom pathlib import Path\n' +
                            'Path(' + repr(str(call_log)) + ').touch()\nprint(' + repr(sinfo_limit) + ')\n')
            fake.chmod(0o755)
            env = dict(os.environ, PATH=str(root) + ':/usr/bin:/bin')
            proc = subprocess.run([sys.executable, str(BIN/'generate_cluster_from_config.py'),
                                   '--config', str(inp), '--out', str(out), *args],
                                  capture_output=True, text=True, env=env)
            self.sinfo_called = call_log.exists()
            self.generated_text = out.read_text() if out.exists() else ""
            return proc, yaml.safe_load(out.read_text()) if out.exists() else None

    def test_source_time_and_resources_survive_without_auto(self):
        proc, cfg = self.generate('''__default__:
  account: group
  partition: cpu
  time: 14-00:00:00
  memory: 99g
  ncpus: 8
rule_a:
  time: 60
  nodes: 3
  account: rulegroup
rule_b:
  time: 72:00:00
rule_c:
  time: 259200
''')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(cfg['__default__']['time'], '14-00:00:00')
        self.assertEqual(cfg['__default__']['account'], 'group')
        self.assertEqual(cfg['__default__']['memory'], '99g')
        self.assertEqual(cfg['rule_a']['time'], '60')
        self.assertEqual(cfg['rule_a']['nodes'], 3)
        self.assertEqual(cfg['rule_a']['account'], 'rulegroup')
        self.assertEqual(cfg['rule_b']['time'], '72:00:00')
        self.assertEqual(cfg['rule_c']['time'], '259200')
        self.assertNotIn('Auto time:', proc.stdout)
        self.assertFalse(self.sinfo_called)

    def test_explicit_cli_sets_defaults_and_preserves_rule_resources(self):
        proc, cfg = self.generate('__default__: {time: "14-00:00:00"}\na: {time: 60, account: old, partition: old}\n',
                                  '--time', '90', '--account', 'new', '--partition', 'new')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(cfg['__default__']['time'], '90')
        self.assertEqual(cfg['__default__']['account'], 'new')
        self.assertEqual(cfg['__default__']['partition'], 'new')
        self.assertEqual(cfg['a'], dict(time='60', account='old', partition='old'))

    def test_auto_is_explicit_and_keeps_per_rule_time(self):
        proc, cfg = self.generate('__default__: {time: "14-00:00:00", partition: cpu}\na: {time: "14-00:00:00"}\n',
                                  '--time', 'auto')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(self.sinfo_called)
        self.assertEqual(cfg['__default__']['time'], '13-00:00:00')
        self.assertEqual(cfg['a']['time'], '14-00:00:00')
        self.assertIn("sinfo output='14-00:00:00'", self.generated_text)
        self.assertIn('SYLVAN_CLUSTER_CONFIG=', proc.stdout)

    def test_auto_small_cap_uses_cap_instead_of_exceeding_it(self):
        proc, cfg = self.generate('__default__: {partition: cpu}\n', sinfo_limit='1:30')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(cfg['__default__']['time'], '0-00:01:30')
        self.assertTrue(self.sinfo_called)

    def test_auto_unknown_cap_records_fallback(self):
        proc, cfg = self.generate('__default__: {partition: cpu}\n', sinfo_limit='n/a')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(cfg['__default__']['time'], '9-00:00:00')
        self.assertIn('WARNING', proc.stderr)
        self.assertIn("sinfo output='n/a'", self.generated_text)

    def test_invalid_source_lexeme_rejected(self):
        proc, cfg = self.generate('__default__: {time: 0x3C}\n')
        self.assertNotEqual(proc.returncode, 0)
        self.assertIsNone(cfg)


class Normalization(unittest.TestCase):
    def test_raw_anchors_merges_and_original_hash(self):
        from cluster_config import dump_cluster_config, load_cluster_config
        source = """common: &resources
  time: &wall 72:00:00
  account: null
  memory: 8g
__default__:
  <<: *resources
  partition: null
rule_a:
  <<: *resources
  time: 60
rule_b:
  time: *wall
  memory: *wall
Input:
  nested: {time: 12, account: null}
"""
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/'config.yml'; path.write_text(source)
            before = hashlib.sha256(path.read_bytes()).hexdigest()
            cfg = load_cluster_config(path)
            emitted = yaml.safe_load(dump_cluster_config(cfg))
            self.assertEqual(emitted['__default__'], dict(time='72:00:00', account='', memory='8g', partition=''))
            self.assertEqual(emitted['rule_a']['time'], '60')
            self.assertEqual(emitted['rule_b'], dict(time='72:00:00', memory=259200))
            self.assertEqual(emitted['Input'], dict(nested=dict(time=12, account=None)))
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before)
            self.assertEqual(emitted, cfg)

    def test_only_explicit_null_optional_keys_are_added_to_overlays(self):
        from cluster_config import load_cluster_config
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/'overlay.yml'
            path.write_text('__default__: {time: 60}\na: {account: null}\n')
            cfg = load_cluster_config(path)
            self.assertEqual(cfg, {'__default__': {'time': '60'}, 'a': {'account': ''}})

    def test_invalid_raw_lexemes_rejected_before_yaml_numeric_conversion(self):
        from cluster_config import load_cluster_config
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/'source.yml'
            for token in ['0x3C', '1_440', '+60', 'true', 'null', '1.5', '72h']:
                path.write_text('__default__:\n  time: ' + token + '\n')
                with self.subTest(token=token), self.assertRaisesRegex(ValueError, 'time'):
                    load_cluster_config(path)


class RealEntry(unittest.TestCase):
    """Actual Bash entry + real Snakemake + actual submitter, recording scheduler only."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='sylvan cluster ')
        self.root = Path(self.temp.name)
        (self.root/'bin').symlink_to(BIN, target_is_directory=True)
        (self.root/'test').mkdir()
        (self.root/'test/annotate_test.sh').symlink_to(BIN.parent/'test/annotate_test.sh')
        fake = self.root/'path'; fake.mkdir()
        self.env = dict(PATH=str(fake) + ':/usr/bin:/bin', HOME=str(self.root),
                        LANG='C.UTF-8', PYTHONDONTWRITEBYTECODE='1',
                        RECORD_ROOT=str(self.root), SYLVAN_RUN_NAME='fixture',
                        SYLVAN_CONFIG='workflow.yml', SYLVAN_CLUSTER_CONFIG='workflow.yml')
        (fake/'python3').symlink_to(sys.executable)
        scripts = {
            'snakemake': 'import os,sys; os.execv(sys.executable,[sys.executable,"-m","snakemake"]+sys.argv[1:])',
            'sinfo': 'print("unlimited|INFINITE")',
            'squeue': '',
            'sacct': 'print("COMPLETED")',
            'sbatch': r"""import json,os,re,sys
from pathlib import Path
root=Path(os.environ['RECORD_ROOT'])
args=sys.argv[1:]
script=Path(args[-1]).resolve()
assert script.is_relative_to(root)
body=script.read_text()
props=json.loads(next(line[len('# properties = '):] for line in body.splitlines() if line.startswith('# properties = ')))
with (root/'submissions.jsonl').open('a') as stream:
    stream.write(json.dumps(dict(args=args,properties=props))+'\n')
for target in props['output']:
    path=(root/target).resolve()
    assert path.is_relative_to(root)
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text('recorded only')
if '.jobfinished' in body:
    (script.parent/(str(props['jobid'])+'.jobfinished')).touch()
print(1000+int(props['jobid']))
"""
        }
        for name, body in scripts.items():
            path = fake/name
            path.write_text('#!' + sys.executable + '\n' + body + '\n')
            path.chmod(0o755)
        self.base = dict(nodes=2, memory='8g', ncpus=4, name='{rule}', partition='unlimited',
                         time='14-00:00:00', output="logs/out with ' quote.txt", error='logs/error path.txt',
                         extra_args='--comment "a b"')

    def tearDown(self):
        self.temp.cleanup()

    def run_entry(self, cfg, rules, extra=(), entry='bin/annotate.sh', expect_failure=False):
        source = cfg if isinstance(cfg, str) else yaml.safe_dump(cfg, sort_keys=False)
        (self.root/'workflow.yml').write_text(source)
        snake = 'rule all:\n    input: ' + repr(['outputs/' + rule for rule in rules]) + '\n'
        for rule in rules:
            snake += '\nrule ' + rule + ':\n    output: "outputs/' + rule + '"\n    shell: "exit 99"\n'
        (self.root/'fixture.smk').write_text(snake)
        cmd = ['bash', str(self.root/entry)]
        if entry.startswith('test/'):
            cmd += ['--run', '--conda-base-path', str(Path(sys.executable).parent.parent)]
        cmd += ['--snakefile', 'fixture.smk', '--jobs', '4', '--latency-wait', '1',
                '--scheduler', 'greedy', '--max-status-checks-per-second', '100', *extra]
        proc = subprocess.run(cmd, cwd=self.root, env=self.env, text=True, capture_output=True, timeout=90)
        if expect_failure:
            self.assertNotEqual(proc.returncode, 0, proc.stderr)
            self.assertFalse((self.root/'submissions.jsonl').exists())
            return proc, {}
        self.assertEqual(proc.returncode, 0, proc.stderr)
        record = self.root/'submissions.jsonl'
        submissions = [json.loads(line) for line in record.read_text().splitlines()]
        return proc, {row['properties']['rule']: row['args'] for row in submissions}

    def test_invalid_yaml_time_never_reaches_sbatch(self):
        cfg = {'__default__': dict(self.base, time='INVALID_RAW')}
        source = yaml.safe_dump(cfg).replace('time: INVALID_RAW', 'time: 0x3C')
        proc, _ = self.run_entry(source, ['rule_a'], expect_failure=True)
        self.assertIn('Invalid Slurm time', proc.stderr)
        self.assertEqual((self.root/'workflow.yml').read_text(), source)

    def test_exact_time_tokens_optional_accounts_and_shell_quoting(self):
        cfg = {'__default__': self.base}
        cases = {'raw': 'RAW_TIME', 'quoted': '72:00:00', 'minutes': 60, 'large': 259200,
                 'seconds': '1:30', 'days': '1-2', 'zero': 0, 'placeholder': '14-00:00:00'}
        for rule, value in cases.items():
            cfg[rule] = {'time': value, 'account': None if rule != 'placeholder' else 'placeholder'}
        source = yaml.safe_dump(cfg, sort_keys=False).replace('time: RAW_TIME', 'time: 72:00:00')
        proc, records = self.run_entry(source, list(cases))
        expected = dict(raw='72:00:00', quoted='72:00:00', minutes='60', large='259200',
                        seconds='1:30', days='1-2', zero='0', placeholder='14-00:00:00')
        for rule, token in expected.items():
            args = records[rule]
            self.assertEqual(args[args.index('-t') + 1], token)
            self.assertNotIn('-A', args)
            self.assertEqual(args[args.index('-o') + 1], "logs/out with ' quote.txt")
            self.assertEqual(args[args.index('--comment') + 1], 'a b')
            self.assertIn('jobscript=', proc.stderr)
        self.assertEqual((self.root/'workflow.yml').read_text(), source)

    def test_overlay_absent_keys_do_not_clobber_earlier_layers(self):
        self.base.update(account='defaultgroup')
        cfg = {'__default__': self.base, 'rule_a': {'account': 'rulegroup', 'time': '60'}}
        (self.root/'overlay one.yml').write_text('__default__: {memory: 12g}\nrule_a: {time: 72:00:00}\n')
        (self.root/'overlay two.yml').write_text('__default__: {ncpus: 7}\n')
        proc, records = self.run_entry(cfg, ['rule_a', 'rule_b'],
                                      ['--cluster-config=overlay one.yml', '-uoverlay two.yml'])
        self.assertEqual(records['rule_a'][records['rule_a'].index('-A') + 1], 'rulegroup')
        self.assertEqual(records['rule_b'][records['rule_b'].index('-A') + 1], 'defaultgroup')
        self.assertIn('--mem=12g', records['rule_a'])
        self.assertIn('--cpus-per-task=7', records['rule_b'])
        self.assertEqual(records['rule_a'][records['rule_a'].index('-t') + 1], '72:00:00')
        provenance = json.loads(next((self.root/'results/cluster-config').glob('*/provenance.json')).read_text())
        self.assertEqual(len(provenance['files']), 3)
        for row in provenance['files']:
            for label in ['original', 'effective']:
                self.assertEqual(hashlib.sha256(Path(row[label]).read_bytes()).hexdigest(), row[label + '_sha256'])
        self.assertNotIn('account', yaml.safe_load(Path(provenance['files'][1]['effective']).read_text())['__default__'])
        self.assertIn(str(self.root/'workflow.yml'), proc.stderr)

    def test_test_entry_uses_same_optional_account_path(self):
        proc, records = self.run_entry({'__default__': self.base}, ['rule_a'], entry='test/annotate_test.sh')
        self.assertNotIn('-A', records['rule_a'])
        self.assertEqual(records['rule_a'][records['rule_a'].index('-p') + 1], 'unlimited')


if __name__ == '__main__':
    unittest.main()
