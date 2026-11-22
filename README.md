# MeshWatch

[![Test](https://github.com/davide-brugnoni/meshwatch/actions/workflows/test.yml/badge.svg)](https://github.com/davide-brugnoni/meshwatch/actions/workflows/test.yml)
![Versione](https://img.shields.io/badge/versione-1.1.0-blue)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)
[![Licenza MIT](https://img.shields.io/badge/licenza-MIT-green)](LICENSE)

Dashboard self-hosted per reti **MeshCore**: segue la posizione GPS di uno o più dispositivi (per esempio un SenseCAP T1000-E), monitora i ripetitori e si gestisce da una pagina web in rete locale o da un **bot Telegram** quando si è fuori casa.

Gira su un Raspberry Pi con una radio MeshCore in modalità *Companion USB* (Heltec, RAK, LilyGO, XIAO, …) collegata via USB, oppure col firmware *Companion WiFi* in rete.

*Self-hosted dashboard for MeshCore networks: GPS tracking of companion devices via telemetry requests, repeater monitoring and remote CLI, local web UI and Telegram bot. Runs on a Raspberry Pi with a USB companion radio. Documentation is in Italian.*

![Mappa](docs/screenshots/mappa.png)

## Indice

- [Cosa fa](#cosa-fa)
- [Come funziona](#come-funziona)
- [Avvio rapido](#avvio-rapido)
- [Installazione](#installazione)
- [Configurazione](#configurazione)
- [Usare la pagina](#usare-la-pagina)
- [Bot Telegram](#bot-telegram)
- [Chat](#chat)
- [Sensori](#sensori)
- [Mappa della rete e rubrica](#mappa-della-rete-e-rubrica)
- [Percorso dei pacchetti](#percorso-dei-pacchetti)
- [Mappa della copertura](#mappa-della-copertura)
- [Mappe e cache delle tessere](#mappe)
- [Tempo reale](#tempo-reale)
- [Manutenzione e backup](#manutenzione-e-backup)
- [Aggiornamenti](#aggiornamenti)
- [Affidabilità: watchdog, radio, WiFi](#affidabilità-watchdog-radio-wifi)
- [Accesso da Internet](#accesso-da-internet)
- [Sicurezza](#sicurezza)
- [Dove stanno i file](#dove-stanno-i-file)
- [Comandi utili](#comandi-utili)
- [Risoluzione dei problemi](#risoluzione-dei-problemi)
- [Limiti](#limiti)
- [API](#api)
- [Database](#database)
- [Struttura del codice](#struttura-del-codice)
- [Sviluppo e test](#sviluppo-e-test)
- [Licenza](#licenza)

## Cosa fa

- **Tracciamento GPS**: a intervalli regolari chiede la telemetria ai dispositivi scelti e salva la traccia. Più dispositivi, ognuno con il suo colore, intervallo, fascia oraria e soglie di avviso.
- **Intervallo adattivo**: facoltativo, per dispositivo. Se tra due posizioni si è spostato più di 150 m viene interrogato più spesso; quando si ferma torna all'intervallo normale.
- **Pulizia tracce**: dal cestino sulla mappa (tutti i dispositivi) o da "Cancella traccia" (uno solo) si eliminano tutte le posizioni salvate, compresa l'ultima: sulla mappa non resta nulla. Solo admin.
- **Sensori**: tutti i valori della telemetria (temperatura, umidità, pressione, luce, tensione, …) salvati a ogni risposta, anche senza fix GPS, con grafico per sensore e **soglie di avviso** su Telegram.
- **Percorso dei pacchetti**: per ogni risposta i ripetitori attraversati (*via Rip-Monte → Rip-Valle*), disegnati sulla mappa dal gateway al dispositivo; percorso anche per ogni contatto della rubrica.
- **Mappa della copertura**: celle colorate dove il dispositivo risponde e dove no, con salti medi: mostra dove manca un ripetitore.
- **Ripetitori**: login e statistiche periodiche (batteria, uptime, rumore, SNR, RSSI, pacchetti) con grafici di batteria, rumore, SNR e RSSI.
- **Chat**: messaggi privati e canali (Public e canali privati) direttamente dalla Heltec collegata al Raspberry, dalla pagina, senza accendere un altro dispositivo. Conferma di consegna, nuovi tentativi automatici, messaggi salvati anche a pagina chiusa.
- **Mappa della rete**: livello "Rete MeshCore" con i nodi che annunciano la posizione e i collegamenti tra ripetitori vicini colorati per SNR.
- **Rubrica del gateway**: contatti della Heltec con salti, posizione e ultimo advert; preferiti, azzera percorso, rimuovi, aggiunta manuale dei nuovi nodi.
- **Mappe a scelta**: OpenStreetMap, CyclOSM, OpenTopoMap, satellite Esri (con o senza nomi). La scelta resta salvata nel browser.
- **Cache delle mappe**: il Raspberry conserva le tessere già viste, così le zone abituali si aprono subito e restano visibili anche senza Internet. Spazio massimo e pulizia dalla Gestione.
- **Terminale**: comandi CLI ai ripetitori dalla pagina o da Telegram, con conferma per i comandi che modificano o riavviano e registro di tutti i comandi inviati.
- **Gateway**: stato e configurazione della radio collegata (nome, potenza, posizione, parametri radio), advert, riavvio.
- **Sistema**: temperatura, memoria, disco, **avviso di tensione bassa**, riavvio e spegnimento del Raspberry. Se la pagina si blocca il programma si riavvia da solo; il WiFi resta sempre sveglio e si spegne da solo quando è collegato il cavo di rete.
- **Radio**: se la radio si scollega arriva un avviso su Telegram e dopo 3 minuti il programma la ricollega da solo, ritrovando la porta USB anche se è cambiata.
- **Manutenzione e backup**: pulizia automatica dei dati più vecchi di N giorni (default 180); backup di database e config scaricabile dalla pagina o inviato ogni settimana su Telegram, facoltativamente protetto da password; **ripristino dalla pagina** con verifica preventiva e annullamento.
- **Registro attività**: login, tentativi falliti e ogni azione di gestione, dalla pagina e da Telegram.
- **Telegram**: posizione con pin e link Google Maps, ultima posizione, traccia, **posizione in diretta**, **mappa come immagine** con la traccia, statistiche, stato del Raspberry, **riepilogo giornaliero** automatico, avvisi (dispositivo che non risponde, batteria bassa, soglie dei sensori, ripetitore muto, radio scollegata, nuova versione disponibile).
- **Avvisi batteria bassa** (Gestione → Dispositivi → Avvisi batteria bassa): ogni quanto ripeterli finché la batteria resta bassa (o una volta sola) e una fascia oraria di silenzio (es. 22:00-07:00). Vale per i dispositivi seguiti e per i ripetitori.
- **Tempo reale**: posizioni, ripetitori, stato della radio e messaggi compaiono sulla pagina appena arrivano, senza ricaricare.
- **Da telefono**: pagina pensata anche per lo smartphone (barra in basso Mappa · Nodi · Chat · Gestione).
- **Aggiornamento dalla pagina**: scarica l'ultima versione da GitHub e riavvia, senza togliere la SD; se la nuova versione non parte torna da sola alla precedente. Quando ne esce una nuova arriva un avviso.
- **Accesso**: la mappa è libera in casa (sola lettura), la Gestione chiede sempre la password admin. Ruoli *admin* (gestisce tutto) e *viewer* (vede, chiede la posizione e usa la chat). Predisposto per l'accesso da Internet tramite Cloudflare Tunnel.
- **Funziona senza Internet**: pagina, radio, tracciamento e mappe già viste restano disponibili anche se la connessione di casa cade; il bot Telegram si ricollega da solo quando torna.

| Gestione | Sistema e gateway |
|---|---|
| ![Gestione](docs/screenshots/gestione.png) | ![Sistema](docs/screenshots/sistema-gateway.png) |

## Come funziona

```
Dispositivo GPS ─LoRa─ ripetitori ─LoRa─ Heltec (Companion USB) ─USB─ Raspberry Pi ─┬─ pagina web (rete di casa)
                                                                                  └─ bot Telegram (ovunque)
```

MeshWatch usa la [libreria Python ufficiale `meshcore`](https://pypi.org/project/meshcore/). La posizione si ottiene con richieste di **telemetria**: il dispositivo da seguire deve permettere al gateway di leggerla (nell'app MeshCore: gateway tra i *preferiti*, telemetria posizione "solo preferiti").

Il programma è un unico processo Python (asyncio) che fa girare insieme:

| Parte | Cosa fa |
|---|---|
| radio (`mesh.py`) | collegamento alla radio, coda delle richieste (una alla volta, la chat ha la precedenza) |
| ciclo dei dispositivi (`core.py`) | richiesta di telemetria a ogni dispositivo secondo intervallo e fascia oraria, avvisi |
| ciclo dei ripetitori (`core.py`) | login e statistiche ogni `repeater_interval_min` minuti, vicini ogni 6 ore |
| pagina e API (`web.py`) | FastAPI + uvicorn, porta 8080, eventi in tempo reale (SSE) |
| bot (`bot.py`) | python-telegram-bot in polling: non serve aprire porte |
| manutenzione (`maintenance.py`) | pulizia giornaliera, backup settimanale |
| watchdog (`watchdog.py`) | segnale a systemd finché la pagina risponde |

Tutti i dati stanno in un file SQLite (`meshwatch.db`); le tessere delle mappe in un secondo file (`tiles.db`).

## Avvio rapido

1. Radio MeshCore con firmware **Companion USB** collegata al Raspberry.
2. Installa con il [kit SD](#metodo-1--sd-pronta-con-dietpi-consigliato-da-windows) (da Windows, senza terminale) o con [`install.sh`](#metodo-2--su-un-raspberry-pi-os-esistente).
3. Apri `http://meshwatch.local:8080` (oppure `http://IP-del-raspberry:8080`).
4. **Gestione** → imposta la password admin → **Dispositivi** → aggiungi il dispositivo da seguire con il nome del contatto MeshCore.
5. Facoltativo: token del bot Telegram nel config (vedi [Bot Telegram](#bot-telegram)).

## Installazione

### Requisiti

- Raspberry Pi 2/3/4/5 (o qualsiasi Linux con Python 3.11+) e microSD da 8 GB o più
- radio MeshCore con firmware **Companion USB**: qualsiasi scheda supportata da MeshCore (Heltec V3/V4, RAK4631/WisMesh, LilyGO T-Beam/T-Echo/T-Deck, Seeed XIAO, Station G2, …). In alternativa firmware **Companion WiFi** con `connection: wifi` e `wifi_host` nel config
- alimentatore adeguato (5,1 V 2,5 A per il Pi 3): con tensione bassa la rete e la USB diventano instabili
- Internet al primo avvio (installazione dei pacchetti); dopo serve solo per Telegram, aggiornamenti e mappe non ancora viste

Librerie Python ([`requirements.txt`](requirements.txt)): `meshcore`, `python-telegram-bot`, `fastapi`, `uvicorn`, `PyYAML`, `httpx`, `Pillow` (mappa come immagine), `pyzipper` (backup con password).

### Metodo 1 — SD pronta con DietPi (consigliato, da Windows)

Nella cartella [`sd-kit/`](sd-kit/) c'è tutto per creare una scheda che si installa da sola al primo avvio. Istruzioni passo passo in [`sd-kit/LEGGIMI.txt`](sd-kit/LEGGIMI.txt).

1. Scarica DietPi per Raspberry Pi (ARMv8 64-bit per Pi 3/4/5, ARMv7 per Pi 2) e scrivilo sulla SD con balenaEtcher (o Raspberry Pi Imager *senza* personalizzazione del sistema operativo).
2. Doppio clic su `sd-kit/PREPARA-SD.bat`: chiede nome del Raspberry in rete, password e WiFi, copia MeshWatch sulla SD e apre `meshwatch-config.yaml` nel Blocco note.
3. Inserisci la SD nel Raspberry con la radio collegata e accendi: in 10–15 minuti (20–30 su Pi 2/3) è pronto su `http://meshwatch.local:8080`.
4. Apri **Gestione**: la prima volta chiede di impostare la password admin.

Cosa fa il primo avvio ([`sd-kit/Automation_Custom_Script.sh`](sd-kit/Automation_Custom_Script.sh)):

- installa Python, avahi (per `meshwatch.local`), `iw` e i font DejaVu;
- crea l'utente di servizio `meshwatch-svc` (senza login, nel gruppo `dialout`) e l'utente SSH `meshwatch` con la password scelta; root e `dietpi` non possono più entrare via SSH;
- copia il programma in `/opt/meshwatch`, crea il venv e installa le librerie;
- configura il servizio con [`tools/configura-servizio.sh`](tools/configura-servizio.sh): a ogni avvio il config viene ricopiato dalla SD in `/var/lib/meshwatch/config.yaml`.

Se qualcosa va storto al primo avvio, sulla SD (vista da Windows) c'è `meshwatch-install.log`.

Accesso SSH con l'utente `meshwatch` (PuTTY/WinSCP, host `meshwatch.local`). Per cambiare token Telegram o impostazioni generali: spegni, modifica `meshwatch-config.yaml` sulla SD dal PC, riaccendi. Per aggiornare: dalla pagina (vedi [Aggiornamenti](#aggiornamenti)); in alternativa SD nel PC e `sd-kit/AGGIORNA-SD.bat`. Config e dati restano.

Per fare più copie: quando una scheda funziona salvane l'immagine con Win32 Disk Imager e scrivila su altre SD (attenzione: contiene config, password e token).

### Metodo 2 — su un Raspberry Pi OS esistente

```bash
git clone https://github.com/davide-brugnoni/meshwatch.git
cd meshwatch
bash install.sh
nano config.yaml
sudo systemctl start meshwatch
```

[`install.sh`](install.sh) installa i pacchetti di sistema (`python3-venv`, `python3-pip`, `fonts-dejavu-core`), crea il venv in `./venv`, aggiunge il tuo utente al gruppo `dialout`, crea `config.yaml` dall'esempio, abilita riavvio/spegnimento dalla pagina, disattiva il risparmio energetico del WiFi e installa il servizio systemd `meshwatch` (eseguito con il tuo utente, nella cartella del clone). Si può rilanciare quando si vuole: config e dati non vengono toccati.

Dopo l'aggiunta al gruppo `dialout` serve un nuovo login (o un riavvio) perché valga anche fuori dal servizio.

### Metodo 3 — a mano (qualsiasi Linux, sviluppo)

```bash
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
cp config.example.yaml config.yaml && nano config.yaml
./venv/bin/python -m meshwatch -c config.yaml
```

## Configurazione

Copia `config.example.yaml` in `config.yaml` (con il kit SD il file è `meshwatch-config.yaml` sulla scheda). Le modifiche al file valgono dal riavvio successivo del servizio.

- `serial_port: auto` trova da solo la radio, qualunque sia la scheda: se ci sono più dispositivi USB li prova uno alla volta, cominciando da quelli con i chip delle radio più diffuse. Per fissarla: `ls /dev/serial/by-id/`.
- `telegram.token`: crealo con @BotFather. `allowed_chat_ids`: chi può usare il bot e riceve gli avvisi; `admin_chat_ids`: chi può usare il terminale. Il tuo ID te lo dà il bot stesso scrivendogli `/start` (oppure @userinfobot).
- Le sezioni `tracker` e `repeaters` vengono importate solo al primo avvio: poi dispositivi, ripetitori e utenti si gestiscono dalla pagina (scheda **Gestione**).

### Tutte le opzioni

| Chiave | Default | Descrizione |
|---|---|---|
| `serial_port` | `auto` | porta della radio USB (`auto` o es. `/dev/serial/by-id/usb-…`) |
| `baudrate` | `115200` | velocità della porta seriale |
| `connection` | `usb` | `wifi` (o `tcp`) per una radio col firmware Companion WiFi |
| `wifi_host` | — | indirizzo IP della radio WiFi (obbligatorio con `connection: wifi`) |
| `wifi_port` | `5000` | porta TCP della radio WiFi |
| `tracker.name` | — | nome del contatto MeshCore da seguire (solo primo avvio) |
| `tracker.interval_min` | `30` | ogni quanti minuti chiedere la posizione (dalla pagina: 5–720) |
| `tracker.active_hours` | vuoto | fascia oraria di interrogazione, es. `"05:00-21:00"` (vuoto = sempre) |
| `tracker.offline_alert_min` | `90` | avviso se non risponde da più di X minuti |
| `tracker.low_battery_v` | `3.5` | soglia di batteria bassa in volt |
| `repeaters` | `[]` | elenco `name` + `password` dei ripetitori (solo primo avvio) |
| `repeater_interval_min` | `60` | ogni quanti minuti interrogare i ripetitori |
| `repeater_low_battery_v` | — | soglia di batteria bassa dei ripetitori in volt |
| `telegram.token` | — | token di @BotFather; vuoto o di esempio = bot disattivato |
| `telegram.allowed_chat_ids` | `[]` | chat autorizzate a usare il bot e a ricevere gli avvisi |
| `telegram.admin_chat_ids` | `[]` | chat che possono usare il terminale e cambiare il riepilogo (vuoto = tutte le autorizzate) |
| `telegram.daily_report` | — | ora del riepilogo giornaliero, es. `"21:00"` (quello impostato dalla pagina ha la precedenza) |
| `web.mode` | `lan` | `lan` (solo in casa) o `public` (dietro Cloudflare Tunnel, login obbligatorio) |
| `web.host` | `0.0.0.0` | indirizzo di ascolto; in modalità `public` usa `127.0.0.1` |
| `web.port` | `8080` | porta della pagina |
| `web.public_map` | `true` in lan, `false` in public | mappa visibile senza login (sola lettura) |
| `web.users` | — | utenti da importare al primo avvio: `username`, `password_hash` (da `meshwatch hashpw`), `role` |
| `database` | `meshwatch.db` | percorso del database SQLite (relativo alla cartella di lavoro del servizio) |
| `update.repo` | `davide-brugnoni/meshwatch` | repository GitHub per l'aggiornamento dalla pagina |
| `update.branch` | `main` | ramo da cui aggiornare |
| `update.notify` | `true` | avviso su Telegram e pallino su Gestione quando esce una nuova versione |

Le altre impostazioni (pulizia dei dati, backup, password del backup, cache delle mappe, avvisi batteria, riepilogo, aggiunta manuale dei contatti) si cambiano dalla pagina e stanno nel database.

### Riga di comando

```bash
python -m meshwatch [run|hashpw] [-c CONFIG]
```

| Comando | Cosa fa |
|---|---|
| `run` (default) | avvia il programma con il config indicato (`-c`, default `config.yaml`) |
| `hashpw` | chiede una password due volte e stampa l'hash PBKDF2 da mettere in `web.users[].password_hash` |

`hashpw` serve solo per preparare un utente senza passare dalla pagina, per esempio per partire subito in modalità `public`:

```yaml
web:
  mode: public
  host: 127.0.0.1
  users:
    - username: admin
      password_hash: "pbkdf2$200000$..."   # output di: ./venv/bin/python -m meshwatch hashpw
      role: admin
```

## Usare la pagina

### Primo accesso e ruoli

- **Primo accesso**: finché non c'è un admin, la pagina mostra la mappa e aprendo **Gestione** chiede di creare la password admin (almeno 8 caratteri). Chi aggiorna da una versione precedente senza utenti trova la stessa richiesta. In modalità `public` il programma non parte senza utenti: crea prima l'admin in modalità `lan` (o con `web.users`).
- **Mappa libera**: `web.public_map` nel config. Di default è `true` in modalità `lan` e `false` in modalità `public`. Con `false` serve il login anche per la mappa.
- Altri utenti (admin o viewer) si creano da **Gestione → Accessi**. Le sessioni durano 30 giorni.

| | Mappa e stato | Chiedere la posizione | Chat, rete, percorsi, copertura | Gestione |
|---|---|---|---|---|
| senza login (in casa) | sì | no | no | no |
| utente *viewer* | sì | sì | sì | no |
| utente *admin* | sì | sì | sì | sì |

### Le schede

| Scheda | Contenuto |
|---|---|
| **Mappa** | mappa con le tracce dei dispositivi, livelli (Rete MeshCore, Percorsi, Copertura), scelta della mappa, cestino delle tracce; accanto la colonna dei **nodi**: schede dei dispositivi (posizione, batteria, sensori, percorso, grafici), ripetitori con statistiche e, per gli admin, il terminale |
| **Chat** | conversazioni private e canali dalla radio del gateway (con login) |
| **Gestione** | tutta la configurazione (solo admin) |

Su telefono la barra in basso ha Mappa · Nodi · Chat · Gestione.

### La scheda Gestione

È divisa in sezioni, scelte dalla barra in alto (resta in vista mentre si scorre; l'ultima sezione aperta resta ricordata):

| Sezione | Cosa contiene |
|---|---|
| **Dispositivi** | dispositivi tracciati (nome contatto, etichetta, colore, intervallo, intervallo in movimento, fascia oraria, avvisi, soglie dei sensori), avvisi batteria bassa |
| **Rete** | ripetitori, gateway (radio collegata al Raspberry), rubrica del gateway |
| **Telegram** | riepilogo giornaliero (acceso/spento, ora di invio, *Invia ora*), backup settimanale, comandi del bot |
| **Sistema** | Raspberry, aggiornamento programma, manutenzione e backup, ripristino, cache delle mappe |
| **Accessi** | utenti della pagina, registro attività |

Le spiegazioni più lunghe sono chiuse sotto *Come funziona*. Su computer largo le schede stanno su due colonne.

### Terminale dei ripetitori

Dalla colonna dei nodi, riquadro **Terminale ripetitori** (solo admin) o da Telegram con `/terminale`: si sceglie un ripetitore configurato e si scrivono i comandi CLI di MeshCore (`ver`, `clock`, `get radio`, `neighbors`, …). I comandi che cambiano la configurazione o riavviano chiedono conferma. Ogni comando e la sua risposta finiscono nel registro del terminale (visibile per ripetitore) e nel registro attività.

### Riavvio e spegnimento dalla pagina

Il servizio gira senza privilegi. Il kit SD e `install.sh` abilitano da soli i due soli comandi necessari; su installazioni manuali o precedenti:

```bash
echo 'meshwatch-svc ALL=(root) NOPASSWD: /usr/bin/systemctl reboot, /usr/bin/systemctl poweroff' | sudo tee /etc/sudoers.d/meshwatch && sudo chmod 440 /etc/sudoers.d/meshwatch && sudo visudo -cf /etc/sudoers.d/meshwatch
```

(con `install.sh` l'utente del servizio è il tuo utente invece di `meshwatch-svc`: lo script lo configura da solo).

## Bot Telegram

### Configurazione

1. Su Telegram scrivi a **@BotFather** → `/newbot` → copia il token in `telegram.token`.
2. Riavvia MeshWatch e scrivi `/start` al bot: risponde con il tuo **chat ID**.
3. Mettilo in `telegram.allowed_chat_ids` (e in `admin_chat_ids` se deve poter usare il terminale) e riavvia.

Il bot funziona in *polling*: non serve aprire porte né esporre il Raspberry. Senza token valido il resto del programma funziona normalmente. I comandi arrivati mentre il servizio era fermo vengono eseguiti alla ripartenza, tranne quelli più vecchi di 3 minuti.

### Comandi

| Comando | Cosa fa |
|---|---|
| `/start` · `/aiuto` | tastiera del bot (e chat ID se non autorizzato) · elenco dei comandi |
| `/posizione` | chiede subito la posizione (pin + link Google Maps) |
| `/dove` · `/ultima` · `/traccia` | ultima posizione ricevuta · punti delle ultime 12 ore |
| `/segui 2` · `/basta` | posizione **in diretta** di Telegram per 2 ore (massimo 8): il pin si sposta a ogni nuova posizione · la ferma |
| `/stats` · `/stats 7` | km percorsi, ore in movimento, risposte, quota e batteria delle ultime 24 ore · degli ultimi 7 giorni |
| `/sensori` · `/percorso` | ultimi valori dei sensori · ripetitori attraversati dall'ultima risposta |
| `/mappa` · `/mappa 7` | **immagine della mappa** con la traccia delle ultime 24 ore · degli ultimi 7 giorni, l'ultima posizione, gateway e ripetitori della zona |
| `/nodi` · `/aggiorna` | stato di dispositivi e ripetitori · interroga subito i ripetitori |
| `/sistema` | Raspberry (temperatura, memoria, disco, tensione bassa), radio, database, ultimo backup |
| `/riepilogo` | riepilogo della giornata con la mappa; `/riepilogo 21:00` ogni giorno a quell'ora, `/riepilogo no` lo spegne |
| `/intervallo 30` · `/pausa` · `/riprendi` | gestione del tracciamento |
| `/advert` | il gateway si annuncia sulla mesh |
| `/aggiornamento` · `/versione` | versione installata e, se ne è uscita una nuova, pulsante **Aggiorna ora** con conferma (solo chat admin) |
| `/terminale` · `/fine` | comandi CLI a un ripetitore (solo chat admin) · chiude il terminale |

La tastiera del bot ha i pulsanti 📍 Posizione ora, 🕓 Ultima posizione, 🗺 Mappa, 👣 Segui 2 ore, 📊 Statistiche, 📝 Riepilogo, 📡 Nodi e 🖥 Sistema. Dopo un aggiornamento che la cambia il bot la rimanda da solo una volta; altrimenti basta /start.

Con più dispositivi si scrive il nome dopo il comando, anche solo l'inizio e senza badare alle maiuscole: `/dove escursionista`, `/stats 7 esc`, `/intervallo 15 ciclista`, `/segui 3 esc`. Senza nome il bot chiede quale con dei pulsanti. Scrivendo `/` Telegram mostra l'elenco dei comandi.

Con `/segui` conviene attivare l'intervallo adattivo del dispositivo: in movimento le posizioni arrivano più spesso e il pin si sposta di conseguenza.

### Avvisi

Arrivano a tutte le chat in `allowed_chat_ids`:

- dispositivo che non risponde da più di `offline_alert_min` minuti, e quando torna;
- batteria bassa di dispositivi e ripetitori (ripetizione e fascia di silenzio in **Gestione → Dispositivi → Avvisi batteria bassa**);
- valore di un sensore fuori soglia e rientro nella norma;
- ripetitore che non risponde;
- radio scollegata e ricollegata;
- nuova versione disponibile (una volta per versione), esito di aggiornamenti e ripristini.

### Mappa e riepilogo su Telegram

Servono a vedere da fuori casa quello che mostra la pagina, senza metterla su Internet. La mostra anche il pulsante **🗺 Mappa** della tastiera del bot.

- La mappa la disegna il Raspberry (libreria Pillow) in qualche secondo. Senza Pillow il bot risponde in testo e dice come installarla. Per le lettere accentate nei nomi serve il font DejaVu (`fonts-dejavu-core`, già installato dal kit SD e da `install.sh`; su schede preparate prima: `sudo apt install fonts-dejavu-core`): senza, i nomi compaiono senza accenti.
- Se all'avvio Internet non c'è ancora, pagina e radio partono lo stesso e il bot si collega a Telegram appena la rete torna (riprova da solo, con attese crescenti fino a 5 minuti).
- Le tessere OpenStreetMap passano dalla [cache delle mappe](#cache-delle-mappe-sul-raspberry), anche quando è disattivata per la pagina: le zone già viste non vengono scaricate di nuovo. Senza Internet la mappa arriva lo stesso, su fondo grigio.
- Gateway e ripetitori compaiono solo se hanno una posizione e cadono nella zona della traccia: un ripetitore lontano non rimpicciolisce la mappa.
- Con più dispositivi, **Tutti** mette le tracce di tutti in un'unica mappa, ognuna con il suo colore.
- **Riepilogo automatico**: si accende dalla pagina (**Gestione → Telegram → Riepilogo giornaliero**: interruttore, ora di invio, *Invia ora* per provarlo) oppure dal bot con `/riepilogo 21:00` (solo chat admin). Lo manda ogni giorno a tutte le chat autorizzate: km, risposte e batteria di ogni dispositivo, stato dei ripetitori e del Raspberry, mappa della giornata. Si può impostare anche nel config con `telegram.daily_report: "21:00"`; quello impostato dalla pagina o da Telegram ha la precedenza. Se a quell'ora il Raspberry era spento, arriva appena riparte, entro mezz'ora.

## Chat

Scheda **Chat** (barra in alto o in basso sul telefono). Serve il login: la usano sia gli *admin* sia i *viewer*.

- **Con chi**: i canali configurati nella Heltec (Public e quelli privati) e i contatti *companion* o *room server* della sua rubrica. Per una nuova conversazione scegli dal menu "Nuova conversazione" oppure "Scrivi" dalla rubrica.
- **Mittente**: chi riceve vede come mittente il **nome del gateway** (Gestione → Rete → Gateway), non quello del tuo telefono.
- **Stato dei messaggi privati**: *in coda* → *inviato* → *✓✓ consegnato* quando arriva la conferma. Senza conferma il gateway riprova (3 tentativi, l'ultimo in flood) e poi segna *nessuna conferma*, con il pulsante **Riprova**. Sui canali non esiste conferma: *✓ trasmesso*.
- **Precedenza**: la radio fa una cosa alla volta. Un messaggio passa davanti alle richieste di posizione e ai ripetitori in coda; se in quel momento ne è in corso una, aspetta che finisca (fino a circa 40 secondi).
- **Lunghezza**: circa 150 byte (le lettere accentate e le emoji contano di più); sui canali un po' meno, perché il nome del gateway fa parte del testo. Al massimo 10 messaggi al minuto, per rispettare il duty cycle.
- **Ricezione**: i messaggi arrivano anche a pagina chiusa e restano nel database (vengono cancellati con la pulizia dei dati vecchi). Il numero sulla scheda Chat indica i non letti. Le risposte del terminale dei ripetitori non finiscono in chat.
- **Cancellare** una conversazione: solo admin.
- Su Telegram i messaggi **non** vengono inoltrati. Nel registro attività finisce chi ha scritto e a chi, non il testo.

## Sensori

Ogni risposta alla telemetria porta, oltre alla posizione, i valori dei sensori del dispositivo in formato Cayenne LPP (per esempio il T1000-E manda temperatura e luce; un nodo sensore può mandare umidità, pressione, tensioni, …). MeshWatch li salva tutti, anche quando il GPS non ha il fix.

- Nella scheda del dispositivo compaiono gli ultimi valori e il grafico del sensore scelto per il periodo selezionato. La tensione sul canale 1 è la batteria del dispositivo.
- **Soglie** (solo admin): Gestione → Dispositivi → Dispositivi tracciati → **Soglie**. Si sceglie un sensore già ricevuto almeno una volta, *sopra* o *sotto* e il valore. Arriva un avviso su Telegram quando il valore esce dalla soglia e uno quando torna nella norma. Massimo 20 soglie per dispositivo.
- Per seguire un nodo che ha solo sensori (senza GPS) si aggiunge come un dispositivo normale: la mappa non mostra posizioni, la scheda mostra i sensori.
- Le letture vengono cancellate con la pulizia dei dati vecchi.

## Mappa della rete e rubrica

- Sulla mappa, dal selettore dei livelli, attiva **Rete MeshCore** (serve il login): rombi gialli i ripetitori, cerchi blu i companion, quadrato verde il gateway. Le linee uniscono i ripetitori che si sentono tra loro: verde SNR ≥ 5 dB, giallo 0–5, arancione sotto 0. La linea tratteggiata indica un ripetitore che il gateway sente direttamente. Compaiono solo i nodi che annunciano la posizione.
- I vicini vengono chiesti ai ripetitori configurati al massimo ogni 6 ore (servono la password del ripetitore e un firmware recente). Per aggiornarli subito: **Gestione → Rete → Rubrica del gateway → Aggiorna mappa della rete**.
- **Rubrica del gateway** (solo admin): cerca, segna i preferiti, azzera il percorso di un nodo che non risponde più, rimuovi i contatti inutili (la Heltec ne tiene un numero limitato). Con l'**aggiunta manuale** attiva i nodi nuovi restano in attesa finché non li aggiungi. Se rimuovi un contatto usato da un dispositivo o da un ripetitore, la pagina ti avvisa.

## Percorso dei pacchetti

Per raggiungere un nodo il gateway usa un percorso: l'elenco dei ripetitori da attraversare, imparato dalle risposte precedenti. A ogni risposta del dispositivo MeshWatch salva salti e percorso.

- Nella scheda del dispositivo: *diretto dal gateway*, *via Rip-Monte → Rip-Valle* oppure *percorso sconosciuto (flood)* quando il gateway non conosce ancora la strada e la richiesta viene diffusa da tutti.
- Sulla mappa (con login), livello **Percorsi**: linea tratteggiata dal gateway, attraverso i ripetitori, all'ultima posizione. Servono le posizioni negli advert di gateway e ripetitori; quelli senza posizione vengono saltati e la linea è approssimata.
- MeshCore identifica i ripetitori del percorso con il primo byte della chiave pubblica: se due ripetitori lo condividono viene scelto quello sentito più di recente e il nome ha un **?**.
- Nella rubrica del gateway ogni contatto mostra il percorso in uso.
- L'ospite senza login non vede i percorsi.

## Mappa della copertura

Livello **Copertura** sulla mappa (con login). Ogni richiesta di posizione viene ricordata con il punto in cui si trovava il dispositivo; le richieste vengono raggruppate in celle di circa 250 m:

| Colore | Il dispositivo ha risposto |
|---|---|
| verde | almeno l'80% delle volte |
| giallo | dal 50 all'80% |
| arancione | meno della metà delle volte |
| rosso | mai |

- Toccando una cella: risposte su richieste, salti medi, ultima richiesta. Le celle più piene sono più intense.
- Il periodo (7 giorni, 30, 90, un anno) si sceglie nella legenda.
- Quando il dispositivo **non risponde** non si sa dove sia: la richiesta va nella sua ultima posizione nota, se ha meno di un'ora (o di due intervalli). È una stima: le celle rosse indicano *dove il dispositivo era quando ha smesso di rispondere*, cioè il bordo della copertura.
- Le richieste fatte prima di questa versione compaiono solo se avevano ricevuto una posizione.

## Mappe

Tutte gratuite e senza chiave, si sceglie dal pulsante dei livelli in alto a destra sulla mappa:

| Mappa | Adatta per |
|---|---|
| OpenStreetMap — predefinita | la più dettagliata (sentieri, edifici, numeri civici) |
| Escursionismo (CyclOSM) | sentieri, piste ciclabili, rilievo |
| Topografica (OpenTopoMap) | curve di livello, montagna (zoom massimo più basso) |
| Satellite (Esri) | foto aeree |
| Satellite con nomi | foto aeree con confini e località |

La libreria della mappa (Leaflet) è inclusa in `web/vendor/`: la pagina si apre anche senza Internet.

### Cache delle mappe sul Raspberry

OpenStreetMap, CyclOSM e OpenTopoMap passano dal Raspberry (`/api/tiles/…`): la prima volta che una zona viene aperta il Raspberry scarica le tessere e le conserva, le volte dopo le dà subito dalla SD. Le zone già viste restano sulla mappa anche se Internet non c'è.

Si gestisce da **Gestione → Sistema → Cache delle mappe**:

- attiva o disattivata (disattivata: il browser carica le tessere direttamente da Internet, come prima);
- spazio massimo sulla SD (default 500 MB, circa 15–25 mila tessere): oltre, si cancellano quelle usate meno di recente;
- tessere salvate, spazio usato, percentuale servita dalla cache, pulsante **Svuota cache**.

Regole rispettate ([uso delle tessere di OpenStreetMap](https://operations.osmfoundation.org/policies/tiles/)):

- si salvano solo le tessere che qualcuno guarda: **niente scaricamento di intere regioni**, che le regole vietano;
- il Raspberry si presenta come `MeshWatch/<versione>`;
- ogni tessera vale quanto dice il server; scaduta, il Raspberry chiede se è cambiata e, se no, il server risponde senza rimandarla. Se Internet non c'è viene mostrata quella vecchia;
- al massimo 4 download contemporanei e 600 al minuto.

Le mappe satellitari Esri non passano dalla cache (le condizioni di Esri non lo prevedono): arrivano sempre direttamente da Internet al browser.

Le tessere stanno in `tiles.db`, accanto a `meshwatch.db` (sulla SD del kit: `/var/lib/meshwatch/`). Non entrano nel backup e si possono cancellare in qualsiasi momento. Sotto i 300 MB di spazio libero sulla SD non se ne salvano di nuove.

Da Internet (Cloudflare Tunnel) le tessere passano dalla connessione di casa: se la mappa risulta più lenta fuori casa, disattiva la cache.

## Tempo reale

La pagina resta collegata al Raspberry (*Server-Sent Events*, `/api/events`): quando arriva una posizione, una lettura dei ripetitori, un messaggio o la radio si scollega, il Raspberry avvisa tutte le pagine aperte e queste si aggiornano subito. Accanto allo stato della radio compare **· in diretta**.

- Se il collegamento cade (Wi‑Fi, riavvio, aggiornamento) il browser si ricollega da solo dopo pochi secondi; nel frattempo la pagina torna a chiedere le novità ogni minuto (ogni 4 secondi la chat aperta).
- Funziona anche tramite Cloudflare Tunnel: ogni 25 secondi passa un segnale che tiene aperta la connessione.
- L'ospite senza login riceve solo gli aggiornamenti della mappa, non quelli della chat. Al massimo 50 pagine collegate insieme.

## Manutenzione e backup

In **Gestione → Sistema → Manutenzione e backup** (il backup su Telegram è in **Gestione → Telegram**):

- **Conserva i dati per N giorni** (default 180, `0` = per sempre). Una volta al giorno vengono cancellati posizioni, letture dei sensori, statistiche dei ripetitori, messaggi, comandi del terminale e registro più vecchi. Restano sempre l'ultima posizione di ogni dispositivo e l'ultimo stato di ogni ripetitore. C'è anche il pulsante per pulire subito.
- **Scarica backup**: uno zip con `meshwatch.db`, `config.yaml` e le istruzioni di ripristino (`LEGGIMI.txt`).
- **Backup settimanale su Telegram**: va attivato. Arriva solo alle chat in `admin_chat_ids` (o, se vuoto, a quelle in `allowed_chat_ids`). Si può anche mandare subito.
- **Password del backup** (facoltativa): database e config nello zip vengono cifrati con AES-256, sia nel backup scaricato sia in quello su Telegram. `LEGGIMI.txt` resta leggibile e spiega come aprirlo. Su Windows serve **7-Zip** (Esplora risorse non apre questi zip), su Linux `7z x` (pacchetto `p7zip-full`; `unzip` non li apre). La pagina non mostra mai la password e il registro segna solo che è stata impostata o tolta.

Il backup contiene password (token Telegram, ripetitori) e posizioni: conservalo con cura, meglio se con la password. La cache delle mappe (`tiles.db`) non è inclusa.

### Ripristino dalla pagina

**Gestione → Sistema → Ripristina un backup**:

1. scegli lo zip (e la password, se è protetto) e premi **Controlla il backup**: il Raspberry verifica password, integrità del database e che questa versione lo sappia aprire, poi mostra cosa contiene (dispositivi, ripetitori, utenti, posizioni, bot Telegram, radio) e gli avvisi (posizioni recenti che si perderebbero, bot o porta della pagina diversi). Fin qui non cambia niente;
2. scegli **Dati**, **Configurazione** o entrambi e premi **Ripristina e riavvia**: lo stato attuale viene salvato, il servizio si riavvia (circa un minuto) e la pagina si ricarica da sola. L'esito finisce nel registro attività e arriva su Telegram;
3. se qualcosa non va: **Annulla l'ultimo ripristino** rimette lo stato di prima.

Da sapere:

- con i dati tornano anche utenti e password della pagina com'erano nel backup; un backup senza utenti admin viene rifiutato;
- **kit SD**: la configurazione si ripristina dopo un'abilitazione una tantum via SSH, `sudo bash /opt/meshwatch/tools/configura-servizio.sh` (le SD preparate da questa versione in poi sono già pronte); il config precedente resta sulla SD come `meshwatch-config.prima-del-ripristino.yaml`. I dati si ripristinano anche senza;
- i file del ripristino stanno in `.restore/` accanto al database.

### Ripristino a mano

Via SSH: ferma il servizio, rimetti i due file al loro posto e riavvia.

- Con il kit SD: `meshwatch.db` in `/var/lib/meshwatch/` (poi `sudo chown meshwatch-svc: /var/lib/meshwatch/meshwatch.db`) e `config.yaml` come `/boot/firmware/meshwatch-config.yaml`, perché viene ricopiato da lì a ogni avvio.
- Con `install.sh`: entrambi nella cartella del programma.

```bash
sudo systemctl stop meshwatch
# ... copia i file ...
sudo systemctl start meshwatch
```

Se il backup è protetto da password, prima estrailo con `7z x backup.zip`.

## Aggiornamenti

Dalla pagina: **Gestione → Sistema → Aggiornamento programma → Controlla aggiornamenti → Aggiorna ora**. Il Raspberry deve raggiungere `api.github.com` e `codeload.github.com`.

Da Telegram, anche fuori casa: **`/aggiornamento`** da una chat admin (`telegram.admin_chat_ids`). Il bot controlla GitHub, mostra la versione installata e quella nuova e chiede conferma con **⬆️ Aggiorna ora** / **✖ Annulla**. Se qualcosa va storto prima del riavvio (download, controllo dei file, librerie) lo scrive in chat e la versione attuale resta com'era. Dopo il riavvio, quando la nuova versione è rimasta accesa 90 secondi, arriva **✅ MeshWatch aggiornato**; se invece non parte e `guard.sh` rimette la precedente, arriva un avviso. L'avvio finisce nel registro attività come gli aggiornamenti dalla pagina.

Cosa succede:

1. scarica l'ultimo commit del ramo `main` (con `install.sh` da un clone git: `git fetch` + `merge --ff-only`);
2. controlla che il pacchetto sia completo e che il codice Python compili;
3. se `requirements.txt` è cambiato aggiorna le librerie nel venv;
4. mette da parte la versione attuale in `.update/backup`, installa la nuova e riavvia il servizio (circa un minuto: pagina e bot non rispondono);
5. se la nuova versione non resta accesa 90 secondi per 3 avvii di fila, [`tools/guard.sh`](tools/guard.sh) (eseguito da systemd prima di ogni avvio) rimette la precedente e la pagina lo segnala.

`config.yaml`, database, utenti e venv non vengono toccati. Repository e ramo si possono cambiare con la sezione `update:` del config.

Ogni 12 ore il programma controlla se c'è una nuova versione: se sì manda un avviso su Telegram (una volta per versione, con l'invito a usare `/aggiornamento`) e mostra un pallino su **Gestione**. Si disattiva con `update.notify: false`.

Prima che un aggiornamento arrivi al Raspberry, GitHub Actions esegue tutti i [test](#test): l'esito è il bollino in cima a questo README.

### Casi particolari

- **Schede preparate prima della versione 1.1** (una volta sola): il programma era di proprietà di root e il servizio non poteva aggiornarsi. Con il Raspberry acceso e in rete, doppio clic su `sd-kit/ABILITA-AGGIORNAMENTI.bat` da Windows, oppure via SSH:

  ```bash
  curl -fsSL https://raw.githubusercontent.com/davide-brugnoni/meshwatch/main/tools/abilita-aggiornamenti.sh | sudo bash
  ```

  Installa l'ultima versione e riconfigura il servizio (`tools/configura-servizio.sh`). Da quel momento la SD non va più tolta per aggiornare.
- **`install.sh` (Metodo 2)**: l'aggiornamento dalla pagina funziona se la cartella è un clone git; rilancia `bash install.sh` una volta per aggiungere la protezione `guard.sh` al servizio. In alternativa: `git pull && ./venv/bin/pip install -r requirements.txt && sudo systemctl restart meshwatch`.
- **Senza Internet sul Raspberry**: SD nel PC e `sd-kit/AGGIORNA-SD.bat`; lascia sulla SD il file `meshwatch/AGGIORNA` e al riavvio il Raspberry copia la nuova versione.
- **Modifiche al servizio di sistema**: l'aggiornamento dalla pagina non cambia `meshwatch.service`. Quando una versione nuova lo richiede (per esempio watchdog e WiFi), una volta sola via SSH:

  ```bash
  sudo bash /opt/meshwatch/tools/configura-servizio.sh && sudo systemctl restart meshwatch
  ```

  (con `install.sh`: `sudo bash tools/configura-wifi.sh`, poi rilancia `bash install.sh`). Config e dati non vengono toccati.

## Affidabilità: watchdog, radio, WiFi

- **Watchdog**: ogni 20 secondi il programma controlla che la propria pagina risponda (`/api/health`) e lo dice a systemd. Se per 5 minuti non risponde (programma acceso ma bloccato) systemd lo riavvia da solo (`WatchdogSec=300` nel servizio). Nel registro: `journalctl -u meshwatch | grep -i watchdog`.
- **Riavvio automatico**: se il programma si chiude, systemd lo riavvia dopo 10 secondi (`Restart=always`).
- **Radio**: se la radio si scollega arriva un avviso su Telegram e dopo 3 minuti il programma la ricollega da solo, ritrovando la porta USB anche se è cambiata. Si può ricollegare subito da **Gestione → Sistema** (*Riconnetti radio*).
- **WiFi sempre sveglio**: il risparmio energetico del WiFi del Raspberry, di notte, fa smettere di rispondere il chip finché non si riavvia. L'installazione lo disattiva ([`tools/configura-wifi.sh`](tools/configura-wifi.sh)): all'avvio e a ogni riconnessione del WiFi. Verifica: `iw dev wlan0 get power_save` → `Power save: off`.
- **Cavo di rete → WiFi spento**: se il cavo è collegato e ha un indirizzo IP il WiFi si spegne da solo; staccato il cavo, entro 20 secondi si riaccende e si ricollega. Un cavo collegato senza rete non spegne il WiFi, e un WiFi spento a mano non viene riacceso. Per tenerlo sempre acceso: `sudo touch /etc/meshwatch-wifi-sempre-acceso`. Registro: `journalctl -u meshwatch-wifi-auto`.
- **Tensione bassa**: la scheda Sistema e `/sistema` su Telegram segnalano se il Raspberry ha avuto sottotensione (alimentatore o cavo inadeguati).

## Accesso da Internet

1. Imposta la password admin dalla pagina (al primo accesso a Gestione).
2. Nel config: `web.mode: public` e `web.host: 127.0.0.1`.
3. Cloudflare Zero Trust → *Tunnels* → crea un tunnel, installa `cloudflared` sul Raspberry con il comando proposto, *Public hostname* `mesh.tuodominio.it` → `http://localhost:8080`.
4. Consigliato: *Access → Applications* con policy per email, come secondo lucchetto.

In modalità `public` il login è obbligatorio (anche per la mappa, salvo `public_map: true`), i cookie viaggiano solo su HTTPS e dopo 8 tentativi errati l'IP viene bloccato per 15 minuti (il blocco resta anche se il servizio si riavvia). Accessi e tentativi falliti finiscono nel **Registro attività**. La pagina mostra la posizione di persone in tempo reale: non esporla senza login. Per far vedere la mappa a qualcuno da fuori, meglio creargli un utente *viewer* che usare `public_map: true`.

Per controllare solo da fuori casa, spesso basta il bot Telegram: non richiede di esporre niente.

## Sicurezza

- Il servizio gira con un utente senza privilegi (`meshwatch-svc` nel kit SD); può eseguire come root solo `systemctl reboot` e `systemctl poweroff`.
- Ogni endpoint dell'API dichiara il ruolo minimo (ospite, viewer, admin); un test ([`tests/test_permissions.py`](tests/test_permissions.py)) fallisce se se ne aggiunge uno senza dichiararlo.
- Cookie di sessione `HttpOnly`, `SameSite=Strict`, `Secure` in modalità `public`; intestazioni `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`.
- Blocco dell'IP per 15 minuti dopo 8 tentativi di login errati.
- Kit SD: SSH consentito solo all'utente `meshwatch`, root disattivato via SSH.

Come sono salvate le password:

| Cosa | Dove | Come |
|---|---|---|
| utenti della pagina | database | solo hash PBKDF2-SHA256 (200.000 iterazioni, sale casuale): non si possono rileggere |
| ripetitori | database | in chiaro: MeshWatch le manda al ripetitore a ogni login. La pagina non le mostra |
| token Telegram | `config.yaml` | in chiaro: serve a ogni avvio |
| password del backup | database | in chiaro: serve per i backup automatici. Protegge le copie che escono dal Raspberry, non la SD |

Chi ha in mano la SD ha accesso a tutto: tienila al sicuro come il Raspberry.

## Dove stanno i file

| | Kit SD (DietPi) | `install.sh` |
|---|---|---|
| programma | `/opt/meshwatch` | cartella del clone |
| venv | `/opt/meshwatch/venv` | `./venv` |
| config da modificare | `meshwatch-config.yaml` sulla SD (`/boot/firmware/` o `/boot/`) | `./config.yaml` |
| config in uso | `/var/lib/meshwatch/config.yaml` (ricopiato dalla SD a ogni avvio) | `./config.yaml` |
| database | `/var/lib/meshwatch/meshwatch.db` | `./meshwatch.db` |
| cache delle mappe | `/var/lib/meshwatch/tiles.db` | `./tiles.db` |
| file del ripristino | `/var/lib/meshwatch/.restore/` | `./.restore/` |
| versione precedente (aggiornamento) | `/opt/meshwatch/.update/` | `./.update/` |
| servizio | `/etc/systemd/system/meshwatch.service` | idem |
| permessi riavvio/spegnimento | `/etc/sudoers.d/meshwatch` | idem |
| utente del servizio | `meshwatch-svc` | il tuo utente |
| log dell'installazione | `meshwatch-install.log` sulla SD | a schermo |

## Comandi utili

```bash
sudo systemctl status meshwatch        # stato del servizio
sudo systemctl restart meshwatch       # riavvio (dopo una modifica al config)
journalctl -u meshwatch -f             # registro in diretta
journalctl -u meshwatch --since today  # registro di oggi
ls /dev/serial/by-id/                  # radio USB collegate
iw dev wlan0 get power_save            # deve essere "off"
vcgencmd get_throttled                 # 0x0 = nessuna sottotensione
curl -s http://localhost:8080/api/health
```

## Risoluzione dei problemi

| Problema | Cosa controllare |
|---|---|
| `meshwatch.local` non si apre | usa l'IP del Raspberry (dal router o `hostname -I`); su Windows/Android il nome `.local` non sempre funziona |
| la pagina non si apre per niente | `systemctl status meshwatch` e `journalctl -u meshwatch -n 50`; con il kit SD, primo avvio non finito: guarda `meshwatch-install.log` sulla SD |
| il servizio esce subito con "web.mode è 'public' ma non ci sono utenti" | crea prima l'admin in modalità `lan`, oppure usa `web.users` con `meshwatch hashpw` |
| radio non trovata / "permission denied" sulla porta | radio con firmware **Companion USB** (non Repeater o Room Server); utente nel gruppo `dialout`; prova a fissare `serial_port` con il percorso da `/dev/serial/by-id/` |
| la radio si scollega spesso | alimentatore e cavo USB (`vcgencmd get_throttled`), cavo dati e non solo ricarica, hub USB alimentato |
| il dispositivo non risponde alla posizione | nell'app MeshCore del dispositivo: gateway tra i *preferiti* e telemetria posizione consentita; nome del contatto identico; il dispositivo deve essere raggiungibile da un ripetitore; prova *azzera percorso* nella rubrica |
| posizione arriva ma senza coordinate | il GPS non ha il fix (al chiuso): i sensori vengono salvati comunque |
| ripetitore senza statistiche | password admin o guest del ripetitore corretta; ripetitore nella rubrica del gateway |
| il bot non risponde | token corretto; chat ID in `allowed_chat_ids` (il bot lo dice scrivendogli `/start`); Internet sul Raspberry; nel registro cerca `Telegram` |
| `/terminale` "non autorizzato" | la chat deve essere in `admin_chat_ids` (se la lista non è vuota) |
| nomi senza accenti nella mappa di Telegram | `sudo apt install fonts-dejavu-core` |
| riavvio/spegnimento dalla pagina dà "permesso mancante" | esegui il comando di [Riavvio e spegnimento](#riavvio-e-spegnimento-dalla-pagina) |
| l'aggiornamento dalla pagina non è disponibile | schede prima della 1.1: [abilita gli aggiornamenti](#casi-particolari); con `install.sh` la cartella deve essere un clone git; il Raspberry deve raggiungere GitHub |
| dopo un aggiornamento è tornata la versione vecchia | la nuova non partiva: lo segnala la pagina; guarda `journalctl -u meshwatch` e apri una issue |
| di notte la pagina smette di rispondere | WiFi in risparmio energetico: `sudo bash tools/configura-wifi.sh` (vedi [Affidabilità](#affidabilità-watchdog-radio-wifi)) |
| password admin dimenticata | via SSH: `sqlite3 /var/lib/meshwatch/meshwatch.db "UPDATE users SET password_hash='<hash>' WHERE username='admin'"` con l'hash di `meshwatch hashpw`, poi riavvia (a servizio fermo; con `install.sh` il database è nella cartella del programma) |
| SD piena | **Gestione → Sistema**: riduci i giorni di conservazione, svuota o riduci la cache delle mappe |

## Limiti

- Se il dispositivo è dove nessun ripetitore lo sente, la richiesta va a vuoto: resta l'ultima posizione con l'orario.
- Ogni richiesta occupa la radio per qualche secondo: intervalli sotto i 10 minuti non sono consigliati.
- Il dispositivo tracciato deve rispondere alla telemetria anche senza telefono collegato: va verificato sul campo.
- Una sola radio gateway per istanza.
- La rubrica della radio ha un numero limitato di contatti: tienila pulita dalla Gestione.

## API

La pagina usa un'API JSON sotto `/api/` (FastAPI). L'autenticazione è con il cookie di sessione ottenuto da `POST /api/login` (`{"username": "...", "password": "..."}`). Ruolo minimo: **ospite** = senza login se la mappa è libera, **viewer**, **admin**; **libero** = sempre accessibile.

<details>
<summary>Elenco degli endpoint</summary>

| Metodo | Percorso | Ruolo | Descrizione |
|---|---|---|---|
| GET | `/` | libero | pagina |
| GET | `/api/health` | libero | il server è vivo (usato dal watchdog) |
| POST | `/api/login` · `/api/logout` | libero | accesso · uscita |
| GET | `/api/me` | libero | utente collegato, stato della configurazione iniziale |
| POST | `/api/setup` | libero (solo senza utenti, mai in `public`) | crea il primo admin |
| GET | `/api/state?hours=24` | ospite | dispositivi, posizioni, ripetitori, gateway, radio |
| GET | `/api/events` | ospite | eventi in tempo reale (Server-Sent Events) |
| GET | `/api/tiles/{layer}/{z}/{x}/{y}` | ospite | tessere della mappa dalla cache |
| GET | `/api/chat` | viewer | conversazioni |
| GET | `/api/chat/messages?kind=&peer=&before=` | viewer | messaggi di una conversazione |
| GET | `/api/chat/updates?after=` | viewer | messaggi nuovi |
| POST | `/api/chat` · `/api/chat/read` | viewer | invia un messaggio · segna come letti |
| DELETE | `/api/chat?kind=&peer=` | admin | cancella una conversazione |
| GET | `/api/network` | viewer | nodi e collegamenti della rete |
| GET | `/api/coverage?days=30&tracker=&cell=250` | viewer | celle della mappa di copertura |
| POST | `/api/network/neighbours` | admin | chiede subito i vicini ai ripetitori |
| POST | `/api/trackers/{tid}/poll` | viewer | chiede subito la posizione |
| POST · PUT · DELETE | `/api/trackers`, `/api/trackers/{tid}` | admin | aggiungi · modifica · rimuovi un dispositivo |
| DELETE | `/api/trackers/{tid}/track` · `/api/track` | admin | cancella la traccia di uno · di tutti |
| GET · POST · DELETE | `/api/trackers/{tid}/rules[/{rid}]` | admin | soglie dei sensori |
| POST | `/api/repeaters/poll` | admin | interroga subito i ripetitori |
| POST · DELETE | `/api/repeaters`, `/api/repeaters/{name}` | admin | aggiungi/modifica · rimuovi un ripetitore |
| GET | `/api/admin` | admin | dati della scheda Gestione |
| POST · PUT · DELETE | `/api/users`, `/api/users/{username}` | admin | utenti della pagina |
| GET · POST | `/api/gateway` | admin | stato · configurazione della radio |
| POST | `/api/gateway/reboot` · `/api/radio/reconnect` · `/api/advert` | admin | riavvia la radio · ricollega · advert |
| GET | `/api/contacts` | admin | rubrica del gateway |
| POST | `/api/contacts/refresh` | admin | rilegge la rubrica |
| POST | `/api/contacts/{key}/reset_path` · `/favorite` | admin | azzera percorso · preferito |
| DELETE | `/api/contacts/{key}?force=` | admin | rimuove un contatto |
| POST | `/api/contacts/pending/{key}` | admin | aggiunge un nodo in attesa |
| PUT | `/api/contacts/settings` | admin | aggiunta manuale dei contatti |
| GET | `/api/system` | admin | stato del Raspberry |
| POST | `/api/system/reboot` · `/api/system/poweroff` | admin | riavvio · spegnimento |
| GET | `/api/update` | admin | stato dell'aggiornamento |
| POST | `/api/update/check` · `/api/update/apply` | admin | controlla · installa |
| GET · PUT | `/api/maintenance` | admin | conservazione dati, backup settimanale, password del backup |
| POST | `/api/maintenance/purge` | admin | pulizia immediata |
| GET | `/api/backup` | admin | scarica il backup (zip) |
| POST | `/api/backup/telegram` | admin | manda subito il backup su Telegram |
| GET | `/api/restore` | admin | stato del ripristino |
| POST | `/api/restore/check` · `/apply` · `/undo` | admin | verifica · applica · annulla un ripristino |
| GET · PUT | `/api/telegram/report` | admin | riepilogo giornaliero |
| POST | `/api/telegram/report/send` | admin | manda subito il riepilogo |
| GET · PUT | `/api/alerts/battery` | admin | avvisi batteria bassa |
| GET · PUT | `/api/tilecache` | admin | cache delle mappe |
| POST | `/api/tilecache/clear` | admin | svuota la cache |
| GET | `/api/audit` | admin | registro attività |
| GET | `/api/cli/{node}` | admin | storico del terminale di un ripetitore |
| POST | `/api/cli` | admin | comando CLI a un ripetitore |

</details>

Esempio:

```bash
curl -c c.txt -H 'Content-Type: application/json' -d '{"username":"admin","password":"..."}' http://meshwatch.local:8080/api/login
curl -b c.txt http://meshwatch.local:8080/api/state
```

## Database

SQLite in `meshwatch.db`; lo schema viene creato e aggiornato da solo all'avvio ([`meshwatch/db.py`](meshwatch/db.py)).

| Tabella | Contenuto |
|---|---|
| `trackers` | dispositivi seguiti e loro impostazioni |
| `positions` | posizioni ricevute, con batteria, salti e percorso |
| `tracker_polls` | ogni richiesta di posizione, riuscita o no (mappa della copertura) |
| `sensor_readings` · `sensor_rules` | letture dei sensori · soglie di avviso |
| `repeaters` · `repeater_status` | ripetitori configurati · statistiche nel tempo |
| `neighbours` | vicini dei ripetitori (mappa della rete) |
| `messages` | chat |
| `cli_log` | comandi del terminale e risposte |
| `users` · `login_failures` | utenti della pagina · tentativi falliti (blocco IP) |
| `audit` | registro attività |
| `settings` | impostazioni cambiate dalla pagina |

La cache delle mappe è in un database separato, `tiles.db` (tabella `tiles`).

## Struttura del codice

```
meshwatch/            pacchetto Python
  __main__.py         avvio (python -m meshwatch -c config.yaml), comando hashpw
  mesh.py             comunicazione con la radio (libreria meshcore)
  core.py             interrogazioni periodiche, avvisi, stato
  db.py               SQLite: dispositivi, posizioni, ripetitori, utenti, impostazioni
  web.py              API FastAPI e pagina
  bot.py              bot Telegram
  charts.py           mappa come immagine per Telegram (traccia, gateway, ripetitori)
  auth.py             login e sessioni
  system.py           stato del Raspberry, riavvio/spegnimento
  updater.py          aggiornamento dalla pagina (GitHub), con ripristino
  maintenance.py      pulizia dei dati vecchi e backup
  restore.py          ripristino di un backup dalla pagina
  events.py           notifiche in tempo reale alla pagina (Server-Sent Events)
  tiles.py            cache delle tessere delle mappe (tiles.db)
  watchdog.py         segnale a systemd finché la pagina risponde (riavvio se si blocca)
web/index.html        struttura della pagina
web/assets/app.css    stile
web/assets/js/        script della pagina, caricati in ordine da index.html:
  core.js             utilità, chiamate all'API, login, schede
  map.js              mappa e livelli (tracce, rete)
  nodes.js            colonna dei nodi: dispositivi, ripetitori, terminale
  chat.js             chat
  admin.js            scheda Gestione
  main.js             avvio (sempre l'ultimo)
web/vendor/           Leaflet
tools/
  guard.sh                  ripristino della versione precedente se un aggiornamento non parte
  configura-servizio.sh     servizio systemd del kit SD
  abilita-aggiornamenti.sh  abilita l'aggiornamento dalla pagina sulle schede prima della 1.1
  configura-wifi.sh         WiFi senza risparmio energetico, spento col cavo di rete
sd-kit/               preparazione e aggiornamento della SD DietPi da Windows
  PREPARA-SD.bat / prepara-sd.ps1       prepara una SD nuova
  AGGIORNA-SD.bat / aggiorna-sd.ps1     aggiorna una SD dal PC
  ABILITA-AGGIORNAMENTI.bat             schede prima della 1.1
  Automation_Custom_Script.sh           installazione al primo avvio di DietPi
  LEGGIMI.txt                           istruzioni passo passo
install.sh            installazione manuale su Raspberry Pi OS / Debian
meshwatch.service     modello del servizio systemd usato da install.sh
config.example.yaml   config di esempio
tests/                test con radio simulata
docs/screenshots/     immagini di questo README
```

## Sviluppo e test

```bash
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt httpx pyflakes playwright
./venv/bin/python -m playwright install chromium   # una volta, per il test nel browser
```

```bash
for t in tests/test_*.py tests/check_page.py; do ./venv/bin/python "$t" || break; done
./venv/bin/python -m pyflakes meshwatch tests
```

I test usano una radio simulata: non serve hardware. Senza Playwright il test nel browser viene saltato; `check_page.py` usa `node` per controllare la sintassi JavaScript.

### Test

A ogni push e pull request GitHub Actions ([`.github/workflows/test.yml`](.github/workflows/test.yml)) esegue i test con Python 3.11 (come DietPi / Raspberry Pi OS Bookworm) e 3.13, il controllo degli errori nel codice, la pagina in un browser vero e la sintassi degli script di shell.

| Test | Cosa controlla |
|---|---|
| `test_smoke.py` | database, ruoli, scheduler, manutenzione, intervallo adattivo, radio |
| `test_chat.py` | chat, rubrica del gateway, mappa della rete, coda della radio |
| `test_update.py` | aggiornamento dalla pagina e ripristino della versione precedente |
| `test_features.py` | sensori e soglie, percorsi, copertura, tempo reale, comandi del bot |
| `test_tiles.py` | cache delle mappe con un server delle mappe finto: scadenza, 304, senza Internet, spazio massimo |
| `test_restore.py` | ripristino dalla pagina: password, file non validi, avvisi, applicazione al riavvio, annullamento, kit SD |
| `test_permissions.py` | **ogni** endpoint dell'API per ospite, viewer e admin; fallisce se se ne aggiunge uno senza dichiarare chi può usarlo |
| `test_watchdog.py` | watchdog di systemd: segnale finché la pagina risponde, riavvio se si blocca |
| `check_page.py` | file della pagina, id usati dagli script, sintassi JavaScript (serve node) |
| `test_page_browser.py` | la pagina in Chromium su computer e telefono, tutte le schede, nessun errore JavaScript |

### Contribuire

- Testi della pagina, del bot e dei commenti sono in italiano semplice: mantieni lo stesso stile.
- Ogni nuovo endpoint dell'API va dichiarato in `tests/test_permissions.py`.
- Prima di aprire una pull request esegui i test e `pyflakes`; la CI deve essere verde.
- Segnalazioni e proposte: [issue su GitHub](https://github.com/davide-brugnoni/meshwatch/issues).

## Licenza

MIT — vedi [LICENSE](LICENSE).
