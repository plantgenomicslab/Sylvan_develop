"""Read cluster YAML before YAML 1.1 can discard walltime scalar lexemes.

Each selected file is normalized independently. Snakemake remains responsible
for merging files and inheriting __default__ values into individual rules.
"""
from pathlib import Path

import yaml

from slurm_time import time_token


class QuotedTime(str):
    pass


class ClusterDumper(yaml.SafeDumper):
    pass


ClusterDumper.add_representer(
    QuotedTime, lambda dumper, value: dumper.represent_scalar('tag:yaml.org,2002:str', value, style='"')
)


def load_cluster_config(path):
    """Preserve time text through anchors/merges; change only optional nulls."""
    path = Path(path)
    loader = yaml.SafeLoader(path.read_text())
    try:
        root = loader.get_single_node()
        if root is None:
            return {}
        if not isinstance(root, yaml.MappingNode):
            raise ValueError(f'{path}: cluster configuration must be a mapping')
        loader.flatten_mapping(root)
        for section_key, section in root.value:
            if not isinstance(section, yaml.MappingNode):
                continue
            loader.flatten_mapping(section)
            replacement = []
            for key, value in section.value:
                if key.value == 'time':
                    if not isinstance(value, yaml.ScalarNode):
                        raise ValueError(f'{path}: {section_key.value}.time must be a scalar')
                    try:
                        token = time_token(value.value)
                    except ValueError as exc:
                        raise ValueError(f'{path}:{value.start_mark.line + 1}: {section_key.value}.time: {exc}') from exc
                    # Clone the scalar: an alias also used in another resource
                    # must retain its original YAML type.
                    value = yaml.ScalarNode('tag:yaml.org,2002:str', token)
                elif key.value in {'account', 'partition', 'extra_args'} and value.tag == 'tag:yaml.org,2002:null':
                    value = yaml.ScalarNode('tag:yaml.org,2002:str', '')
                replacement.append((key, value))
            section.value = replacement
        cfg = loader.construct_object(root, deep=True)
        for section in cfg.values():
            if isinstance(section, dict) and 'time' in section:
                section['time'] = QuotedTime(section['time'])
        return cfg
    finally:
        loader.dispose()


def dump_cluster_config(config):
    return yaml.dump(config, Dumper=ClusterDumper, sort_keys=False, default_flow_style=False)
