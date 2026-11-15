# Pornește bridge-ul PC <-> Arduino Nano pe Windows, cu instalare automată la prima rulare.
# Exemple:
#   .\start-bridge.ps1                         # placa găsită automat, backend http://127.0.0.1:8000
#   .\start-bridge.ps1 -Port COM7
#   .\start-bridge.ps1 -Port COM7 -Backend http://192.168.1.50:8000 -DashboardKey "..."
param(
    [string]$Port = "",
    [string]$Backend = "http://127.0.0.1:8000",
    [string]$DashboardKey = $env:VESTA_DASHBOARD_API_KEY
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    Write-Host "Prima rulare: creez mediul Python și instalez pyserial + websockets..."
    py -3 -m venv .venv
    .\.venv\Scripts\python.exe -m pip install --quiet -r requirements.txt
}

$arguments = @("nano_bridge.py", "bridge", "--backend", $Backend, "--keep-running")
if ($Port) { $arguments += @("--port", $Port) }
# Cheia se dă prin variabila de mediu, nu în linia de comandă (nu apare în lista de procese).
if ($DashboardKey) { $env:VESTA_DASHBOARD_API_KEY = $DashboardKey }

$env:PYTHONIOENCODING = "utf-8"
Write-Host "Bridge pornit. Ctrl+C îl oprește (placa trece în 'Astept PC', fără eroare)."
.\.venv\Scripts\python.exe @arguments
