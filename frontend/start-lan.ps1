# Pornește interfața Vesta pentru telefon, dispecerat și persoana de încredere, prin HTTPS pe
# rețeaua locală (telefonul cere https:// pentru microfon și locație).
#   powershell -ExecutionPolicy Bypass -File .\start-lan.ps1
param([int]$Port = 5173)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path "node_modules")) {
    Write-Host "Prima rulare: instalez dependențele (npm install)..."
    npm install
}

$lanIps = @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
    Where-Object { $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.254.*" } |
    Select-Object -ExpandProperty IPAddress)

# Certificat local (autosemnat) pentru localhost și IP-urile laptopului. Se reface dacă IP-ul se schimbă.
$certDir = Join-Path $PSScriptRoot ".cert"
$ipsFile = Join-Path $certDir "ips.txt"
$wanted = ($lanIps | Sort-Object) -join ","
$current = if (Test-Path $ipsFile) { ([System.IO.File]::ReadAllText($ipsFile)).Trim() } else { "" }
if (-not (Test-Path (Join-Path $certDir "cert.pem")) -or $current -ne $wanted) {
    $openssl = "C:\Program Files\Git\usr\bin\openssl.exe"
    if (-not (Test-Path $openssl)) { $openssl = "openssl" }
    New-Item -ItemType Directory -Force $certDir | Out-Null
    $san = @("DNS:localhost", "IP:127.0.0.1") + ($lanIps | ForEach-Object { "IP:$_" })
    $config = @(
        "[req]", "distinguished_name=dn", "x509_extensions=ext", "prompt=no",
        "[dn]", "CN=Vesta local",
        "[ext]", "subjectAltName=$($san -join ',')", "basicConstraints=CA:FALSE",
        "keyUsage=digitalSignature,keyEncipherment", "extendedKeyUsage=serverAuth"
    )
    $configFile = Join-Path $certDir "openssl.cnf"
    [System.IO.File]::WriteAllLines($configFile, $config)
    & $openssl req -x509 -newkey rsa:2048 -nodes -days 365 `
        -keyout (Join-Path $certDir "key.pem") -out (Join-Path $certDir "cert.pem") -config $configFile 2>$null
    [System.IO.File]::WriteAllText($ipsFile, $wanted)
    Write-Host "Certificat local creat pentru: $($san -join ', ')"
}

# O singură dată: firewall pentru portul paginii, doar din rețeaua locală (fereastra „Da/Nu”).
$ruleName = "Vesta web (port $Port, retea locala)"
if (-not (Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue)) {
    $command = "New-NetFirewallRule -DisplayName '$ruleName' -Direction Inbound -Action Allow " +
        "-Protocol TCP -LocalPort $Port -RemoteAddress LocalSubnet -Profile Any | Out-Null"
    try {
        Start-Process powershell -Verb RunAs -Wait -ArgumentList "-NoProfile -Command $command"
    } catch {
        Write-Host "Permisiunea a fost refuzată: pagina merge pe laptop, dar nu și de pe telefon."
    }
}

Write-Host ""
Write-Host "Deschide (prima dată browserul avertizează despre certificat: Avansat -> Continuă):"
Write-Host "  Dispecerat (laptop):          https://localhost:$Port/"
foreach ($ip in $lanIps) {
    Write-Host "  Telefonul victimei (Samsung):  https://${ip}:$Port/companion"
    Write-Host "  Persoana de încredere:         https://${ip}:$Port/contact"
    Write-Host "  Butonul (în locul joystickului): https://${ip}:$Port/buton"
}
Write-Host ""
$env:VESTA_HTTPS = "1"
npx vite --host 0.0.0.0 --port $Port --strictPort
