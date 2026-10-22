#!/usr/bin/env python3
"""Generate a cluster-only YAML while preserving explicitly configured resources."""
from __future__ import annotations

import argparse
from pathlib import Path
import shlex
import subprocess
import sys

from cluster_config import QuotedTime, dump_cluster_config, load_cluster_config
from slurm_time import format_seconds, time_seconds, time_token

FALLBACK_TIME = '9-00:00:00'
RESOURCE_KEYS = {'account', 'partition', 'memory', 'ncpus', 'threads', 'nodes',
                 'time', 'name', 'output', 'error', 'extra_args'}


def build_arg_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--out', required=True)
    parser.add_argument('--account', default=None, help='Override the default account; preserve explicit per-rule accounts')
    parser.add_argument('--partition', default=None, help='Override the default partition; preserve explicit per-rule partitions')
    parser.add_argument('--time', default=None,
                        help='Override the default time; preserve per-rule times; auto explicitly selects the cap policy')
    parser.add_argument('--default-memory', default=None)
    parser.add_argument('--default-ncpus', type=int, default=None)
    parser.add_argument('--output-pattern', default=None)
    parser.add_argument('--error-pattern', default=None)
    return parser


def auto_time(partition, notes):
    """Explicit auto policy: finite cap minus one day (cap itself if <= one day)."""
    output = ''
    reason = 'no explicit partition'
    if partition and partition.lower() != 'placeholder':
        try:
            result = subprocess.run(['sinfo', '-p', partition, '-h', '-o', '%l'],
                                    capture_output=True, text=True, timeout=10)
            output = result.stdout.strip()
            if result.returncode:
                raise ValueError(f'sinfo exited {result.returncode}: {result.stderr.strip()}')
            values = [time_seconds(line.strip()) for line in output.splitlines() if line.strip()]
            if not values:
                raise ValueError('sinfo returned no limits')
            finite = [value for value in values if value is not None]
            if len(finite) != len(values):
                reason = 'unlimited partition'
            else:
                cap = max(finite)
                seconds = cap - 86400 if cap > 86400 else cap
                value = format_seconds(seconds)
                notes.append(f'auto partition={partition!r}; sinfo output={output!r}; selected={value}')
                return value
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            reason = str(exc)
    note = f'auto partition={partition!r}; sinfo output={output!r}; {reason}; fallback={FALLBACK_TIME}'
    notes.append(note)
    print('WARNING: ' + note, file=sys.stderr)
    return FALLBACK_TIME


def main():
    args = build_arg_parser().parse_args()
    try:
        cfg = load_cluster_config(args.config)
        source_default = cfg.get('__default__', {})
        if not isinstance(source_default, dict):
            raise ValueError('__default__ must be a mapping')
        default = dict(account='', partition='', memory='4g', ncpus=1, nodes=1,
                       name='{rule}.{wildcards}', output='results/logs/{rule}_{wildcards}.out',
                       error='results/logs/{rule}_{wildcards}.err', extra_args='')
        default.update(source_default)
        for key, value in [('memory', args.default_memory), ('ncpus', args.default_ncpus),
                           ('output', args.output_pattern), ('error', args.error_pattern)]:
            if value is not None:
                default[key] = value
        cluster = {'__default__': default}
        for name, section in cfg.items():
            if name != '__default__' and isinstance(section, dict) and RESOURCE_KEYS.intersection(section):
                cluster[name] = {key: value for key, value in section.items() if key in RESOURCE_KEYS}
        notes = ['Explicit CLI account/partition/time set __default__; explicit per-rule values win.',
                 'Omitted CLI values preserve source defaults; absent per-rule values inherit __default__.',
                 'extra_args are applied after resource fields by cluster_submit.py.',
                 'Auto is used only for --time auto or an absent default time: finite cap minus one day;',
                 'for caps <= one day use the cap; unavailable/unlimited caps use ' + FALLBACK_TIME + '.']
        for key in ['account', 'partition', 'time']:
            value = getattr(args, key)
            if value is not None:
                default[key] = value
        for section in cluster.values():
            if section is default and (section.get('time') == 'auto' or 'time' not in section):
                partition = section.get('partition', default['partition'])
                section['time'] = auto_time(partition, notes)
                print(f'Auto time: {section["time"]} (partition: {partition})')
            if 'time' in section:
                section['time'] = QuotedTime(time_token(section['time']))
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(''.join('# ' + line + '\n' for line in notes) + dump_cluster_config(cluster))
    except (OSError, ValueError) as exc:
        raise SystemExit(f'ERROR: {exc}')
    print(f'Wrote cluster config to {out}')
    print('Next command (keeps the workflow configuration unchanged):')
    print(f'export SYLVAN_CLUSTER_CONFIG={shlex.quote(str(out))}')
    print('./bin/annotate.sh')


if __name__ == '__main__':
    main()
