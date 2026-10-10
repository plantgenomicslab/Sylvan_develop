import tempfile
from pathlib import Path
import unittest

class MergeTrustedSplice(unittest.TestCase):
    def test_union_preserves_flanking_coordinates_and_strand(self):
        from merge_trusted_splice import merge
        from refine_boundaries import load_splice_junctions
        with tempfile.TemporaryDirectory() as td:
            p=Path(td);a=p/'a';b=p/'b';out=p/'out'
            a.write_text('chr1 100 200 10 + 10 0 0 0\n')
            b.write_text('chr1 100 200 10 + 10 0 0 0\nchr1 100 200 10 - 10 0 0 0\n')
            merge([a,b],out)
            self.assertEqual(len(out.read_text().splitlines()),2)
            j=load_splice_junctions(out)
            self.assertEqual(j[('chr1','+')],{(101,199)})
            self.assertEqual(j[('chr1','-')],{(101,199)})

    def test_missing_declared_input_is_an_error(self):
        from merge_trusted_splice import merge
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)
            with self.assertRaises(FileNotFoundError):merge([p/'missing'],p/'out')
            self.assertFalse((p/'out').exists())

    def test_empty_bin_is_valid_but_malformed_coordinates_are_not(self):
        from merge_trusted_splice import merge
        with tempfile.TemporaryDirectory() as td:
            p=Path(td);a=p/'empty';a.touch();merge([a],p/'out')
            self.assertEqual((p/'out').read_text(),'')
            a.write_text('chr1 bad 200 10 +\n')
            with self.assertRaises(ValueError):merge([a],p/'invalid')

if __name__=='__main__':unittest.main()
