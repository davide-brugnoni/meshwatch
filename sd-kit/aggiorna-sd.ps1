# MeshWatch - aggiorna il programma su una SD gia' preparata. Config e dati non vengono toccati.
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
Write-Host "=== MeshWatch - aggiornamento scheda SD ===`n"

if ($SdPath) { $drives = @([pscustomobject]@{ Root = $SdPath }) }
else { $drives = @(Get-PSDrive -PSProvider FileSystem | Where-Object { Test-Path (Join-Path $_.Root 'meshwatch-config.yaml') }) }
if ($drives.Count -eq 0) {
    Write-Host "Non trovo la scheda MeshWatch (manca meshwatch-config.yaml)." -ForegroundColor Red
    Write-Host "Spegni il Raspberry, togli la SD, inseriscila nel PC e rilancia."
    Write-Host "Se la SD non e' mai stata preparata usa PREPARA-SD.bat."
    exit 1
}
$drive = $drives[0]
if ($drives.Count -gt 1) {
    for ($i = 0; $i -lt $drives.Count; $i++) { Write-Host "  $i) $($drives[$i].Root)" }
    $drive = $drives[[int](Read-Host "Quale scheda aggiorno? (numero)")]
}
$sd = $drive.Root
$dst = Join-Path $sd 'meshwatch'
if (Test-Path $dst) { Remove-Item $dst -Recurse -Force }
Copy-App $dst
# segnale per il Raspberry: al prossimo avvio copia il programma dalla SD (poi lo cancella da solo)
[IO.File]::WriteAllText((Join-Path $dst 'AGGIORNA'), "aggiornamento da SD`n")
Copy-Item (Join-Path $here 'Automation_Custom_Script.sh') $sd -Force
Write-Host "Programma aggiornato su $sd" -ForegroundColor Green
Write-Host "meshwatch-config.yaml e i dati sul Raspberry non sono stati toccati."
Write-Host "`nSuggerimento: dalla versione 1.1 puoi aggiornare anche dalla pagina web"
Write-Host "(Gestione -> Sistema -> Aggiornamento), senza togliere la SD."
Write-Host "`nEspelli la scheda, rimettila nel Raspberry e accendilo: al riavvio usa la nuova versione."
