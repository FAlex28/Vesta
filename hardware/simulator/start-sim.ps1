# Pornește Raspberry Pi-ul și Arduino Nano-ul virtuale lângă backendul de pe laptop.
#   powershell -ExecutionPolicy Bypass -File .\hardware\simulator\start-sim.ps1
#   ... -Backend http://192.168.1.20:8000
param(
    [string]$Backend = "http://127.0.0.1:8000"
)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

python -c "import gpiozero, serial, websockets" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Prima rulare: instalez gpiozero, pyserial, websockets..."
    python -m pip install --user "gpiozero>=2.0,<3.0" "pyserial>=3.5" "websockets>=12"
}

# Aceeași cheie de dispozitiv pe care backend\start-lan.ps1 o dă backendului (ca deploy.ps1 pe Pi).
$keyFile = Join-Path $PSScriptRoot "..\..\backend\.state\device.key"
if (-not $env:VESTA_DEVICE_API_KEY -and (Test-Path $keyFile)) {
    $env:VESTA_DEVICE_API_KEY = ([System.IO.File]::ReadAllText($keyFile)).Trim()
}
$env:PYTHONIOENCODING = "utf-8"
python -u vesta_sim.py --backend $Backend
