#!/usr/bin/env python3
"""Pair, validate and namespace the declared neighbor FASTA/GFF inputs.

Only identity-bearing columns change. FASTA sequence lines are streamed verbatim;
GFF coordinates, phase and unrelated attributes remain untouched. No files are
published until all pairs and relationship graphs validate. Explicit config:
Input.liftoff.pairs: {reference_name: {fasta: filename_or_path, gff: filename_or_path}}
Names must uniquely identify files in the declared Snakemake input inventory.
"""
from collections import defaultdict, deque
import csv
import hashlib
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import quote, unquote

FASTA_EXTENSIONS = ('.fa', '.fasta', '.fna', '.fas', '.fsa', '.seq')
GFF_EXTENSIONS = ('.gff', '.gff3')
IDENTITY_ATTRIBUTES = frozenset(('ID', 'Parent', 'Derives_from'))


def find_references(path, extensions):
    """Discover supported files once; these exact paths become rule inputs."""
    path = Path(path)
    if path.is_file():
        return [str(path)] if path.suffix in extensions else []
    return sorted(str(Path(root)/name) for root, _, names in os.walk(path)
                  for name in names if Path(name).suffix in extensions)


def _inventory(paths, extensions):
    result = set()
    for value in paths:
        path = Path(value).absolute()
        if path.suffix not in extensions or not path.is_file():
            raise ValueError(f'Unsupported or missing neighbor input: {path}')
        if path in result:
            raise ValueError(f'Duplicate neighbor input: {path}')
        result.add(path)
    return result


def _explicit_path(value, inventory):
    if not isinstance(value, str) or not value:
        raise ValueError('Input.liftoff.pairs needs a nonempty fasta and gff filename/path')
    path = Path(value)
    exact = path.absolute()
    matches = [p for p in inventory if p == exact or (len(path.parts) == 1 and p.name == value)]
    if len(matches) != 1:
        raise ValueError(f'Input.liftoff.pairs file {value!r} must match exactly one declared input; '
                         f'found {len(matches)}. Use its full path if basenames repeat.')
    return matches[0]


def pair_references(fastas, gffs, pairs=None):
    """Exact stems, then unambiguous .genome/.gene stems; never prefix guesses."""
    remaining_fa = _inventory(fastas, FASTA_EXTENSIONS)
    remaining_gf = _inventory(gffs, GFF_EXTENSIONS)
    matched = []
    if pairs is not None and not isinstance(pairs, dict):
        raise ValueError('Input.liftoff.pairs must map reference names to {fasta: ..., gff: ...}')
    for identity, entry in (pairs or {}).items():
        if not isinstance(identity, str) or not identity or not isinstance(entry, dict) or set(entry) != {'fasta', 'gff'}:
            raise ValueError('Input.liftoff.pairs must map nonempty reference names to {fasta: ..., gff: ...}')
        fa = _explicit_path(entry['fasta'], remaining_fa)
        gf = _explicit_path(entry['gff'], remaining_gf)
        matched.append((identity, fa, gf))
        remaining_fa.remove(fa); remaining_gf.remove(gf)
    for legacy in (False, True):
        fa_by_stem, gf_by_stem = defaultdict(list), defaultdict(list)
        for path in remaining_fa:
            stem = path.stem.removesuffix('.genome') if legacy else path.stem
            fa_by_stem[stem].append(path)
        for path in remaining_gf:
            stem = path.stem.removesuffix('.gene') if legacy else path.stem
            gf_by_stem[stem].append(path)
        for identity in sorted(fa_by_stem.keys() & gf_by_stem.keys()):
            if len(fa_by_stem[identity]) == len(gf_by_stem[identity]) == 1:
                fa, gf = fa_by_stem[identity][0], gf_by_stem[identity][0]
                matched.append((identity, fa, gf))
                remaining_fa.remove(fa); remaining_gf.remove(gf)
    if remaining_fa or remaining_gf:
        names = '\n  '.join(str(p) for p in sorted(remaining_fa | remaining_gf))
        raise ValueError('Unpaired or ambiguous neighbor files:\n  ' + names +
                         '\nUse matching stems, unambiguous .genome/.gene names, or Input.liftoff.pairs.')
    if not matched:
        raise ValueError('No paired neighbor FASTA/GFF references; check Input.liftoff paths and extensions.')
    identities = [identity for identity, _, _ in matched]
    if len(set(identities)) != len(identities):
        raise ValueError('Ambiguous reference identities; assign distinct names using Input.liftoff.pairs.')
    bases = {identity: re.sub('_+', '_', re.sub(r'[^A-Za-z0-9_.-]', '_', identity)) or 'reference'
             for identity in identities}
    counts = defaultdict(int)
    for base in bases.values():
        counts[base] += 1
    namespaces = {identity: (base + '_' + hashlib.sha256(identity.encode()).hexdigest()[:12]
                             if counts[base] > 1 else base) for identity, base in bases.items()}
    if len(set(namespaces.values())) != len(namespaces):
        raise ValueError('Reference namespace collision; assign distinct names using Input.liftoff.pairs.')
    return [(identity, namespaces[identity], fa, gf) for identity, fa, gf in sorted(matched)]


def _copy_fasta(path, namespace, output, global_seqids):
    ids = set()
    current, length = None, 0
    last = b''
    with path.open('rb') as handle:
        for number, line in enumerate(handle, 1):
            if line.startswith(b'>'):
                if current is not None and not length:
                    raise ValueError(f'{path}:{number}: empty FASTA sequence {current!r}')
                match = re.match(rb'>([^\s]+)', line)
                if not match:
                    raise ValueError(f'{path}:{number}: empty FASTA identifier')
                current = match[1].decode('utf-8')
                if current in ids:
                    raise ValueError(f'{path}:{number}: duplicate FASTA seqid {current!r}')
                ids.add(current)
                qualified = quote(namespace + '__' + current, safe='.:^*$@!+_?-|')
                if qualified in global_seqids:
                    raise ValueError(f'Global FASTA seqid collision: {qualified}')
                global_seqids.add(qualified)
                output.write(b'>' + qualified.encode() + line[match.end():])
                length = 0
            else:
                if line.strip() and current is None:
                    raise ValueError(f'{path}:{number}: sequence before FASTA header')
                length += len(line.strip())
                output.write(line)
            last = line
    if current is None or not length:
        raise ValueError(f'{path}: empty FASTA reference or sequence')
    # A missing terminal newline must not fuse the next reference header to bases.
    if not last.endswith(b'\n'):
        output.write(b'\n')
    return ids


def _decode(value, location):
    if not value or re.search(r'%(?![0-9a-fA-F]{2})', value):
        raise ValueError(f'{location}: empty or malformed escaped identifier {value!r}')
    return unquote(value, errors='strict')


def _attributes(text, namespace, location):
    attrs, rewritten, seen = {}, [], set()
    if text == '.':
        return attrs, text
    for entry in text.split(';'):
        if not entry:
            rewritten.append(entry)
            continue
        key, separator, value = entry.partition('=')
        if not separator or not key or key in seen:
            raise ValueError(f'{location}: malformed or duplicate GFF attribute {entry!r}')
        seen.add(key)
        if key in IDENTITY_ATTRIBUTES:
            values = [_decode(token, location) for token in value.split(',')]
            if key == 'ID' and len(values) != 1:
                raise ValueError(f'{location}: ID must contain one identifier')
            attrs[key] = values
            value = ','.join(quote(namespace + '__' + token, safe='.:^*$@!+_?-|') for token in values)
            entry = key + '=' + value
        rewritten.append(entry)
    return attrs, ';'.join(rewritten)


def _validate_graph(identities, relations, path):
    graph = {identity: set() for identity in identities}
    for child, kind, target, line in relations:
        if target not in identities:
            raise ValueError(f'{path}:{line}: dangling {kind}={target!r}')
        if child is not None:
            graph[child].add(target)
    # Kahn traversal avoids recursion limits on large/deep annotation graphs.
    incoming = dict.fromkeys(graph, 0)
    for targets in graph.values():
        for target in targets:
            incoming[target] += 1
    ready = deque(node for node, count in incoming.items() if count == 0)
    visited = 0
    while ready:
        node = ready.popleft(); visited += 1
        for target in graph[node]:
            incoming[target] -= 1
            if incoming[target] == 0:
                ready.append(target)
    if visited != len(graph):
        raise ValueError(f'{path}: cycle in Parent/Derives_from relationships')


def _copy_gff(path, namespace, seqids, output, global_ids):
    identities, relations = {}, []
    count = 0
    with path.open(encoding='utf-8', newline=None) as handle:
        for number, line in enumerate(handle, 1):
            text = line.rstrip('\r\n')
            if text == '##FASTA':
                break
            if not text or text.startswith('#'):
                continue
            location = f'{path}:{number}'
            fields = text.split('\t')
            if len(fields) != 9:
                raise ValueError(f'{location}: expected 9 GFF columns, found {len(fields)}')
            seqid = _decode(fields[0], location)
            if seqid not in seqids:
                raise ValueError(f'{location}: GFF seqid {seqid!r} absent from paired FASTA')
            try:
                start, end = int(fields[3]), int(fields[4])
            except ValueError as exc:
                raise ValueError(f'{location}: invalid GFF coordinates') from exc
            if not 1 <= start <= end or fields[7] not in ('.', '0', '1', '2') or fields[6] not in ('+', '-', '.', '?'):
                raise ValueError(f'{location}: invalid GFF coordinates, phase or strand')
            attrs, fields[8] = _attributes(fields[8], namespace, location)
            identity = attrs.get('ID', [None])[0]
            if identity is not None:
                # Discontinuous features may repeat their ID, coordinates/phase and
                # parent lists may differ. A different landmark/type/strand is not
                # the same feature and must not silently merge into one identity.
                signature = seqid, fields[2], fields[6]
                if identity in identities and identities[identity] != signature:
                    raise ValueError(f'{location}: conflicting duplicate ID {identity!r}')
                if identity not in identities:
                    qualified = namespace + '__' + identity
                    if qualified in global_ids:
                        raise ValueError(f'{location}: global feature ID collision {qualified!r}')
                    global_ids.add(qualified)
                    identities[identity] = signature
            for kind in ('Parent', 'Derives_from'):
                relations.extend((identity, kind, target, number) for target in attrs.get(kind, []))
            # FASTA and GFF must use the exact same qualified landmark token.
            fields[0] = quote(namespace + '__' + seqid, safe='.:^*$@!+_?-|')
            output.write('\t'.join(fields) + '\n')
            count += 1
    _validate_graph(identities, relations, path)
    return count


def _publish(staged, destinations, folder):
    """Publish complete files, restoring the old set on a catchable I/O failure.

    Separate filenames cannot be replaced atomically as a group. Snakemake waits
    for this rule before starting consumers. Each replacement is atomic; backups
    permit rollback on exceptions (not machine failure or SIGKILL).
    """
    # bedtools and Liftoff/pyfaidx cache byte offsets beside the derived FASTA.
    # Keeping that index after replacing FASTA can silently extract wrong bases.
    # This is our output companion, never an original reference's index.
    fasta_index = Path(str(destinations[0]) + '.fai')
    backups, published = {}, []
    for index, destination in enumerate([*destinations, fasta_index]):
        if destination.exists():
            backup = folder/f'previous-{index}'
            os.link(destination, backup)
            backups[destination] = backup
    try:
        if fasta_index in backups:
            fasta_index.unlink()
            published.append(fasta_index)
        for source, destination in zip(staged, destinations):
            os.replace(source, destination)
            published.append(destination)
    except BaseException:
        for destination in reversed(published):
            if destination in backups:
                os.replace(backups[destination], destination)
            else:
                destination.unlink()
        raise


def prepare_references(fastas, gffs, fasta_output, gff_output, pairs_output, pairs=None):
    """Prepare exactly the supplied inventory; originals are opened read-only."""
    references = pair_references(fastas, gffs, pairs)
    destinations = [Path(path).absolute() for path in (fasta_output, gff_output, pairs_output)]
    if len(set(destinations)) != 3 or len({p.parent for p in destinations}) != 1:
        raise ValueError('Combined FASTA, GFF and provenance must be distinct files in one output directory')
    inputs = {path.resolve() for _, _, fa, gf in references for path in (fa, gf)}
    if any(path.resolve() in inputs for path in destinations):
        raise ValueError('Combined outputs must not overwrite neighbor inputs')
    parent = destinations[0].parent
    parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.prepare-liftoff-', dir=parent) as temporary:
        folder = Path(temporary)
        staged = [folder/path.name for path in destinations]
        global_seqids, global_ids, total = set(), set(), 0
        with staged[0].open('wb') as fa_out, staged[1].open('w', encoding='utf-8', newline='\n') as gf_out, staged[2].open('w', encoding='utf-8', newline='') as manifest:
            writer = csv.writer(manifest, delimiter='\t', lineterminator='\n')
            writer.writerow(('reference', 'namespace', 'fasta', 'gff', 'sequences', 'features'))
            for identity, namespace, fa, gf in references:
                seqids = _copy_fasta(fa, namespace, fa_out, global_seqids)
                count = _copy_gff(gf, namespace, seqids, gf_out, global_ids)
                total += count
                writer.writerow((identity, namespace, fa, gf, len(seqids), count))
            if not total:
                raise ValueError('Combined GFF is empty: no annotation features in paired references')
            for handle in (fa_out, gf_out, manifest):
                handle.flush(); os.fsync(handle.fileno())
        _publish(staged, destinations, folder)
