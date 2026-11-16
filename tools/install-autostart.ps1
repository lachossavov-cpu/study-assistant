# Runs the JARVIS server at logon, with no console window.
# Without it, the pinned icon opens a "connection refused" page when the server is down.
#
#   powershell -ExecutionPolicy Bypass -File tools\install-autostart.ps1
#   powershell -ExecutionPolicy Bypass -File tools\install-autostart.ps1 -Remove
#
# ASCII ONLY, on purpose. Windows PowerShell 5.1 reads .ps1 files as ANSI unless they
# carry a UTF-8 BOM, so any Cyrillic here turns to mojibake and the parser fails on it.
# tests/run_tests.py asserts this file stays ASCII.

param([switch]$Remove)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$startup = [Environment]::GetFolderPath('Startup')
$link = Join-Path $startup 'JARVIS server.lnk'

if ($Remove) {
  if (Test-Path $link) { Remove-Item $link; Write-Output "Removed: $link" }
  else { Write-Output "Nothing to remove." }
  exit 0
}

# pythonw.exe runs without a console window - that is the difference from python.exe
$py = (Get-Command python).Source
$pyw = Join-Path (Split-Path -Parent $py) 'pythonw.exe'
if (-not (Test-Path $pyw)) { throw "pythonw.exe not found next to $py" }

$app = Join-Path $root 'server\app.py'
if (-not (Test-Path $app)) { throw "not found: $app" }

$shell = New-Object -ComObject WScript.Shell
$sc = $shell.CreateShortcut($link)
$sc.TargetPath = $pyw
$sc.Arguments = '"' + $app + '"'
$sc.WorkingDirectory = $root
$sc.WindowStyle = 7
$sc.Description = 'JARVIS - local server for the Avans dashboard'
$ico = Join-Path $root 'jarvis.ico'
if (Test-Path $ico) { $sc.IconLocation = $ico }
$sc.Save()

Write-Output "Created: $link"
Write-Output "Target:  $pyw"
Write-Output "Args:    $($sc.Arguments)"
Write-Output ""
Write-Output "The server will now start on every logon."
Write-Output "To start it right now without logging out:"
Write-Output "  Start-Process '$pyw' -ArgumentList '`"$app`"' -WorkingDirectory '$root' -WindowStyle Hidden"
