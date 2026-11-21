# MeshWatch - prepara la scheda SD con DietPi gia' scritto sopra.
param([string]$SdPath)
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path

# Copia il programma sulla SD: funziona sia dal kit (cartella meshwatch accanto) sia dalla repo (sd-kit/..)
function Copy-App([string]$dest) {
    $src = Join-Path $here 'meshwatch'
    if (Test-Path (Join-Path $src 'meshwatch\__main__.py')) {
        Copy-Item $src $dest -Recurse
    } else {
        $root = Split-Path -Parent $here
        if (-not (Test-Path (Join-Path $root 'meshwatch\__main__.py'))) { throw "Non trovo i file di MeshWatch accanto a questo script." }
        New-Item -ItemType Directory $dest | Out-Null
        foreach ($i in 'meshwatch', 'web', 'tools', 'requirements.txt', 'config.example.yaml', 'README.md', 'LICENSE') {
            Copy-Item (Join-Path $root $i) $dest -Recurse
        }
    }
    Get-ChildItem $dest -Recurse -Directory -Filter '__pycache__' | Remove-Item -Recurse -Force
}

function Write-LF($path, $text) {
    [IO.File]::WriteAllText($path, ($text -replace "`r`n", "`n"), (New-Object Text.UTF8Encoding($false)))
}
function Set-Key([string]$text, [string]$key, [string]$value) {
    $pattern = '(?m)^' + [regex]::Escape($key) + '=.*$'
    if ($text -match $pattern) { return [regex]::Replace($text, $pattern, { param($m) "$key=$value" }) }
    return $text.TrimEnd() + "`n$key=$value`n"
}

Write-Host "=== MeshWatch - preparazione scheda SD ===`n"

# 1. trova la SD con DietPi
if ($SdPath) { $drives = @([pscustomobject]@{ Root = $SdPath }) }
else { $drives = @(Get-PSDrive -PSProvider FileSystem | Where-Object { Test-Path (Join-Path $_.Root 'dietpi.txt') }) }
if ($drives.Count -eq 0) {
    Write-Host "Non trovo nessuna scheda con DietPi." -ForegroundColor Red
    Write-Host "Scrivi prima l'immagine DietPi sulla SD (Raspberry Pi Imager o balenaEtcher),"
    Write-Host "poi scollega e ricollega la scheda e rilancia questo file."
    exit 1
}
$drive = $drives[0]
if ($drives.Count -gt 1) {
    Write-Host "Ho trovato piu' schede DietPi:"
    for ($i = 0; $i -lt $drives.Count; $i++) { Write-Host "  $i) $($drives[$i].Root)" }
    $drive = $drives[[int](Read-Host "Quale uso? (numero)")]
}
$sd = $drive.Root
Write-Host "Scheda trovata in $sd`n" -ForegroundColor Green

# 2. domande
$hostname = Read-Host "Nome del Raspberry in rete [meshwatch]"
if (-not $hostname) { $hostname = 'meshwatch' }
do {
    $p1 = Read-Host "Password per accedere al Raspberry via SSH (utente meshwatch)" -AsSecureString
    $p2 = Read-Host "Ripeti la password" -AsSecureString
    $pw1 = [System.Net.NetworkCredential]::new('', $p1).Password
    $pw2 = [System.Net.NetworkCredential]::new('', $p2).Password
    $bad = ($pw1 -ne $pw2) -or ($pw1.Length -lt 6) -or ($pw1 -match '["$\\`'' ]')
    if ($bad) { Write-Host 'Password diverse, troppo corte (min 6) o con caratteri non ammessi: spazio, virgolette, apostrofo, dollaro, barra rovescia.' -ForegroundColor Yellow }
} while ($bad)

$ssid = Read-Host "Nome rete WiFi (lascia vuoto se usi il cavo di rete)"
$wifipw = ''
if ($ssid) {
    if ($ssid -match "'") { Write-Host "Il nome WiFi non puo' contenere l'apostrofo." -ForegroundColor Red; exit 1 }
    $wifipw = Read-Host "Password WiFi"
    if ($wifipw -match "'") { Write-Host "La password WiFi non puo' contenere l'apostrofo." -ForegroundColor Red; exit 1 }
}

# 3. dietpi.txt
$cfgPath = Join-Path $sd 'dietpi.txt'
$t = [IO.File]::ReadAllText($cfgPath)
$settings = [ordered]@{
    'AUTO_SETUP_AUTOMATED'            = '1'
    'AUTO_SETUP_GLOBAL_PASSWORD'      = $pw1
    'AUTO_SETUP_NET_HOSTNAME'         = $hostname
    'AUTO_SETUP_TIMEZONE'             = 'Europe/Rome'
    'AUTO_SETUP_KEYBOARD_LAYOUT'      = 'it'
    'AUTO_SETUP_NET_ETHERNET_ENABLED' = '1'
    'AUTO_SETUP_NET_WIFI_ENABLED'     = $(if ($ssid) { '1' } else { '0' })
    'AUTO_SETUP_NET_WIFI_COUNTRY_CODE'= 'IT'
    'AUTO_SETUP_SSH_SERVER_INDEX'     = '-2'
    'CONFIG_SERIAL_CONSOLE_ENABLE'    = '0'
    'AUTO_SETUP_APT_INSTALLS'         = 'python3 python3-venv python3-pip python3-yaml avahi-daemon iw'
    'AUTO_SETUP_CUSTOM_SCRIPT_EXEC'   = '0'
    'SURVEY_OPTED_IN'                 = '0'
}
foreach ($k in $settings.Keys) { $t = Set-Key $t $k $settings[$k] }
Write-LF $cfgPath $t
Write-Host "dietpi.txt configurato"

# 4. WiFi
if ($ssid) {
    $wPath = Join-Path $sd 'dietpi-wifi.txt'
    $w = if (Test-Path $wPath) { [IO.File]::ReadAllText($wPath) } else { '' }
    $w = Set-Key $w 'aWIFI_SSID[0]' "'$ssid'"
    $w = Set-Key $w 'aWIFI_KEY[0]' "'$wifipw'"
    Write-LF $wPath $w
    Write-Host "WiFi configurato"
}

# 5. file MeshWatch
Copy-Item (Join-Path $here 'Automation_Custom_Script.sh') $sd -Force
$dst = Join-Path $sd 'meshwatch'
if (Test-Path $dst) { Remove-Item $dst -Recurse -Force }
Copy-App $dst
$cfgDst = Join-Path $sd 'meshwatch-config.yaml'
if (Test-Path $cfgDst) {
    Write-Host "meshwatch-config.yaml gia' presente sulla SD: lo lascio com'e'."
} else {
    Copy-Item (Join-Path $dst 'config.example.yaml') $cfgDst
}
Write-Host "File MeshWatch copiati`n"

Write-Host "Ora apro meshwatch-config.yaml: inserisci nome del SenseCAP, ripetitori e token Telegram, poi salva." -ForegroundColor Cyan
if ($IsWindows -ne $false) { Start-Process notepad.exe $cfgDst -Wait }

Write-Host "`nFatto." -ForegroundColor Green
Write-Host "Espelli la scheda, mettila nel Raspberry con la radio collegata e accendilo."
Write-Host "Dopo 10-15 minuti la pagina e' su  http://$hostname.local:8080"
