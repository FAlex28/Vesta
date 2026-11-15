# Instalează pe Raspberry Pi programul care trimite poziția GPS-ului (UART, GPIO14/15) la
# backendul Vesta de pe laptop. Rulează de pe laptop, după joystick:
#   powershell -ExecutionPolicy Bypass -File .\pi\gps_forwarder\deploy.ps1
param([string]$Target = "alex@172.20.10.9")

$ErrorActionPreference = "Stop"
$src = $PSScriptRoot
Write-Host "Copiez programul GPS pe $Target ..."
ssh $Target "mkdir -p ~/vesta-gps"
scp "$src\gps_forwarder.py" "$src\install.sh" "${Target}:~/vesta-gps/"
if ($LASTEXITCODE -ne 0) { throw "Copierea pe Pi a eșuat." }
ssh $Target "cd ~/vesta-gps && sed -i 's/\r$//' install.sh gps_forwarder.py && chmod +x install.sh && ./install.sh"
if ($LASTEXITCODE -ne 0) { throw "Instalarea pe Pi a eșuat." }
Write-Host "Gata. Jurnal: ssh $Target journalctl -u vesta-gps -f"
