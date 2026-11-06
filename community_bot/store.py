"""SQLite state: processed webhook events, payment links to Discord members, payments,
members, tickets, AI usage, the audit log, and the two retry queues (role changes and
outbound hooks). One file, one connection, one lock: the bot and the web server share it."""
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  provider TEXT NOT NULL, event_id TEXT NOT NULL, type TEXT, created INTEGER,
  received_at REAL, status TEXT, detail TEXT,
  PRIMARY KEY (provider, event_id));
CREATE TABLE IF NOT EXISTS links (
  provider TEXT NOT NULL, customer_id TEXT NOT NULL, discord_user_id TEXT, email TEXT,
  subscription_id TEXT, plan TEXT, status TEXT, past_due_since REAL,
  last_event_created INTEGER DEFAULT 0, updated_at REAL,
  PRIMARY KEY (provider, customer_id));
CREATE TABLE IF NOT EXISTS payments (
  id INTEGER PRIMARY KEY AUTOINCREMENT, provider TEXT, event_id TEXT, kind TEXT,
  amount_minor INTEGER, currency TEXT, customer_id TEXT, discord_user_id TEXT, at REAL);
CREATE TABLE IF NOT EXISTS members (
  discord_user_id TEXT PRIMARY KEY, username TEXT, joined_at REAL, left_at REAL);
CREATE TABLE IF NOT EXISTS tickets (
  id INTEGER PRIMARY KEY AUTOINCREMENT, discord_user_id TEXT, username TEXT, subject TEXT,
  body TEXT, source TEXT, status TEXT DEFAULT 'open', created_at REAL, closed_at REAL,
  closed_by TEXT, mod_message_id TEXT);
CREATE TABLE IF NOT EXISTS ai_usage (
  day TEXT, discord_user_id TEXT, calls INTEGER DEFAULT 0, tokens_in INTEGER DEFAULT 0,
  tokens_out INTEGER DEFAULT 0, PRIMARY KEY (day, discord_user_id));
CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL, actor TEXT, action TEXT,
  discord_user_id TEXT, role_id TEXT, reason TEXT, ref TEXT, result TEXT);
CREATE TABLE IF NOT EXISTS role_ops (
  id INTEGER PRIMARY KEY AUTOINCREMENT, discord_user_id TEXT, role_id TEXT, op TEXT,
  reason TEXT, ref TEXT, attempts INTEGER DEFAULT 0, next_at REAL, status TEXT DEFAULT 'pending',
  last_error TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS outbox (
  id TEXT PRIMARY KEY, event TEXT, body TEXT, attempts INTEGER DEFAULT 0, next_at REAL,
  status TEXT DEFAULT 'pending', last_error TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS bookings (
  invitee_uri TEXT PRIMARY KEY, discord_user_id TEXT, name TEXT, email TEXT, event_name TEXT,
  start_time TEXT, status TEXT, updated_at REAL);
"""


class Store:
    def __init__(self, path, clock=time.time):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self.lock = threading.RLock()
        self.clock = clock

    # --- helpers
    def _q(self, sql: str, args=()) -> List[Dict[str, Any]]:
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def _one(self, sql: str, args=()) -> Optional[Dict[str, Any]]:
        rows = self._q(sql, args)
        return rows[0] if rows else None

    def _x(self, sql: str, args=()) -> sqlite3.Cursor:
        with self.lock:
            return self.db.execute(sql, args)

    # --- webhook events (idempotency)
    def claim_event(self, provider: str, event_id: str, type_: str, created: int) -> bool:
        """True the first time an event id is seen. A redelivery returns False."""
        try:
            self._x("INSERT INTO events (provider, event_id, type, created, received_at, status) VALUES (?,?,?,?,?,?)",
                    (provider, event_id, type_, created, self.clock(), "processing"))
            return True
        except sqlite3.IntegrityError:
            return False

    def finish_event(self, provider: str, event_id: str, status: str, detail: Any = None):
        self._x("UPDATE events SET status=?, detail=? WHERE provider=? AND event_id=?",
                (status, json.dumps(detail) if detail is not None else None, provider, event_id))

    def release_event(self, provider: str, event_id: str):
        """Forget an event that failed before anything was applied, so a redelivery is processed."""
        self._x("DELETE FROM events WHERE provider=? AND event_id=?", (provider, event_id))

    def events(self, limit=200):
        return self._q("SELECT * FROM events ORDER BY received_at DESC LIMIT ?", (limit,))

    # --- links (payment customer <-> Discord member)
    def get_link(self, provider: str, customer_id: str):
        return self._one("SELECT * FROM links WHERE provider=? AND customer_id=?", (provider, customer_id))

    def link_for_user(self, discord_user_id: str):
        return self._one("SELECT * FROM links WHERE discord_user_id=? ORDER BY updated_at DESC LIMIT 1",
                         (discord_user_id,))

    def links_for_user(self, discord_user_id: str):
        return self._q("SELECT * FROM links WHERE discord_user_id=?", (discord_user_id,))

    def upsert_link(self, provider: str, customer_id: str, **fields):
        current = self.get_link(provider, customer_id) or {}
        merged = {**current, **{k: v for k, v in fields.items() if v is not None}}
        if "past_due_since" in fields:  # allowed to be cleared
            merged["past_due_since"] = fields["past_due_since"]
        self._x("""INSERT INTO links (provider, customer_id, discord_user_id, email, subscription_id, plan, status,
                   past_due_since, last_event_created, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(provider, customer_id) DO UPDATE SET discord_user_id=excluded.discord_user_id,
                   email=excluded.email, subscription_id=excluded.subscription_id, plan=excluded.plan,
                   status=excluded.status, past_due_since=excluded.past_due_since,
                   last_event_created=excluded.last_event_created, updated_at=excluded.updated_at""",
                (provider, customer_id, merged.get("discord_user_id"), merged.get("email"),
                 merged.get("subscription_id"), merged.get("plan"), merged.get("status"),
                 merged.get("past_due_since"), merged.get("last_event_created") or 0, self.clock()))
        return self.get_link(provider, customer_id)

    def links(self):
        return self._q("SELECT * FROM links ORDER BY updated_at DESC")

    # --- payments
    def add_payment(self, provider, event_id, kind, amount_minor, currency, customer_id, discord_user_id):
        self._x("INSERT INTO payments (provider, event_id, kind, amount_minor, currency, customer_id, discord_user_id, at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (provider, event_id, kind, amount_minor, currency, customer_id, discord_user_id, self.clock()))

    def payments(self, limit=200):
        return self._q("SELECT * FROM payments ORDER BY at DESC, id DESC LIMIT ?", (limit,))

    # --- members
    def member_joined(self, discord_user_id: str, username: str):
        self._x("""INSERT INTO members (discord_user_id, username, joined_at, left_at) VALUES (?,?,?,NULL)
                   ON CONFLICT(discord_user_id) DO UPDATE SET username=excluded.username,
                   joined_at=excluded.joined_at, left_at=NULL""", (discord_user_id, username, self.clock()))

    def member_left(self, discord_user_id: str):
        self._x("UPDATE members SET left_at=? WHERE discord_user_id=?", (self.clock(), discord_user_id))

    def remember_member(self, discord_user_id: str, username: str):
        """Note a member seen in a command, without changing their join time."""
        self._x("""INSERT INTO members (discord_user_id, username, joined_at) VALUES (?,?,NULL)
                   ON CONFLICT(discord_user_id) DO UPDATE SET username=excluded.username""",
                (discord_user_id, username))

    def member_by_username(self, username: str):
        name = username.strip().lstrip("@").lower()
        return self._one("SELECT * FROM members WHERE lower(username)=? AND left_at IS NULL", (name,))

    def members(self):
        return self._q("SELECT * FROM members ORDER BY COALESCE(joined_at, 0) DESC")

    # --- tickets
    def open_ticket(self, discord_user_id, username, subject, body, source) -> int:
        cur = self._x("INSERT INTO tickets (discord_user_id, username, subject, body, source, status, created_at)"
                      " VALUES (?,?,?,?,?,'open',?)", (discord_user_id, username, subject, body, source, self.clock()))
        return cur.lastrowid

    def set_ticket_message(self, ticket_id: int, message_id: str):
        self._x("UPDATE tickets SET mod_message_id=? WHERE id=?", (message_id, ticket_id))

    def close_ticket(self, ticket_id: int, closed_by: str) -> Optional[Dict]:
        t = self.ticket(ticket_id)
        if not t or t["status"] != "open":
            return None
        self._x("UPDATE tickets SET status='closed', closed_at=?, closed_by=? WHERE id=?",
                (self.clock(), closed_by, ticket_id))
        return self.ticket(ticket_id)

    def ticket(self, ticket_id: int):
        return self._one("SELECT * FROM tickets WHERE id=?", (ticket_id,))

    def tickets(self, status: Optional[str] = None):
        if status:
            return self._q("SELECT * FROM tickets WHERE status=? ORDER BY id DESC", (status,))
        return self._q("SELECT * FROM tickets ORDER BY id DESC")

    # --- AI usage
    def ai_calls_today(self, day: str, discord_user_id: Optional[str] = None) -> int:
        if discord_user_id is None:
            row = self._one("SELECT COALESCE(SUM(calls),0) AS n FROM ai_usage WHERE day=?", (day,))
        else:
            row = self._one("SELECT COALESCE(SUM(calls),0) AS n FROM ai_usage WHERE day=? AND discord_user_id=?",
                            (day, discord_user_id))
        return int(row["n"])

    def add_ai_call(self, day: str, discord_user_id: str, tokens_in: int, tokens_out: int):
        self._x("""INSERT INTO ai_usage (day, discord_user_id, calls, tokens_in, tokens_out) VALUES (?,?,1,?,?)
                   ON CONFLICT(day, discord_user_id) DO UPDATE SET calls=calls+1,
                   tokens_in=tokens_in+excluded.tokens_in, tokens_out=tokens_out+excluded.tokens_out""",
                (day, discord_user_id, tokens_in, tokens_out))

    def ai_usage(self):
        return self._q("SELECT day, SUM(calls) AS calls, SUM(tokens_in) AS tokens_in, SUM(tokens_out) AS tokens_out"
                       " FROM ai_usage GROUP BY day ORDER BY day DESC")

    # --- audit log
    def audit(self, actor, action, discord_user_id=None, role_id=None, reason=None, ref=None, result="ok"):
        self._x("INSERT INTO audit (at, actor, action, discord_user_id, role_id, reason, ref, result)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (self.clock(), actor, action, discord_user_id, role_id, reason, ref, result))

    def audit_log(self, limit=300):
        return self._q("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,))

    # --- role change retry queue
    def queue_role_op(self, discord_user_id, role_id, op, reason, ref, error, delay=30.0):
        self._x("INSERT INTO role_ops (discord_user_id, role_id, op, reason, ref, attempts, next_at, status, last_error,"
                " created_at) VALUES (?,?,?,?,?,1,?,'pending',?,?)",
                (discord_user_id, role_id, op, reason, ref, self.clock() + delay, error, self.clock()))

    def due_role_ops(self):
        return self._q("SELECT * FROM role_ops WHERE status='pending' AND next_at<=? ORDER BY id", (self.clock(),))

    def update_role_op(self, op_id, status, error=None, delay=None):
        if delay is None:
            self._x("UPDATE role_ops SET status=?, last_error=?, attempts=attempts+1 WHERE id=?", (status, error, op_id))
        else:
            self._x("UPDATE role_ops SET status=?, last_error=?, attempts=attempts+1, next_at=? WHERE id=?",
                    (status, error, self.clock() + delay, op_id))

    def role_ops(self, status: Optional[str] = None):
        if status:
            return self._q("SELECT * FROM role_ops WHERE status=? ORDER BY id DESC", (status,))
        return self._q("SELECT * FROM role_ops ORDER BY id DESC")

    # --- outbound hook queue
    def enqueue_hook(self, hook_id: str, event: str, body: str) -> bool:
        try:
            self._x("INSERT INTO outbox (id, event, body, next_at, status, created_at) VALUES (?,?,?,?,'pending',?)",
                    (hook_id, event, body, self.clock(), self.clock()))
            return True
        except sqlite3.IntegrityError:
            return False

    def due_hooks(self):
        return self._q("SELECT * FROM outbox WHERE status='pending' AND next_at<=? ORDER BY created_at", (self.clock(),))

    def update_hook(self, hook_id, status, error=None, delay=None):
        self._x("UPDATE outbox SET status=?, last_error=?, attempts=attempts+1, next_at=? WHERE id=?",
                (status, error, self.clock() + (delay or 0), hook_id))

    def hooks(self):
        return self._q("SELECT * FROM outbox ORDER BY created_at DESC")

    # --- Calendly bookings
    def upsert_booking(self, invitee_uri, **fields):
        current = self._one("SELECT * FROM bookings WHERE invitee_uri=?", (invitee_uri,)) or {}
        merged = {**current, **{k: v for k, v in fields.items() if v is not None}}
        self._x("""INSERT INTO bookings (invitee_uri, discord_user_id, name, email, event_name, start_time, status,
                   updated_at) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(invitee_uri) DO UPDATE SET
                   discord_user_id=excluded.discord_user_id, name=excluded.name, email=excluded.email,
                   event_name=excluded.event_name, start_time=excluded.start_time, status=excluded.status,
                   updated_at=excluded.updated_at""",
                (invitee_uri, merged.get("discord_user_id"), merged.get("name"), merged.get("email"),
                 merged.get("event_name"), merged.get("start_time"), merged.get("status"), self.clock()))

    def bookings(self):
        return self._q("SELECT * FROM bookings ORDER BY updated_at DESC")

    def active_bookings_for(self, discord_user_id: str):
        return self._q("SELECT * FROM bookings WHERE discord_user_id=? AND status='active'", (discord_user_id,))
