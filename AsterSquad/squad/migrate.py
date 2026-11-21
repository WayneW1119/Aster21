"""Explicit offline data-root migration, without rewriting agent history/files."""
import argparse
import os
from contextlib import ExitStack
from datetime import datetime, timezone
try:
    import fcntl
except ImportError:
    from squad import _windows_locks as fcntl
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import tomllib

import toml


def rebase(value, source, destination):
    if isinstance(value, str):
        if value == str(source) or value.startswith(str(source) + '/'):
            return str(destination) + value[len(str(source)):]
    elif isinstance(value, list):
        return [rebase(v, source, destination) for v in value]
    elif isinstance(value, dict):
        return {k: rebase(v, source, destination) for k, v in value.items()}
    return value


def fingerprint(root):
    result = {}
    for path in root.rglob('*'):
        if path.is_symlink():
            result[str(path.relative_to(root))] = ['link', str(path.readlink())]
        elif path.is_file():
            digest = hashlib.sha256()
            with path.open('rb') as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                    digest.update(chunk)
            result[str(path.relative_to(root))] = digest.hexdigest()
    return result


def migrate(source, destination, *, apply=False, backup=None):
    if os.name == 'nt':
        raise ValueError('Windows preview does not support legacy root migration; use a new Windows data directory')
    source = Path(source).expanduser().absolute()
    destination = Path(destination).expanduser().absolute()
    if source.is_symlink() or destination.exists() or destination.is_symlink():
        raise ValueError('Source must not be a symlink; destination must not exist')
    if not source.is_dir() or source == destination or source in destination.parents or destination in source.parents:
        raise ValueError('Source and destination must be separate data directories')
    # Do not let an alias cause recursive copies or ambiguous path updates.
    if source.resolve() != source or destination.parent.resolve() != destination.parent:
        raise ValueError('Migration paths must not cross symlinks')
    roots = [source, *sorted((source / 'instances').glob('slot-*'))]
    plan = {'source': str(source), 'destination': str(destination),
            'squad_databases': sum((r / 'control/squad.db').exists() for r in roots),
            'threads': sum(len(list((r / 'runtime').glob('**/thread.toml'))) for r in roots)}
    if not apply:
        return {**plan, 'applied': False}
    stamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')
    backup = Path(backup).expanduser().absolute() if backup else destination / 'control/migration-backups' / stamp
    if (backup.exists() or backup in (source, destination) or source in backup.parents
            or destination in backup.parents and not backup.is_relative_to(destination / 'control')):
        raise ValueError('Backup must be a new directory outside source and destination')
    stage = destination.with_name(destination.name + '.migration-staging')
    if stage.exists():
        raise ValueError('Migration staging already exists; inspect it before retrying')
    with ExitStack() as locks:
        # Keep every old writer locked throughout copying and switching roots.
        files = [source / 'control/console.lock', *source.glob('**/rollout.jsonl')]
        for path in files:
            if path.exists():
                stream = locks.enter_context(path.open('r', encoding="utf-8"))
                try:
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise ValueError('Stop the console and all agents before migration') from None
        before = fingerprint(source)
        shutil.copytree(source, stage, symlinks=True)
        stage.chmod(0o700)
        if fingerprint(stage) != before:
            raise ValueError('Copied data verification failed; source is unchanged')
        staged_roots = [stage, *sorted((stage / 'instances').glob('slot-*'))]
        for root in staged_roots:
            for path in (root / 'control').glob('**/*.json'):
                # Current host configuration only; not chat, memory or workspace.
                data = json.loads(path.read_text(encoding="utf-8"))
                updated = rebase(data, source, destination)
                if updated != data:
                    path.write_text(json.dumps(updated, ensure_ascii=False, indent=2) + '\n', encoding="utf-8")
            for path in (root / 'runtime').glob('**/thread.toml'):
                data = tomllib.loads(path.read_text(encoding="utf-8"))
                updated = rebase(data, source, destination)
                if updated != data:
                    path.write_text(toml.dumps(updated), encoding="utf-8")
            path = root / 'control/squad.db'
            if path.exists():
                with sqlite3.connect(path) as db:
                    for ident, config in db.execute('SELECT id, config FROM agents').fetchall():
                        data = json.loads(config)
                        # Identity text is agent configuration, not a deployment path.
                        data['grants'] = rebase(data.get('grants', []), source, destination)
                        db.execute('UPDATE agents SET config=? WHERE id=?', (json.dumps(data, ensure_ascii=False), ident))
        path = stage / 'control/fleet/console.db'
        if path.exists():
            with sqlite3.connect(path) as db:
                for ident, root in db.execute('SELECT id, root FROM resources').fetchall():
                    db.execute('UPDATE resources SET root=? WHERE id=?', (rebase(root, source, destination), ident))
        # Ensure history and all workspace content are exact copies after edits.
        after = fingerprint(stage)
        immutable = {k: v for k, v in before.items() if 'workspaces' in Path(k).parts or k.endswith('rollout.jsonl')}
        if any(after.get(k) != v for k, v in immutable.items()):
            raise ValueError('Immutable data verification failed; source is unchanged')
        stage.rename(destination)
        try:
            backup.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            source.rename(backup)
            try:
                source.symlink_to(destination, target_is_directory=True)
            except BaseException:
                backup.rename(source)
                raise
        except BaseException:
            destination.rename(stage)
            raise
    report = {**plan, 'applied': True, 'backup': str(backup), 'compatibility_link': str(source),
              'verified_files': len(before), 'immutable_files': len(immutable)}
    (destination / 'control/migration.json').write_text(json.dumps(report, indent=2) + '\n', encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True)
    parser.add_argument('--destination', required=True)
    parser.add_argument('--backup')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    print(json.dumps(migrate(args.source, args.destination, apply=args.apply, backup=args.backup), indent=2))


if __name__ == '__main__':
    main()
