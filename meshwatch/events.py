"""Notifiche in tempo reale per la pagina (Server-Sent Events).

Il programma pubblica un "argomento" quando cambia qualcosa; ogni pagina aperta ha una coda e
riceve l'argomento, poi chiede all'API i dati aggiornati. Gli argomenti:
    state   posizioni, ripetitori, radio, dispositivi (la pagina ricarica lo stato)
    chat    messaggi ricevuti o cambi di stato degli invii (solo utenti con login)
"""
import asyncio
import logging

log = logging.getLogger("meshwatch.events")

PING_S = 25          # commento periodico: tiene aperta la connessione (proxy, Cloudflare)
MAX_CLIENTS = 50


class Events:
    def __init__(self):
        self.subs = set()      # (loop, coda)
        self.closed = False

    def subscribe(self):
        if self.closed:
            raise RuntimeError("chiusura in corso")
        if len(self.subs) >= MAX_CLIENTS:
            raise OverflowError("troppe pagine aperte")
        sub = (asyncio.get_running_loop(), asyncio.Queue(maxsize=100))
        self.subs.add(sub)
        return sub

    def unsubscribe(self, sub):
        self.subs.discard(sub)

    @staticmethod
    def _put(q, item):
        try:
            q.put_nowait(item)
        except asyncio.QueueFull:
            pass   # pagina lenta: tanto ricarica tutto al prossimo argomento

    def publish(self, topic):
        """Si può chiamare da qualsiasi thread o loop."""
        for sub in list(self.subs):
            loop, q = sub
            try:
                loop.call_soon_threadsafe(self._put, q, topic)
            except RuntimeError:   # loop chiuso
                self.subs.discard(sub)

    def close(self):
        """Riavvio del servizio: chiude tutte le connessioni (il browser si ricollega da solo)."""
        self.closed = True
        for loop, q in list(self.subs):
            try:
                loop.call_soon_threadsafe(self._put, q, None)
            except RuntimeError:
                pass

    async def stream(self, sub, allowed, is_disconnected=None, ping=PING_S):
        """Generatore del flusso text/event-stream per una pagina. Più argomenti arrivati
        insieme diventano un evento solo per argomento."""
        _, q = sub
        try:
            yield "retry: 5000\nevent: hello\ndata: {}\n\n"
            while True:
                try:
                    topic = await asyncio.wait_for(q.get(), ping)
                except asyncio.TimeoutError:
                    if is_disconnected and await is_disconnected():
                        return
                    yield ": ping\n\n"
                    continue
                topics = [topic]
                while not q.empty():
                    topics.append(q.get_nowait())
                if None in topics:
                    return
                for t in dict.fromkeys(topics):
                    if t in allowed:
                        yield f"event: {t}\ndata: {{}}\n\n"
        finally:
            self.unsubscribe(sub)
