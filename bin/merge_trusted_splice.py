#!/usr/bin/env python3
"""Union PsiCLASS bin junction records without changing coordinates or support."""

import argparse
import os
from pathlib import Path
import tempfile


def merge(inputs, output):
    records = set()
    for source in inputs:
        with open(source) as handle:
            for number, line in enumerate(handle, 1):
                fields = tuple(line.split())
                if not fields:
                    continue
                try:
                    start, end = int(fields[1]), int(fields[2])
                    valid = len(fields) >= 5 and start >= 0 and end > start and fields[4] in ("+", "-", ".")
                except (ValueError, IndexError):
                    valid = False
                if not valid:
                    raise ValueError("{}:{}: invalid trusted-splice record".format(source, number))
                records.add(fields)
    destination = Path(output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    tempname = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=str(destination.parent), delete=False) as handle:
            tempname = handle.name
            for fields in sorted(records, key=lambda r: (r[0], int(r[1]), int(r[2]), r[4], r)):
                handle.write("\t".join(fields) + "\n")
        os.replace(tempname, destination)
    finally:
        if tempname and os.path.exists(tempname):
            os.unlink(tempname)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("inputs", nargs="*")
    args = parser.parse_args()
    merge(args.inputs, args.output)
