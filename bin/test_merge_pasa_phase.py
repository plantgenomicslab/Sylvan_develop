"""Regression fixtures for phase metadata; these are not experimental results."""
import pathlib
import subprocess
import sys
import tempfile
import unittest

SCRIPT = pathlib.Path(__file__).with_name('merge_pasa_evm.py')


def fixture(strand, segments):
    lo = min(s for s, e, phase in segments)
    hi = max(e for s, e, phase in segments)
    lines = [f'chr1\tEVM\tgene\t{lo}\t{hi}\t.\t{strand}\t.\tID=g1',
             f'chr1\tEVM\tmRNA\t{lo}\t{hi}\t.\t{strand}\t.\tID=t1;Parent=g1']
    for i, (start, end, phase) in enumerate(segments, 1):
        lines.extend([f'chr1\tEVM\texon\t{start}\t{end}\t.\t{strand}\t.\tID=e{i};Parent=t1',
                      f'chr1\tEVM\tCDS\t{start}\t{end}\t.\t{strand}\t{phase}\tID=c{i};Parent=t1'])
    return '##gff-version 3\n' + '\n'.join(lines) + '\n'


def rows(text):
    return [line.split('\t') for line in text.splitlines() if line and not line.startswith('#')]


class PhaseExportTests(unittest.TestCase):
    def merge(self, evm, pasa, splice=None):
        with tempfile.TemporaryDirectory(prefix='sylvan-phase-test-') as folder:
            root = pathlib.Path(folder)
            (root/'evm.gff3').write_text(evm)
            (root/'pasa.gff3').write_text(pasa)
            command = [sys.executable, str(SCRIPT), str(root/'pasa.gff3'),
                       str(root/'evm.gff3'), str(root/'out.gff3')]
            if splice is not None:
                (root/'splice.tsv').write_text(splice)
                command += ['--splice', str(root/'splice.tsv')]
            run = subprocess.run(command,
                                 capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            return (root/'out.gff3').read_text(), run.stderr

    def assert_other_columns_preserved(self, before, after):
        self.assertEqual([r[:7]+r[8:] for r in rows(before)],
                         [r[:7]+r[8:] for r in rows(after)])

    def test_positive_observed_305_base_first_cds_needs_phase_one(self):
        evm = fixture('+', [(48266,48570,0),(48630,49068,1)])
        pasa = fixture('+', [(48266,48570,0),(48630,49068,2)])
        result, _ = self.merge(evm, pasa)
        self.assertEqual([r[7] for r in rows(result) if r[2]=='CDS'], ['0','1'])
        self.assert_other_columns_preserved(pasa, result)

    def test_reverse_phase_walk_starts_at_high_coordinate(self):
        evm = fixture('-', [(100,201,2),(400,466,0)])
        pasa = fixture('-', [(100,201,1),(400,466,0)])
        result, _ = self.merge(evm, pasa)
        self.assertEqual([r[7] for r in rows(result) if r[2]=='CDS'], ['2','0'])
        self.assert_other_columns_preserved(pasa, result)

    def test_current_first_phase_is_not_replaced_by_reference_first_phase(self):
        evm = fixture('+', [(1,5,0),(11,17,1)])
        pasa = fixture('+', [(1,5,1),(11,17,0)])
        result, log = self.merge(evm, pasa)
        self.assertEqual([r[7] for r in rows(result) if r[2]=='CDS'], ['1','2'])
        self.assert_other_columns_preserved(pasa, result)
        self.assertIn('1 first-phase differences from EVM preserved', log)

    def test_partial_initial_phase_restored_only_for_inconsistent_full_inversion(self):
        for strand in ['+', '-']:
            for initial in [1, 2]:
                with self.subTest(strand=strand, initial=initial):
                    reference = ([(100, 234 + initial, initial), (400, 594, 0)]
                                 if strand == '+' else
                                 [(100, 294, 0), (400, 534 + initial, initial)])
                    inverted = [(s, e, (-p) % 3) for s, e, p in reference]
                    evm, pasa = fixture(strand, reference), fixture(strand, inverted)
                    result, log = self.merge(evm, pasa)
                    self.assertEqual(rows(result), rows(evm))
                    self.assert_other_columns_preserved(pasa, result)
                    self.assertIn('1 initial phases restored', log)
                    self.assertIn('0 first-phase differences from EVM preserved', log)
                    replay, replay_log = self.merge(evm, result)
                    self.assertEqual(result, replay)
                    self.assertIn('CDS phase metadata: 0 transcripts', replay_log)
                    self.assertIn('0 initial phases restored', replay_log)

    def test_continuity_consistent_initial_inversions_are_preserved_and_reported(self):
        for reference in [[(1, 135, 1), (201, 335, 1)], [(1, 136, 1)]]:
            with self.subTest(reference=reference):
                evm = fixture('+', reference)
                pasa = fixture('+', [(s, e, (-p) % 3) for s, e, p in reference])
                result, log = self.merge(evm, pasa)
                self.assertEqual(rows(result), rows(pasa))
                self.assertIn('1 first-phase differences from EVM preserved', log)
                self.assertIn('1 unresolved initial-phase inversions', log)
                self.assertIn('0 initial phases restored', log)

    def test_invalid_reference_phase_chain_is_not_used(self):
        evm = fixture('+', [(1, 305, 0), (400, 600, 2)])
        pasa = fixture('+', [(1, 305, 0), (400, 600, 2)])
        result, _ = self.merge(evm, pasa)
        self.assertEqual(rows(result), rows(pasa))

    def test_changed_coding_chain_is_not_rewritten(self):
        evm = fixture('+', [(1,305,0),(400,600,1)])
        pasa = fixture('+', [(1,305,0),(401,600,2)])
        result, _ = self.merge(evm, pasa)
        self.assertEqual(rows(result), rows(pasa))

    def test_explicit_translation_exception_is_not_rewritten(self):
        evm = fixture('+', [(1,305,0),(400,600,1)])
        pasa = fixture('+', [(1,305,0),(400,600,2)]).replace(
            'ID=t1;Parent=g1', 'ID=t1;Parent=g1;exception=ribosomal_slippage')
        result, _ = self.merge(evm, pasa)
        self.assertEqual(rows(result), rows(pasa))

    def test_duplicate_reference_transcript_is_not_used(self):
        evm = fixture('+', [(1,305,0),(400,600,1)])
        pasa = fixture('+', [(1,305,0),(400,600,2)])
        result, _ = self.merge(evm + evm, pasa)
        self.assertEqual(rows(result), rows(pasa))

    def test_unsafe_duplicate_reference_is_not_hidden_by_validity_filter(self):
        valid = fixture('+', [(1,305,0),(400,600,1)])
        pasa = fixture('+', [(1,305,0),(400,600,2)])
        unsafe = {
            'exception': valid.replace('ID=t1;Parent=g1',
                                      'ID=t1;Parent=g1;exception=ribosomal_slippage'),
            'unknown_phase': valid.replace('\t+\t1\tID=c2', '\t+\t.\tID=c2'),
            'shared_parent': valid.replace('ID=c2;Parent=t1', 'ID=c2;Parent=t1,t2'),
            'overlap': valid.replace('CDS\t400\t600', 'CDS\t300\t600'),
        }
        for reason, duplicate in unsafe.items():
            for order, evm in [('valid_first', valid + duplicate),
                               ('unsafe_first', duplicate + valid)]:
                with self.subTest(reason=reason, order=order):
                    result, _ = self.merge(evm, pasa)
                    self.assertEqual(rows(result), rows(pasa))

    def test_selected_pasa_graft_is_repaired_before_existing_id_rename(self):
        evm = fixture('+', [(1,305,0),(400,600,1)])
        primary = fixture('+', [(1,305,0),(410,600,2)]).replace('t1', 'tnew')
        secondary = fixture('+', [(1,305,0),(400,600,2)])
        secondary_rows = [line for line in secondary.splitlines()
                          if not line.startswith('#') and '\tgene\t' not in line]
        pasa = primary + '\n'.join(secondary_rows) + '\n'
        result, log = self.merge(evm, pasa, 'chr1 305 400 10 + 10 0 0 0\n')
        cds = [r for r in rows(result) if r[2] == 'CDS']
        self.assertEqual([r[7] for r in cds if 'Parent=t1_pasa_iso' in r[8]], ['0', '1'])
        self.assertNotIn('tnew_pasa_iso', result)
        self.assertEqual([r[7] for r in cds if r[8].endswith('Parent=t1')], ['0', '1'])
        self.assertIn('CDS phase metadata: 1 transcripts, 1 downstream rows corrected', log)

    def test_noncontiguous_changed_chain_is_not_repaired_from_matching_prefix(self):
        evm = fixture('+', [(1,305,0),(400,600,1)]).replace(
            '\t1\t600\t', '\t1\t900\t')
        features = [
            ('gene', 1, 900, '.', 'ID=g1'),
            ('mRNA', 1, 900, '.', 'ID=tnew;Parent=g1'),
            ('CDS', 1, 305, '0', 'ID=n1;Parent=tnew'),
            ('CDS', 410, 600, '2', 'ID=n2;Parent=tnew'),
            ('mRNA', 1, 900, '.', 'ID=t1;Parent=g1'),
            ('CDS', 1, 305, '0', 'ID=p1;Parent=t1'),
            ('CDS', 400, 600, '2', 'ID=p2;Parent=t1'),
            ('mRNA', 650, 900, '.', 'ID=t2;Parent=g1'),
            ('CDS', 700, 720, '2', 'ID=p3;Parent=t1'),
            ('CDS', 800, 900, '0', 'ID=q1;Parent=t2'),
        ]
        pasa = ''.join(f'chr1\tPASA\t{kind}\t{start}\t{end}\t.\t+\t{phase}\t{attrs}\n'
                       for kind, start, end, phase, attrs in features)
        result, log = self.merge(evm, pasa,
                                 'chr1 305 400 10 + 10 0 0 0\nchr1 720 800 10 + 10 0 0 0\n')
        graft = [r for r in rows(result) if r[2] == 'CDS' and 'Parent=t1_pasa_iso' in r[8]]
        self.assertEqual([(r[3], r[4], r[7]) for r in graft],
                         [('1', '305', '0'), ('400', '600', '2'), ('700', '720', '2')])
        self.assertIn('CDS phase metadata: 0 transcripts, 0 downstream rows corrected', log)

    def test_repair_is_idempotent(self):
        evm = fixture('+', [(1,305,0),(400,600,1)])
        pasa = fixture('+', [(1,305,0),(400,600,2)])
        repaired, _ = self.merge(evm, pasa)
        replay, _ = self.merge(evm, repaired)
        self.assertEqual(repaired, replay)


if __name__ == '__main__':
    unittest.main()
