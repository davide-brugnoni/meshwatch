import sqlite3
import threading
import time

COLORS = ["#2fb38a", "#ff7a45", "#5aa9ff", "#e8c547", "#c77dff", "#ff5c8a"]


class DB:
    def __init__(self, path):
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
                CREATE INDEX IF NOT EXISTS i_pos_ts ON positions(ts);
                CREATE INDEX IF NOT EXISTS i_rep ON repeater_status(name, ts);
                """
            )
            # migrazione dalla prima versione: posizioni e richieste legate a un tracker
            for table in ("positions", "tracker_polls"):
                cols = [r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")]
                if "tracker_id" not in cols:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN tracker_id INTEGER")
            self.conn.execute("CREATE INDEX IF NOT EXISTS i_pos_tr ON positions(tracker_id, ts)")
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
                      "low_battery_v", "paused", "color")

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
        if not keep_history:
            self._exec("DELETE FROM positions WHERE tracker_id=?", (tid,))
            self._exec("DELETE FROM tracker_polls WHERE tracker_id=?", (tid,))

    def clear_track(self, tid=None, keep_last=True):
        """Cancella i punti della traccia (di un dispositivo o di tutti). Con keep_last resta
        l'ultima posizione di ciascuno, così la mappa mostra ancora dov'è."""
        where, args = ("WHERE tracker_id=?", [tid]) if tid is not None else ("", [])
        if keep_last:
            keep = "rowid NOT IN (SELECT (SELECT p2.rowid FROM positions p2 WHERE p2.tracker_id=t.id " \
                   "ORDER BY p2.ts DESC, p2.rowid DESC LIMIT 1) FROM trackers t)"
            where = (where + " AND " if where else "WHERE ") + keep
        return self._exec(f"DELETE FROM positions {where}", args).rowcount

    def add_position(self, tid, lat, lon, alt, batt_v):
        self._exec("INSERT INTO positions (ts, lat, lon, alt, batt_v, tracker_id) VALUES (?,?,?,?,?,?)",
                   (int(time.time()), lat, lon, alt, batt_v, tid))

    def add_tracker_poll(self, tid, ok):
        self._exec("INSERT INTO tracker_polls (ts, ok, tracker_id) VALUES (?,?,?)",
                   (int(time.time()), 1 if ok else 0, tid))

    def last_position(self, tid):
        return self._one("SELECT * FROM positions WHERE tracker_id=? ORDER BY ts DESC LIMIT 1", (tid,))

    def positions_since(self, tid, ts):
        return self._all("SELECT * FROM positions WHERE tracker_id=? AND ts>=? ORDER BY ts", (tid, ts))

    def last_tracker_ok(self, tid):
        r = self._one("SELECT ts FROM tracker_polls WHERE tracker_id=? AND ok=1 ORDER BY ts DESC LIMIT 1", (tid,))
        return r["ts"] if r else None

    # ---------- ripetitori
    def repeaters(self):
        return self._all("SELECT * FROM repeaters ORDER BY name")

    def repeater(self, name):
        return self._one("SELECT * FROM repeaters WHERE name=?", (name,))

    def set_repeater(self, name, password):
        self._exec("INSERT OR REPLACE INTO repeaters VALUES (?,?)", (name, password or ""))

    def delete_repeater(self, name):
        self._exec("DELETE FROM repeaters WHERE name=?", (name,))

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
            "SELECT ts, bat_mv, noise, snr FROM repeater_status WHERE name=? AND ok=1 AND ts>=? ORDER BY ts",
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

    # ---------- terminale
    def add_cli(self, user, node, cmd, reply):
        self._exec("INSERT INTO cli_log VALUES (?,?,?,?,?)", (int(time.time()), user, node, cmd, reply))

    def cli_history(self, node, limit=50):
        rows = self._all("SELECT * FROM cli_log WHERE node=? ORDER BY rowid DESC LIMIT ?", (node, limit))
        return list(reversed(rows))

    # ---------- impostazioni
    def get(self, key, default=None):
        r = self._one("SELECT val FROM settings WHERE key=?", (key,))
        return r["val"] if r else default

    def set(self, key, val):
        self._exec("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, str(val)))
