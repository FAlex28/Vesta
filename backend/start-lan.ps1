# Pornește backendul Vesta pe laptop (mod dezvoltare), accesibil din rețeaua locală, ca să
# primească apăsarea joystickului de pe Raspberry Pi (pi\joystick_forwarder).
#   powershell -ExecutionPolicy Bypass -File .\start-lan.ps1
param(
    [int]$Port = 8000,
    # Serverul camerelor de pe Raspberry Pi (pi\camera_test) și contul SSH de pe care se ia cheia lui.
    [string]$CameraUrl = "http://alex.local:8080",
    [string]$PiTarget = "alex@172.20.10.9"
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    Write-Host "Lipsește backend\.venv. Creează-l: py -3 -m venv .venv; .\.venv\Scripts\python.exe -m pip install -e "".[dev]"""
    exit 1
}

# Cheia dispozitivului, comună cu Pi-ul (o pune acolo pi\joystick_forwarder\deploy.ps1). Nu ajunge în git.
$keyFile = ".state\device.key"
if (-not (Test-Path $keyFile)) {
    New-Item -ItemType Directory -Force ".state" | Out-Null
    $bytes = New-Object byte[] 32
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $newKey = [Convert]::ToBase64String($bytes).TrimEnd("=").Replace("+", "-").Replace("/", "_")
    [System.IO.File]::WriteAllText((Join-Path $PSScriptRoot $keyFile), $newKey)
    Write-Host "Cheie nouă de dispozitiv creată. Rulează și pi\joystick_forwarder\deploy.ps1 ca Pi-ul s-o primească."
}

$busy = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($busy) {
    Write-Host "Portul $Port este deja folosit (proces $($busy[0].OwningProcess))."
    Write-Host "Închide fereastra în care rulează backendul vechi (Ctrl+C) și pornește din nou acest script."
    exit 1
}

# O singură dată: regula de firewall care lasă dispozitivele din aceeași rețea (Pi-ul) să ajungă
# la portul backendului. Windows cere confirmarea (fereastra „Da/Nu”).
$ruleName = "Vesta backend (port $Port, retea locala)"
if (-not (Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue)) {
    Write-Host "Cer permisiunea Windows pentru conexiunile din rețeaua locală pe portul $Port ..."
    $command = "New-NetFirewallRule -DisplayName '$ruleName' -Direction Inbound -Action Allow " +
        "-Protocol TCP -LocalPort $Port -RemoteAddress LocalSubnet -Profile Any | Out-Null"
    try {
        Start-Process powershell -Verb RunAs -Wait -ArgumentList "-NoProfile -Command $command"
    } catch {
        Write-Host "Permisiunea a fost refuzată: backendul merge pe laptop, dar Pi-ul nu va ajunge la el."
    }
}

$env:VESTA_CONTROL_SIGNING_SECRET = "vesta-local-secret-0123456789abcdefghij"
$env:VESTA_GPIO_MODE = "mock"
$env:VESTA_VOICE_MODE = "demo"
$env:VESTA_DEVICE_PAIRING_CODE = "VESTA23456"
$env:VESTA_STATE_DIR = ".state"
$env:VESTA_DEVICE_API_KEY = ([System.IO.File]::ReadAllText((Join-Path $PSScriptRoot $keyFile))).Trim()

# Camerele: backendul cere fluxul de la serverul de pe Pi cu cheia acestuia; browserul nu o vede.
# Cheia se copiază o singură dată de pe Pi (/etc/vesta/camera.env) în .state\camera.key.
$cameraKeyFile = Join-Path $PSScriptRoot ".state\camera.key"
if (-not (Test-Path $cameraKeyFile)) {
    Write-Host "Iau cheia serverului de camere de pe $PiTarget ..."
    $cameraKey = ssh -o BatchMode=yes -o ConnectTimeout=5 $PiTarget "sudo sed -n 's/^VESTA_CAMERA_API_KEY=//p' /etc/vesta/camera.env"
    if ($LASTEXITCODE -eq 0 -and $cameraKey -and "$cameraKey".Trim()) {
        [System.IO.File]::WriteAllText($cameraKeyFile, "$cameraKey".Trim())
    } else {
        Write-Host "Nu am putut lua cheia camerelor (Pi oprit sau în altă rețea). Pornesc fără camere."
    }
}
if (Test-Path $cameraKeyFile) {
    $env:VESTA_CAMERA_BASE_URL = $CameraUrl
    $env:VESTA_CAMERA_API_KEY = ([System.IO.File]::ReadAllText($cameraKeyFile)).Trim()
}

# Hackathon: telefonul și persoana de încredere deschid paginile fără cod de asociere.
$env:VESTA_OPEN_ACCESS = "true"
# Fluxul vocal: la apăsarea de 5 s Vesta întreabă imediat dacă a fost o greșeală; fără răspuns
# întreabă din nou la 15 s, iar după ~30 s fără răspuns sesiunea trece în alertă (roșu).
$env:VESTA_FIRST_CHECKIN_DELAY_SECONDS = "2"
$env:VESTA_CHECKIN_RESPONSE_TIMEOUT_SECONDS = "14"
$env:VESTA_CHECKIN_RETRY_DELAY_SECONDS = "2"
$env:VESTA_CHECKIN_INTERVAL_SECONDS = "120"

# Cheile Gemini și ElevenLabs: în .state\ai-keys.env (nu ajunge în git). La prima rulare se
# creează fișierul și se deschide în Notepad; fără chei, asistentul afișează întrebările ca text.
$aiKeysFile = Join-Path $PSScriptRoot ".state\ai-keys.env"
if (-not (Test-Path $aiKeysFile)) {
    $template = @(
        "# Cheile asistentului Vesta. Lipește valorile după semnul =, salvează și repornește backendul.",
        "# Gemini (gratuit): https://aistudio.google.com/apikey",
        "GEMINI_API_KEY=",
        "# ElevenLabs: https://elevenlabs.io/app/settings/api-keys (permisiunea Text to Speech)",
        "ELEVENLABS_API_KEY=",
        "# Opțional: vocea ElevenLabs (ID din Voice Library). Gol = vocea implicită.",
        "ELEVENLABS_VOICE_ID="
    )
    [System.IO.File]::WriteAllLines($aiKeysFile, $template)
    Write-Host "Am creat $aiKeysFile. Lipește acolo cheile Gemini și ElevenLabs, salvează și repornește backendul."
    Start-Process notepad.exe $aiKeysFile
}
foreach ($line in [System.IO.File]::ReadAllLines($aiKeysFile)) {
    if ($line -match '^\s*(GROQ_API_KEY|GROQ_STT_MODEL|GROQ_LLM_MODEL|GEMINI_API_KEY|GEMINI_MODEL|ELEVENLABS_API_KEY|ELEVENLABS_VOICE_ID|ELEVENLABS_TTS_MODEL|TIMESCALE_SERVICE_URL)\s*=\s*(\S+)\s*$') {
        Set-Item -Path "Env:$($Matches[1])" -Value $Matches[2]
    }
}
$gemini = if ($env:GROQ_API_KEY) { "Groq" } elseif ($env:GEMINI_API_KEY) { "Gemini" } else { "NU (răspunsuri doar cu butoanele)" }
$eleven = if ($env:ELEVENLABS_API_KEY) { "da" } else { "NU (vocea telefonului)" }
Write-Host "Asistent: înțelegere $gemini · voce ElevenLabs $eleven"
# Tiger Data: cu adresa în ai-keys.env, istoricul, transcrierile și înregistrările merg și în Tiger.
if ($env:TIMESCALE_SERVICE_URL) {
    $env:VESTA_PERSISTENCE = "tiger"
    Write-Host "Baza de date: Tiger Data"
} else {
    Write-Host "Baza de date: locală (adaugă TIMESCALE_SERVICE_URL în .state\ai-keys.env pentru Tiger Data)"
}

# Telefonul deschide pagina prin HTTPS pe IP-ul laptopului (frontend\start-lan.ps1): originile
# acelea trebuie permise pentru WebSocket.
$origins = @("http://localhost:5173", "http://127.0.0.1:5173", "https://localhost:5173", "https://127.0.0.1:5173")
$lanIps = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
    Where-Object { $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.254.*" } |
    Select-Object -ExpandProperty IPAddress
foreach ($ip in $lanIps) { $origins += @("https://${ip}:5173", "http://${ip}:5173") }
$env:VESTA_CORS_ORIGINS = ($origins -join ",")

$hostName = [System.Net.Dns]::GetHostName()
Write-Host "Backend Vesta pe http://127.0.0.1:$Port (Pi-ul îl găsește la http://$hostName.local:$Port). Ctrl+C oprește."
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port $Port --workers 1
