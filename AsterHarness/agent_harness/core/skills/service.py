"""Host-side skill sources, independent of Agent filesystem permissions."""

from copy import deepcopy
import hashlib
import os
from pathlib import Path, PurePosixPath
import stat

from . import scan, validate_directories, MAX_FILE_BYTES, MAX_CATALOG_BYTES
import json


def validate_sources(sources):
    if not isinstance(sources, (list, tuple)) or len(sources) > 256:
        raise ValueError('skill_sources must be an array of at most 256 sources')
    result = []
    for source in sources:
        if (not isinstance(source, dict) or set(source) - {'path', 'expose_path', 'label'}
                or 'path' not in source or type(source.get('expose_path', False)) is not bool):
            raise ValueError('Each skill source needs path and optional expose_path')
        if 'label' in source and (not isinstance(source['label'], str) or not 1 <= len(source['label']) <= 128):
            raise ValueError('Skill source label must contain 1–128 characters')
        validate_directories([source['path']])
        result.append({**source, 'expose_path': source.get('expose_path', False)})
    return tuple(result)


class SkillService:
    def __init__(self, workspace, directories=(), sources=(), *, workspace_skills=True, sandbox=None):
        self.workspace = Path(workspace)
        self.sandbox = sandbox
        self.configured = ([{'path': str(self.workspace / 'skills'), 'expose_path': True}]
                           if workspace_skills else [])
        self.configured += [{'path': p, 'expose_path': True} for p in directories]
        self.ordinary_count = len(self.configured)
        self.configured += list(validate_sources(sources))
        self.managed = []
        self.managed_names = set()
        self.loaded = []
        self.excluded = set()
        self.bindings = {}
        self.ordinary_bindings = set()

    def snapshot_state(self):
        return {'loaded': deepcopy(self.loaded), 'excluded': sorted(self.excluded)}

    def restore(self, state):
        if (not isinstance(state, dict) or set(state) != {'loaded', 'excluded'}
                or not isinstance(state['excluded'], list)
                or len(state['excluded']) > 256
                or any(not isinstance(s, str) for s in state['excluded'])):
            raise ValueError('Invalid saved skill sources')
        self.loaded = list(validate_sources(state['loaded']))
        self.excluded = set(state['excluded'])

    def _root(self, source):
        path = Path(source['path']).expanduser()
        if not path.is_absolute():
            path = self.workspace / path
        if path.name == 'SKILL.md':
            path = path.parent
        return path.absolute()

    def _scan(self, source, ordinary=False):
        if ordinary and self.sandbox is not None and self.sandbox.enabled:
            try:
                return self.sandbox.file_operation("skills", roots=[str(self._root(source))])
            except ValueError:
                return {"entries": {}, "errors": [{"path": "(runtime skill source)",
                        "error": "Source is unavailable under current filesystem permissions"}]}
        return scan([self._root(source)])

    def scan(self, _roots=()):
        entries, errors, bindings = {}, [], {}
        ordinary_names = set()
        self.managed_names.clear()
        for index, source in enumerate(self.managed + self.configured + self.loaded, start=-len(self.managed)):
            ordinary = 0 <= index < self.ordinary_count or index >= len(self.configured)
            found = self._scan(source, ordinary)
            if source['expose_path']:
                errors.extend(found['errors'])
            elif found['errors']:
                errors.append({'path': '(skill source)', 'error': 'Some skills could not be loaded'})
            for name, entry in found['entries'].items():
                if index < 0:
                    self.managed_names.add(name)
                if index >= 0 and name in self.excluded:
                    continue
                if name in entries:
                    if bindings[name] == Path(entry["path"]):
                        continue
                    errors.append({'path': source.get('label', '(skill source)'),
                                   'error': 'Duplicate skill ' + name + '; first source retained: ' +
                                            entries[name].get('source', '(configured source)')})
                    continue
                if len(entries) >= 256:
                    if len(errors) < 32:
                        errors.append({'path': '(skill sources)', 'error': 'Skill catalog limit reached (256 entries)'})
                    continue
                visible = deepcopy(entry)
                if source.get('label'):
                    visible['source'] = source['label']
                if not source['expose_path']:
                    visible.pop('path')
                candidate = {**entries, name: visible}
                if len(json.dumps(candidate, ensure_ascii=False).encode()) > MAX_CATALOG_BYTES:
                    if len(errors) < 32:
                        errors.append({'path': '(skill sources)', 'error': 'Skill catalog limit reached (64 KiB)'})
                    continue
                bindings[name] = Path(entry['path'])
                entries[name] = visible
                if ordinary:
                    ordinary_names.add(name)
        self.bindings = bindings
        self.ordinary_bindings = ordinary_names
        return {'entries': entries, 'errors': errors[:32]}

    def prepare_load(self, source, expose_path=False):
        candidate = validate_sources([{'path': source, 'expose_path': expose_path}])[0]
        candidate['path'] = str(self._root(candidate))
        found = self._scan(candidate, True)
        if not found['entries']:
            raise ValueError('Source contains no valid skills')
        return {'action': 'load', 'source': candidate}

    def apply(self, change):
        old = self.snapshot_state()
        try:
            if change['action'] == 'load':
                source = validate_sources([change['source']])[0]
                found = self._scan(source, True)
                if not found['entries']:
                    raise ValueError('Source contains no valid skills')
                self.loaded = [s for s in self.loaded if s['path'] != source['path']]
                self.loaded.append(source)
                if len(self.loaded) > 256:
                    raise ValueError('At most 256 runtime skill sources are supported')
                self.excluded.difference_update(found['entries'])
                ids = sorted(found['entries'])
            elif change['action'] == 'unload':
                skill_id = change['skill_id']
                if skill_id not in self.scan()['entries']:
                    raise ValueError('Unknown skill_id')
                if skill_id in self.managed_names:
                    raise ValueError('This skill is managed by its suite')
                self.excluded.add(skill_id)
                if len(self.excluded) > 256:
                    raise ValueError('At most 256 unloaded skill entries are supported')
                ids = [skill_id]
            else:
                raise ValueError('Unknown skill operation')
            self.scan()
            return {'status': 'applied', 'action': change['action'], 'skill_ids': ids}
        except Exception:
            self.restore(old)
            self.scan()
            raise

    def read(self, skill_id, resource='SKILL.md'):
        self.scan()
        if skill_id not in self.bindings:
            raise ValueError('Unknown skill_id; consult the available skills catalog')
        if not isinstance(resource, str) or not resource or '\\' in resource or '\x00' in resource:
            raise ValueError('resource must be a package-relative path')
        parts = PurePosixPath(resource)
        if parts.is_absolute() or '..' in parts.parts or not parts.parts:
            raise ValueError('resource must stay within the selected skill package')
        # Resolve each component using directory descriptors. Symlinks, FIFOs and
        # traversal cannot turn this dedicated read capability into an arbitrary read.
        root = self.bindings[skill_id].parent
        if skill_id in self.ordinary_bindings and self.sandbox is not None and self.sandbox.enabled:
            import base64
            raw = base64.b64decode(self.sandbox.file_operation(
                'bytes', path=str(root / resource), limit=MAX_FILE_BYTES))
            return {'skill_id': skill_id, 'resource': resource,
                    'sha256': hashlib.sha256(raw).hexdigest(), 'text': raw.decode('utf-8-sig')}
        if os.name == 'nt':
            from agent_harness._windows import read_confined
            raw = read_confined(root / resource, MAX_FILE_BYTES)
            return {'skill_id': skill_id, 'resource': resource,
                    'sha256': hashlib.sha256(raw).hexdigest(), 'text': raw.decode('utf-8-sig')}
        descriptors = []
        try:
            fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            descriptors.append(fd)
            for part in parts.parts[:-1]:
                fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                descriptors.append(fd)
            fd = os.open(parts.parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            descriptors.append(fd)
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError('Skill resource must be a regular text file')
            raw = b''
            while len(raw) <= MAX_FILE_BYTES:
                chunk = os.read(fd, min(65536, MAX_FILE_BYTES + 1 - len(raw)))
                if not chunk:
                    break
                raw += chunk
            if len(raw) > MAX_FILE_BYTES:
                raise ValueError('Skill resource exceeds 256 KiB')
            return {'skill_id': skill_id, 'resource': resource,
                    'sha256': hashlib.sha256(raw).hexdigest(),
                    'text': raw.decode('utf-8-sig')}
        except (OSError, UnicodeError) as exc:
            raise ValueError('Cannot read the selected skill text resource') from exc
        finally:
            for fd in reversed(descriptors):
                os.close(fd)
