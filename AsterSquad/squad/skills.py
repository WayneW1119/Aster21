"""Host-managed shared skill catalogs; never populate an agent workspace."""
import json
import hashlib
from pathlib import Path
import os
import tempfile


# Only the known entry shipped before the product guides were consolidated.
# Custom entries are never silently removed during an upgrade.
RETIRED_BUILTINS = {
    'aster-squad': '12c4247d4a94dcc4c39331b54db1f3c2b23f18408df29901680ffbf45650d14a',
}


def retire_builtins(root, retired=RETIRED_BUILTINS):
    root = Path(root)
    for name, expected in retired.items():
        source = root / 'skills/global' / name
        if not source.exists():
            continue
        entry = source / 'SKILL.md'
        if (source.is_symlink() or not entry.is_file() or entry.is_symlink()
                or hashlib.sha256(entry.read_bytes()).hexdigest() != expected):
            raise ValueError('Retired built-in was customized; review before merging: ' + name)
        archive = root / 'control/retired-skills'
        archive.mkdir(parents=True, exist_ok=True, mode=0o700)
        folder = Path(tempfile.mkdtemp(prefix=name + '-', dir=archive))
        source.rename(folder / name)


def install_builtins(root):
    from aster.skills import builtin_directory
    import relay
    from .service import ASSETS
    destination = Path(root) / 'skills/global'
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    retire_builtins(root)
    packages = [ASSETS / 'skills', Path(relay.__file__).parent / 'assets/skills',
                builtin_directory()]
    for package in packages:
        if not package.is_dir():
            continue
        for source in package.iterdir():
            if source.is_dir() and (source / 'SKILL.md').is_file():
                for item in source.rglob('*'):
                    if not item.is_file() or item.is_symlink():
                        continue
                    target = destination / source.name / item.relative_to(source)
                    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    raw = item.read_bytes()
                    if target.exists() and target.read_bytes() == raw:
                        continue
                    fd, temporary = tempfile.mkstemp(dir=target.parent)
                    try:
                        with os.fdopen(fd, 'wb') as stream:
                            stream.write(raw)
                        os.replace(temporary, target)
                    finally:
                        if os.path.exists(temporary):
                            os.unlink(temporary)
    return destination


def sources(root, shared_root, agent_id):
    """Registered sources are host configuration, not agent runtime load requests."""
    root, shared_root = Path(root), Path(shared_root)
    result = []
    # Global first: workspace copies cannot shadow system-maintained guides.
    for scope, path in [('global', shared_root / 'skills/global'),
                        ('space', root / 'skills/space'),
                        ('agent', root / 'skills/agents' / (agent_id or '_pending'))]:
        if scope == 'agent' and agent_id is None:
            continue
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        result.append({'path': str(path), 'expose_path': False, 'label': scope})
    for scope, base in [('global', shared_root), ('space', root)]:
        manifest = base / 'skills/sources.json'
        if manifest.exists():
            from aster.skills import validate_sources
            extra = validate_sources(json.loads(manifest.read_text(encoding="utf-8")))
            for source in extra:
                path = Path(source['path']).expanduser()
                if not path.is_absolute() or not path.is_dir() or path.resolve() != path.absolute():
                    raise ValueError('Registered skill roots must be existing absolute directories without symlinks')
                result.append({**source, 'expose_path': False,
                               'label': source.get('label', scope + '-external')})
    # A root Squad has the same global and space manifest; don't register twice.
    unique = {}
    for source in result:
        unique.setdefault(source['path'], source)
    return list(unique.values())
