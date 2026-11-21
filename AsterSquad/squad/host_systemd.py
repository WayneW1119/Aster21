"""Explicit full user-systemd authority, exercised outside the agent sandbox."""
import asyncio
import os
if os.name != "nt":
    import pwd
import signal
from pathlib import Path

from relay.store import Problem


PROGRAMS = {name: '/usr/bin/' + name for name in ('systemctl', 'journalctl', 'systemd-run')}
OUTPUT_LIMIT = 64 * 1024


async def execute(program, arguments, timeout=30):
    if os.name == 'nt':
        raise Problem('systemd management is unavailable on Windows', 400)
    if program not in PROGRAMS:
        raise Problem('program 须为 systemctl、journalctl 或 systemd-run')
    if (not isinstance(arguments, list) or len(arguments) > 128
            or any(not isinstance(arg, str) or '\0' in arg or len(arg) > 16384 for arg in arguments)):
        raise Problem('arguments 须为最多 128 个字符串参数，不使用 shell 字符串')
    if type(timeout) is not int or not 1 <= timeout <= 120:
        raise Problem('timeout 须为 1–120 秒；长期任务请启动 systemd 服务')
    # Never target another machine, system manager, image or filesystem root.
    # systemd-run's payload after -- is an ordinary executable argument list.
    options = arguments[:arguments.index('--')] if '--' in arguments else arguments
    blocked = ('--system', '--global', '--host', '--machine', '--root', '--image')
    if any(arg.split('=', 1)[0] in blocked or arg.startswith(('-H', '-M')) for arg in options):
        raise Problem('只允许当前宿主用户的 systemd，不允许切换系统、主机或根目录', 403)
    user = pwd.getpwuid(os.getuid())
    runtime = f'/run/user/{user.pw_uid}'
    env = {'HOME': user.pw_dir, 'USER': user.pw_name, 'LOGNAME': user.pw_name,
           'PATH': '/usr/local/bin:/usr/bin:/bin', 'LANG': 'C.UTF-8',
           'XDG_RUNTIME_DIR': runtime, 'DBUS_SESSION_BUS_ADDRESS': f'unix:path={runtime}/bus',
           'SYSTEMD_PAGER': 'cat', 'SYSTEMD_COLORS': '0', 'PAGER': 'cat'}
    if not Path(PROGRAMS[program]).is_file():
        raise Problem('宿主未安装 ' + program, 409)
    proc = await asyncio.create_subprocess_exec(
        PROGRAMS[program], '--user', *arguments, env=env, cwd=user.pw_dir,
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, start_new_session=True)

    async def capture(stream):
        result = bytearray()
        truncated = False
        while chunk := await stream.read(16384):
            room = OUTPUT_LIMIT - len(result)
            result.extend(chunk[:room])
            truncated |= len(chunk) > room
        return result.decode('utf-8', errors='replace'), truncated

    readers = [asyncio.create_task(capture(stream)) for stream in (proc.stdout, proc.stderr)]
    timed_out = False
    try:
        try:
            await asyncio.wait_for(asyncio.shield(proc.wait()), timeout)
        except asyncio.TimeoutError:
            timed_out = True
            os.killpg(proc.pid, signal.SIGKILL)
            await proc.wait()
        # An attached service may keep a pipe open after its client exits.
        # Bound the whole tool call rather than waiting forever on that pipe.
        try:
            outputs = await asyncio.wait_for(asyncio.gather(*readers), 2)
        except asyncio.TimeoutError:
            outputs = [('', True), ('Output pipe stayed open after command exit.', True)]
        return {'exit_code': proc.returncode, 'stdout': outputs[0][0], 'stderr': outputs[1][0],
                'truncated': any(item[1] for item in outputs), 'timed_out': timed_out}
    finally:
        if proc.returncode is None:
            os.killpg(proc.pid, signal.SIGKILL)
            await proc.wait()
        for reader in readers:
            reader.cancel()
        await asyncio.gather(*readers, return_exceptions=True)
