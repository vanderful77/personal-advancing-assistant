import hashlib
import json
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class Store:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.root / "state.sqlite", timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sources(name TEXT PRIMARY KEY,status TEXT,last_ok TEXT,detail TEXT);
        CREATE TABLE IF NOT EXISTS docs(id TEXT PRIMARY KEY,source TEXT,title TEXT,url TEXT,
          body TEXT,hash TEXT,updated TEXT,meta TEXT,active INTEGER DEFAULT 1);
        CREATE TABLE IF NOT EXISTS jobs(doc_id TEXT PRIMARY KEY,hash TEXT,status TEXT);
        CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY,doc_id TEXT,title TEXT,start TEXT,
          end TEXT,kind TEXT,location TEXT,quote TEXT,status TEXT,hash TEXT,source_hash TEXT);
        CREATE TABLE IF NOT EXISTS outbox(id TEXT PRIMARY KEY,body TEXT,status TEXT DEFAULT 'pending');
        CREATE TABLE IF NOT EXISTS drafts(id TEXT PRIMARY KEY,payload TEXT,version TEXT,
          status TEXT,updated TEXT);
        CREATE TABLE IF NOT EXISTS budget(id INTEGER PRIMARY KEY,month TEXT,reserved REAL);
        ''')
        self.db.commit()

    def get(self, key, default=None):
        row = self.db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO kv VALUES (?,?)", (key, json.dumps(value)))

    def status(self, name, status, detail=""):
        with self.db:
            self.db.execute('''INSERT INTO sources VALUES (?,?,?,?) ON CONFLICT(name) DO UPDATE SET
            status=excluded.status,last_ok=COALESCE(excluded.last_ok,sources.last_ok),detail=excluded.detail''',
                            (name, status, now() if status in ("ok", "partial") else None, detail))

    def enqueue(self, key, body):
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO outbox(id,body) VALUES (?,?)", (key, body))

    def document(self, key, source, title, url, body, meta=None):
        meta = meta or {}
        h = digest(json.dumps([title, url, body, meta], sort_keys=True, ensure_ascii=False))
        prev = self.db.execute("SELECT hash,active FROM docs WHERE id=?", (key,)).fetchone()
        if prev and prev[0] == h and prev[1]:
            return False
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO docs VALUES (?,?,?,?,?,?,?,?,?)",
                            (key, source, title, url, body, h, now(), json.dumps(meta), 1))
            self.db.execute("INSERT OR REPLACE INTO jobs VALUES (?,?,?)", (key, h, "pending"))
            # Previously approved extracted dates must not survive changed evidence silently.
            self.db.execute("UPDATE events SET status='stale' WHERE doc_id=?", (key,))
        folder = self.root / "sources" / source
        folder.mkdir(parents=True, exist_ok=True)
        (folder / (digest(key) + ".md")).write_text(
            f"# {title}\n\nSource: {url}\nID: {key}\nVersion: {h}\n\n{body}\n", encoding="utf-8")
        return True

    def retire_missing(self, prefix, seen):
        rows = self.db.execute("SELECT id FROM docs WHERE id LIKE ? AND active=1", (prefix + "%",)).fetchall()
        with self.db:
            for row in rows:
                if row[0] not in seen:
                    self.db.execute("UPDATE docs SET active=0 WHERE id=?", (row[0],))
                    self.db.execute("DELETE FROM jobs WHERE doc_id=?", (row[0],))
                    self.db.execute("UPDATE events SET status='stale' WHERE doc_id=?", (row[0],))

    def search(self, query, limit=8):
        # Parameterized literal substring search works with Chinese, Dutch and English.
        return self.db.execute("SELECT * FROM docs WHERE active=1 AND instr(lower(title || char(10) || body),lower(?))>0 LIMIT ?",
                               (query, limit)).fetchall()

    def event(self, key, doc_id, title, start, end, kind, location, quote, approved=False):
        if kind not in ("deadline", "activity", "preparation"):
            raise ValueError("Invalid event kind")
        dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            raise ValueError("A timezone is required")
        if end:
            end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
            if end_dt.tzinfo is None or end_dt <= dt:
                raise ValueError("Invalid end time")
        elif kind == "activity":
            raise ValueError("Activity end time must be confirmed")
        doc = self.db.execute("SELECT * FROM docs WHERE id=?", (doc_id,)).fetchone()
        if not doc or not doc["active"] or not quote or quote not in doc["body"]:
            raise ValueError("Exact source evidence is required")
        h = digest(json.dumps([title, start, end, kind, location, quote], ensure_ascii=False))
        old = self.db.execute("SELECT * FROM events WHERE id=?", (key,)).fetchone()
        status = "approved" if approved else "pending"
        if old and old["hash"] == h and old["source_hash"] == doc["hash"]:
            status = old["status"]
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                            (key, doc_id, title, start, end, kind, location, quote, status, h, doc["hash"]))
        return not old or old["hash"] != h

    def approve(self, event_id):
        with self.db:
            row = self.db.execute('''SELECT e.*,d.hash current_hash,d.active FROM events e JOIN docs d ON d.id=e.doc_id
                                 WHERE e.id=?''', (event_id,)).fetchone()
            if not row or not row["active"] or row["source_hash"] != row["current_hash"] or row["status"] == "stale":
                raise ValueError("Evidence has changed; re-extract before approving")
            self.db.execute("UPDATE events SET status='approved' WHERE id=?", (event_id,))

    def draft(self, draft_id, payload):
        if not payload.get("to") or not payload.get("body"):
            raise ValueError("Draft requires recipients and body")
        existing = self.db.execute("SELECT status FROM drafts WHERE id=?", (draft_id,)).fetchone()
        if existing and existing[0] in ("sending", "unknown", "sent"):
            raise ValueError("Cannot edit a draft with an unresolved or completed send")
        serialized = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        version = digest(serialized)
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO drafts VALUES (?,?,?,?,?)",
                            (draft_id, serialized, version, "review", now()))
        return version

    def confirm_draft(self, draft_id, version):
        with self.db:
            changed = self.db.execute("UPDATE drafts SET status='confirmed' WHERE id=? AND version=? AND status='review'",
                                      (draft_id, version)).rowcount
        if changed != 1:
            raise ValueError("Confirmation expired or already used")

    def claim_send(self, draft_id, version):
        # The browser sender must read back the exact payload before invoking this.
        with self.db:
            count = self.db.execute("UPDATE drafts SET status='sending' WHERE id=? AND version=? AND status='confirmed'",
                                    (draft_id, version)).rowcount
        if count != 1:
            raise ValueError("No matching unused confirmation")

    def reserve(self, month, amount, cap):
        if not math.isfinite(amount) or not math.isfinite(cap) or amount <= 0 or cap <= 0:
            raise ValueError("Positive budget settings required")
        try:
            self.db.execute("BEGIN IMMEDIATE")
            total = self.db.execute("SELECT COALESCE(SUM(reserved),0) FROM budget WHERE month=?", (month,)).fetchone()[0]
            if total + amount > cap:
                raise ValueError("Monthly model budget reached")
            self.db.execute("INSERT INTO budget(month,reserved) VALUES (?,?)", (month, amount))
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
