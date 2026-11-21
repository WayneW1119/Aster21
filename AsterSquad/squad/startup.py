"""Owner-controlled login startup for an explicitly registered host launcher."""
from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path
import re
import subprocess
import sys

from relay.store import Problem


class Startup:
    def __init__(self, *, unit='', desktop_root=''):
        if unit and not re.fullmatch(r'[A-Za-z0-9_.@-]+\.service', unit):
            raise ValueError('startup unit must be a user service name')
        self.unit = unit
        self.desktop_root = Path(desktop_root).resolve() if desktop_root else None
        self.lock = asyncio.Lock()

    async def systemctl(self, *arguments):
        try:
            process = await asyncio.create_subprocess_exec('systemctl', '--user', *arguments,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(), 10)
            except asyncio.TimeoutError:
                process.kill()
                await process.communicate()
                raise Problem('系统自启动操作超时，请刷新检查状态', 503) from None
        except OSError as error:
            raise Problem('无法连接用户级 systemd：' + str(error), 503) from error
        return process.returncode, stdout.decode(errors='replace').strip(), stderr.decode(errors='replace').strip()

    def windows(self, enabled=None):
        import winreg
        name = 'AsterSquad-' + hashlib.sha256(str(self.desktop_root).encode()).hexdigest()[:12]
        python = Path(sys.executable).with_name('pythonw.exe')
        if not python.exists():
            python = Path(sys.executable)
        command = subprocess.list2cmdline([str(python), '-P', '-m', 'squad.windows_desktop',
                                          'start', '--root', str(self.desktop_root)])
        path = r'Software\Microsoft\Windows\CurrentVersion\Run'
        if enabled is not None:
            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_SET_VALUE) as key:
                if enabled:
                    winreg.SetValueEx(key, name, 0, winreg.REG_SZ, command)
                else:
                    try:
                        winreg.DeleteValue(key, name)
                    except FileNotFoundError:
                        pass
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as key:
                value, _ = winreg.QueryValueEx(key, name)
                return value == command
        except FileNotFoundError:
            return False

    async def status(self):
        if os.name == 'nt' and self.desktop_root:
            try:
                return {'available': True, 'enabled': await asyncio.to_thread(self.windows),
                        'description': 'Windows 登录后自动启动此工作台及其配置的配套服务。'}
            except OSError as error:
                return {'available': False, 'enabled': None, 'description': str(error)}
        if os.name != 'nt' and self.unit:
            try:
                code, state, error = await self.systemctl('is-enabled', self.unit)
                available = state in {'enabled', 'disabled'}
                return {'available': available, 'enabled': state == 'enabled' if available else None,
                        'description': '控制已安装的工作台用户服务；WSL 仍需先启动。' if available else error or state}
            except Problem as error:
                return {'available': False, 'enabled': None, 'description': str(error)}
        return {'available': False, 'enabled': None,
                'description': '当前为手动启动。Linux 可安装用户服务并指定 --startup-unit；Windows 请使用桌面启动器。'}

    async def configure(self, enabled):
        if type(enabled) is not bool:
            raise Problem('enabled 须为布尔值')
        async with self.lock:
            if not (await self.status())['available']:
                raise Problem('当前启动方式不支持在网页设置自启动', 409)
            if os.name == 'nt':
                try:
                    await asyncio.to_thread(self.windows, enabled)
                except OSError as error:
                    raise Problem('保存登录自启动失败：' + str(error), 503) from error
            else:
                code, _, error = await self.systemctl('enable' if enabled else 'disable', self.unit)
                if code:
                    raise Problem('保存系统自启动失败：' + error, 503)
            # Never start/stop the currently running console from this toggle.
            return await self.status()
