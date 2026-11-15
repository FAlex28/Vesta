# Instalează pe Raspberry Pi programul care trimite apăsarea joystickului (GPIO17) la backendul
# Vesta de pe acest laptop. Rulează de pe laptop:
#   powershell -ExecutionPolicy Bypass -File .\pi\joystick_forwarder\deploy.ps1
#   powershell -ExecutionPolicy Bypass -File .\pi\joystick_forwarder\deploy.ps1 -Target alex@172.20.10.9
param(
    [string]$Target = "alex@172.20.10.9",
    # Adresa la care Pi-ul găsește laptopul. Numele .local (mDNS) rămâne valabil când se schimbă IP-ul.
    [string]$BackendUrl = ("http://{0}.local:8000" -f [System.Net.Dns]::GetHostName())
)

$ErrorActionPreference = "Stop"
$src = $PSScriptRoot
$stateDir = Join-Path $src "..\..\backend\.state"
$keyFile = Join-Path $stateDir "device.key"

# Cheia dispozitivului: aceeași pe laptop (backend\start-lan.ps1) și pe Pi. Nu ajunge în git (.state/).
if (-not (Test-Path $keyFile)) {
    New-Item -ItemType Directory -Force $stateDir | Out-Null
    $bytes = New-Object byte[] 32
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $newKey = [Convert]::ToBase64String($bytes).TrimEnd("=").Replace("+", "-").Replace("/", "_")
    [System.IO.File]::WriteAllText($keyFile, $newKey)
}
$key = ([System.IO.File]::ReadAllText($keyFile)).Trim()

Write-Host "Copiez programul pe $Target ..."
ssh $Target "mkdir -p ~/vesta-joystick"
scp "$src\joystick_forwarder.py" "$src\install.sh" "${Target}:~/vesta-joystick/"
if ($LASTEXITCODE -ne 0) { throw "Copierea pe Pi a eșuat." }

Write-Host "Instalez serviciul vesta-joystick (backend: $BackendUrl) ..."
# Cheia merge pe stdin, nu în linia de comandă (UTF-8 fără BOM).
$OutputEncoding = New-Object System.Text.UTF8Encoding $false
$key | ssh $Target "cd ~/vesta-joystick && sed -i 's/\r$//' install.sh joystick_forwarder.py && chmod +x install.sh && ./install.sh '$BackendUrl'"
if ($LASTEXITCODE -ne 0) { throw "Instalarea pe Pi a eșuat." }
Write-Host "Gata. Joystickul trimite activările la $BackendUrl."
