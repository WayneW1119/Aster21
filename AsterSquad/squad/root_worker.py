"""Owner-enabled root worker startup; no additional agent tools or shell gateway."""
import asyncio
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys

HELPER = Path('/usr/local/libexec/aster-squad-root-worker')
SETTINGS = Path('/etc/aster-squad/root-worker.json')


def available(root=None):
    try:
        if not HELPER.is_file() or HELPER.stat().st_uid != 0 or HELPER.stat().st_mode & 0o022:
            return False
        info = SETTINGS.stat()
        if info.st_uid != 0 or info.st_mode & 0o022:
            return False
        settings = json.loads(SETTINGS.read_text())
        base = Path(settings['data_root']).resolve()
        path = Path(root).resolve() if root is not None else base
        return settings['uid'] == os.getuid() and (path == base or path.parent == base/'instances' and bool(re.fullmatch('slot-[0-9]+', path.name)))
    except (OSError, ValueError, KeyError, TypeError):
        return False


async def check(root=None):
    from relay.store import Problem
    if not available(root):
        raise Problem('宿主尚未安装 root 启动器；请由管理员按部署说明安装', 409)
    proc = await asyncio.create_subprocess_exec('/usr/bin/sudo', '-n', '--', str(HELPER), 'check',
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    try:
        code = await asyncio.wait_for(proc.wait(), 5)
    except asyncio.TimeoutError:
        proc.kill(); await proc.wait(); code = 1
    if code:
        raise Problem('宿主 root 启动器不可用，请检查管理员安装与免交互启动授权', 409)


def command(config):
    return ['/usr/bin/sudo', '-n', '--', str(HELPER), 'run', str(config)]


async def release(config):
    from relay.store import Problem
    await check()
    proc = await asyncio.create_subprocess_exec('/usr/bin/sudo', '-n', '--', str(HELPER), 'release', str(config),
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    # Do not kill a privileged launcher during ownership handoff.
    if await proc.wait():
        raise Problem('root 运行数据尚未交还普通用户，配置未更改', 409)


def restore_ownership(home, uid, gid):
    """Only internal runtime data; never follow symlinks or alter workspace files."""
    home = Path(home)
    for folder, dirs, files in os.walk(home, followlinks=False):
        dirs[:] = [name for name in dirs if not (Path(folder)/name).is_symlink()
                   and not os.path.ismount(Path(folder)/name)]
        for path in [Path(folder), *(Path(folder)/name for name in files)]:
            info = path.lstat()
            if info.st_uid == 0 and not stat.S_ISLNK(info.st_mode) and (stat.S_ISDIR(info.st_mode) or info.st_nlink == 1):
                os.chown(path, uid, gid, follow_symlinks=False)


def validated_config(path, settings):
    path = Path(path).resolve(strict=True)
    if path.name != 'worker.json' or path.parent.parent.name != 'agents' or path.parents[2].name != 'control':
        raise ValueError('Not a Squad worker configuration')
    path.relative_to(Path(settings['data_root']).resolve())
    data = json.loads(path.read_text())
    if not re.fullmatch('[a-f0-9]{32}', path.parent.name) or data['id'] != path.parent.name:
        raise ValueError('Worker identity mismatch')
    info = path.stat()
    if info.st_uid != settings['uid'] or info.st_mode & 0o022:
        raise ValueError('Worker configuration must belong to the configured host user')
    root = path.parents[3]
    base = Path(settings['data_root']).resolve()
    if root != base and (root.parent != base/'instances' or not re.fullmatch('slot-[0-9]+', root.name)):
        raise ValueError('Not an authorized Squad root')
    home = Path(data['home']).resolve()
    if home.parent != root/'runtime' or not re.fullmatch('slot-[0-9]{2}', home.name):
        raise ValueError('Unexpected runtime directory')
    if Path(data['workspace']).resolve() != root/'workspaces'/home.name or data['mode'] != 'real':
        raise ValueError('Unexpected workspace or mode')
    import sqlite3
    with sqlite3.connect('file:'+str(root/'control/squad.db')+'?mode=ro', uri=True) as db:
        row = db.execute('SELECT slot, archived, config FROM agents WHERE id=?', (data['id'],)).fetchone()
    if not row or row[1] or home.name != 'slot-%02d' % row[0]:
        raise ValueError('Unknown or archived member')
    data['run_as_root'] = json.loads(row[2]).get('run_as_root') is True and data.get('run_as_root') is True
    return data, home


def main():
    if os.geteuid() != 0:
        raise ValueError('Root launcher must run as root')
    info = SETTINGS.stat()
    if info.st_uid != 0 or info.st_mode & 0o022:
        raise ValueError('Root launcher settings must be administrator-owned')
    settings = json.loads(SETTINGS.read_text())
    if int(os.environ.get('SUDO_UID', '-1')) != settings['uid']:
        raise ValueError('Caller is not the configured host user')
    action = sys.argv[1] if len(sys.argv) > 1 else ''
    if action == 'check' and len(sys.argv) == 2:
        if not Path(settings['python']).is_file():
            raise ValueError('Worker Python is unavailable')
        return
    if action not in ('run', 'release') or len(sys.argv) != 3:
        raise ValueError('Expected check, run CONFIG, or release CONFIG')
    data, home = validated_config(sys.argv[2], settings)
    # Refuse to change files belonging to a live Thread.
    import fcntl
    for path in (home/'threads').glob('*/rollout.jsonl'):
        with path.open() as stream:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
    restore_ownership(home, settings['uid'], settings['gid'])
    if action == 'release':
        return
    if data.get('run_as_root') is not True:
        raise ValueError('This worker has no root grant')
    # One bootstrap line carries only the already-selected worker environment.
    # stdin subsequently remains the normal Squad lifecycle/message channel.
    with os.fdopen(os.dup(0), 'rb', buffering=0) as stream:
        line = stream.readline(1024*1024 + 1)
    if len(line) > 1024*1024 or not line.endswith(b'\n'):
        raise ValueError('Invalid worker environment')
    inherited = json.loads(line)
    if not isinstance(inherited, dict) or any(not isinstance(k, str) or not isinstance(v, str) or '\0' in v for k,v in inherited.items()):
        raise ValueError('Invalid worker environment')
    env = {k:v for k,v in inherited.items() if re.fullmatch('[A-Z][A-Z0-9_]*', k)
           and k not in {'HOME','USER','LOGNAME','PATH','ENV','BASH_ENV','SHELLOPTS','CDPATH'}
           and not k.startswith(('PYTHON','LD_','DYLD_','WSL_'))}
    env.update(HOME='/root', USER='root', LOGNAME='root', LANG='C.UTF-8',
        PATH=str(Path(settings['python']).parent)+':/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin',
        PYTHONPATH=os.pathsep.join(settings.get('pythonpath', [])), PYTHONNOUSERSITE='1',
        SQUAD_ROOT_OWNER_UID=str(settings['uid']), SQUAD_ROOT_OWNER_GID=str(settings['gid']))
    proc = subprocess.Popen([settings['python'], '-P', '-m', 'squad.worker', '--config', str(Path(sys.argv[2]).resolve())],
        cwd=data['workspace'], env=env)
    def forward(signum, frame):
        if proc.poll() is None:
            proc.send_signal(signum)
    signal.signal(signal.SIGTERM, forward)
    signal.signal(signal.SIGINT, forward)
    try:
        code = proc.wait()
    finally:
        restore_ownership(home, settings['uid'], settings['gid'])
    raise SystemExit(code if code >= 0 else 128-code)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        # No environment, credentials, request data, or provider bodies in errors.
        print('Root worker startup failed: '+type(error).__name__, file=sys.stderr)
        raise SystemExit(1)
