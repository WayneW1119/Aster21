"""Local SKILL.md discovery; guides are data, not executable registrations."""

import hashlib
import os
import re
from pathlib import Path

import yaml

MAX_FILE_BYTES = 256 * 1024
MAX_FILES = 256
MAX_CATALOG_BYTES = 64 * 1024
NAME = re.compile(r'[a-z0-9]+(?:-[a-z0-9]+)*\Z')


def validate_directories(value):
    if (not isinstance(value, (list, tuple)) or any(
            not isinstance(path, str) or not path.strip() or '\x00' in path for path in value)):
        raise ValueError('skill_directories must be a list of nonempty directory paths')
    return tuple(dict.fromkeys(value))


def scan(roots):
    """Deterministic, bounded discovery; malformed skills do not hide valid peers.

    Roots are host configuration. Links below roots are skipped. Earlier roots,
    then sorted relative paths, win duplicate names. No scripts are executed.
    """
    entries, errors, count, directories = {}, [], 0, 0
    def error(path, reason):
        if len(errors) < 32:
            errors.append({'path': str(path), 'error': reason})
    for configured in dict.fromkeys(Path(root).absolute() for root in roots):
        if not configured.exists():
            continue
        if configured.is_symlink() or not configured.is_dir():
            error(configured, 'Skill root must be a real directory')
            continue
        root = configured.resolve()
        for directory, dirs, files in os.walk(root, followlinks=False,
                onerror=lambda exc: error(exc.filename or root, 'Cannot scan directory')):
            directories += 1
            if directories > 4096:
                error(root, 'Skill discovery limit reached (4096 directories)')
                return {'entries': entries, 'errors': errors}
            dirs[:] = sorted(name for name in dirs if not name.startswith('.')
                             and not (Path(directory) / name).is_symlink())
            if len(Path(directory).relative_to(root).parts) >= 8:
                dirs[:] = []
            if 'SKILL.md' not in files:
                continue
            path = Path(directory) / 'SKILL.md'
            count += 1
            if count > MAX_FILES:
                error(root, f'Skill discovery limit reached ({MAX_FILES} files)')
                return {'entries': entries, 'errors': errors}
            try:
                if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root):
                    raise ValueError('SKILL.md must stay inside its root without symlinks')
                with path.open('rb') as stream:
                    raw = stream.read(MAX_FILE_BYTES + 1)
                if len(raw) > MAX_FILE_BYTES:
                    raise ValueError('SKILL.md exceeds 256 KiB')
                text = raw.decode('utf-8-sig').replace('\r\n', '\n')
                lines = text.splitlines()
                if not lines or lines[0] != '---':
                    raise ValueError('SKILL.md must start with YAML frontmatter')
                end = next((i for i in range(1, len(lines)) if lines[i] == '---'), None)
                if end is None:
                    raise ValueError('Missing closing frontmatter delimiter')
                metadata = yaml.safe_load('\n'.join(lines[1:end]))
                if not isinstance(metadata, dict):
                    raise ValueError('Frontmatter must be a mapping')
                name, description = metadata.get('name'), metadata.get('description')
                if not isinstance(name, str) or len(name) > 64 or not NAME.fullmatch(name):
                    raise ValueError('name must be 1–64 lowercase letters/digits separated by hyphens')
                if not isinstance(description, str) or not description.strip() or len(description) > 1024:
                    raise ValueError('description must contain 1–1024 characters')
                if not '\n'.join(lines[end + 1:]).strip():
                    raise ValueError('SKILL.md must contain instructions after frontmatter')
                if name in entries:
                    raise ValueError(f'Duplicate skill name: {name}; earlier root/path wins')
                candidate = {'description': ' '.join(description.split()), 'path': str(path),
                             'sha256': hashlib.sha256(raw).hexdigest()}
                # JSON accounting also bounds paths and multibyte descriptions.
                import json
                prospective = {**entries, name: candidate}
                if len(json.dumps(prospective, ensure_ascii=False).encode()) > MAX_CATALOG_BYTES:
                    raise ValueError('Skill catalog exceeds 64 KiB; shorten or split configured directories')
                entries[name] = candidate
            except (OSError, ValueError, yaml.YAMLError, RecursionError) as exc:
                # Do not copy parser excerpts from guide contents into the catalog.
                reason = str(exc) if type(exc) is ValueError else 'Cannot read UTF-8 skill or parse safe YAML'
                error(path, reason[:200])
    return {'entries': entries, 'errors': errors}
