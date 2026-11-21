import hashlib
import json
import secrets
import sqlite3
import time
import uuid
from pathlib import Path

from relay.store import Problem


class Store:
    @staticmethod
    def normalize_config(config):
        config = dict(config)
        legacy_default = config.pop('sandbox_default_access', 'read')
        if config.get('filesystem_format') == 2:
            return config
        grants = list(config.get('grants', []))
        if legacy_default != 'deny' and not any(g['path'] == '/' for g in grants):
            grants.insert(0, {'path': '/', 'access': 'read'})
        return {**config, 'grants': grants, 'filesystem_format': 2}

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS agents(
          id TEXT PRIMARY KEY, handle TEXT UNIQUE NOT NULL, parent TEXT,
          slot INTEGER UNIQUE NOT NULL, config TEXT NOT NULL, token_hash TEXT UNIQUE NOT NULL,
          relay_account TEXT, thread TEXT, status TEXT NOT NULL DEFAULT 'offline',
          error TEXT, created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS messages(
          id INTEGER PRIMARY KEY AUTOINCREMENT, sender TEXT NOT NULL, recipient TEXT NOT NULL,
          kind TEXT NOT NULL, text TEXT NOT NULL, created REAL NOT NULL, accepted INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS terminal(
          id INTEGER PRIMARY KEY, agent TEXT NOT NULL, direction TEXT NOT NULL, text TEXT NOT NULL, created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS audit(
          id INTEGER PRIMARY KEY, actor TEXT NOT NULL, action TEXT NOT NULL, target TEXT NOT NULL, created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)
        if "archived" not in {r[1] for r in self.db.execute("PRAGMA table_info(agents)")}:
            self.db.execute("ALTER TABLE agents ADD COLUMN archived INTEGER NOT NULL DEFAULT 0")
            self.db.commit()

        if "telemetry" not in {r[1] for r in self.db.execute("PRAGMA table_info(agents)")}:
            self.db.execute("ALTER TABLE agents ADD COLUMN telemetry TEXT NOT NULL DEFAULT '{}'")
            self.db.commit()

        if "run_requested" not in {r[1] for r in self.db.execute("PRAGMA table_info(agents)")}:
            self.db.execute("ALTER TABLE agents ADD COLUMN run_requested INTEGER NOT NULL DEFAULT 0")
            self.db.commit()

        # Preserve historical implicit read access as an editable explicit rule.
        with self.db:
            for row in self.db.execute("SELECT id,config FROM agents").fetchall():
                config = json.loads(row['config'])
                if config.get('filesystem_format') == 2 and 'sandbox_default_access' not in config:
                    continue
                config = self.normalize_config(config)
                self.db.execute('UPDATE agents SET config=? WHERE id=?',
                                (json.dumps(config, ensure_ascii=False), row['id']))

    def one(self, sql, args=()):
        row = self.db.execute(sql, args).fetchone()
        return dict(row) if row else None

    def restore_members(self):
        row = self.one("SELECT value FROM settings WHERE key='restore_members'")
        # Older clients' member-level settings remain effective until the owner
        # explicitly chooses a space policy. New spaces default to no restoration.
        return json.loads(row['value']) if row else None

    def set_restore_members(self, enabled):
        if type(enabled) is not bool:
            raise Problem('restore_members 须为布尔值')
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO settings VALUES('restore_members',?)", (json.dumps(enabled),))

    def rows(self, sql, args=()):
        return [dict(row) for row in self.db.execute(sql, args)]

    def agent(self, ident):
        row = self.one("SELECT * FROM agents WHERE id=?", (ident,))
        if not row:
            raise Problem("agent 不存在", 404)
        row.pop("token_hash")
        row["config"] = json.loads(row["config"])
        row["telemetry"] = json.loads(row["telemetry"])
        return row

    def agents(self):
        return [self.agent(row["id"]) for row in self.rows("SELECT id FROM agents ORDER BY created")]

    def create(self, handle, parent, slot, config, account):
        config = {**config, 'filesystem_format': 2}
        ident, token = uuid.uuid4().hex, secrets.token_urlsafe(32)
        with self.db:
            self.db.execute("INSERT INTO agents(id,handle,parent,slot,config,token_hash,relay_account,created) VALUES(?,?,?,?,?,?,?,?)",
                            (ident, handle, parent, slot, json.dumps(config, ensure_ascii=False), hashlib.sha256(token.encode()).hexdigest(), account, time.time()))
        return self.agent(ident), token

    def authenticate(self, token):
        row = self.one("SELECT id FROM agents WHERE token_hash=?", (hashlib.sha256(token.encode()).hexdigest(),))
        if not token or not row:
            raise Problem("Squad 凭据无效", 401)
        agent = self.agent(row["id"])
        if agent["archived"]:
            raise Problem("agent 已归档", 401)
        return agent

    def update(self, ident, **fields):
        allowed = {"config", "thread", "status", "error", "archived", "telemetry", "run_requested"}
        if not fields.keys() <= allowed:
            raise ValueError("Unsupported update")
        with self.db:
            for key, value in fields.items():
                self.db.execute(f"UPDATE agents SET {key}=? WHERE id=?", (json.dumps(value, ensure_ascii=False) if key in {"config", "telemetry"} else value, ident))

    def audit(self, actor, action, target):
        with self.db:
            self.db.execute("INSERT INTO audit(actor,action,target,created) VALUES(?,?,?,?)", (actor, action, target, time.time()))

    def message(self, sender, recipient, kind, text):
        if not isinstance(text, str) or not text.strip() or len(text) > 20000:
            raise Problem("消息须为 1–20000 字")
        with self.db:
            cur = self.db.execute("INSERT INTO messages(sender,recipient,kind,text,created) VALUES(?,?,?,?,?)", (sender, recipient, kind, text, time.time()))
        return cur.lastrowid

    def accept(self, ident, message):
        with self.db:
            self.db.execute("UPDATE messages SET accepted=1 WHERE id=? AND recipient=?", (message, ident))

    def pending(self, ident):
        return self.rows("SELECT * FROM messages WHERE recipient=? AND accepted=0 ORDER BY id", (ident,))

    def terminal(self, ident, direction, text):
        with self.db:
            self.db.execute("INSERT INTO terminal(agent,direction,text,created) VALUES(?,?,?,?)", (ident, direction, text, time.time()))
