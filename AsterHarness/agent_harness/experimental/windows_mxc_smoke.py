"""Run a fixed PowerShell workload against synthetic files under native MXC."""

import base64
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import subprocess
from tempfile import TemporaryDirectory
from threading import Event, Thread

from . import windows_mxc_probe as mxc
from .windows_mxc_policy import compile_filesystem


RESULT_PREFIX = b"ASTER_MXC_RESULT:"
EXTERNAL_URL = "https://example.com/"
EXPECTED_CHECKS = (
    "workspace_read", "workspace_write", "readonly_read", "readonly_write_blocked",
    "explicit_deny_read_blocked", "protected_read_blocked", "outside_read_blocked",
    "outside_write_blocked", "reopened_child_read", "denied_sibling_read_blocked",
    "host_loopback", "public_https",
)


@contextmanager
def _loopback():
    token = secrets.token_hex(16)
    reached = Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != "/" + token:
                self.send_error(404)
                return
            body = token.encode("ascii")
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            reached.set()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}/{token}", token, reached
        finally:
            server.shutdown()
            thread.join()


def _powershell_string(value):
    return "'" + str(value).replace("'", "''") + "'"


def _command(shell, workspace, readonly, outside, url, token):
    variables = {
        "workspace": workspace, "readonly": readonly, "outside": outside,
        "loopback": url, "token": token, "public": EXTERNAL_URL,
    }
    script = "\n".join(f"${name} = {_powershell_string(value)}" for name, value in variables.items())
    script += r"""
$ErrorActionPreference = 'Stop'
function ReadBlocked([string]$path) {
    try {
        [void][IO.File]::ReadAllText($path)
        return $false
    } catch [UnauthorizedAccessException], [IO.FileNotFoundException], [IO.DirectoryNotFoundException] {
        return $true
    }
}
function WriteBlocked([string]$path) {
    try {
        [IO.File]::AppendAllText($path, 'unexpected write')
        return $false
    } catch [UnauthorizedAccessException], [IO.FileNotFoundException], [IO.DirectoryNotFoundException] {
        return $true
    }
}
function Request([string]$url) {
    $request = [Net.WebRequest]::Create($url)
    $request.Proxy = $null
    $request.AllowAutoRedirect = $false
    $request.Timeout = 8000
    $request.ReadWriteTimeout = 8000
    return $request.GetResponse()
}
$result = [ordered]@{}
$result.workspace_read = [IO.File]::ReadAllText([IO.Path]::Combine($workspace, 'input.txt')) -ceq 'workspace fixture'
[IO.File]::WriteAllText([IO.Path]::Combine($workspace, 'output.txt'), 'sandbox write')
$result.workspace_write = $true
$readonlyFile = [IO.Path]::Combine($readonly, 'input.txt')
$result.readonly_read = [IO.File]::ReadAllText($readonlyFile) -ceq 'readonly fixture'
$result.readonly_write_blocked = WriteBlocked $readonlyFile
$result.explicit_deny_read_blocked = ReadBlocked ([IO.Path]::Combine($workspace, 'denied.txt'))
$result.protected_read_blocked = ReadBlocked ([IO.Path]::Combine($workspace, 'protected\input.txt'))
$outsideFile = [IO.Path]::Combine($outside, 'input.txt')
$result.outside_read_blocked = ReadBlocked $outsideFile
$result.outside_write_blocked = WriteBlocked $outsideFile
$result.reopened_child_read = $false
try {
    $result.reopened_child_read = [IO.File]::ReadAllText([IO.Path]::Combine($workspace, 'restricted\allowed\input.txt')) -ceq 'allowed fixture'
} catch [UnauthorizedAccessException], [IO.FileNotFoundException], [IO.DirectoryNotFoundException] {}
$result.denied_sibling_read_blocked = ReadBlocked ([IO.Path]::Combine($workspace, 'restricted\input.txt'))
$result.host_loopback = $false
try {
    $response = Request $loopback
    try {
        $reader = [IO.StreamReader]::new($response.GetResponseStream())
        try {
            $result.host_loopback = ($response.StatusCode -eq 200) -and ($reader.ReadToEnd() -ceq $token)
        } finally { $reader.Dispose() }
    } finally { $response.Dispose() }
} catch [Net.WebException] {}
$result.public_https = $false
try {
    $response = Request $public
    try { $result.public_https = $response.StatusCode -eq 200 }
    finally { $response.Dispose() }
} catch [Net.WebException] {}
[Console]::WriteLine('ASTER_MXC_RESULT:' + ($result | ConvertTo-Json -Compress))
"""
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return [str(shell), "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded]


def _result(stdout):
    records = [line[len(RESULT_PREFIX):] for line in stdout.splitlines() if line.startswith(RESULT_PREFIX)]
    if len(records) != 1:
        return None
    try:
        checks = json.loads(records[0])
    except (ValueError, UnicodeError):
        return None
    if (not isinstance(checks, dict) or set(checks) != set(EXPECTED_CHECKS)
            or any(type(value) is not bool for value in checks.values())):
        return None
    return checks


def _smoke_mode(executor, default_access):
    report = {"request_tier": None}
    with TemporaryDirectory(prefix="aster-mxc-smoke-") as temporary, _loopback() as loopback:
        directory = Path(temporary).resolve()
        workspace, readonly, outside = (directory / name for name in ("workspace", "readonly", "outside"))
        protected = workspace / "protected"
        restricted = workspace / "restricted"
        allowed = restricted / "allowed"
        for path in (workspace, readonly, outside, protected, restricted, allowed):
            path.mkdir()
            (path / "input.txt").write_text(f"{path.name} fixture", encoding="utf-8")
        denied = workspace / "denied.txt"
        denied.write_text("denied fixture", encoding="utf-8")
        system = Path(os.environ["SystemRoot"]).resolve(strict=True)
        shell = system / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        url, token, reached = loopback
        command = _command(shell, workspace, readonly, outside, url, token)
        filesystem = compile_filesystem(
            workspace, {
                "rules": ([{"path": "/", "access": "read"}] if default_access == "read" else []) + [
                    {"path": str(readonly), "access": "read"},
                    {"path": str(denied), "access": "deny"},
                    {"path": str(protected / "input.txt"), "access": "write"},
                    {"path": str(restricted), "access": "deny"},
                    {"path": str(allowed), "access": "read"},
                ],
            }, private_roots=[protected], runtime_roots=[system],
        )
        policy = mxc._policy(workspace, filesystem, command)
        policy["process"]["timeout"] = 30000
        policy["ui"] = {"disable": False, "clipboard": "none", "injection": False}
        config = directory / "request.json"
        if not mxc._check_request(executor, report, config, policy):
            return report
        try:
            result = mxc._run(executor, "--config", str(config), timeout=45)
        except subprocess.TimeoutExpired:
            report["status"] = "smoke_timed_out"
            return report

        report["exit_code"] = result.returncode
        checks = _result(result.stdout)
        if result.returncode != 0 or checks is None:
            report["status"] = "smoke_execution_failed"
            report["diagnostic"] = result.stderr.decode("utf-8", errors="replace")[-2000:].strip()
            return report
        output = workspace / "output.txt"
        checks["workspace_write"] &= output.is_file() and output.read_bytes() == b"sandbox write"
        checks["readonly_write_blocked"] &= (readonly / "input.txt").read_bytes() == b"readonly fixture"
        checks["outside_write_blocked"] &= (outside / "input.txt").read_bytes() == b"outside fixture"
        checks["outside_read_matches_default"] = checks.pop("outside_read_blocked") == (default_access == "deny")
        checks["host_loopback"] &= reached.is_set()
        report["smoke_checks"] = checks
        report["status"] = "smoke_passed" if all(checks.values()) else "smoke_checks_failed"
        return report


def smoke(executor):
    """Exercise each default path policy only after native BaseContainer preflight."""
    executor = mxc._local_executor(executor)
    report = mxc._host_report(executor)
    if not all(report["checks"].values()):
        return report
    modes = {mode: _smoke_mode(executor, mode) for mode in ("deny", "read")}
    report.pop("request_tier")
    report["modes"] = modes
    report["status"] = ("smoke_passed" if all(r["status"] == "smoke_passed" for r in modes.values())
                        else "smoke_checks_failed")
    return report
