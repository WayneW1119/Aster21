param(
    [ValidateRange(1, 65535)][int]$Port = 54321,
    [ValidateRange(1, 3600)][int]$LifetimeSeconds = 600,
    [string]$Distro = "",
    [string]$WslPython = "python3",
    [Parameter(Mandatory = $true)][ValidatePattern('^/')][string]$HelperPath
)
$ErrorActionPreference = 'Stop'
# Temporary Windows 127.0.0.1 -> WSL 127.0.0.1 relay. No administrator rights.
# Neither the HTTP authorization callback nor credentials are written to logs.
Add-Type -TypeDefinition @'
using System;
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Net;
using System.Net.Sockets;
using System.Threading;
using System.Threading.Tasks;
public static class AsterChatGPTLoopback {
    private static string Quote(string value) {
        // All configurable command arguments are WSL names or POSIX paths.
        // Reject Windows quoting metacharacters; spaces are supported.
        if (value.IndexOfAny(new char[] { '"', '\\', '\r', '\n' }) >= 0)
            throw new ArgumentException("Use a WSL name or POSIX path without quotes or backslashes.");
        return "\"" + value + "\"";
    }
    public static void Run(int port, int seconds, string distro, string python, string helper) {
        var arguments = (String.IsNullOrEmpty(distro) ? "" : "--distribution " + Quote(distro) + " ")
            + "--exec " + Quote(python) + " -u " + Quote(helper) + " " + port;
        if (!File.Exists(Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.System), "wsl.exe")))
            throw new InvalidOperationException("WSL is required on this Windows computer.");
        var listener = new TcpListener(IPAddress.Loopback, port);
        listener.Start();
        var deadline = DateTime.UtcNow.AddSeconds(seconds);
        try {
            while (DateTime.UtcNow < deadline) {
                if (!listener.Pending()) { Thread.Sleep(200); continue; }
                using (var client = listener.AcceptTcpClient()) {
                    var start = new ProcessStartInfo("wsl.exe", arguments);
                    start.UseShellExecute = false;
                    start.CreateNoWindow = true;
                    start.RedirectStandardInput = true;
                    start.RedirectStandardOutput = true;
                    start.RedirectStandardError = true;
                    using (var process = Process.Start(start)) {
                        try {
                            var socket = client.GetStream();
                            socket.ReadTimeout = 5000;
                            // This helper handles only a small GET callback. Close stdin
                            // after its headers so wsl.exe flushes redirected input.
                            using (var request = new MemoryStream()) {
                                int value;
                                while (request.Length < 16384 && (value = socket.ReadByte()) >= 0) {
                                    request.WriteByte((byte)value);
                                    var bytes = request.GetBuffer();
                                    var n = (int)request.Length;
                                    if (n >= 4 && bytes[n-4] == 13 && bytes[n-3] == 10 && bytes[n-2] == 13 && bytes[n-1] == 10) break;
                                }
                                var payload = request.ToArray();
                                process.StandardInput.BaseStream.Write(payload, 0, payload.Length);
                                process.StandardInput.BaseStream.Flush();
                                process.StandardInput.Close();
                            }
                            var receiving = process.StandardOutput.BaseStream.CopyToAsync(socket);
                            receiving.Wait(30000);
                        } catch {} finally {
                            client.Close();
                            if (!process.WaitForExit(2000)) { try { process.Kill(); } catch {} }
                        }
                    }
                }
            }
        } finally { listener.Stop(); }
    }
}
'@
[AsterChatGPTLoopback]::Run($Port, $LifetimeSeconds, $Distro, $WslPython, $HelperPath)
