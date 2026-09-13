$ErrorActionPreference = "Continue"

$ssh = (Get-Command ssh.exe -ErrorAction Stop).Source
$arguments = @(
    "-NT",
    "-L", "127.0.0.1:18080:127.0.0.1:8888",
    "-o", "BatchMode=yes",
    "-o", "ExitOnForwardFailure=yes",
    "-o", "ServerAliveInterval=30",
    "-o", "ServerAliveCountMax=3",
    "my-vps"
)

while ($true) {
    & $ssh @arguments
    Start-Sleep -Seconds 5
}
