@echo off
title MeshWatch - abilita gli aggiornamenti dalla pagina web
echo Una tantum, per le schede preparate prima della versione 1.1.
echo Il Raspberry deve essere acceso, in rete e collegato a Internet.
echo.
set /p HOST="Nome del Raspberry in rete [meshwatch.local]: "
if "%HOST%"=="" set HOST=meshwatch.local
echo.
echo Ti verra' chiesta due volte la password dell'utente meshwatch (accesso e sudo).
echo Alla prima connessione rispondi "yes" alla domanda sull'impronta della chiave.
echo.
ssh -t meshwatch@%HOST% "curl -fsSL https://raw.githubusercontent.com/davide-brugnoni/meshwatch/main/tools/abilita-aggiornamenti.sh | sudo bash"
echo.
pause
