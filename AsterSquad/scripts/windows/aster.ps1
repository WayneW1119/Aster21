param(
    [ValidateSet('init', 'start', 'stop', 'restart', 'status', 'open', 'window', 'sandbox-mxc-probe', 'sandbox-mxc-smoke')]
    [string]$Action = 'status',
    [string]$Root = (Join-Path $env:LOCALAPPDATA 'AsterWindows'),
    [string]$Python = '',
    [ValidateSet('squad', 'relay')]
    [string]$Page = 'squad',
    [string]$MxcExecutor = ''
)
$ErrorActionPreference = 'Stop'
if (-not $Python) {
    $command = Get-Command python.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $command) { throw 'Activate the Python environment or specify -Python.' }
    $Python = $command.Source
}
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "Python environment not found: $Python. Specify -Python with the installed Windows venv."
}
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$launcherArgs = @('-P', '-m', 'squad.windows_desktop', $Action, '--root', $Root)
if ($Action -eq 'window') { $launcherArgs += @('--page', $Page) }
if ($Action -in 'sandbox-mxc-probe', 'sandbox-mxc-smoke') {
    if (-not $MxcExecutor) { throw 'Specify -MxcExecutor with the absolute path to Microsoft wxc-exec.exe.' }
    $launcherArgs += @('--mxc-executor', $MxcExecutor)
} elseif ($MxcExecutor -and $Action -in 'start', 'restart') {
    $launcherArgs += @('--mxc-executor', $MxcExecutor)
}
& $Python @launcherArgs
if ($LASTEXITCODE -ne 0) { throw "Aster desktop command failed ($LASTEXITCODE)." }
