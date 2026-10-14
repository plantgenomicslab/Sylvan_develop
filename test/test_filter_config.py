#!/usr/bin/env python3
"""Guard shipped filter templates against missing direct workflow config keys.

This inventory complements the cluster allocation check. It does not validate
input paths or replace a Snakemake DAG dry-run with actual annotation outputs.
"""

import re
import shlex
import unittest
from pathlib import Path
from types import SimpleNamespace

import yaml

ROOT = Path(__file__).resolve().parents[1]
KEY_RE = re.compile(
    r"config_dict\[['\"](Input|Cutoff|Threads)['\"]\]\[['\"]([^'\"]+)['\"]\]"
)


def required_config_keys(text):
    return sorted({
        key for line in text.splitlines()
        if not line.lstrip().startswith("#")
        for key in KEY_RE.findall(line)
    })


class TestFilterConfig(unittest.TestCase):
    def test_busco_output_name_and_directory_are_separate(self):
        text = (ROOT / "bin/Snakefile_filter").read_text()
        block = text.split("\nrule busco:", 1)[1].split("\nrule filter:", 1)[0]
        shell = re.search(r'shell:\s*"""(.*?)"""', block, re.S).group(1)
        for prefix in ("results/", "../results/", "/tmp/sylvan.clean/results/"):
            with self.subTest(results_dir=prefix):
                rendered = shell.format(input="proteins.fa", threads=4,
                                        params=SimpleNamespace(busco_lin="eudicots_odb10", results_dir=prefix))
                argv = shlex.split(rendered.replace("\\\n", ""))
                argv = argv[argv.index("busco") + 1:]
                name = argv[argv.index("-o") + 1]
                self.assertEqual(name, "busco_eudicots_odb10",
                                 "BUSCO strips leading slashes from -o; it must be a basename")
                self.assertIn("--out_path", argv)
                directory = argv[argv.index("--out_path") + 1]
                self.assertEqual(Path(directory) / name,
                                 Path(prefix) / "FILTER/busco_eudicots_odb10")

    def test_shipped_templates_cover_required_workflow_keys(self):
        required = required_config_keys((ROOT / "bin/Snakefile_filter").read_text())
        self.assertTrue(required, "workflow required-key inventory is empty")
        for name in (
            "config/config_filter.yml",
            "toydata/config/config_filter.yml",
            "toydata/config/config_filter_local.yml",
        ):
            with self.subTest(config=name):
                config = yaml.safe_load((ROOT / name).read_text())
                missing = [f"{section}.{key}" for section, key in required
                           if key not in config.get(section, {})]
                self.assertEqual(missing, [], f"{name}: missing {missing}")

    def test_inventory_excludes_comments_and_optional_defaults(self):
        text = '''
            # config_dict["Cutoff"]["retired"]
            threads: config_dict["Threads"].get("optional", 2)
            input: config_dict['Input']['genome']
            params: config_dict["Cutoff"]["miniprot_cov"]
            threads: config_dict["Threads"]["busco"]
        '''
        self.assertEqual(required_config_keys(text), [
            ("Cutoff", "miniprot_cov"), ("Input", "genome"), ("Threads", "busco"),
        ])


if __name__ == "__main__":
    unittest.main(verbosity=2)
