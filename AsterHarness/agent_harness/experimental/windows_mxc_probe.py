"""Diagnose the native MXC backend without launching a sandboxed process."""

import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory

from .windows_mxc_policy import compile_filesystem


def _executor_environment():
    return {key: os.environ[key] for key in (
        "SystemRoot", "SystemDrive", "LOCALAPPDATA", "USERPROFILE", "TEMP", "TMP"
    ) if key in os.environ}


def _run(executor, *arguments, timeout=15):
    return subprocess.run(
        [str(executor), *arguments], cwd=executor.parent, env=_executor_environment(),
        stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout,
    )


def _probe(executor, *arguments):
    result = _run(executor, "--probe", *arguments)
    result.check_returncode()
    report = json.loads(result.stdout)
    if not isinstance(report, dict) or not isinstance(report.get("probes"), dict):
        raise ValueError("MXC executor returned an invalid probe report")
    return report


def _native(report):
    return (
        report.get("tier") == "base-container"
        and report.get("needsDaclAugmentation") is False
        and report["probes"].get("baseContainerApiPresent") is True
        and "error" not in report
    )


def _request(directory):
    workspace = directory / "workspace"
    workspace.mkdir()
    denied = workspace / "denied.txt"
    denied.write_text("Aster MXC probe fixture", encoding="utf-8")
    shell = Path(os.environ["SystemRoot"]) / "System32" / "cmd.exe"
    filesystem = compile_filesystem(workspace, {
        "rules": [{"path": str(denied), "access": "deny"}],
    })
    return _policy(workspace, filesystem, [str(shell), "/d", "/c", "exit", "0"])


def _policy(workspace, filesystem, command):
    return {
        "version": "1.0.0",
        "containment": "processcontainer",
        "process": {
            "commandLine": subprocess.list2cmdline(command),
            "cwd": str(workspace),
        },
        "filesystem": filesystem,
        "network": {
            "egress": {"default": "allow"},
            "ingress": {"default": "deny", "hostLoopback": "allow"},
        },
    }


def _local_executor(executor):
    if os.name != "nt":
        raise OSError("The MXC probe requires native Windows")
    executor = Path(executor)
    if not executor.is_absolute() or executor.anchor.startswith("\\\\"):
        raise ValueError("Specify the absolute local path to Microsoft's wxc-exec.exe")
    executor = executor.resolve(strict=True)
    if executor.name.lower() != "wxc-exec.exe" or not executor.is_file():
        raise ValueError("Specify the absolute local path to Microsoft's wxc-exec.exe")
    if executor.anchor.startswith("\\\\"):
        raise ValueError("The MXC executor must resolve to a local path")
    return executor


def _host_report(executor):
    host = _probe(executor)
    checks = {
        "native_base_container": _native(host),
        "explicit_deny": host["probes"].get("baseContainerSupportsDenyPaths") is True,
        "host_loopback": (
            host["probes"].get("baseContainerSupportsIngressHostLoopbackAllow") is True
        ),
    }
    return {
        "windows_build": sys.getwindowsversion().build,
        "host_tier": host.get("tier"),
        "checks": checks,
        "request_tier": None,
        "status": "host_requirements_missing",
    }


def _check_request(executor, report, config, policy):
    config.write_text(json.dumps(policy), encoding="utf-8")
    request = _probe(executor, "--config", str(config))
    report["request_tier"] = request.get("tier")
    report["status"] = "compatible_for_smoke_test" if _native(request) else "request_rejected"
    if isinstance(request.get("error"), str):
        report["request_error"] = request["error"]
    return _native(request)


def diagnose(executor):
    """Report compatibility; only an execution test can establish enforcement."""
    executor = _local_executor(executor)
    report = _host_report(executor)
    if all(report["checks"].values()):
        with TemporaryDirectory(prefix="aster-mxc-probe-") as temporary:
            directory = Path(temporary)
            _check_request(executor, report, directory / "request.json", _request(directory))
    return report
