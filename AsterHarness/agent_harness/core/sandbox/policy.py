"""Pure path policy decisions shared by validation and execution planning."""
from pathlib import Path

# A tie denies first; specificity is compared before this rank.
TIE = {'read': 0, 'write': 1, 'deny': 2}
AUTHORITY = {'deny': 0, 'read': 1, 'write': 2}


def access_for(path, rules, *, default='deny'):
    path = Path(path)
    candidates = [r for r in rules if r['path'] == '/' or path.is_relative_to(Path(r['path']))]
    return max(candidates, key=lambda r: (0 if r['path'] == '/' else len(Path(r['path']).parts), TIE[r['access']]))['access'] if candidates else default


def covers(granted, requested):
    """An entire requested policy must fit inside explicit granted authority.

    Checking every boundary covers each constant-permission region, including
    restrictions inside a broad grant and exceptions inside those restrictions.
    """
    boundaries = {r['path'] for r in [*granted, *requested]}
    return all(AUTHORITY[access_for(p, requested, default='deny')] <=
               AUTHORITY[access_for(p, granted, default='deny')] for p in boundaries)
