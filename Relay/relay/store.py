from __future__ import annotations

import hashlib
import re
import secrets
import sqlite3
import time
import uuid
from pathlib import Path


class Problem(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Store:
    def __init__(self, path: Path, name: str = "Relay"):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS domain(id TEXT PRIMARY KEY, name TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS accounts(
          id TEXT PRIMARY KEY, handle TEXT UNIQUE COLLATE NOCASE NOT NULL,
          name TEXT NOT NULL, manager TEXT REFERENCES accounts(id),
          can_create INTEGER NOT NULL, disabled INTEGER NOT NULL DEFAULT 0,
          token_hash TEXT UNIQUE NOT NULL, created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS messages(
          id INTEGER PRIMARY KEY AUTOINCREMENT, sender TEXT NOT NULL REFERENCES accounts(id),
          recipient TEXT NOT NULL REFERENCES accounts(id), text TEXT NOT NULL,
          created REAL NOT NULL, client_id TEXT, acked INTEGER NOT NULL DEFAULT 0,
          read INTEGER NOT NULL DEFAULT 0, UNIQUE(sender,client_id));
        CREATE INDEX IF NOT EXISTS recipient_messages ON messages(recipient,id);
        CREATE TABLE IF NOT EXISTS audit(
          id INTEGER PRIMARY KEY, ts REAL NOT NULL, actor TEXT NOT NULL,
          action TEXT NOT NULL, target TEXT NOT NULL);
        """)
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS chat_groups(
          id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
          owner TEXT NOT NULL REFERENCES accounts(id), muted INTEGER NOT NULL DEFAULT 0,
          dissolved INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS group_members(
          group_id INTEGER NOT NULL REFERENCES chat_groups(id),
          account TEXT NOT NULL REFERENCES accounts(id), muted INTEGER NOT NULL DEFAULT 0,
          join_after INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(group_id,account));
        CREATE TABLE IF NOT EXISTS group_messages(
          id INTEGER PRIMARY KEY AUTOINCREMENT, group_id INTEGER NOT NULL REFERENCES chat_groups(id),
          sender TEXT NOT NULL REFERENCES accounts(id), text TEXT NOT NULL, created REAL NOT NULL,
          client_id TEXT, UNIQUE(sender,client_id));
        CREATE INDEX IF NOT EXISTS group_history ON group_messages(group_id,id);
        """)
        columns={r["name"] for r in self.db.execute("PRAGMA table_info(messages)")}
        if "group_id" not in columns:
            self.db.execute("ALTER TABLE messages ADD COLUMN group_id INTEGER REFERENCES chat_groups(id)")
        if "group_message_id" not in columns:
            self.db.execute("ALTER TABLE messages ADD COLUMN group_message_id INTEGER REFERENCES group_messages(id)")
        self.db.execute("CREATE UNIQUE INDEX IF NOT EXISTS group_deliveries ON messages(recipient,group_message_id) WHERE group_message_id IS NOT NULL")
        self.db.execute("""CREATE TABLE IF NOT EXISTS files(
          id TEXT PRIMARY KEY, sender TEXT NOT NULL REFERENCES accounts(id),
          name TEXT NOT NULL, size INTEGER NOT NULL, sha256 TEXT NOT NULL)""")
        for table in ("messages", "group_messages"):
            if "file_id" not in {r["name"] for r in self.db.execute(f"PRAGMA table_info({table})")}:
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN file_id TEXT REFERENCES files(id)")
            self.db.execute(f"CREATE INDEX IF NOT EXISTS {table}_files ON {table}(file_id) WHERE file_id IS NOT NULL")
        self.file_root = path.parent / (path.name + ".files")
        self.file_root.mkdir(exist_ok=True, mode=0o700)
        self.db.commit()
        if not self.db.execute("SELECT 1 FROM domain").fetchone():
            self.db.execute("INSERT INTO domain(id,name) VALUES(?,?)", (uuid.uuid4().hex, name))
            self.db.commit()

    def one(self, sql, args=()):
        row = self.db.execute(sql, args).fetchone()
        return self.message_data(dict(row)) if row else None

    def rows(self, sql, args=()):
        return [self.message_data(dict(r)) for r in self.db.execute(sql, args)]

    def message_data(self, row):
        if row.get("file_id"):
            row["attachment"] = self.one("SELECT id,name,size,sha256 FROM files WHERE id=?", (row["file_id"],))
        return row

    def register_file(self, actor, attachment):
        if attachment is not None:
            self.db.execute("INSERT INTO files(id,sender,name,size,sha256) VALUES(?,?,?,?,?)",
                (attachment["id"], actor, attachment["name"], attachment["size"], attachment["sha256"]))
        return attachment["id"] if attachment else None

    @staticmethod
    def same_file(previous, attachment):
        old = previous.get("attachment")
        if old is None or attachment is None:
            return old is None and attachment is None
        return all(old[key] == attachment[key] for key in ("name", "size", "sha256"))

    def file(self, actor, ident):
        if not isinstance(ident, str) or not re.fullmatch(r"[0-9a-f]{32}", ident):
            raise Problem("文件不存在或无权下载", 404)
        private = self.one("SELECT id FROM messages WHERE file_id=? AND group_id IS NULL AND (sender=? OR recipient=?) LIMIT 1", (ident, actor, actor))
        group = self.one("""SELECT g.id FROM group_messages g
            JOIN group_members m ON m.group_id=g.group_id AND m.account=?
            JOIN chat_groups c ON c.id=g.group_id AND c.dissolved=0
            WHERE g.file_id=? AND g.id>m.join_after LIMIT 1""", (actor, ident))
        if not self.enabled(actor) or not (private or group):
            raise Problem("文件不存在或无权下载", 404)
        return self.one("SELECT id,name,size,sha256 FROM files WHERE id=?", (ident,))

    def domain(self):
        return self.one("SELECT * FROM domain")

    def publish_agent_url(self, url):
        """Host-published endpoint; refreshed from the actual listener at startup.

        Keep it in domain metadata so running clients read the same address,
        including installations where the web entry is behind a reverse proxy.
        """
        from urllib.parse import urlsplit
        parsed = urlsplit(url)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError("Invalid agent service URL")
        with self.db:
            if "agent_url" not in {r["name"] for r in self.db.execute("PRAGMA table_info(domain)")}:
                self.db.execute("ALTER TABLE domain ADD COLUMN agent_url TEXT NOT NULL DEFAULT ''")
            self.db.execute("UPDATE domain SET agent_url=?", (url.rstrip("/"),))

    def account(self, ident: str):
        if not isinstance(ident, str):
            raise Problem("账号 ID 须为字符串")
        row = self.one("SELECT id,handle AS username,name,manager,can_create,disabled,created FROM accounts WHERE id=?", (ident,))
        if not row:
            raise Problem("账号不存在", 404)
        return row

    def resolve(self, address: str) -> str:
        """Resolve domain-local @username; internal HTTP clients may also use IDs."""
        if not isinstance(address, str) or not address.strip():
            raise Problem("请提供域内用户名")
        username = address.strip().removeprefix("@").lower()
        row = self.one("SELECT id FROM accounts WHERE handle=?", (username,))
        if row:
            return row["id"]
        # Kept at the storage boundary for internal references, not required by MCP.
        return self.account(address)["id"]

    def public_account(self, row: dict) -> dict:
        """Agent-facing identity: usernames, without internal UUIDs."""
        return {key: value for key, value in {
            "username": row["username"], "name": row["name"],
            "manager": self.account(row["manager"])["username"] if row["manager"] else None,
            "can_create": bool(row["can_create"]), "disabled": bool(row["disabled"]),
            "active": self.enabled(row["id"]), "manageable": row.get("manageable"),
        }.items() if value is not None}

    def public_message(self, row: dict) -> dict:
        result = {**row, "sender": self.account(row["sender"])["username"],
                  "recipient": self.account(row["recipient"])["username"]}
        if row.get("group_id") is not None:
            result.update(kind="group", group_name=self.one("SELECT name FROM chat_groups WHERE id=?", (row["group_id"],))["name"])
        return result

    def enabled(self, ident: str):
        seen = set()
        while ident:
            if ident in seen:
                return False
            seen.add(ident)
            row = self.account(ident)
            if row["disabled"]:
                return False
            ident = row["manager"]
        return True

    def authenticate(self, token: str):
        row = self.one("SELECT id FROM accounts WHERE token_hash=?", (digest(token),))
        if not token or not row or not self.enabled(row["id"]):
            raise Problem("登录凭据无效或账号已停用", 401)
        return self.account(row["id"])

    def manages(self, actor: str, target: str):
        current = self.account(target)["manager"]
        while current:
            if current == actor:
                return True
            current = self.account(current)["manager"]
        return False

    def accounts(self, actor: str):
        result = self.rows("SELECT id,handle AS username,name,manager,can_create,disabled,created FROM accounts ORDER BY created")
        for row in result:
            row["manageable"] = self.manages(actor, row["id"])
            row["active"] = self.enabled(row["id"])
        return result

    def create(self, actor: str | None, handle: str, name: str = "", can_create: bool = True):
        if not isinstance(handle, str):
            raise Problem("用户名须为字符串")
        handle = handle.strip().removeprefix("@").lower()
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", handle):
            raise Problem("用户名须为 1–32 位字母、数字、下划线或减号，并以字母开头；统一保存为小写")
        if len(name) > 80 or type(can_create) is not bool:
            raise Problem("名称最长 80 字，can_create 必须为布尔值")
        if actor is None:
            if self.db.execute("SELECT 1 FROM accounts").fetchone():
                raise Problem("域已经初始化", 403)
        elif not self.account(actor)["can_create"] or not self.enabled(actor):
            raise Problem("没有创建账号的权限", 403)
        ident, token = uuid.uuid4().hex, secrets.token_urlsafe(32)
        try:
            with self.db:
                self.db.execute("INSERT INTO accounts(id,handle,name,manager,can_create,token_hash,created) VALUES(?,?,?,?,?,?,?)",
                                (ident, handle, name.strip() or handle, actor, int(can_create), digest(token), time.time()))
                self.audit(actor or ident, "create_account", ident)
        except sqlite3.IntegrityError:
            raise Problem("该用户名在当前域中已存在", 409) from None
        return {"account": self.account(ident), "token": token}

    def audit(self, actor, action, target):
        self.db.execute("INSERT INTO audit(ts,actor,action,target) VALUES(?,?,?,?)", (time.time(), actor, action, target))

    def manage(self, actor: str, target: str, action: str):
        target = self.resolve(target)
        if not self.manages(actor, target):
            raise Problem("只能管理自己创建的账号及其下属账号，不能管理自己", 403)
        result = {"ok": True}
        with self.db:
            if action in {"disable", "enable"}:
                self.db.execute("UPDATE accounts SET disabled=? WHERE id=?", (int(action == "disable"), target))
            elif action == "reset_token":
                token = secrets.token_urlsafe(32)
                self.db.execute("UPDATE accounts SET token_hash=? WHERE id=?", (digest(token), target))
                result["token"] = token
            else:
                raise Problem("未知账号操作")
            self.audit(actor, action, target)
        return result

    def send(self, actor: str, to: str, text: str, client_id: str = "", *, attachment=None):
        to = self.resolve(to)
        if not self.enabled(to):
            raise Problem("收件账号已停用", 409)
        if actor == to:
            raise Problem("请选择其他账号")
        if not isinstance(text, str) or not text.strip() or len(text) > 20000:
            raise Problem("消息须为 1–20000 字")
        if not isinstance(client_id, str) or len(client_id) > 128:
            raise Problem("client_id 最长 128 字")
        with self.db:
            previous = self.one("SELECT * FROM messages WHERE sender=? AND client_id=?", (actor, client_id)) if client_id else None
            if previous:
                if previous["group_id"] is not None or previous["recipient"] != to or previous["text"] != text or not self.same_file(previous, attachment):
                    raise Problem("client_id 已用于不同的消息", 409)
                return previous
            file_id = self.register_file(actor, attachment)
            cur = self.db.execute("INSERT INTO messages(sender,recipient,text,created,client_id,file_id) VALUES(?,?,?,?,?,?)",
                                  (actor, to, text, time.time(), client_id or None, file_id))
        return self.one("SELECT * FROM messages WHERE id=?", (cur.lastrowid,))

    def inbox(self, actor: str, limit: int = 50):
        return self.rows("SELECT * FROM messages WHERE recipient=? AND acked=0 AND (group_id IS NULL OR EXISTS (SELECT 1 FROM group_members gm JOIN chat_groups g ON g.id=gm.group_id WHERE gm.group_id=messages.group_id AND gm.account=? AND g.dissolved=0 AND messages.group_message_id>gm.join_after)) ORDER BY id LIMIT ?", (actor, actor, self.limit(limit)))

    def acknowledge(self, actor: str, ids: list[int], read: bool = False):
        if type(read) is not bool:
            raise Problem("read 须为布尔值")
        if not isinstance(ids, list) or len(ids) > 200 or any(type(i) is not int or i < 1 for i in ids):
            raise Problem("ids 须为最多 200 个正整数")
        if not ids:
            return {"ok": True, "count": 0}
        placeholders = ",".join("?" for _ in ids)
        with self.db:
            cur = self.db.execute(f"UPDATE messages SET acked=1,read=MAX(read,?) WHERE recipient=? AND id IN ({placeholders})", (int(read), actor, *ids))
        return {"ok": True, "count": cur.rowcount}

    @staticmethod
    def limit(value):
        if type(value) is not int or not 1 <= value <= 200:
            raise Problem("limit 须为 1–200")
        return value

    def history(self, actor: str, peer: str, before: int = 0, limit: int = 50):
        peer = self.resolve(peer)
        if type(before) is not int or before < 0:
            raise Problem("before 须为非负整数")
        return self.rows("SELECT * FROM messages WHERE group_id IS NULL AND ((sender=? AND recipient=?) OR (sender=? AND recipient=?)) AND (?=0 OR id<?) ORDER BY id DESC LIMIT ?",
                         (actor, peer, peer, actor, before, before, self.limit(limit)))[::-1]

    def latest(self, actor: str):
        return self.one("SELECT COALESCE(MAX(id),0) AS id FROM messages WHERE recipient=? AND (group_id IS NULL OR EXISTS (SELECT 1 FROM group_members gm JOIN chat_groups g ON g.id=gm.group_id WHERE gm.group_id=messages.group_id AND gm.account=? AND g.dissolved=0 AND messages.group_message_id>gm.join_after))", (actor, actor))["id"]

    def group(self, actor: str, ident: int):
        if type(ident) is not int or not 1 <= ident <= 9223372036854775807:
            raise Problem('群编号须为正整数')
        row=self.one('SELECT g.*,m.muted AS member_muted,m.join_after FROM chat_groups g JOIN group_members m ON m.group_id=g.id WHERE g.id=? AND m.account=? AND g.dissolved=0',(ident,actor))
        if not row or not self.enabled(actor):
            raise Problem('群不存在或你已不在群中',403)
        members=self.rows('SELECT a.id,a.handle AS username,a.name,m.muted FROM group_members m JOIN accounts a ON a.id=m.account WHERE m.group_id=? ORDER BY a.created',(ident,))
        for m in members:m['active']=self.enabled(m['id'])
        return {**row,'owner_username':self.account(row['owner'])['username'],'members':members,'member_count':len(members),
                'can_manage':row['owner']==actor,'can_send':row['owner']==actor or not (row['muted'] or row['member_muted'])}

    def groups(self, actor: str):
        result=[]
        for row in self.rows('SELECT g.id FROM chat_groups g JOIN group_members m ON m.group_id=g.id WHERE m.account=? AND g.dissolved=0 ORDER BY g.id',(actor,)):
            group=self.group(actor,row['id'])
            group['unread']=self.one('SELECT COUNT(*) AS n FROM messages WHERE recipient=? AND group_id=? AND group_message_id>? AND read=0',(actor,row['id'],group['join_after']))['n']
            result.append({k:v for k,v in group.items() if k!='members'})
        return result

    @staticmethod
    def group_name(name):
        if not isinstance(name,str) or not name.strip() or len(name)>80:
            raise Problem('群名称须为 1–80 字')
        return name.strip()

    def invitees(self, members):
        if not isinstance(members,list) or len(members)>99:
            raise Problem('一次最多选择 99 位群成员')
        ids=list(dict.fromkeys(self.resolve(name) for name in members))
        if any(not self.enabled(ident) for ident in ids):
            raise Problem('不能邀请已停用的账号',409)
        return ids

    def create_group(self, actor: str, name: str, members=None):
        name=self.group_name(name);ids=list(dict.fromkeys([actor,*self.invitees([] if members is None else members)]))
        if not self.enabled(actor):raise Problem('账号已停用',403)
        if len(ids)>100:raise Problem('每个群最多 100 位成员')
        with self.db:
            cursor=self.db.execute('INSERT INTO chat_groups(name,owner,created) VALUES(?,?,?)',(name,actor,time.time()))
            ident=cursor.lastrowid
            self.db.executemany('INSERT INTO group_members(group_id,account) VALUES(?,?)',[(ident,i) for i in ids])
            self.audit(actor,'create_group',str(ident))
        return self.group(actor,ident)

    def manage_group(self, actor: str, ident: int, action: str, *, members=None, member=None, name=None):
        group=self.group(actor,ident)
        if action=='leave':
            if group['owner']==actor:raise Problem('群主请先解散群聊',409)
            with self.db:
                self.db.execute('DELETE FROM group_members WHERE group_id=? AND account=?',(ident,actor))
                self.audit(actor,'leave_group',str(ident))
            return {'ok':True}
        if not group['can_manage']:raise Problem('只有群主可以管理群聊',403)
        if action=='invite':
            ids=self.invitees(members)
            existing={m['id'] for m in group['members']}
            if len(existing|set(ids))>100:raise Problem('每个群最多 100 位成员')
            join_after=self.one('SELECT COALESCE(MAX(id),0) AS id FROM group_messages')['id']
            with self.db:
                self.db.executemany('INSERT OR IGNORE INTO group_members(group_id,account,join_after) VALUES(?,?,?)',[(ident,i,join_after) for i in ids])
                self.audit(actor,'invite_group',str(ident))
        elif action in ('remove','mute','unmute'):
            target=self.resolve(member)
            if target==group['owner']:raise Problem('不能移除或禁言群主')
            if not any(m['id']==target for m in group['members']):raise Problem('账号不在群中',404)
            with self.db:
                if action=='remove':self.db.execute('DELETE FROM group_members WHERE group_id=? AND account=?',(ident,target))
                else:self.db.execute('UPDATE group_members SET muted=? WHERE group_id=? AND account=?',(int(action=='mute'),ident,target))
                self.audit(actor,action+'_group_member',str(ident)+':'+target)
        elif action in ('mute_all','unmute_all','rename','dissolve'):
            if action=='rename':name=self.group_name(name)
            with self.db:
                if action=='rename':self.db.execute('UPDATE chat_groups SET name=? WHERE id=?',(name,ident))
                elif action=='dissolve':self.db.execute('UPDATE chat_groups SET dissolved=1 WHERE id=?',(ident,))
                else:self.db.execute('UPDATE chat_groups SET muted=? WHERE id=?',(int(action=='mute_all'),ident))
                self.audit(actor,action+'_group',str(ident))
        else:raise Problem('未知群操作')
        return {'ok':True} if action=='dissolve' else self.group(actor,ident)

    def send_group(self, actor: str, ident: int, text: str, client_id: str='', *, attachment=None):
        group=self.group(actor,ident)
        if not isinstance(text,str) or not text.strip() or len(text)>20000:raise Problem('消息须为 1–20000 字')
        if not isinstance(client_id,str) or len(client_id)>128:raise Problem('client_id 最长 128 字')
        previous=self.one('SELECT * FROM group_messages WHERE sender=? AND client_id=?',(actor,client_id)) if client_id else None
        if previous:
            if previous['group_id']!=ident or previous['text']!=text or not self.same_file(previous, attachment):raise Problem('client_id 已用于不同的群消息',409)
            return previous
        if not group['can_send']:raise Problem('当前群聊已禁言，你不能发送消息',403)
        with self.db:
            created=time.time()
            file_id=self.register_file(actor, attachment)
            cur=self.db.execute('INSERT INTO group_messages(group_id,sender,text,created,client_id,file_id) VALUES(?,?,?,?,?,?)',(ident,actor,text,created,client_id or None,file_id))
            message=cur.lastrowid
            self.db.executemany('INSERT INTO messages(sender,recipient,text,created,group_id,group_message_id,file_id) VALUES(?,?,?,?,?,?,?)',[(actor,m['id'],text,created,ident,message,file_id) for m in group['members'] if m['id']!=actor])
        return self.one('SELECT * FROM group_messages WHERE id=?',(message,))

    def group_history(self, actor: str, ident: int, before: int=0, limit: int=50):
        group=self.group(actor,ident)
        if type(before) is not int or before<0:raise Problem('before 须为非负整数')
        return self.rows('SELECT g.*,d.id AS delivery_id,COALESCE(d.read,0) AS read FROM group_messages g LEFT JOIN messages d ON d.group_message_id=g.id AND d.recipient=? WHERE g.group_id=? AND g.id>? AND (?=0 OR g.id<?) ORDER BY g.id DESC LIMIT ?',
                         (actor,ident,group['join_after'],before,before,self.limit(limit)))[::-1]

    def public_group_message(self, row):
        result = {**row,'message_id':row['id'],'kind':'group','sender':self.account(row['sender'])['username'],
                  'group_name':self.one('SELECT name FROM chat_groups WHERE id=?',(row['group_id'],))['name']}
        result.pop('id', None)
        return result
