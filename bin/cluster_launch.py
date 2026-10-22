#!/usr/bin/env python3
"""Normalize selected cluster files, record provenance, then launch Snakemake.

Snakemake 7's parser selects files (including -u and repeated flags); its own
recursive merge determines precedence. No workflow config path is rewritten.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from cluster_config import dump_cluster_config, load_cluster_config


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def prepare_args(argv):
    # Use the pinned CLI parser to resolve aliases/abbreviations and its one-file
    # append semantics. Do not duplicate Snakemake's config merge algorithm.
    from snakemake import get_argument_parser
    parser = get_argument_parser()
    parsed, _ = parser.parse_known_args(argv)
    sources = parsed.cluster_config
    if not sources:
        return argv
    parent = Path('results/cluster-config')
    parent.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix='effective-', dir=parent)).resolve()
    defaults = directory/'000-defaults.yml'
    defaults.write_text(dump_cluster_config({'__default__': {'account': '', 'partition': '', 'extra_args': ''}}))
    records = []
    effective = []
    for index, source in enumerate(sources, 1):
        original = Path(source).resolve()
        original_sha = digest(original)
        cfg = load_cluster_config(original)
        if digest(original) != original_sha:
            raise ValueError(f'Cluster configuration changed while reading: {original}')
        target = directory/f'{index:03d}-{original.name}'
        target.write_text(dump_cluster_config(cfg))
        record = dict(original=str(original), original_sha256=original_sha,
                      effective=str(target), effective_sha256=digest(target))
        records.append(record)
        effective.append(str(target))
        print('cluster_config: ' + json.dumps(record, sort_keys=True), file=sys.stderr)
    provenance = dict(defaults=dict(path=str(defaults), sha256=digest(defaults)), files=records)
    for variable in ['SYLVAN_CONFIG', 'SYLVAN_FILTER_CONFIG']:
        path = os.environ.get(variable)
        if path:
            provenance[variable] = dict(path=str(Path(path).resolve()),
                                        sha256=digest(path) if Path(path).is_file() else None)
    (directory/'provenance.json').write_text(json.dumps(provenance, indent=2) + '\n')
    print(f'cluster_config: retained provenance={directory / "provenance.json"}', file=sys.stderr)
    result = ['--cluster-config', str(defaults)]
    i = 0
    selected = 0
    while i < len(argv):
        arg = argv[i]
        if arg == '--':
            result.extend(argv[i:])
            break
        option = parser._parse_optional(arg) if arg.startswith('-') else None
        if option is not None and option[0] is not None and option[0].dest == 'cluster_config':
            result.extend(['--cluster-config', effective[selected]])
            selected += 1
            # Equals and attached -u forms carry the value in this same token.
            i += 1 if option[2] is not None else 2
        else:
            result.append(arg)
            i += 1
    if selected != len(effective):
        raise ValueError('Could not normalize every selected cluster config; use repeated --cluster-config FILE flags')
    return result


def main():
    try:
        args = prepare_args(sys.argv[1:])
    except (OSError, ValueError) as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        return 2
    return subprocess.call(['snakemake'] + args)


if __name__ == '__main__':
    raise SystemExit(main())
