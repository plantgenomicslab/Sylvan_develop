"""Run the real preparation rule; requires the supported Snakemake environment."""
import ast
import importlib.util
import re
import shutil
from types import SimpleNamespace
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from test_prepare_liftoff import annotation

REPO = Path(__file__).resolve().parent.parent


@unittest.skipUnless(importlib.util.find_spec('snakemake'), 'Snakemake is required for real workflow checks')
class WorkflowTests(unittest.TestCase):
    def test_explicit_pairs_and_boolean_no_exon_option_through_real_workflow(self):
        import yaml
        for allow in (False, True):
            with self.subTest(allow=allow), tempfile.TemporaryDirectory(prefix='sylvan workflow ') as folder:
                root = Path(folder)
                fa = root/'neighbor genomes'; fa.mkdir()
                gf = root/'neighbor annotations'; gf.mkdir()
                (fa/'assembly.fna').write_text('>chr1\nACGTACGTACGT\n')
                (gf/'different annotation.gff3').write_text(annotation())
                (root/'rna').mkdir()
                (root/'target.fa').write_text('>target\nACGTACGTACGT\n')
                (root/'protein.fa').write_text('>protein\nMTEYK\n')
                (root/'repeats.fa').write_text('>repeat\nACGT\n')
                (root/'weights.txt').write_text('TRANSCRIPT gmap.exon_match 1\n')
                config = yaml.safe_load((REPO/'config/config_annotate.yml').read_text())
                config['Input'].update(genome=str(root/'target.fa'), rna_seq=str(root/'rna'),
                                       proteins=str(root/'protein.fa'), singularity='',
                                       evm_weights=str(root/'weights.txt'), swissprot='placeholder',
                                       allow_missing_gmap_exon=allow,
                                       liftoff={'neighbor_fasta': str(fa), 'neighbor_gff': str(gf),
                                                'pairs': {'curated': {'fasta': 'assembly.fna',
                                                                     'gff': 'different annotation.gff3'}}})
                config['Input']['geta'].update(RM_lib=str(root/'repeats.fa'), dfam_lib='placeholder')
                config_path = root/'config.yml'; config_path.write_text(yaml.safe_dump(config))
                results = root/'results with spaces'; results.mkdir()
                (results/'.micromamba_hook.sh').write_text(':\n')
                env = dict(os.environ, SYLVAN_CONFIG=str(config_path), SYLVAN_RESULTS_DIR=str(results),
                           PYTHONDONTWRITEBYTECODE='1')
                command = [sys.executable, '-B', '-m', 'snakemake', '--snakefile',
                           str(REPO/'bin/Snakefile_annotate'), 'prepare_liftoff', '--cores', '1',
                           '--allowed-rules', 'prepare_liftoff', '--nolock', '--force-use-threads',
                           '--latency-wait', '0', '--directory', str(root)]
                run = subprocess.run(command, env=env, text=True, capture_output=True, timeout=90)
                self.assertEqual(run.returncode, 0, run.stderr)
                combined = results/'LIFTOVER/LiftOff'
                self.assertTrue((combined/'combined.fasta').read_text().startswith('>curated__chr1'))
                self.assertIn('ID=curated__g', (combined/'combined.gff3').read_text())
                self.assertTrue((combined/'combined.pairs.tsv').is_file())

    def test_actual_gmap_policy_keeps_empty_and_no_exon_cases_distinct(self):
        from prepare_liftoff import prepare_references
        from snakemake.utils import format as snake_format
        source = (REPO/'bin/Snakefile_annotate').read_text()
        block = source.split('rule gmapPrep:', 1)[1].split('rule gmapExon:', 1)[0]
        command = ast.literal_eval(re.search(r'\tshell:\s*(.*)', block, re.S)[1].strip())
        bedtools = shutil.which('bedtools')
        if not bedtools:
            self.skipTest('real bedtools is required for consumer checks')
        command = command.replace('/opt/envs/alignment/bin/bedtools', bedtools)
        for mode, allow in [('empty', False), ('empty', True), ('cds', False), ('cds', True), ('exon', False)]:
            with self.subTest(mode=mode, allow=allow), tempfile.TemporaryDirectory(prefix='sylvan gmap ') as folder:
                root = Path(folder)
                fa = root/'c.fa'; fa.write_text('>chr1\nACGTACGTACGT\n')
                gf = root/'c.gff3'
                gff = annotation()
                if mode == 'cds':
                    gff = '\n'.join(r for r in gff.splitlines() if '\texon\t' not in r) + '\n'
                gf.write_text(gff)
                fasta, gff, pairs = (root/'combined.fasta', root/'combined.gff3', root/'combined.pairs.tsv')
                prepare_references([fa], [gf], fasta, gff, pairs)
                if mode == 'empty':
                    gff.write_text('')
                inputs = SimpleNamespace(combined_fasta=str(fasta), combined_gff=str(gff), genome_fasta=str(fa))
                params = SimpleNamespace(results_dir=str(root)+'/', allow_no_exon=str(int(allow)))
                shell = snake_format(command, input=inputs, params=params)
                # Activation and GMAP indexing are unavailable locally. The real
                # policy shell and real bedtools run; this recorder proves only
                # whether policy allowed progression to indexing.
                prelude = 'micromamba() { :; }\ngmap_build() { touch "$GMAP_CALLED"; }\n'
                result = subprocess.run(['bash', '-e', '-o', 'pipefail', '-c', prelude + shell],
                                        env=dict(os.environ, GMAP_CALLED=str(root/'indexed')),
                                        capture_output=True, text=True, timeout=20)
                exon_fasta = root/'TRANSCRIPT/Gmap/liftoffExon.fasta'
                if mode == 'exon':
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn('ACGTAC', exon_fasta.read_text())
                elif mode == 'cds' and allow:
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(exon_fasta.read_bytes(), b'')
                    self.assertIn('WARNING', result.stderr)
                else:
                    self.assertNotEqual(result.returncode, 0)
                    self.assertFalse((root/'indexed').exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)
