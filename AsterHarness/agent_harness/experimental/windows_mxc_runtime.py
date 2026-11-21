"""Opt-in MXC adapter for unified exec and the trusted filesystem worker."""

import asyncio
import base64
from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory

from agent_harness._windows import private_file, spawn, terminate_tree
from agent_harness.core.sandbox import parse_policy
from . import windows_mxc_probe as mxc
from .windows_mxc_policy import compile_filesystem


class _CommandProcess:
    def __init__(self, process, scratch):
        self.process = process
        self.stdout = process.stdout
        self.scratch = scratch

    @property
    def returncode(self):
        return self.process.returncode

    async def wait(self):
        code = await self.process.wait()
        self.scratch.cleanup()
        return code

    async def _aster_sandbox_stop(self):
        await terminate_tree(self.process)


class WindowsMxcRuntime:
    """For explicit local acceptance, separate from the default Session backend."""

    def __init__(self, executor, workspace, policy, *, python, powershell, private_roots=()):
        self.executor = mxc._local_executor(executor)
        self.workspace = Path(workspace).resolve(strict=True)
        self.policy = parse_policy(policy)
        self.python = Path(python).resolve(strict=True)
        self.powershell = Path(powershell).resolve(strict=True)
        self.system = Path(os.environ["SystemRoot"]).resolve(strict=True)
        self.worker = Path(__file__).resolve().parents[1] / "core/sandbox/worker.py"
        self.runtime_roots = (self.system, self.python.parent, self.powershell.parent, self.worker.parents[2])
        public_package = self.worker.parents[3] / "aster"
        if public_package.is_dir():
            self.runtime_roots += (public_package,)
        if self.python == Path(sys.executable).resolve():
            self.runtime_roots += (Path(sys.prefix), Path(sys.base_prefix))
        self.private_roots = tuple(private_roots)
        if (self.powershell.name.lower() not in ("pwsh.exe", "powershell.exe")
                or not self.powershell.is_file() or not self.python.is_file()):
            raise ValueError("MXC acceptance requires explicit Python and PowerShell executables")
        for root in (*self.runtime_roots, self.executor.parent):
            if self.workspace.is_relative_to(root) or root.is_relative_to(self.workspace):
                raise ValueError("MXC workspace must be separate from the trusted executables")
        if not all(mxc._host_report(self.executor)["checks"].values()):
            raise RuntimeError("The host cannot provide native MXC with deny paths and loopback")
        compile_filesystem(self.workspace, self.policy, private_roots=self.private_roots,
                           runtime_roots=self.runtime_roots)

    def _prepare(self, directory, argv, cwd, timeout, *, policy=None, role_grants=(), extra_env=None):
        directory = Path(directory).resolve(strict=True)
        filesystem = compile_filesystem(
            self.workspace, self.policy if policy is None else policy, cwd=cwd,
            runtime_roots=self.runtime_roots, private_roots=(*self.private_roots, directory),
            role_grants=role_grants,
        )
        policy = mxc._policy(cwd, filesystem, argv)
        environment = {
            "SystemRoot": str(self.system),
            "SystemDrive": self.system.drive,
            "LOCALAPPDATA": os.environ["LOCALAPPDATA"],
            "HARNESS_PYTHON": str(self.python),
            "COMSPEC": str(self.system / "System32" / "cmd.exe"),
            "PATHEXT": ".COM;.EXE;.BAT;.CMD",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
            "PATH": os.pathsep.join(map(str, (self.system / "System32", self.powershell.parent, self.python.parent))),
        }
        reserved = {key.upper() for key in environment}
        for name, value in (extra_env or {}).items():
            if name.upper() not in reserved:
                environment[name] = value
        policy["process"].update(env=[f"{key}={value}" for key, value in environment.items()], timeout=timeout)
        policy["ui"] = {"disable": False, "clipboard": "none", "injection": False}
        config = directory / "request.json"
        config.touch(exist_ok=False)
        private_file(config)
        report = {}
        if not mxc._check_request(self.executor, report, config, policy):
            raise RuntimeError("Native MXC rejected the execution policy")
        return [str(self.executor), "--config", str(config)]

    @contextmanager
    def execution(self, argv, *, cwd, timeout=0, policy=None, role_grants=(), extra_env=None):
        with TemporaryDirectory(prefix="aster-mxc-launch-") as directory:
            args = self._prepare(directory, argv, cwd, timeout, policy=policy,
                                 role_grants=role_grants, extra_env=extra_env)
            yield args, mxc._executor_environment()

    async def spawn_windows_exec(self, command, *, cwd, shell):
        if Path(shell).resolve(strict=True) != self.powershell:
            raise ValueError("Use the PowerShell executable configured for this MXC workspace")
        script = (
            "[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)\n"
            "$ProgressPreference = 'SilentlyContinue'\n" + command
        )
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        argv = [str(self.powershell), "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded]
        if len(subprocess.list2cmdline(argv).encode("utf-16-le")) >= 65532:
            raise ValueError("PowerShell command exceeds the Windows command line limit")
        scratch = TemporaryDirectory(prefix="aster-mxc-exec-")
        try:
            args = await asyncio.to_thread(self._prepare, scratch.name, argv, cwd, 0)
            process = await spawn(
                *args, cwd=self.executor.parent, env=mxc._executor_environment(),
                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except BaseException:
            scratch.cleanup()
            raise
        return _CommandProcess(process, scratch)

    def file_operation(self, operation, **arguments):
        if operation not in {"read", "list", "bytes", "patch", "skills"}:
            raise ValueError("Unsupported MXC file operation")
        if "workspace" in arguments:
            raise ValueError("Cannot replace the MXC worker workspace")
        if operation == "bytes":
            limit = arguments.get("limit")
            if type(limit) is not int or not 0 < limit <= 8 * 1024 * 1024:
                raise ValueError("Byte read limit must be 1..8 MiB")
        request = json.dumps({"operation": operation, "workspace": str(self.workspace), **arguments}).encode()
        if len(request) > 12 * 1024 * 1024:
            raise ValueError("MXC file request too large")
        argv = [str(self.python), "-I", "-X", "utf8", str(self.worker)]
        with self.execution(argv, cwd=self.workspace, timeout=25000) as (command, env):
            result = subprocess.run(
                command, cwd=self.executor.parent, env=env,
                input=request, capture_output=True, timeout=30,
            )
        if result.returncode:
            raise RuntimeError("MXC file worker failed: " + result.stderr.decode("utf-8", errors="replace")[:2000])
        try:
            response = json.loads(result.stdout)
        except ValueError:
            raise RuntimeError("MXC file worker returned invalid output") from None
        if not isinstance(response, dict) or type(response.get("ok")) is not bool:
            raise RuntimeError("MXC file worker returned invalid output")
        if not response["ok"]:
            raise ValueError(response.get("error", "MXC file operation failed"))
        return response["value"]
