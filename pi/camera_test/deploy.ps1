# Copiază serverul de test pe Raspberry Pi și îl instalează ca serviciu.
# Exemplu: .\pi\camera_test\deploy.ps1 -Target alex@172.20.10.9
param([string]$Target = "alex@alex.local")

$src = $PSScriptRoot
ssh $Target "mkdir -p ~/vesta-camera-test"
scp "$src\camera_server.py" "$src\index.html" "$src\install.sh" "$src\diagnose.sh" "${Target}:~/vesta-camera-test/"
ssh -t $Target "cd ~/vesta-camera-test && chmod +x *.sh && ./diagnose.sh && ./install.sh"
