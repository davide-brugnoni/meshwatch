"""Login con utenti e ruoli, sessioni firmate in cookie.

Ruoli, dal meno al più potente:
    guest   senza login: vede la mappa e lo stato (se la mappa è libera), non chiede posizioni
    viewer  come guest, in più chiede la posizione
    admin   gestisce tutto (dispositivi, utenti, gateway, sistema, aggiornamenti, terminale)

La gestione richiede sempre un utente admin: finché non esiste, la pagina propone di crearlo.

Le password nel config sono salvate come hash:
    python -m meshwatch hashpw
"""
import base64
import hashlib
import hmac
import json
import secrets
import time

ITER = 200_000
SESSION_DAYS = 30


def hash_password(pw):
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, ITER)
    return f"pbkdf2${ITER}${salt.hex()}${dk.hex()}"


def check_password(pw, stored):
    try:
        _, it, salt, dk = stored.split("$")
        test = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), int(it))
        return hmac.compare_digest(test.hex(), dk)
    except Exception:
        return False


class Auth:
    """Utenti salvati nel database (gestibili dalla pagina)."""

    def __init__(self, db):
        self.db = db
        secret = db.get("session_secret")
        if not secret:
            secret = secrets.token_hex(32)
            db.set("session_secret", secret)
        self.secret = secret.encode()
        self.failures = {}   # ip -> (count, first_ts)

    @property
    def enabled(self):
        return bool(self.db.users())

    def _sign(self, data):
        return hmac.new(self.secret, data, hashlib.sha256).hexdigest()

    def login(self, username, password, ip):
        n, t0 = self.failures.get(ip, (0, time.time()))
        if time.time() - t0 > 900:
            n, t0 = 0, time.time()
        if n >= 8:
            return None   # troppi tentativi: blocco 15 minuti
        u = self.db.user(username)
        if not u or not check_password(password, u.get("password_hash", "")):
            self.failures[ip] = (n + 1, t0)
            return None
        self.failures.pop(ip, None)
        payload = base64.urlsafe_b64encode(json.dumps(
            {"u": username, "h": u["password_hash"][-8:],
             "exp": int(time.time()) + SESSION_DAYS * 86400}).encode())
        return payload.decode() + "." + self._sign(payload)

    def user_from_token(self, token):
        """Ritorna {"username","role"} o None."""
        if not token or "." not in token:
            return None
        payload, sig = token.rsplit(".", 1)
        if not hmac.compare_digest(self._sign(payload.encode()), sig):
            return None
        try:
            data = json.loads(base64.urlsafe_b64decode(payload))
        except Exception:
            return None
        if data.get("exp", 0) < time.time():
            return None
        u = self.db.user(data.get("u"))
        # utente eliminato o password cambiata: la sessione non vale più
        if not u or data.get("h") != u["password_hash"][-8:]:
            return None
        return {"username": u["username"], "role": u.get("role", "viewer")}
