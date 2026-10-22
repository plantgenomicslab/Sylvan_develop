"""Slurm duration semantics, independent of YAML and scheduler availability."""
import re

TIME_PATTERN = re.compile(r'(?:[0-9]+(?::[0-9]+){0,2}|[0-9]+-[0-9]+(?::[0-9]+){0,2})\Z')


def time_token(value):
    """Validate a request without changing its units or lexical representation."""
    token = str(value)
    if not TIME_PATTERN.fullmatch(token):
        raise ValueError(
            f"Invalid Slurm time {token!r}; use minutes, minutes:seconds, "
            "hours:minutes:seconds, days-hours[:minutes[:seconds]], or 0 "
            "for unlimited. Quote time values in YAML."
        )
    return token


def time_seconds(value):
    """Return seconds for a request or sinfo limit; None means unlimited."""
    if str(value).upper() in {'INFINITE', 'UNLIMITED'}:
        return None
    value = time_token(value)
    if '-' in value:
        day, clock = value.split('-')
        fields = [int(part) for part in clock.split(':')]
        fields += [0] * (3 - len(fields))
        total = int(day) * 86400 + fields[0] * 3600 + fields[1] * 60 + fields[2]
    else:
        fields = [int(part) for part in value.split(':')]
        if len(fields) == 1:
            total = fields[0] * 60
        elif len(fields) == 2:
            total = fields[0] * 60 + fields[1]
        else:
            total = fields[0] * 3600 + fields[1] * 60 + fields[2]
    return total or None


def format_seconds(seconds):
    """Format a computed duration; never use this to reinterpret a YAML integer."""
    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f'{days}-{hours:02d}:{minutes:02d}:{seconds:02d}'
