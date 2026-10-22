"""Reference preparation contracts; fixtures are synthetic, not annotation results."""
import csv
import importlib.util
import os
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

HELPER = Path(__file__).with_name('prepare_liftoff.py')


def annotation(seqid='chr1'):
    return ('##gff-version 3\n'
            f'{seqid}\tfixture\tgene\t1\t12\t.\t+\t.\tID=g;Name=unchanged%3Bname\n'
            f'{seqid}\tfixture\tmRNA\t1\t12\t.\t+\t.\tID=t;Parent=g\n'
            f'{seqid}\tfixture\tmRNA\t1\t12\t.\t+\t.\tID=u;Parent=g\n'
            f'{seqid}\tfixture\texon\t1\t6\t.\t+\t.\tID=e;Parent=t,u\n'
            f'{seqid}\tfixture\tCDS\t1\t3\t.\t+\t0\tID=c;Parent=t\n'
            f'{seqid}\tfixture\tCDS\t7\t12\t.\t+\t0\tID=c;Parent=t\n'
            f'{seqid}\tfixture\tpolypeptide\t1\t12\t.\t+\t.\tID=p;Derives_from=t\n')


class PreparationTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(HELPER.is_file(), 'Missing paired reference preparation boundary')
        spec = importlib.util.spec_from_file_location('prepare_liftoff', HELPER)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.temp = tempfile.TemporaryDirectory(prefix='sylvan neighbor ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.fa = self.root/'genomes'; self.fa.mkdir()
        self.gf = self.root/'annotations'; self.gf.mkdir()
        self.out = self.root/'outputs'
        self.outputs = [self.out/name for name in ('combined.fasta', 'combined.gff3', 'combined.pairs.tsv')]

    def pair(self, name='c', fa_ext='fna', gf_name=None, gff=None, fasta=None):
        fa = self.fa/f'{name}.{fa_ext}'
        gf = self.gf/(gf_name or f'{name}.gff3')
        fa.write_bytes(fasta if fasta is not None else b'>chr1 description\nACGTacgt\nACGT\n')
        gf.write_text(annotation() if gff is None else gff)
        return fa, gf

    def prepare(self, pairs=None, reverse=False):
        fastas = self.module.find_references(self.fa, self.module.FASTA_EXTENSIONS)
        gffs = self.module.find_references(self.gf, self.module.GFF_EXTENSIONS)
        if reverse:
            fastas.reverse(); gffs.reverse()
        self.module.prepare_references(fastas, gffs, *self.outputs, pairs=pairs)
        return self.outputs[0].read_bytes(), self.outputs[1].read_text()

    def test_short_stem_and_relationships_keep_coordinates_and_sequence_bytes(self):
        fa, gf = self.pair()
        before = fa.read_bytes(), gf.read_bytes()
        fasta, gff = self.prepare()
        self.assertEqual(fasta, b'>c__chr1 description\nACGTacgt\nACGT\n')
        expected = annotation().splitlines()[1:]
        observed = gff.splitlines()
        self.assertEqual([r.split('\t')[1:8] for r in observed], [r.split('\t')[1:8] for r in expected])
        self.assertIn('ID=c__e;Parent=c__t,c__u', gff)
        self.assertEqual(gff.count('ID=c__c;Parent=c__t'), 2)
        self.assertIn('ID=c__p;Derives_from=c__t', gff)
        self.assertIn('Name=unchanged%3Bname', gff)
        self.assertEqual(before, (fa.read_bytes(), gf.read_bytes()))
        with self.outputs[2].open() as handle:
            manifest = list(csv.DictReader(handle, delimiter='\t'))
        self.assertEqual(manifest[0]['namespace'], 'c')
        self.assertEqual(Path(manifest[0]['fasta']), fa)
        self.assertEqual(Path(manifest[0]['gff']), gf)

    def test_all_recognized_extensions_are_paired_from_same_inventory(self):
        for i, ext in enumerate(('fa', 'fasta', 'fna', 'fas', 'fsa', 'seq')):
            self.pair(f'R{i}', ext, f'R{i}.' + ('gff' if i % 2 else 'gff3'))
        (self.fa/'ignored.fa.gz').write_text('ignore')
        fasta, gff = self.prepare()
        self.assertEqual(fasta.count(b'>'), 6)
        self.assertEqual(len(gff.splitlines()), 42)

    def test_distinct_toy_and_ncbi_references_shared_gene_ids_and_order(self):
        for name in ('aly4', 'aly5', 'GCF_000001', 'GCF_000002'):
            self.pair(name)
        first = self.prepare()
        self.assertEqual(first, self.prepare(reverse=True))
        headers = [r.split()[0] for r in first[0].splitlines() if r.startswith(b'>')]
        self.assertEqual(len(set(headers)), 4)
        genes = [r.split('ID=')[1] for r in first[1].splitlines() if '\tgene\t' in r]
        self.assertEqual(len(set(genes)), 4)

    def test_legacy_genome_gene_and_explicit_pairing(self):
        self.pair('Aly.genome', 'fa', 'Aly.gene.gff3')
        fasta, _ = self.prepare()
        self.assertTrue(fasta.startswith(b'>Aly__chr1'))
        self.pair('unusual assembly', 'fas', 'renamed annotation.gff')
        with self.assertRaisesRegex(ValueError, 'unpaired|Unpaired'):
            self.prepare()
        fasta, gff = self.prepare({'other': {'fasta': 'unusual assembly.fas', 'gff': 'renamed annotation.gff'}})
        self.assertIn(b'>other__chr1', fasta)
        self.assertIn('ID=other__g', gff)

    def test_sanitized_namespace_collisions_are_distinct_and_deterministic(self):
        self.pair('a b'); self.pair('a_b')
        fasta, gff = self.prepare()
        self.assertEqual((fasta, gff), self.prepare(reverse=True))
        headers = [line.split()[0] for line in fasta.splitlines() if line.startswith(b'>')]
        self.assertEqual(len(set(headers)), 2)
        genes = [r.split('ID=')[1] for r in gff.splitlines() if '\tgene\t' in r]
        self.assertEqual(len(set(genes)), 2)

    def test_ambiguous_and_incomplete_pairs_rejected(self):
        self.pair('Ath', 'fa'); (self.fa/'Ath.fna').write_text('>x\nA\n')
        with self.assertRaisesRegex(ValueError, 'unpaired|Unpaired|ambiguous'):
            self.prepare()
        (self.fa/'Ath.fna').unlink(); (self.gf/'Ath.gff3').unlink()
        with self.assertRaisesRegex(ValueError, 'unpaired|Unpaired'):
            self.prepare()

    def test_crlf_and_embedded_fasta_preserve_real_features(self):
        original_fasta = b'>chr1 description\r\nACGTacgt\r\nACGT\r\n'
        self.pair(fasta=original_fasta, gff=annotation().replace('\n', '\r\n') + '##FASTA\r\n>unrelated\r\nAAA\r\n')
        fasta, gff = self.prepare()
        self.assertEqual(fasta, original_fasta.replace(b'>chr1', b'>c__chr1'))
        self.assertEqual(len(gff.splitlines()), 7)
        self.assertNotIn('unrelated', gff)

    def test_escaped_identity_tokens_and_leading_punctuation_seqid(self):
        gff = annotation('_chr').replace('ID=g;', 'ID=g%3B1;').replace('Parent=g\n', 'Parent=g%3b1\n')
        gff = gff.replace('ID=t;', 'ID=t%2C1;').replace('Parent=t', 'Parent=t%2c1').replace('Derives_from=t\n', 'Derives_from=t%2C1\n')
        self.pair(fasta=b'>_chr\nACGTacgtACGT\n', gff=gff)
        fasta, output = self.prepare()
        self.assertIn(b'>c___chr', fasta)
        self.assertIn('ID=c__g%3B1;', output)
        self.assertIn('Parent=c__g%3B1', output)
        self.assertIn('Parent=c__t%2C1,c__u', output)

    def test_malformed_inputs_do_not_replace_existing_outputs(self):
        fa, gf = self.pair(); self.prepare()
        before = [path.read_bytes() for path in self.outputs]
        invalid = {
            'columns': annotation().replace('\tfixture\t', '\tbroken\textra\t', 1),
            'coordinate': annotation().replace('\t1\t12\t', '\t0\t12\t', 1),
            'landmark': annotation('missing'),
            'parent': annotation().replace('Parent=t', 'Parent=missing'),
            'derivation': annotation().replace('Derives_from=t', 'Derives_from=missing'),
            'duplicate ID': annotation().replace('ID=t;', 'ID=g;'),
            'cycle': annotation().replace('ID=g;', 'ID=g;Parent=t;'),
            'attribute': annotation().replace('ID=g;', 'ID=g;ID=duplicate;'),
            'empty annotation': '',
        }
        for reason, content in invalid.items():
            with self.subTest(reason=reason):
                gf.write_text(content)
                with self.assertRaises(ValueError):
                    self.prepare()
                self.assertEqual(before, [path.read_bytes() for path in self.outputs])
        gf.write_text(annotation()); fa.write_text('')
        with self.assertRaises(ValueError):
            self.prepare()
        self.assertEqual(before, [path.read_bytes() for path in self.outputs])

    def test_multipart_feature_with_distinct_parent_lists_is_preserved(self):
        self.pair(gff=annotation().replace('ID=c;Parent=t\n', 'ID=c;Parent=u\n', 1))
        _, gff = self.prepare()
        self.assertIn('ID=c__c;Parent=c__u', gff)
        self.assertIn('ID=c__c;Parent=c__t', gff)

    def test_no_references_rejected_and_cds_only_annotation_remains_nonempty(self):
        with self.assertRaisesRegex(ValueError, 'no paired|No paired|No neighbor'):
            self.prepare()
        self.pair(gff='\n'.join(r for r in annotation().splitlines() if '\texon\t' not in r)+'\n')
        _, gff = self.prepare()
        self.assertNotIn('\texon\t', gff)
        self.assertIn('\tCDS\t', gff)

    def test_publish_error_rolls_back_entire_existing_set(self):
        fa, _ = self.pair(); self.prepare()
        index = Path(str(self.outputs[0]) + '.fai')
        index.write_text('old derived index\n')
        before = [path.read_bytes() for path in self.outputs]
        fa.write_bytes(b'>chr1\nTTTTTTTTTTTT\n')
        real_replace = os.replace
        calls = 0
        def fail_second(source, dest):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError('simulated publication failure')
            return real_replace(source, dest)
        with patch.object(self.module.os, 'replace', side_effect=fail_second):
            with self.assertRaises(OSError):
                self.prepare()
        self.assertEqual(before, [path.read_bytes() for path in self.outputs])
        self.assertEqual(index.read_text(), 'old derived index\n')

    @unittest.skipUnless(shutil.which('bedtools'), 'real bedtools required for index lifecycle')
    def test_reference_replacement_invalidates_only_owned_fasta_index(self):
        fa, gf = self.pair(fasta=b'>chr1\nCCCCCCCCCCCC\n')
        source_index = Path(str(fa) + '.fai')
        source_index.write_text('source index must not change\n')
        self.prepare()
        def extract():
            result = subprocess.run(['bedtools', 'getfasta', '-fi', str(self.outputs[0]),
                                     '-bed', str(self.outputs[1])], text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            return result.stdout
        extract()  # bedtools writes combined.fasta.fai alongside the derived output.
        self.pair('a', fasta=b'>chr1\nAAAAAAAAAAAA\n')
        self.prepare()
        result = extract()
        self.assertIn('>a__chr1:0-12\nAAAAAAAAAAAA\n', result)
        self.assertIn('>c__chr1:0-12\nCCCCCCCCCCCC\n', result)
        self.assertEqual(source_index.read_text(), 'source index must not change\n')
        # Removing the reference shifts offsets again and must invalidate the cache.
        (self.fa/'a.fna').unlink(); (self.gf/'a.gff3').unlink()
        self.prepare()
        self.assertNotIn('>a__chr1', extract())
        self.assertIn('>c__chr1:0-12\nCCCCCCCCCCCC\n', extract())

    def test_percent_escaped_seqid_matches_fasta_landmark(self):
        self.pair(fasta=b'>chr:1\nACGTACGTACGT\n', gff=annotation('chr%3A1'))
        fasta, gff = self.prepare()
        self.assertTrue(fasta.startswith(b'>c__chr:1\n'))
        self.assertTrue(all(r.startswith('c__chr:1\t') for r in gff.splitlines()))

    def test_evm_converter_preserves_namespaced_relationships(self):
        gff = annotation().replace('ID=g;', 'ID=a:b;').replace('Parent=g\n', 'Parent=a:b\n')
        gff += 'chr1\tfixture\tgene\t1\t12\t.\t+\t.\tID=a_b\n'
        gff += 'chr1\tfixture\tgene\t1\t12\t.\t+\t.\tID=a%3Bb\n'
        self.pair(gff=gff); _, gff = self.prepare()
        self.assertIn('ID=c__a:b;', gff)
        self.assertIn('ID=c__a_b\n', gff)
        self.assertIn('ID=c__a%3Bb\n', gff)
        run = subprocess.run([sys.executable, str(HELPER.with_name('gff_to_evm.py')), str(self.outputs[1]), '--feature', '.*'], capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(run.stdout, gff)


if __name__ == '__main__':
    unittest.main(verbosity=2)
