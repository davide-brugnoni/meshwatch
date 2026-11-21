import sqlite3
import threading
import time

COLORS = ["#2fb38a", "#ff7a45", "#5aa9ff", "#e8c547", "#c77dff", "#ff5c8a"]


class DB:
    def __init__(self, path):
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS positions (
                    ts INTEGER, lat REAL, lon REAL, alt REAL, batt_v REAL
                );
                CREATE TABLE IF NOT EXISTS tracker_polls (ts INTEGER, ok INTEGER);
                CREATE TABLE IF NOT EXISTS repeater_status (
                    ts INTEGER, name TEXT, ok INTEGER, bat_mv INTEGER, uptime INTEGER,
                    noise INTEGER, rssi INTEGER, snr REAL, nb_recv INTEGER, nb_sent INTEGER
                );
                CREATE TABLE IF NOT EXISTS cli_log (
                    ts INTEGER, user TEXT, node TEXT, cmd TEXT, reply TEXT
                );
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, val TEXT);
                CREATE TABLE IF NOT EXISTS trackers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    contact TEXT NOT NULL,          -- nome del contatto MeshCore
                    label TEXT NOT NULL,            -- nome visualizzato
                    interval_min INTEGER DEFAULT 30,
                    active_hours TEXT DEFAULT '',
                    offline_alert_min INTEGER DEFAULT 90,
                    low_battery_v REAL DEFAULT 3.5,
                    paused INTEGER DEFAULT 0,
                    color TEXT
                );
                CREATE TABLE IF NOT EXISTS repeaters (
                    name TEXT PRIMARY KEY, password TEXT DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS users (
                    username TEXT PRIMARY KEY, password_hash TEXT NOT NULL, role TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS login_failures (
                    ip TEXT PRIMARY KEY, count INTEGER, first_ts INTEGER
                );
                CREATE TABLE IF NOT EXISTS audit (
                    ts INTEGER, user TEXT, ip TEXT, action TEXT, detail TEXT
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts INTEGER,
                    dir TEXT,               -- 'in' ricevuto, 'out' inviato
                    kind TEXT,              -- 'dm' privato, 'chan' canale
                    peer TEXT,              -- prefisso chiave (12 hex) o numero del canale
                    peer_name TEXT,
                    sender TEXT,            -- nei canali: chi ha scritto
                    text TEXT,
                    status TEXT,            -- out: queued, sending, sent, delivered, failed
                    user TEXT,              -- chi l'ha inviato dalla pagina
                    snr REAL, path_len INTEGER, sender_ts INTEGER,
                    attempts INTEGER, trip_ms INTEGER,
                    read INTEGER DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS i_msg_peer ON messages(kind, peer, id);
                CREATE TABLE IF NOT EXISTS neighbours (
                    ts INTEGER, repeater TEXT, prefix TEXT, snr REAL, secs_ago INTEGER
                );
                CREATE INDEX IF NOT EXISTS i_nb ON neighbours(repeater);
                CREATE INDEX IF NOT EXISTS i_audit_ts ON audit(ts);
                CREATE INDEX IF NOT EXISTS i_pos_ts ON positions(ts);
                CREATE INDEX IF NOT EXISTS i_rep ON repeater_status(name, ts);
                CREATE TABLE IF NOT EXISTS sensor_readings (
                    ts INTEGER, tracker_id INTEGER, channel INTEGER, type TEXT, value REAL
                );
                CREATE INDEX IF NOT EXISTS i_sens ON sensor_readings(tracker_id, ts);
                CREATE TABLE IF NOT EXISTS sensor_rules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tracker_id INTEGER, type TEXT, channel INTEGER,
                    op TEXT,                -- '<' sotto la soglia, '>' sopra la soglia
                    value REAL
                );
                """
            )
            # migrazione dalla prima versione: posizioni e richieste legate a un tracker
            for table in ("positions", "tracker_polls"):
                cols = [r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")]
                if "tracker_id" not in cols:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN tracker_id INTEGER")
            self.conn.execute("CREATE INDEX IF NOT EXISTS i_pos_tr ON positions(tracker_id, ts)")
            # percorso dei pacchetti: salti e ripetitori attraversati dalla risposta
            cols = [r[1] for r in self.conn.execute("PRAGMA table_info(tracker_polls)")]
            for col, typ in (("hops", "INTEGER"), ("path", "TEXT")):
                if col not in cols:
                    self.conn.execute(f"ALTER TABLE tracker_polls ADD COLUMN {col} {typ}")
            # mappa della copertura: dove si trovava il dispositivo a ogni richiesta
            # (est=1: non ha risposto o non aveva il fix, è la sua ultima posizione nota)
            if "lat" not in cols:
                for col, typ in (("lat", "REAL"), ("lon", "REAL"), ("est", "INTEGER")):
                    self.conn.execute(f"ALTER TABLE tracker_polls ADD COLUMN {col} {typ}")
                # richieste riuscite delle versioni precedenti: posizione salvata nello stesso momento
                same = ("FROM positions p WHERE p.tracker_id=tracker_polls.tracker_id "
                        "AND p.ts BETWEEN tracker_polls.ts - 1 AND tracker_polls.ts + 1")
                self.conn.execute(f"UPDATE tracker_polls SET lat=(SELECT p.lat {same} LIMIT 1), "
                                  f"lon=(SELECT p.lon {same} LIMIT 1), est=0 WHERE ok=1 AND EXISTS (SELECT 1 {same})")
            self.conn.execute("CREATE INDEX IF NOT EXISTS i_polls_tr ON tracker_polls(tracker_id, ts)")
            # ultimi valori di ogni sensore senza leggere tutto lo storico (vedi sensor_latest)
            self.conn.execute("CREATE INDEX IF NOT EXISTS i_sens_key ON sensor_readings(tracker_id, type, channel)")
            # intervallo adattivo: 0 = disattivato
            cols = [r[1] for r in self.conn.execute("PRAGMA table_info(trackers)")]
            if "moving_interval_min" not in cols:
                self.conn.execute("ALTER TABLE trackers ADD COLUMN moving_interval_min INTEGER DEFAULT 0")
            self.conn.commit()

    def _exec(self, sql, args=()):
        with self.lock:
            cur = self.conn.execute(sql, args)
            self.conn.commit()
            return cur

    def _all(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def _one(self, sql, args=()):
        r = self._all(sql, args)
        return r[0] if r else None

    # ---------- primo avvio: importa dal config.yaml
    def seed_from_config(self, cfg):
        if self.get("seeded") == "1":
            return
        t = cfg.get("tracker")
        if t and t.get("name") and not self.trackers():
            tid = self.add_tracker(
                contact=t["name"], label=t.get("label", t["name"]),
                interval_min=int(self.get("interval_min", t.get("interval_min", 30))),
                active_hours=t.get("active_hours", ""), offline_alert_min=t.get("offline_alert_min", 90),
                low_battery_v=t.get("low_battery_v", 3.5))
            if self.get("paused") == "1":
                self.update_tracker(tid, paused=1)
            # dati della prima versione: appartengono a questo tracker
            self._exec("UPDATE positions SET tracker_id=? WHERE tracker_id IS NULL", (tid,))
            self._exec("UPDATE tracker_polls SET tracker_id=? WHERE tracker_id IS NULL", (tid,))
        if not self.repeaters():
            for r in cfg.get("repeaters") or []:
                self.set_repeater(r["name"], r.get("password", ""))
        if not self.users():
            for u in (cfg.get("web") or {}).get("users") or []:
                self._exec("INSERT OR REPLACE INTO users VALUES (?,?,?)",
                           (u["username"], u["password_hash"], u.get("role", "viewer")))
        self.set("seeded", "1")

    # ---------- tracker
    TRACKER_FIELDS = ("contact", "label", "interval_min", "active_hours", "offline_alert_min",
                      "low_battery_v", "paused", "color", "moving_interval_min")

    def trackers(self):
        return self._all("SELECT * FROM trackers ORDER BY id")

    def tracker(self, tid):
        return self._one("SELECT * FROM trackers WHERE id=?", (tid,))

    def add_tracker(self, **f):
        if not f.get("color"):
            used = {t["color"] for t in self.trackers()}
            f["color"] = next((c for c in COLORS if c not in used), COLORS[len(used) % len(COLORS)])
        keys = [k for k in self.TRACKER_FIELDS if k in f]
        cur = self._exec(f"INSERT INTO trackers ({','.join(keys)}) VALUES ({','.join('?' * len(keys))})",
                         [f[k] for k in keys])
        return cur.lastrowid

    def update_tracker(self, tid, **f):
        keys = [k for k in self.TRACKER_FIELDS if k in f]
        if keys:
            self._exec(f"UPDATE trackers SET {','.join(k + '=?' for k in keys)} WHERE id=?",
                       [f[k] for k in keys] + [tid])

    def delete_tracker(self, tid, keep_history=False):
        self._exec("DELETE FROM trackers WHERE id=?", (tid,))
        self._exec("DELETE FROM sensor_rules WHERE tracker_id=?", (tid,))
        if not keep_history:
            self._exec("DELETE FROM positions WHERE tracker_id=?", (tid,))
            self._exec("DELETE FROM tracker_polls WHERE tracker_id=?", (tid,))
            self._exec("DELETE FROM sensor_readings WHERE tracker_id=?", (tid,))

    def clear_track(self, tid=None, keep_last=False):
        """Cancella la traccia (di un dispositivo o di tutti): dalla mappa sparisce tutto, anche
        l'ultima posizione e i punti della copertura. Con keep_last resta l'ultima posizione."""
        where, args = ("WHERE tracker_id=?", [tid]) if tid is not None else ("", [])
        if keep_last:
            # niente NULL nella lista: con un dispositivo senza posizioni "NOT IN" non cancellerebbe nulla
            keep = "rowid NOT IN (SELECT (SELECT p2.rowid FROM positions p2 WHERE p2.tracker_id=t.id " \
                   "ORDER BY p2.ts DESC, p2.rowid DESC LIMIT 1) AS r FROM trackers t WHERE r IS NOT NULL)"
            where = (where + " AND " if where else "WHERE ") + keep
        else:
            # le richieste restano (statistiche delle risposte), ma senza il punto in cui erano
            self._exec(f"UPDATE tracker_polls SET lat=NULL, lon=NULL {where}", args)
        return self._exec(f"DELETE FROM positions {where}", args).rowcount

    def add_position(self, tid, lat, lon, alt, batt_v):
        self._exec("INSERT INTO positions (ts, lat, lon, alt, batt_v, tracker_id) VALUES (?,?,?,?,?,?)",
                   (int(time.time()), lat, lon, alt, batt_v, tid))

    def add_tracker_poll(self, tid, ok, hops=None, path=None, lat=None, lon=None, est=None):
        self._exec("INSERT INTO tracker_polls (ts, ok, tracker_id, hops, path, lat, lon, est) VALUES (?,?,?,?,?,?,?,?)",
                   (int(time.time()), 1 if ok else 0, tid, hops, ",".join(path) if path else None, lat, lon, est))

    def coverage_polls(self, since, tid=None):
        sql = "SELECT ts, ok, tracker_id, hops, lat, lon, est FROM tracker_polls WHERE ts>=? AND lat IS NOT NULL"
        args = [since]
        if tid is not None:
            sql += " AND tracker_id=?"
            args.append(tid)
        return self._all(sql, args)

    def last_route(self, tid):
        """Percorso dell'ultima risposta: {ts, hops, path: [prefissi]} o None."""
        r = self._one("SELECT ts, hops, path FROM tracker_polls WHERE tracker_id=? AND ok=1 AND hops IS NOT NULL "
                      "ORDER BY ts DESC, rowid DESC LIMIT 1", (tid,))
        if r:
            r["path"] = r["path"].split(",") if r["path"] else []
        return r

    def last_poll(self, tid):
        """Ultima richiesta, riuscita o no: {ts, ok, hops, path: [prefissi]} o None.
        Per quelle senza risposta hops/path sono il percorso provato per primo."""
        r = self._one("SELECT ts, ok, hops, path FROM tracker_polls WHERE tracker_id=? "
                      "ORDER BY ts DESC, rowid DESC LIMIT 1", (tid,))
        if r:
            r["path"] = r["path"].split(",") if r["path"] else []
        return r

    def last_position(self, tid):
        return self._one("SELECT * FROM positions WHERE tracker_id=? ORDER BY ts DESC LIMIT 1", (tid,))

    def positions_since(self, tid, ts):
        return self._all("SELECT * FROM positions WHERE tracker_id=? AND ts>=? ORDER BY ts", (tid, ts))

    def last_tracker_ok(self, tid):
        r = self._one("SELECT ts FROM tracker_polls WHERE tracker_id=? AND ok=1 ORDER BY ts DESC LIMIT 1", (tid,))
        return r["ts"] if r else None

    # ---------- sensori (telemetria Cayenne LPP)
    def add_sensor_readings(self, tid, items, ts=None):
        ts = ts or int(time.time())
        with self.lock:
            self.conn.executemany("INSERT INTO sensor_readings VALUES (?,?,?,?,?)",
                                  [(ts, tid, i["channel"], i["type"], i["value"]) for i in items])
            self.conn.commit()

    def sensor_latest(self, tid):
        """Ultimo valore di ogni sensore del dispositivo: [{type, channel, value, ts}].
        Salta da un sensore al successivo sull'indice i_sens_key: poche letture anche con mesi
        di storico (un GROUP BY leggerebbe ogni riga del dispositivo a ogni apertura della pagina)."""
        sel = "SELECT type, channel FROM sensor_readings WHERE tracker_id=? AND "
        last = ("SELECT type, channel, value, ts FROM sensor_readings WHERE rowid="
                "(SELECT MAX(rowid) FROM sensor_readings WHERE tracker_id=? AND type IS ? AND channel IS ?)")

        def after(col, val):   # valori dopo val nell'ordine dell'indice (NULL viene per primo)
            return (f"{col} IS NOT NULL", ()) if val is None else (f"{col}>?", (val,))

        out = []
        with self.lock:
            key = self.conn.execute(sel + "1 ORDER BY type, channel LIMIT 1", (tid,)).fetchone()
            while key:
                typ, ch = key["type"], key["channel"]
                r = self.conn.execute(last, (tid, typ, ch)).fetchone()
                if r:
                    out.append(dict(r))
                c_sql, c_arg = after("channel", ch)
                t_sql, t_arg = after("type", typ)
                key = (self.conn.execute(sel + f"type IS ? AND {c_sql} ORDER BY channel LIMIT 1",
                                         (tid, typ, *c_arg)).fetchone()
                       or self.conn.execute(sel + f"{t_sql} ORDER BY type, channel LIMIT 1", (tid, *t_arg)).fetchone())
        return sorted(out, key=lambda x: (x["channel"] is not None, x["channel"] or 0, x["type"] or ""))

    def sensor_history(self, tid, since):
        return self._all("SELECT ts, type, channel, value FROM sensor_readings WHERE tracker_id=? AND ts>=? ORDER BY ts",
                         (tid, since))

    def sensor_rules(self, tid=None):
        if tid is None:
            return self._all("SELECT * FROM sensor_rules ORDER BY id")
        return self._all("SELECT * FROM sensor_rules WHERE tracker_id=? ORDER BY id", (tid,))

    def add_sensor_rule(self, tid, type, channel, op, value):
        return self._exec("INSERT INTO sensor_rules (tracker_id, type, channel, op, value) VALUES (?,?,?,?,?)",
                          (tid, type, channel, op, value)).lastrowid

    def delete_sensor_rule(self, rid):
        return self._exec("DELETE FROM sensor_rules WHERE id=?", (rid,)).rowcount

    # ---------- ripetitori
    def repeaters(self):
        return self._all("SELECT * FROM repeaters ORDER BY name")

    def repeater(self, name):
        return self._one("SELECT * FROM repeaters WHERE name=?", (name,))

    def set_repeater(self, name, password):
        self._exec("INSERT OR REPLACE INTO repeaters VALUES (?,?)", (name, password or ""))

    def delete_repeater(self, name):
        self._exec("DELETE FROM repeaters WHERE name=?", (name,))
        self._exec("DELETE FROM neighbours WHERE repeater=?", (name,))

    def add_repeater_status(self, name, ok, s=None):
        s = s or {}
        self._exec(
            "INSERT INTO repeater_status VALUES (?,?,?,?,?,?,?,?,?,?)",
            (int(time.time()), name, 1 if ok else 0, s.get("bat"), s.get("uptime"),
             s.get("noise_floor"), s.get("last_rssi"), s.get("last_snr"),
             s.get("nb_recv"), s.get("nb_sent")),
        )

    def repeater_latest(self, name):
        last = self._one("SELECT * FROM repeater_status WHERE name=? ORDER BY ts DESC LIMIT 1", (name,))
        good = self._one("SELECT * FROM repeater_status WHERE name=? AND ok=1 ORDER BY ts DESC LIMIT 1", (name,))
        return last, good

    def repeater_history(self, name, since):
        return self._all(
            "SELECT ts, bat_mv, noise, snr, rssi FROM repeater_status WHERE name=? AND ok=1 AND ts>=? ORDER BY ts",
            (name, since))

    # ---------- utenti
    def users(self):
        return self._all("SELECT username, role FROM users ORDER BY username")

    def user(self, username):
        return self._one("SELECT * FROM users WHERE username=?", (username,))

    def set_user(self, username, password_hash=None, role=None):
        u = self.user(username)
        if u:
            self._exec("UPDATE users SET password_hash=?, role=? WHERE username=?",
                       (password_hash or u["password_hash"], role or u["role"], username))
        else:
            self._exec("INSERT INTO users VALUES (?,?,?)", (username, password_hash, role or "viewer"))

    def delete_user(self, username):
        self._exec("DELETE FROM users WHERE username=?", (username,))

    # ---------- tentativi di login falliti (restano anche dopo un riavvio)
    def login_failures(self, ip):
        r = self._one("SELECT count, first_ts FROM login_failures WHERE ip=?", (ip,))
        return (r["count"], r["first_ts"]) if r else (0, None)

    def set_login_failures(self, ip, count, first_ts):
        self._exec("INSERT OR REPLACE INTO login_failures VALUES (?,?,?)", (ip, count, first_ts))

    def clear_login_failures(self, ip=None):
        if ip is None:
            self._exec("DELETE FROM login_failures WHERE first_ts < ?", (int(time.time()) - 86400,))
        else:
            self._exec("DELETE FROM login_failures WHERE ip=?", (ip,))

    # ---------- registro attività
    def add_audit(self, user, action, detail="", ip=""):
        self._exec("INSERT INTO audit VALUES (?,?,?,?,?)",
                   (int(time.time()), user or "", ip or "", action, (detail or "")[:300]))

    def audit(self, limit=200):
        return self._all("SELECT * FROM audit ORDER BY rowid DESC LIMIT ?", (limit,))

    # ---------- terminale
    def add_cli(self, user, node, cmd, reply):
        self._exec("INSERT INTO cli_log VALUES (?,?,?,?,?)", (int(time.time()), user, node, cmd, reply))

    def cli_history(self, node, limit=50):
        rows = self._all("SELECT * FROM cli_log WHERE node=? ORDER BY rowid DESC LIMIT ?", (node, limit))
        return list(reversed(rows))

    # ---------- chat
    MSG_FIELDS = ("ts", "dir", "kind", "peer", "peer_name", "sender", "text", "status", "user",
                  "snr", "path_len", "sender_ts", "attempts", "trip_ms", "read")

    def add_message(self, **f):
        f.setdefault("ts", int(time.time()))
        keys = [k for k in self.MSG_FIELDS if k in f]
        return self._exec(f"INSERT INTO messages ({','.join(keys)}) VALUES ({','.join('?' * len(keys))})",
                          [f[k] for k in keys]).lastrowid

    def update_message(self, mid, **f):
        keys = [k for k in self.MSG_FIELDS if k in f]
        if keys:
            self._exec(f"UPDATE messages SET {','.join(k + '=?' for k in keys)} WHERE id=?",
                       [f[k] for k in keys] + [mid])

    def message(self, mid):
        return self._one("SELECT * FROM messages WHERE id=?", (mid,))

    def is_duplicate_message(self, kind, peer, text, sender_ts):
        """Lo stesso messaggio può arrivare due volte (ripetuto da più ripetitori)."""
        if not sender_ts:
            return False
        return self._one("SELECT id FROM messages WHERE dir='in' AND kind=? AND peer=? AND text=? AND sender_ts=?",
                         (kind, peer, text, sender_ts)) is not None

    def messages(self, kind, peer, before=None, limit=100):
        sql, args = "SELECT * FROM messages WHERE kind=? AND peer=?", [kind, peer]
        if before:
            sql += " AND id<?"
            args.append(before)
        rows = self._all(sql + " ORDER BY id DESC LIMIT ?", args + [limit])
        return list(reversed(rows))

    def messages_after(self, after):
        return self._all("SELECT * FROM messages WHERE id>? ORDER BY id", (after,))

    def conversations(self):
        """Una riga per conversazione: ultimo messaggio e non letti."""
        return self._all(
            "SELECT m.kind, m.peer, m.peer_name, m.text, m.ts, m.dir, m.sender, m.status, m.id AS last_id, "
            "(SELECT COUNT(*) FROM messages u WHERE u.kind=m.kind AND u.peer=m.peer AND u.dir='in' AND u.read=0) AS unread "
            "FROM messages m WHERE m.id IN (SELECT MAX(id) FROM messages GROUP BY kind, peer) ORDER BY m.id DESC")

    def mark_read(self, kind, peer):
        self._exec("UPDATE messages SET read=1 WHERE kind=? AND peer=? AND dir='in' AND read=0", (kind, peer))

    def unread_count(self):
        return self._one("SELECT COUNT(*) AS n FROM messages WHERE dir='in' AND read=0")["n"]

    def sent_since(self, ts):
        return self._one("SELECT COUNT(*) AS n FROM messages WHERE dir='out' AND ts>=?", (ts,))["n"]

    def delete_conversation(self, kind, peer):
        return self._exec("DELETE FROM messages WHERE kind=? AND peer=?", (kind, peer)).rowcount

    # ---------- vicini dei ripetitori (mappa della rete): resta solo l'ultima lettura
    def set_neighbours(self, repeater, items):
        now = int(time.time())
        with self.lock:
            self.conn.execute("DELETE FROM neighbours WHERE repeater=?", (repeater,))
            self.conn.executemany("INSERT INTO neighbours VALUES (?,?,?,?,?)",
                                  [(now, repeater, n["prefix"], n.get("snr"), n.get("secs_ago")) for n in items])
            self.conn.commit()

    def neighbours(self):
        return self._all("SELECT * FROM neighbours ORDER BY repeater")

    def neighbours_ts(self, repeater):
        r = self._one("SELECT MAX(ts) AS ts FROM neighbours WHERE repeater=?", (repeater,))
        return r["ts"] if r else None

    def backup_to(self, dest):
        """Copia coerente del database (API di backup di SQLite)."""
        import sqlite3 as _sq
        out = _sq.connect(dest)
        try:
            with self.lock:
                self.conn.backup(out)
        finally:
            out.close()

    # ---------- impostazioni
    def get(self, key, default=None):
        r = self._one("SELECT val FROM settings WHERE key=?", (key,))
        return r["val"] if r else default

    def set(self, key, val):
        self._exec("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, str(val)))
