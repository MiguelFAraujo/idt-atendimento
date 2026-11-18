#!/usr/bin/env python3
# registro: 2026-11-14 | autor: opencode | modelo: big-pickle
"""
IDT Atendimento — backend proprio (Opcao B) sobre WAHA.

Espelho de conversas do WhatsApp + handoff humano (modo bot/humano) para o
produto de atendimento do IDT-Lab. Sem plataforma externa (nada de Chatwoot).

Ingestao em dois modos:
  - poller: le /api/{session}/chats/overview e /api/{session}/chats/{id}/messages
  - webhook: recebe eventos do WAHA em POST /webhook/waha/<token>

Endpoints:
  GET  /                         -> app (espelho)
  GET  /health                   -> healthcheck (sem auth)
  GET  /api/conversations        -> lista conversas
  GET  /api/conversations/<id>   -> conversa + mensagens
  POST /api/conversations/<id>/mode      {"mode":"bot"|"humano"}  (o check/x)
  POST /api/conversations/<id>/read      marca lida
  POST /api/conversations/<id>/send      {"text":"..."} envia pelo WAHA
  GET  /assumir/<id>             -> botao ✓ do Telegram (pausa o bot na conversa)
  GET  /bot/<id>                 -> botao ✕ do Telegram (devolve ao bot)
  GET  /api/bot/settings         -> estado do bot (modo, KB, guardrails, contadores)
  POST /api/bot/settings         -> liga/desliga e alterna off|dry|live
  GET  /api/kb                   -> knowledge base carregada
  GET  /api/slots?days=N         -> horarios livres (F4, derivados da KB)
  GET  /api/bookings             -> agendamentos
  POST /api/bookings             -> cria agendamento (valida slot deterministicamente)
  POST /api/bookings/<id>/cancel -> cancela agendamento
  GET  /api/bot/test/<id>        -> dry-run do bot para 1 conversa (nao envia)
  POST /webhook/waha/<token>     -> ingestao de eventos WAHA

F2: mensagem recebida nova -> contador de nao lidas + notificacao ao dono no
Telegram com botoes de URL (✓ Assumir / ✕ Deixar no bot / Abrir conversa).
F3: bot IA sobre a KB do cliente. Padrao ARBITER — o LLM propoe a resposta e o
executor deterministico valida (precos/promessas contra a KB); falha vira handoff.
Guardrails: cooldown, teto diario, horario silencioso, allowlist (obrigatoria em
live) e modo off|dry|live.
F4: agenda deterministica derivada dos horarios da KB + reservas validadas.
"""
import json
import os
import re
import sqlite3
from contextlib import contextmanager
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.request
import queue
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from flask import Flask, Response, jsonify, request

# Carrega .env se existir (sem depender de python-dotenv)
ENV_FILE = Path(__file__).resolve().parent / ".env"
if ENV_FILE.exists():
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

# Cliente Map-OS (integracao com ERP para criacao e consulta de OS)
try:
    from mapos_client import MaposClient, MaposClientError
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from mapos_client import MaposClient, MaposClientError

# ---------------------------------------------------------------- config
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("ATEND_DATA", "/mnt/storage/lab/data/idt-atendimento"))
DB_PATH = DATA_DIR / "atendimento.db"
WAHA_URL = os.getenv("WAHA_URL", "http://127.0.0.1:3003").rstrip("/")
SESSIONS = [s.strip() for s in os.getenv("ATEND_SESSIONS", "idt-lab-wpp").split(",") if s.strip()]
POLL_SECONDS = int(os.getenv("ATEND_POLL_SECONDS", "20"))
CHAT_LIMIT = int(os.getenv("ATEND_CHAT_LIMIT", "100"))
MSG_LIMIT = int(os.getenv("ATEND_MSG_LIMIT", "30"))
APP_TOKEN = os.getenv("ATEND_TOKEN", "")
WEBHOOK_TOKEN = os.getenv("ATEND_WEBHOOK_TOKEN", "")
PORT = int(os.getenv("ATEND_PORT", "8138"))
BIND = os.getenv("ATEND_BIND", "0.0.0.0")

# --- Multi-tenant client config directory ---
CLIENTS_DIR = Path("/mnt/storage/client-bots/clients")

def load_client_config(session: str) -> dict:
    """Load client config.json by session name (instance or nome field)."""
    for client_dir in CLIENTS_DIR.iterdir():
        if not client_dir.is_dir():
            continue
        cfg_path = client_dir / "config.json"
        if not cfg_path.exists():
            continue
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            if cfg.get("instance") == session or cfg.get("nome") == session:
                cfg["_client_dir"] = client_dir
                return cfg
        except Exception:
            continue
    return {}


# --- SSE: clientes conectados para eventos em tempo real ---
SSE_CLIENTS = set()
SSE_LOCK = threading.Lock()

def _emit_bot_event(session, event_type, payload):
    """Grava evento na tabela events e broadcast para clientes SSE conectados."""
    try:
        with db() as c:
            c.execute(
                "INSERT INTO events (received_at, event, session, payload) VALUES (?,?,?,?)",
                (now_iso(), event_type, session, json.dumps(payload, ensure_ascii=False)[:200000]),
            )
    except Exception as e:
        pass

    evt = json.dumps({"session": session, "event": event_type, "payload": payload}, ensure_ascii=False)
    with SSE_LOCK:
        dead = []
        for q in SSE_CLIENTS:
            try:
                q.put_nowait(evt)
            except Exception:
                dead.append(q)
        for q in dead:
            SSE_CLIENTS.discard(q)

# WAHA timeout (configurable per session via ATEND_WHA_TIMEOUT_<SESSION>)
WAHA_TIMEOUT = int(os.getenv("WAHA_TIMEOUT", "60"))

# Per-session limits (fallback to global)
def _session_limit(session: str, env_prefix: str, default: int) -> int:
    key = f"ATEND_{env_prefix}_{session.upper().replace('-', '_')}"
    return int(os.getenv(key, str(default)))

# --- F2: notificacao ao dono (Telegram) ---
TG_BOT_TOKEN = os.getenv("IDT_TG_BOT_TOKEN", "")
TG_CHAT = os.getenv("IDT_TG_CHAT", "")
PUBLIC_URL = os.getenv("ATEND_PUBLIC_URL", "http://100.104.13.42:8138").rstrip("/")
NOTIFY_COOLDOWN = int(os.getenv("ATEND_NOTIFY_COOLDOWN", "120"))
NOTIFY_GROUPS = os.getenv("ATEND_NOTIFY_GROUPS", "0") == "1"
NOTIFY_ENABLED = os.getenv("ATEND_NOTIFY", "1") == "1"

# --- multi-numero: rotulos e KB por sessao ---
def _parse_map(raw):
    out = {}
    for item in (raw or "").split(","):
        if "=" in item:
            k, v = item.split("=", 1)
            k, v = k.strip(), v.strip()
            if k and v:
                out[k] = v
    return out


SESSION_LABELS = _parse_map(os.getenv("ATEND_SESSION_LABELS", ""))

# --- F3: bot IA (LLM propoe, executor deterministico valida) ---
KB_PATH = os.getenv("ATEND_KB", "")                            # KB global (fallback)
KB_MAP = _parse_map(os.getenv("ATEND_KB_MAP", ""))             # session=path,... (multi-numero)
BOT_MODE = os.getenv("ATEND_BOT_MODE", "off").lower()          # off | dry | live
BOT_ALLOWLIST = {x.strip() for x in os.getenv("ATEND_BOT_ALLOWLIST", "").split(",") if x.strip()}


def session_label(session):
    if session in SESSION_LABELS:
        return SESSION_LABELS[session]
    cfg = load_client_config(session)
    if cfg and cfg.get("display"):
        return cfg["display"]
    if session == "idt-lab-wpp":
        return "Miguel · IDT Lab"
    if session == "cli-002-sebastiancom":
        return "SebastianCom Informática"
    return session


def kb_path_for(session):
    cfg = load_client_config(session)
    if cfg and cfg.get("knowledge_base"):
        return cfg["knowledge_base"]
    return KB_MAP.get(session) or KB_PATH


def bot_allowed(session, cid):
    """Allowlist aceita 'cid' (qualquer sessao), 'session:cid' (so aquela) ou '*' (todos)."""
    try:
        with db() as c:
            row = c.execute("SELECT v FROM settings WHERE k='bot_allow_all'").fetchone()
            if row and row["v"] == "1":
                return True
    except Exception:
        pass
    cfg = load_client_config(session)
    if cfg and cfg.get("allowlist"):
        c_allow = cfg.get("allowlist")
        if "*" in c_allow or cid in c_allow:
            return True
    if not BOT_ALLOWLIST:
        return False
    if "*" in BOT_ALLOWLIST:
        return True
    return cid in BOT_ALLOWLIST or f"{session}:{cid}" in BOT_ALLOWLIST
BOT_POLL_SECONDS = int(os.getenv("ATEND_BOT_POLL_SECONDS", "15"))
OMNI_URL = os.getenv("ATEND_OMNI_URL", "http://127.0.0.1:20128/v1").rstrip("/")
OMNI_KEY = os.getenv("ATEND_OMNI_KEY", "")
OMNI_MODEL = os.getenv("ATEND_OMNI_MODEL", "best-chat")
OMNI_FALLBACK = [m.strip() for m in os.getenv("ATEND_OMNI_FALLBACK", "flash,gratis,lfm2-lab:latest").split(",") if m.strip()]
OMNI_MAX_TOKENS = int(os.getenv("ATEND_OMNI_MAX_TOKENS", "900"))
OMNI_TIMEOUT = int(os.getenv("ATEND_OMNI_TIMEOUT", "90"))
BOT_COOLDOWN = int(os.getenv("ATEND_BOT_COOLDOWN", "3"))
BOT_DAILY_CAP = int(os.getenv("ATEND_BOT_DAILY_CAP", "200"))
BOT_QUIET_START = int(os.getenv("ATEND_BOT_QUIET_START", "22"))
BOT_QUIET_END = int(os.getenv("ATEND_BOT_QUIET_END", "8"))
BOT_MAX_REPLY = int(os.getenv("ATEND_BOT_MAX_REPLY", "600"))
BOT_GROUPS = os.getenv("ATEND_BOT_GROUPS", "0") == "1"
BOT_HISTORY = int(os.getenv("ATEND_BOT_HISTORY", "8"))
# --- Thread pool for async OmniRoute calls ---
OMNI_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="omni")
# --- F4: agenda (slots deterministicos a partir da KB) ---
BOOKING_STEP_MIN = int(os.getenv("ATEND_BOOKING_STEP", "60"))
BOOKING_DAYS = int(os.getenv("ATEND_BOOKING_DAYS", "7"))
TZ_OFFSET_H = int(os.getenv("ATEND_TZ_OFFSET", "-3"))          # America/Sao_Paulo

DATA_DIR.mkdir(parents=True, exist_ok=True)
app = Flask(__name__)

# ---------------------------------------------------------------- mapos client
MAPOS = MaposClient(
    base_url=os.getenv("MAPOS_BASE_URL"),
    email=os.getenv("MAPOS_CLIENT_EMAIL"),
    password=os.getenv("MAPOS_CLIENT_PASSWORD"),
    token_file=os.getenv("MAPOS_TOKEN_FILE"),
    timeout=int(os.getenv("MAPOS_REQUEST_TIMEOUT", "30")),
)


# ---------------------------------------------------------------- telegram
def _h(s):
    return (str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def tg_send(text, buttons=None, disable_notification=False):
    """Envia mensagem pelo bot do Telegram. buttons = lista de linhas de botoes."""
    if not TG_BOT_TOKEN or not TG_CHAT:
        return {"ok": False, "error": "telegram nao configurado"}
    payload = {"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML",
               "disable_web_page_preview": True}
    if buttons:
        payload["reply_markup"] = {"inline_keyboard": buttons}
    if disable_notification:
        payload["disable_notification"] = True
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": f"HTTP {e.code}: {e.read().decode()[:200]}"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

# ---------------------------------------------------------------- waha key
_KEY_CACHE = {"v": None}


def waha_api_key():
    if _KEY_CACHE["v"]:
        return _KEY_CACHE["v"]
    k = os.getenv("WAHA_API_KEY")
    if not k:
        try:
            raw = subprocess.check_output(["docker", "inspect", "waha"], text=True)
            env = dict(z.split("=", 1) for z in json.loads(raw)[0]["Config"]["Env"] if "=" in z)
            k = env.get("WAHA_API_KEY", "")
        except Exception:
            k = ""
    _KEY_CACHE["v"] = k
    return k


def waha(method, path, payload=None, timeout=None):
    if timeout is None:
        timeout = WAHA_TIMEOUT
    url = f"{WAHA_URL}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("X-Api-Key", waha_api_key())
    if data:
        req.add_header("Content-Type", "application/json")
    
    # Retry logic with exponential backoff
    max_retries = 3
    base_delay = 1.0
    last_error = None
    
    for attempt in range(max_retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read().decode() or "null"
                return json.loads(body)
        except urllib.error.HTTPError as e:
            # Don't retry on client errors (4xx)
            if 400 <= e.code < 500:
                body = e.read().decode() or "null"
                raise Exception(f"HTTP {e.code}: {body}")
            last_error = e
        except urllib.error.URLError as e:
            last_error = e
        except Exception as e:
            last_error = e
        
        if attempt < max_retries - 1:
            delay = base_delay * (2 ** attempt)
            time.sleep(delay)
    
    raise Exception(f"WAHA {method} {path} failed after {max_retries} attempts: {last_error}")


def waha_send_text(session, cid, text):
    """Envia texto pelo WAHA e retorna o message id.

    O WAHA (engine NOWEB) devolve o id em res['key']['id']; versoes/aliases
    antigos podem devolver res['id'] (str) ou res['id']['id'] (dict). Cobre os 3.
    """
    # Use per-session timeout for sends too
    waha_timeout = int(os.getenv(f"WAHA_TIMEOUT_{session.upper().replace('-', '_')}", str(WAHA_TIMEOUT)))
    res = waha("POST", "/api/sendText", {"session": session, "chatId": cid, "text": text}, timeout=waha_timeout)
    if not isinstance(res, dict):
        return None
    key = res.get("key")
    if isinstance(key, dict) and key.get("id"):
        return key["id"]
    rid = res.get("id")
    if isinstance(rid, str):
        return rid
    if isinstance(rid, dict):
        return rid.get("id")
    return None


def waha_send_buttons(session, cid, text, buttons):
    """Envia texto com botoes interativos pelo WAHA (/api/sendButtons).

    buttons: lista de dicts [{"id": "...", "title": "..."}, ...] (max 3).
    Retorna o message id ou None.
    """
    waha_timeout = int(os.getenv(f"WAHA_TIMEOUT_{session.upper().replace(chr(45), chr(95))}", str(WAHA_TIMEOUT)))
    waha_buttons = []
    for b in (buttons or [])[:3]:
        waha_buttons.append({
            "buttonId": str(b.get("id", b.get("buttonId", ""))),
            "text": str(b.get("title", b.get("buttonText", "")))
        })
    res = waha("POST", "/api/sendButtons", {
        "session": session,
        "chatId": cid,
        "text": text,
        "buttons": waha_buttons,
    }, timeout=waha_timeout)
    if not isinstance(res, dict):
        return None
    key = res.get("key")
    if isinstance(key, dict) and key.get("id"):
        return key["id"]
    rid = res.get("id")
    if isinstance(rid, str):
        return rid
    if isinstance(rid, dict):
        return rid.get("id")
    return None


def waha_send_list(session, cid, text, title, description, rows):
    """Envia lista interativa pelo WAHA (/api/sendList).

    rows: lista de dicts [{"id": "...", "title": "...", "description": "..."}, ...].
    Retorna o message id ou None.
    """
    waha_timeout = int(os.getenv(f"WAHA_TIMEOUT_{session.upper().replace(chr(45), chr(95))}", str(WAHA_TIMEOUT)))
    waha_rows = []
    for r in (rows or [])[:10]:
        waha_rows.append({
            "rowId": str(r.get("id", r.get("rowId", ""))),
            "title": str(r.get("title", "")),
            "description": str(r.get("description", "")),
        })
    res = waha("POST", "/api/sendList", {
        "session": session,
        "chatId": cid,
        "text": text,
        "title": title,
        "description": description,
        "sections": [{"title": title, "rows": waha_rows}],
    }, timeout=waha_timeout)
    if not isinstance(res, dict):
        return None
    key = res.get("key")
    if isinstance(key, dict) and key.get("id"):
        return key["id"]
    rid = res.get("id")
    if isinstance(rid, str):
        return rid
    if isinstance(rid, dict):
        return rid.get("id")
    return None


def waha_download_media(session, message_id):
    """Baixa mídia do WAHA pelo endpoint /api/{session}/media/{messageId}.
    
    Retorna bytes da mídia ou None se falhar.
    """
    try:
        path = f"/api/{session}/media/{message_id}"
        # GET sem payload, timeout maior para download de mídia
        waha_timeout = int(os.getenv(f"WAHA_TIMEOUT_{session.upper().replace('-', '_')}", str(WAHA_TIMEOUT * 3)))
        url = f"{WAHA_URL}{path}"
        req = urllib.request.Request(url, method="GET")
        req.add_header("X-Api-Key", waha_api_key())
        with urllib.request.urlopen(req, timeout=waha_timeout) as r:
            return r.read()
    except Exception as e:
        print(json.dumps({"t": now_iso(), "waha_download_media": "error", "session": session, "message_id": message_id, "err": str(e)}), flush=True)
        return None


# ---------------------------------------------------------------- db
@contextmanager
def db():
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=30000")
    try:
        with c:
            yield c
    finally:
        c.close()


def init_db():
    with db() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS conversations (
              session TEXT NOT NULL,
              id TEXT NOT NULL,
              name TEXT,
              is_group INTEGER DEFAULT 0,
              mode TEXT DEFAULT 'bot',
              last_message_at INTEGER,
              last_message_preview TEXT,
              last_message_id TEXT,
              unread INTEGER DEFAULT 0,
              updated_at TEXT,
              PRIMARY KEY (session, id)
            );
            CREATE TABLE IF NOT EXISTS messages (
              id TEXT PRIMARY KEY,
              conversation_id TEXT NOT NULL,
              session TEXT NOT NULL,
              from_me INTEGER DEFAULT 0,
              sender TEXT,
              body TEXT,
              has_media INTEGER DEFAULT 0,
              media_type TEXT,
              timestamp INTEGER,
              ack INTEGER,
              created_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_msg_conv ON messages(conversation_id, timestamp);
            CREATE TABLE IF NOT EXISTS events (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              received_at TEXT,
              event TEXT,
              session TEXT,
              payload TEXT
            );
            CREATE TABLE IF NOT EXISTS settings (k TEXT PRIMARY KEY, v TEXT);
            CREATE TABLE IF NOT EXISTS os_conversation_map (
              os_id INTEGER NOT NULL,
              session TEXT NOT NULL,
              cid TEXT NOT NULL,
              created_at TEXT,
              PRIMARY KEY (os_id, session, cid)
            );
            CREATE INDEX IF NOT EXISTS idx_os_map_os ON os_conversation_map(os_id);
            CREATE INDEX IF NOT EXISTS idx_os_map_cid ON os_conversation_map(session, cid);
            """
        )
    migrate_db()


def _table_sql(c, name):
    row = c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
    return (row["sql"] or "") if row else ""


def _add_tenant_id_column(c):
    """Adiciona coluna tenant_id às tabelas principais se não existir."""
    changed = False
    for table in ("conversations", "messages", "bookings", "os_conversation_map"):
        cols = {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}
        if "tenant_id" not in cols:
            c.execute(f"ALTER TABLE {table} ADD COLUMN tenant_id TEXT")
            changed = True
    # Atualiza tenant_id baseado na session (mapeia session -> client_name via config)
    # Para dados existentes, tenta inferir do KB_MAP ou usa session como fallback
    if changed:
        c.execute("""
            UPDATE conversations SET tenant_id = session WHERE tenant_id IS NULL
        """)
        c.execute("""
            UPDATE messages SET tenant_id = session WHERE tenant_id IS NULL
        """)
        c.execute("""
            UPDATE bookings SET tenant_id = session WHERE tenant_id IS NULL
        """)
        c.execute("""
            UPDATE os_conversation_map SET tenant_id = session WHERE tenant_id IS NULL
        """)
    return changed


def _rebuild_conversations_composite(c):
    """Migra conversations de PK(id) para PK(session,id). Idempotente."""
    if "PRIMARY KEY (session" in _table_sql(c, "conversations"):
        return False
    c.executescript(
        """
        CREATE TABLE conversations_new (
          session TEXT NOT NULL, id TEXT NOT NULL, name TEXT, is_group INTEGER DEFAULT 0,
          mode TEXT DEFAULT 'bot', last_message_at INTEGER, last_message_preview TEXT,
          last_message_id TEXT, unread INTEGER DEFAULT 0, updated_at TEXT,
          last_notified_at INTEGER DEFAULT 0, muted INTEGER DEFAULT 0,
          bot_last_reply_at INTEGER DEFAULT 0, bot_last_inbound_id TEXT, bot_replies_day TEXT,
          bot_replies_count INTEGER DEFAULT 0, bot_last_reply TEXT, bot_last_error TEXT,
          tenant_id TEXT,
          PRIMARY KEY (session, id)
        );
        INSERT OR IGNORE INTO conversations_new
          (session,id,name,is_group,mode,last_message_at,last_message_preview,last_message_id,
           unread,updated_at,last_notified_at,muted,bot_last_reply_at,bot_last_inbound_id,
           bot_replies_day,bot_replies_count,bot_last_reply,bot_last_error,tenant_id)
        SELECT session,id,name,is_group,mode,last_message_at,last_message_preview,last_message_id,
           unread,updated_at,COALESCE(last_notified_at,0),COALESCE(muted,0),
           COALESCE(bot_last_reply_at,0),bot_last_inbound_id,bot_replies_day,
           COALESCE(bot_replies_count,0),bot_last_reply,bot_last_error,tenant_id
        FROM conversations;
        DROP TABLE conversations;
        ALTER TABLE conversations_new RENAME TO conversations;
        """
    )
    return True


def _rebuild_bookings_session(c):
    """Torna a unicidade do agendamento por sessao: UNIQUE(session,slot,status)."""
    if "UNIQUE(session, slot" in _table_sql(c, "bookings"):
        return False
    c.executescript(
        """
        CREATE TABLE bookings_new (
          id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT NOT NULL, session TEXT,
          name TEXT, phone TEXT, service TEXT, slot TEXT NOT NULL,
          status TEXT DEFAULT 'confirmado', note TEXT, created_at TEXT, updated_at TEXT,
          UNIQUE(session, slot, status)
        );
        INSERT OR IGNORE INTO bookings_new
          (id,conversation_id,session,name,phone,service,slot,status,note,created_at,updated_at)
        SELECT id,conversation_id,session,name,phone,service,slot,status,note,created_at,updated_at
        FROM bookings;
        DROP TABLE bookings;
        ALTER TABLE bookings_new RENAME TO bookings;
        CREATE INDEX IF NOT EXISTS idx_book_slot ON bookings(slot);
        """
    )
    return True


def migrate_db():
    """Adiciona colunas/tabelas novas em bancos ja existentes (F2/F3/F4 + multi-numero)."""
    with db() as c:
        cols = {r["name"] for r in c.execute("PRAGMA table_info(conversations)")}
        for name, ddl in (
            ("last_notified_at", "INTEGER DEFAULT 0"),
            ("muted", "INTEGER DEFAULT 0"),
            ("bot_last_reply_at", "INTEGER DEFAULT 0"),
            ("bot_last_inbound_id", "TEXT"),
            ("bot_replies_day", "TEXT"),
            ("bot_replies_count", "INTEGER DEFAULT 0"),
            ("bot_last_reply", "TEXT"),
            ("bot_last_error", "TEXT"),
        ):
            if name not in cols:
                c.execute(f"ALTER TABLE conversations ADD COLUMN {name} {ddl}")
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS bookings (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              conversation_id TEXT NOT NULL,
              session TEXT,
              name TEXT,
              phone TEXT,
              service TEXT,
              slot TEXT NOT NULL,
              status TEXT DEFAULT 'confirmado',
              note TEXT,
              created_at TEXT,
              updated_at TEXT,
              UNIQUE(session, slot, status)
            );
            CREATE INDEX IF NOT EXISTS idx_book_slot ON bookings(slot);
            CREATE TABLE IF NOT EXISTS os_conversation_map (
              os_id INTEGER NOT NULL,
              session TEXT NOT NULL,
              cid TEXT NOT NULL,
              created_at TEXT,
              PRIMARY KEY (os_id, session, cid)
            );
            CREATE INDEX IF NOT EXISTS idx_os_map_os ON os_conversation_map(os_id);
            CREATE INDEX IF NOT EXISTS idx_os_map_cid ON os_conversation_map(session, cid);
            """
        )
        moved = _rebuild_conversations_composite(c)
        _rebuild_bookings_session(c)
        c.execute("CREATE INDEX IF NOT EXISTS idx_conv_session ON conversations(session, last_message_at)")
    if moved:
        print(json.dumps({"t": now_iso(), "migrate": "conversations -> PK(session,id)"}), flush=True)


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def message_exists(c, mid):
    return c.execute("SELECT 1 FROM messages WHERE id=?", (mid,)).fetchone() is not None


def upsert_message(c, m, session, conv_id):
    # Get tenant_id from client config
    tenant_id = ""
    cfg = load_client_config(session)
    if cfg:
        tenant_id = cfg.get("nome", session)
    mid = str(m.get("id") or "")
    if not mid:
        return False
    media = m.get("media") or {}
    body = m.get("body")
    if body is None and media:
        body = media.get("filename") or media.get("mimetype") or "[midia]"
    c.execute(
        """INSERT INTO messages (id, conversation_id, session, from_me, sender, body,
                                 has_media, media_type, timestamp, ack, created_at, tenant_id)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET
             body=excluded.body, ack=excluded.ack, has_media=excluded.has_media,
             media_type=excluded.media_type""",
        (
            mid, conv_id, session, 1 if m.get("fromMe") else 0,
            str(m.get("from") or ""), body, 1 if m.get("hasMedia") else 0,
            # Clamp timestamp to now + 60s to handle future-dated timestamps from WAHA
            media.get("mimetype"), min(int(m.get("timestamp") or 0), int(time.time()) + 60),
            int(m.get("ack") or 0), now_iso(), tenant_id,
        ),
    )
    return True



def upsert_conversation(c, chat, session):
    # Get tenant_id from client config
    tenant_id = ""
    cfg = load_client_config(session)
    if cfg:
        tenant_id = cfg.get("nome", session)
    cid = str(chat.get("id") or "")
    if not cid:
        return None
    last = chat.get("lastMessage") or {}
    preview = last.get("body")
    if preview is None and last.get("hasMedia"):
        preview = "[midia]"
    is_group = 1 if ("@g.us" in cid) else 0
    row = c.execute("SELECT mode FROM conversations WHERE session=? AND id=?",
                    (session, cid)).fetchone()
    mode = row["mode"] if row else "bot"
    c.execute(
        """INSERT INTO conversations (session, id, name, is_group, mode, last_message_at,
                                      last_message_preview, last_message_id, updated_at, tenant_id)
           VALUES (?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(session, id) DO UPDATE SET
             name=COALESCE(excluded.name, conversations.name),
             last_message_at=COALESCE(excluded.last_message_at, conversations.last_message_at),
             last_message_preview=COALESCE(excluded.last_message_preview, conversations.last_message_preview),
             last_message_id=COALESCE(excluded.last_message_id, conversations.last_message_id),
             updated_at=excluded.updated_at, tenant_id=excluded.tenant_id""",
        (
            session, cid, chat.get("name"), is_group, mode,
            min(int(last.get("timestamp") or 0), int(time.time()) + 60) or None,
            (preview or "")[:500] or None, str(last.get("id") or "") or None, now_iso(),
            tenant_id,
        ),
    )
    return cid




# ---------------------------------------------------------------- Human intent detection
import unicodedata

def _normalize(text: str) -> str:
    """Normalize text: lowercase + remove accents."""
    if not text:
        return ""
    return "".join(c for c in unicodedata.normalize("NFD", text.lower()) if unicodedata.category(c) != "Mn")

def detect_human_intent(text: str, triggers: list) -> bool:
    """Detect if user wants to talk to a human agent."""
    if not text:
        return False
    text_norm = _normalize(text)
    for trigger in triggers:
        if _normalize(trigger) in text_norm:
            return True
    return False


# ---------------------------------------------------------------- Dynamic few-shot loader
def load_fewshot(client_dir: Path) -> list:
    """Load few-shot examples from client's prompts/fewshot.json"""
    fewshot_path = client_dir / "prompts" / "fewshot.json"
    if fewshot_path.exists():
        try:
            with open(fewshot_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data.get("exemplos", [])
        except Exception:
            pass
    return DEFAULT_FEWSHOT


DEFAULT_FEWSHOT = [
    {"role": "user", "content": "Oi, tudo bem?"},
    {"role": "assistant", "content": "Oi! Tudo ótimo. Em que posso ajudar?"},
    {"role": "user", "content": "Quanto custa montar um PC?"},
    {"role": "assistant", "content": "Depende das peças. Me passa seu orçamento e quais jogos quer rodar que te oriento."},
    {"role": "user", "content": "Tenho Ryzen 7 e RTX 4050"},
    {"role": "assistant", "content": "Show! Qual Ryzen 7 exatamente (5700X, 5800X3D, 7700X)? A 4050 é desktop 6GB/8GB ou notebook? E o que já tem: placa-mãe, RAM, SSD, fonte, gabinete, cooler?"},
    {"role": "user", "content": "Quero falar com atendente"},
    {"role": "assistant", "content": "Entendido. Transferindo para um atendente humano..."},
]



def _notify_handoff(session, cid, tenant_id, inbound_text, cfg):
    """Notify owner via Telegram with inline buttons for handoff (Assumir / Devolver)."""
    owner_number = cfg.get("owner_number", "5521988743980")
    owner_name = cfg.get("owner_name", "Dono")
    display = cfg.get("display", session)

    txt = (f"🤝 <b>HANDOFF SOLICITADO</b>\n"
           f"📱 <i>{display}</i>\n"
           f"👤 {owner_name} ({owner_number})\n"
           f"💬 Cliente: {cid}\n"
           f'📝 "{inbound_text[:200]}"\n'
           f"🏷️ Tenant: {tenant_id}")

    qcid = quote(cid, safe="")
    qs = quote(session, safe="")
    tok = f"?token={APP_TOKEN}&s={qs}"
    buttons = [
        [{"text": "✓ Assumir (pausa o bot)", "url": f"{PUBLIC_URL}/assumir/{qcid}{tok}"},
         {"text": "✕ Devolver ao bot", "url": f"{PUBLIC_URL}/bot/{qcid}{tok}"}],
        [{"text": "Abrir conversa", "url": f"{PUBLIC_URL}/{tok}&conv={qcid}"}],
    ]
    res = tg_send(txt, buttons)
    print(json.dumps({"t": now_iso(), "notify": "handoff", "cid": cid, "session": session,
                      "ok": res.get("ok"), "message_id": (res.get("result") or {}).get("message_id")}))
    return res.get("ok")

# ---------------------------------------------------------------- poller
STATE = {"last_cycle": None, "last_ok": None, "last_error": None, "cycles": 0,
         "new_msgs": 0, "notified": 0}


def maybe_notify(session, cid, d):
    """Notifica o dono no Telegram (1x por conversa por cooldown). Retorna True se enviou."""
    with db() as c:
        row = c.execute("SELECT * FROM conversations WHERE session=? AND id=?",
                        (session, cid)).fetchone()
        if not row:
            return False
        if row["mode"] == "humano":          # dono ja assumiu -> silencio
            return False
        if row["is_group"] and not NOTIFY_GROUPS:
            return False
        if row["muted"]:
            return False
        now = int(time.time())
        if (now - int(row["last_notified_at"] or 0)) < NOTIFY_COOLDOWN:
            return False
        c.execute("UPDATE conversations SET last_notified_at=? WHERE session=? AND id=?",
                  (now, session, cid))
        name = row["name"] or cid
        unread = int(row["unread"] or 0)
        is_group = bool(row["is_group"])
    qcid = quote(cid, safe="")
    qs = quote(session, safe="")
    kind = "Grupo" if is_group else "Nova mensagem"
    label = session_label(session)
    txt = (f"🔔 <b>{kind}</b> — {_h(name)}\n"
           f"📱 <i>{_h(label)}</i>\n"
           f"{_h((d.get('last') or '')[:400])}\n"
           f"<i>{unread} não lida(s)</i>")
    tok = f"?token={APP_TOKEN}&s={qs}"
    buttons = [
        [{"text": "✓ Assumir (pausa o bot)", "url": f"{PUBLIC_URL}/assumir/{qcid}{tok}"},
         {"text": "✕ Deixar no bot", "url": f"{PUBLIC_URL}/bot/{qcid}{tok}"}],
        [{"text": "Abrir conversa", "url": f"{PUBLIC_URL}/{tok}&conv={qcid}"}],
    ]
    res = tg_send(txt, buttons)
    print(json.dumps({"t": now_iso(), "notify": "sent" if res.get("ok") else "error", "cid": cid,
                      "message_id": (res.get("result") or {}).get("message_id"),
                      "err": res.get("error")}), flush=True)
    return bool(res.get("ok"))


def poll_once():
    total_new = 0
    inbound = {}
    warmup = STATE["cycles"] == 0          # 1o ciclo nao notifica (evita rajada)
    with db() as c:
        for session in SESSIONS:
            # Per-session limits and timeout
            chat_limit = _session_limit(session, "CHAT_LIMIT", CHAT_LIMIT)
            msg_limit = _session_limit(session, "MSG_LIMIT", MSG_LIMIT)
            waha_timeout = int(os.getenv(f"WAHA_TIMEOUT_{session.upper().replace('-', '_')}", str(WAHA_TIMEOUT)))
            
            # Circuit breaker: track consecutive failures
            failure_key = f"waha_failures:{session}"
            failures = int(STATE.get(failure_key, 0))
            if failures >= 5:
                STATE["last_error"] = f"{session}: circuit breaker open ({failures} failures)"
                print(json.dumps({"t": now_iso(), "poller": "circuit_open", "session": session, "failures": failures}), flush=True)
                continue
            
            try:
                chats = waha("GET", f"/api/{session}/chats/overview?limit={chat_limit}", timeout=waha_timeout)
                # Reset failure count on success
                STATE[failure_key] = 0
            except Exception as e:
                # Increment failure count
                STATE[failure_key] = failures + 1
                STATE["last_error"] = f"{session}: {type(e).__name__}: {e}"
                print(json.dumps({"t": now_iso(), "poller": "error", "session": session, "failures": failures + 1, "err": str(e)}), flush=True)
                continue
            if not isinstance(chats, list):
                continue
            for chat in chats:
                cid = str(chat.get("id") or "")
                if not cid:
                    continue
                last_id = str((chat.get("lastMessage") or {}).get("id") or "")
                unread_count = int(chat.get("unreadCount") or 0)
                prev = c.execute(
                    "SELECT last_message_id, unread FROM conversations WHERE session=? AND id=?",
                    (session, cid),
                ).fetchone()
                
                # Atualiza registro de conversa no banco
                upsert_conversation(c, chat, session)
                
                # Busca mensagens completas se houver novidade, nova conversa ou mensagens não lidas
                needs = True and not warmup
                if prev and prev["last_message_id"] and last_id:
                    if message_exists(c, last_id) and unread_count == 0:
                        needs = False
                if needs:
                    try:
                        msgs = waha("GET", f"/api/{session}/chats/{cid}/messages?limit={msg_limit}&downloadMedia=false", timeout=waha_timeout)
                    except Exception:
                        msgs = []
                    if isinstance(msgs, list):
                        for m in msgs:
                            mid = str(m.get("id") or "")
                            if not mid:
                                continue
                            existed = message_exists(c, mid)
                            upsert_message(c, m, session, cid)
                            if existed:
                                continue
                            total_new += 1
                            if not m.get("fromMe"):          # mensagem RECEBIDA
                                d = inbound.setdefault(
                                    (session, cid),
                                    {"session": session, "cid": cid, "count": 0, "last": "", "last_ts": 0})
                                d["count"] += 1
                                # Clamp timestamp to handle future-dated timestamps from WAHA
                                ts = min(int(m.get("timestamp") or 0), int(time.time()) + 60)
                                if ts >= d["last_ts"]:
                                    d["last_ts"] = ts
                                    body = m.get("body")
                                    if not body and m.get("hasMedia"):
                                        body = "[midia]"
                                    d["last"] = body or ""
                else:
                    if not warmup and chat.get("lastMessage"):
                        upsert_message(c, chat.get("lastMessage"), session, cid)
            c.execute("INSERT INTO settings (k,v) VALUES (?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                      (f"cursor:{session}", now_iso()))
        for (session, cid), d in inbound.items():
            c.execute("UPDATE conversations SET unread=COALESCE(unread,0)+? WHERE session=? AND id=?",
                      (d["count"], session, cid))
    STATE["cycles"] += 1
    STATE["new_msgs"] += total_new
    STATE["last_cycle"] = now_iso()
    STATE["last_ok"] = now_iso()
    if (not warmup) and inbound and NOTIFY_ENABLED:
        for (session, cid), d in inbound.items():
            try:
                if maybe_notify(session, cid, d):
                    STATE["notified"] += 1
            except Exception as e:
                STATE["last_error"] = f"notify: {type(e).__name__}: {e}"
    return total_new


def poller_loop():
    while True:
        try:
            n = poll_once()
            print(json.dumps({"t": now_iso(), "poller": "ok", "new": n, "cycles": STATE["cycles"]}), flush=True)
        except Exception as e:
            STATE["last_error"] = f"{type(e).__name__}: {e}"
            print(json.dumps({"t": now_iso(), "poller": "error", "err": STATE["last_error"]}), flush=True)
        time.sleep(POLL_SECONDS)


# ================================================================ F3/F4: bot IA + agenda
# Padrao ARBITER: o LLM PROPÕE a resposta; o executor deterministico VALIDA
# (precos/promessas/horarios contra a KB) antes de enviar. Falha de validacao
# nunca vira envio: vira handoff para o dono.
BOT_STATE = {"last_cycle": None, "cycles": 0, "replies": 0, "dry": 0,
             "handoff": 0, "errors": 0, "last_error": None, "last_reply": None}


def _local_now():
    """Hora local do negocio (offset fixo; America/Sao_Paulo nao tem mais DST)."""
    return datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=TZ_OFFSET_H)


def _strip_accents(s):
    return "".join(ch for ch in unicodedata.normalize("NFD", s or "")
                   if unicodedata.category(ch) != "Mn")


_KB_CACHE = {}


def load_kb(session=None):
    """KB da sessao (multi-numero) com fallback para a KB global. Cache por caminho."""
    path = kb_path_for(session) if session else KB_PATH
    if not path:
        return {}

    try:
        mt = os.path.getmtime(path)
    except OSError:
        return {}

    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


# --- SSE: clientes conectados para eventos em tempo real ---
SSE_CLIENTS = set()
SSE_LOCK = threading.Lock()

def _emit_bot_event(session, event_type, payload):
    """Grava evento na tabela events e broadcast para clientes SSE conectados."""
    try:
        with db() as c:
            c.execute(
                "INSERT INTO events (received_at, event, session, payload) VALUES (?,?,?,?)",
                (now_iso(), event_type, session, json.dumps(payload, ensure_ascii=False)[:200000]),
            )
    except Exception as e:
        pass

    evt = json.dumps({"session": session, "event": event_type, "payload": payload}, ensure_ascii=False)
    with SSE_LOCK:
        dead = []
        for q in SSE_CLIENTS:
            try:
                q.put_nowait(evt)
            except Exception:
                dead.append(q)
        for q in dead:
            SSE_CLIENTS.discard(q)


def build_system_prompt(kb, slot_labels=None, client_dir=None):
    neg = kb.get("negocio") or {}
    hor = kb.get("horarios") or {}
    servs = kb.get("servicos") or []
    faq = kb.get("faq") or []
    parts = [f"Voce e o atendente automatico de {neg.get('nome', 'o negocio')} no WhatsApp."]
    if neg.get("descricao"):
        parts.append(neg["descricao"])
    if hor:
        parts.append("Horario: " + "; ".join(f"{k}: {v}" for k, v in hor.items()))
    if servs:
        parts.append("Servicos: " + "; ".join(f"{s.get('nome')}" for s in servs if s.get("nome")))
    if kb.get("marcas"):
        parts.append("Marcas: " + ", ".join(kb["marcas"]))
    # FAQ resumido: apenas 3-5 essenciais para nao inflar prompt
    if faq:
        parts.append("FAQs chave:")
        parts.extend(f"- {q.get('pergunta')} -> {q.get('resposta')}" for q in faq[:5])
    if kb.get("tom"):
        parts.append("Tom: " + str(kb["tom"]))
    
    # Few-shot dinâmico do cliente
    fewshot_examples = load_fewshot(client_dir) if client_dir else DEFAULT_FEWSHOT
    if fewshot_examples:
        parts.append("EXEMPLOS DE RESPOSTA (siga exatamente este estilo):")
        for ex in fewshot_examples:
            role = ex.get("role", "")
            content = ex.get("content", "")
            if role == "user":
                parts.append(f"Usuario: {content}")
            elif role == "assistant":
                parts.append(f"Bot: {content}")
    
    rules = [
        "Responda SOMENTE com base no acima. Nao invente precos, prazos, promocoes.",
        "Se nao souber, responda exatamente [[HANDOFF]] e nada mais.",
        "Nao de diagnostico definitivo sem ver equipamento; nao prometa prazo exato.",
        "Risco (bateria inchada, cheiro queimado, liquido): diga DESLIGUE e traga a loja.",
        "SEJA BREVE: 2-3 frases maximo. Tom WhatsApp natural. Nao use 'Ola, como posso ajudar?'.",
        "Nao repita telefone/email/link - o cliente ja esta no WhatsApp da loja.",
    ]
    for r in (kb.get("restricoes") or []):
        rules.append(str(r))
    if slot_labels:
        rules.append("So ofereca horario se PEDIR agendar. Livres: " + ", ".join(slot_labels[:6]) + ". Peca escolha.")
    parts.append("Regras:")
    parts.extend("- " + r for r in rules)
    return "\n".join(p for p in parts if p)


def omni_chat(messages, model=None, max_tokens=None):
    """Chama o OmniRoute com fallback ordenado. Retorna {ok, model, content} ou {ok:False,error}."""
    primary = model or OMNI_MODEL
    models = [primary] + [m for m in OMNI_FALLBACK if m and m != primary]
    last_err = None
    
    def _call_model(m):
        payload = {"model": m, "messages": messages,
                   "max_tokens": max_tokens or OMNI_MAX_TOKENS, "temperature": 0.4}
        req = urllib.request.Request(OMNI_URL + "/chat/completions",
                                     data=json.dumps(payload).encode(), method="POST")
        req.add_header("Content-Type", "application/json")
        if OMNI_KEY:
            req.add_header("Authorization", "Bearer " + OMNI_KEY)
        with urllib.request.urlopen(req, timeout=OMNI_TIMEOUT) as r:
            d = json.loads(r.read().decode() or "{}")
        choice = (d.get("choices") or [{}])[0]
        content = ((choice.get("message") or {}).get("content") or "").strip()
        if content and choice.get("finish_reason") != "length":
            return {"ok": True, "model": d.get("model") or m, "content": content}
        if content:
            raise Exception(f"{m}: resposta truncada")
        raise Exception(f"{m}: resposta vazia")
    
    for m in models:
        try:
            future = OMNI_EXECUTOR.submit(_call_model, m)
            # Use a slightly shorter timeout to allow for fallback
            res = future.result(timeout=OMNI_TIMEOUT - 2)
            return res
        except FuturesTimeoutError:
            last_err = f"{m}: timeout ({OMNI_TIMEOUT}s)"
        except Exception as e:
            last_err = f"{m}: {type(e).__name__}: {e}"
    return {"ok": False, "error": last_err or "sem modelo"}


PRICE_RE = re.compile(r"R\$\s?\d[\d.,]*|\b\d[\d.,]*\s*(?:reais|conto|pila)\b", re.I)
PROMISE_RE = re.compile(r"\b(100%|garantido|garantia total|melhor preco do mercado|de graca|gratis|sem risco)\b", re.I)


def _kb_text(kb):
    return _strip_accents(json.dumps(kb, ensure_ascii=False)).lower()


def validate_reply(reply, kb, inbound_text=None, session=None):
    """Valida a proposta do LLM contra a KB. Retorna (ok, motivo)."""
    r = (reply or "").strip()
    if not r:
        return False, "resposta vazia"
    if "[[HANDOFF]]" in r.upper():
        return False, "handoff solicitado"
    if len(r) > BOT_MAX_REPLY:
        return False, "resposta longa demais"
    kbtext = _kb_text(kb)
    for m in PRICE_RE.findall(r):
        num = re.sub(r"[^\d]", "", m)
        if num and num not in kbtext:
            return False, f"preco nao autorizado: {m.strip()}"
    for m in PROMISE_RE.findall(r):
        if _strip_accents(m).lower() not in kbtext:
            return False, f"promessa nao autorizada: {m}"
    # Validacao FAQ mais flexivel: threshold 30% (era 50%), permite parafrasear
    if inbound_text:
        faq = kb.get("faq") or []
        inbound_norm = _strip_accents(inbound_text or "").lower()
        for q in faq:
            p = _strip_accents(q.get("pergunta") or "").lower()
            # Match simples: se a pergunta do FAQ está contida na mensagem do usuário
            if p in inbound_norm or inbound_norm in p:
                # Verifica se a resposta do LLM é compatível com a do FAQ
                faq_resp = _strip_accents(q.get("resposta") or "").lower()
                reply_norm = _strip_accents(r).lower()
                # Check overlap: palavras-chave da resposta do FAQ devem aparecer na resposta do LLM
                faq_words = set(w for w in faq_resp.split() if len(w) > 3)
                reply_words = set(w for w in reply_norm.split() if len(w) > 3)
                if faq_words and len(faq_words & reply_words) < max(1, len(faq_words) // 3):
                    return False, f"resposta diverge do FAQ: {q.get('pergunta')}"
    # Enforce brevity for sebastiancom: hard truncate to ~400 chars if longer
    if session == "cli-002-sebastiancom" and len(r) > 400:
        # Truncate at last sentence boundary before 400 chars
        truncated = r[:400]
        last_period = max(truncated.rfind("."), truncated.rfind("!"), truncated.rfind("?"))
        if last_period > 100:  # Only truncate if we have a reasonable sentence
            return False, f"resposta longa demais para sebastiancom (truncado em {last_period+1} chars)"
    return True, "ok"


# ---------------------------------------------------------------- F4: agenda
_WD = {"segunda": 0, "terca": 1, "quarta": 2, "quinta": 3, "sexta": 4, "sabado": 5, "domingo": 6}
_DOW = ["seg", "ter", "qua", "qui", "sex", "sab", "dom"]


def _parse_interval(v):
    m = re.findall(r"(\d{1,2})[:h](\d{2})", str(v or ""))
    if len(m) >= 2:
        a, b = m[0], m[1]
        return int(a[0]) * 60 + int(a[1]), int(b[0]) * 60 + int(b[1])
    return None


def kb_open_intervals(kb):
    out = {}
    for k, v in (kb.get("horarios") or {}).items():
        kk = _strip_accents(str(k)).lower()
        iv = _parse_interval(v)
        if not iv:
            continue
        days = set()
        if "segunda" in kk and "sexta" in kk:
            days = {0, 1, 2, 3, 4}
        elif "segunda" in kk and "sabado" in kk:
            days = {0, 1, 2, 3, 4, 5}
        else:
            for name, idx in _WD.items():
                if name in kk:
                    days.add(idx)
        for d in days:
            out[d] = iv
    return out


def slot_label(iso):
    dt = datetime.strptime(iso, "%Y-%m-%dT%H:%M")
    return f"{_DOW[dt.weekday()]} {dt.strftime('%d/%m')} {dt.strftime('%H:%M')}"


def free_slots(kb, session=None, days=None, step=None, limit=None):
    days = BOOKING_DAYS if days is None else days
    step = BOOKING_STEP_MIN if step is None else step
    iv = kb_open_intervals(kb)
    now = _local_now()
    with db() as c:
        if session:
            booked = {r["slot"] for r in c.execute(
                "SELECT slot FROM bookings WHERE status='confirmado' AND session=?", (session,))}
        else:
            booked = {r["slot"] for r in c.execute(
                "SELECT slot FROM bookings WHERE status='confirmado'")}
    out = []
    for d in range(0, days + 1):
        day = now.date() + timedelta(days=d)
        wd = day.weekday()
        if wd not in iv:
            continue
        ini, fim = iv[wd]
        t = ini
        while t + step <= fim:
            hh, mm = divmod(t, 60)
            dt = datetime(day.year, day.month, day.day, hh, mm)
            if dt > now + timedelta(hours=1):
                iso = dt.strftime("%Y-%m-%dT%H:%M")
                if iso not in booked:
                    out.append(iso)
            t += step
    return out[:limit] if limit else out


INTENT_RE = re.compile(
    r"\b(agendar|agenda|agendamento|marcar|horario|horarios|disponivel|disponiveis|"
    r"pode ser|que horas|tem vaga|encaixe|quero (?:levar|trazer|agendar|um horario))\b", re.I)

OS_INTENT_RE = re.compile(
    r"\b(abrir\s+os|nova\s+os|nova\s+ordem|criar\s+os|abrir\s+ordem|or[cç]amento|consertar|reparar|"
    r"quebrad[ao]|defeituos[ao]|n[aã]o\s+liga|n[aã]o\s+funciona|problema|defeito|manuten[cç][aã]o|"
    r"tela\s+quebrad[ao]|bateria|carregador|microfone|alto-falante|c[aâ]mera)\b", re.I)


def detect_booking_intent(text):
    return bool(INTENT_RE.search(_strip_accents(text or "").lower()))


def detect_os_intent(text):
    """Detecta intenção de criar uma OS (ordem de serviço)."""
    return bool(OS_INTENT_RE.search(_strip_accents(text or "").lower()))


def parse_slot_choice(text, slots):
    """Mapeia escolha em linguagem natural para um slot livre. Deterministico."""
    t = _strip_accents(text or "").lower()
    now = _local_now()
    target = None
    if "amanha" in t:
        target = (now + timedelta(days=1)).date()
    elif "hoje" in t:
        target = now.date()
    m = re.search(r"\b(\d{1,2})/(\d{1,2})\b", t)
    if m:
        try:
            target = now.date().replace(month=int(m.group(2)), day=int(m.group(1)))
            if target < now.date():
                target = target.replace(year=target.year + 1)
        except ValueError:
            target = None
    if target is None:
        m = re.search(r"\bdia\s+(\d{1,2})\b", t)
        if m:
            day = int(m.group(1))
            try:
                target = now.date().replace(day=day)
                if target < now.date():
                    nm = (now.month % 12) + 1
                    ny = now.year + (1 if now.month == 12 else 0)
                    target = now.date().replace(year=ny, month=nm, day=day)
            except ValueError:
                target = None
    if not target:
        for name, idx in _WD.items():
            if name in t:
                delta = (idx - now.weekday()) % 7 or 7
                target = now.date() + timedelta(days=delta)
                break
    hour = minute = None
    if "meio dia" in t or "meio-dia" in t:
        hour, minute = 12, 0
    m = re.search(r"\b(\d{1,2}):(\d{2})\b", t) or re.search(r"\b(\d{1,2})h(\d{2})\b", t)
    if m:
        hour, minute = int(m.group(1)), int(m.group(2))
    elif hour is None:
        m = re.search(r"\b(?:as |a |por |pelas |para as )?(\d{1,2})\s*(?:h|horas|hs)\b", t)
        if m:
            hour, minute = int(m.group(1)), 0
    if target is None and hour is None:
        return None
    for iso in slots:
        dt = datetime.strptime(iso, "%Y-%m-%dT%H:%M")
        if target and dt.date() != target:
            continue
        if hour is not None:
            if dt.hour != hour:
                continue
            if minute and abs(dt.minute - minute) > 30:
                continue
        return iso
    return None


def extract_os_info_from_text(text, kb):
    """Extrai informações para criar OS a partir do texto do cliente."""
    if not text:
        return None
    t = _strip_accents(text or "").lower()
    
    # Tenta extrair produto/dispositivo do texto ou da KB
    descricao_produto = "Dispositivo não especificado"
    defeito = text.strip()
    observacoes = ""
    
    # Padrões comuns de produtos
    device_patterns = [
        (r"\b(iPhone\s*\d+\w*)\b", "iPhone"),
        (r"\b(iPad\s*\w*)\b", "iPad"),
        (r"\b(MacBook\s*\w*)\b", "MacBook"),
        (r"\b(Samsung\s*\w*)\b", "Samsung"),
        (r"\b(Xiaomi\s*\w*)\b", "Xiaomi"),
        (r"\b(Motorola\s*\w*)\b", "Motorola"),
        (r"\b(notebook|notebbok|note\s*book)\b", "Notebook"),
        (r"\b(celular|smartphone|telefone)\b", "Smartphone"),
        (r"\b(tablet)\b", "Tablet"),
        (r"\b(computador|pc|desktop)\b", "Computador"),
        (r"\b(fone|headphone|headset|airpods)\b", "Fone de ouvido"),
        (r"\b(carregador|cabo)\b", "Carregador"),
    ]
    
    for pattern, label in device_patterns:
        if re.search(pattern, t):
            descricao_produto = label
            break
    
    # Se tiver info de produtos na KB, tenta usar
    if kb and kb.get("produtos"):
        for prod in kb["produtos"]:
            nome = prod.get("nome", "").lower()
            if nome and nome in t:
                descricao_produto = prod["nome"]
                break
    
    # Extrai defeito específico se mencionado
    defeito_keywords = {
        "tela quebrada": "Tela quebrada/trincada",
        "tela trincada": "Tela trincada",
        "nao liga": "Não liga",
        "não liga": "Não liga",
        "nao carrega": "Não carrega",
        "não carrega": "Não carrega",
        "bateria": "Problema na bateria",
        "microfone": "Problema no microfone",
        "audio": "Problema de áudio",
        "som": "Problema de áudio",
        "camera": "Problema na câmera",
        "câmera": "Problema na câmera",
        "wifi": "Problema no Wi-Fi",
        "wi-fi": "Problema no Wi-Fi",
        "bluetooth": "Problema no Bluetooth",
        "botao": "Botão quebrado",
        "botão": "Botão quebrado",
        "touch": "Touch não funciona",
        "lento": "Lentidão/travamento",
        "trava": "Travamento",
        "esquenta": "Superaquecimento",
        "quente": "Superaquecimento",
    }
    
    for keyword, desc in defeito_keywords.items():
        if keyword in t:
            defeito = desc
            break
    
    return {
        "descricao_produto": descricao_produto,
        "defeito": defeito,
        "observacoes": observacoes
    }


def create_booking(session, conv_id, slot, service=None, note=None):
    with db() as c:
        row = c.execute("SELECT * FROM conversations WHERE session=? AND id=?",
                        (session, conv_id)).fetchone()
        if not row:
            return {"ok": False, "error": "conversa nao encontrada"}
        if c.execute("SELECT 1 FROM bookings WHERE slot=? AND status='confirmado' AND session=?",
                     (slot, session)).fetchone():
            return {"ok": False, "error": "horario indisponivel"}
        cur = c.execute(
            """INSERT INTO bookings (conversation_id, session, tenant_id, name, phone, service, slot,
                                     status, note, created_at, updated_at)
               VALUES (?,?,?,?,?,?, 'confirmado', ?, ?, ?)""",
            (conv_id, session, session, row["name"], conv_id.split("@")[0], service, slot,
             note, now_iso(), now_iso()))
        bid = cur.lastrowid
    return {"ok": True, "id": bid, "slot": slot, "label": slot_label(slot)}


# ---------------------------------------------------------------- F3: loop do bot
def _bot_effective_mode():
    with db() as c:
        rows = {r["k"]: r["v"] for r in c.execute(
            "SELECT k,v FROM settings WHERE k IN ('bot_enabled','bot_mode')")}
    if rows.get("bot_enabled") == "0":
        return "off"
    return (rows.get("bot_mode") or BOT_MODE).lower()


def _quiet_now():
    h = _local_now().hour
    if BOT_QUIET_START == BOT_QUIET_END:
        return False
    if BOT_QUIET_START < BOT_QUIET_END:
        return BOT_QUIET_START <= h < BOT_QUIET_END
    return h >= BOT_QUIET_START or h < BOT_QUIET_END

def match_faq(kb, text):
    """Match user message to FAQ entry. Returns FAQ answer if match, else None."""
    if not text or not kb:
        return None
    faq = kb.get("faq") or []
    if not faq:
        return None
    
    import re
    # Normalize text
    t = text.lower().strip()
    # Remove punctuation for matching
    t_clean = re.sub(r'[?!.\,;:\-]', '', t)
    
    for q in faq:
        pergunta = (q.get("pergunta") or "").lower().strip()
        pergunta_clean = re.sub(r'[?!.\,;:\-]', '', pergunta)
        resposta = q.get("resposta") or ""
        
        # Exact match or containment (both ways)
        if pergunta_clean == t_clean or pergunta_clean in t_clean or t_clean in pergunta_clean:
            return resposta
        
        # Word overlap matching for partial matches
        p_words = set(w for w in pergunta_clean.split() if len(w) > 3)
        t_words = set(w for w in t_clean.split() if len(w) > 3)
        if p_words and t_words:
            overlap = len(p_words & t_words)
            if overlap >= max(1, len(p_words) // 2):  # At least 50% of FAQ keywords present
                return resposta
    
    return None


def bot_process_conversation(session, cid, mode, send=True, advance=True):
    """Monta prompt -> LLM propoe -> executor valida -> (envia | handoff)."""
    kb = load_kb(session)
    if not kb:
        return {"cid": cid, "session": session, "action": "skip", "reason": "KB ausente"}
    
    # Load client config for tenant-aware processing
    cfg = load_client_config(session)
    tenant_id = cfg.get("nome", session) if cfg else session
    client_dir = Path(cfg["_client_dir"]) if cfg and cfg.get("_client_dir") else None
    
    with db() as c:
        conv = c.execute("SELECT * FROM conversations WHERE session=? AND id=?", (session, cid)).fetchone()
        if not conv:
            return {"cid": cid, "session": session, "action": "skip", "reason": "sem conversa"}
        rows = c.execute(
            "SELECT * FROM messages WHERE conversation_id=? AND session=? ORDER BY timestamp DESC LIMIT ?",
            (cid, session, BOT_HISTORY)).fetchall()
    msgs = list(reversed(rows))
    last_in = next((m for m in reversed(msgs) if not m["from_me"]), None)
    if not last_in:
        return {"cid": cid, "action": "skip", "reason": "sem inbound"}
    inbound_text = last_in["body"] or ""

    # Defense-in-depth safety checks
    if conv["mode"] != "bot":
        return {"cid": cid, "session": session, "action": "skip", "reason": "modo_humano"}

    if conv["is_group"] and not BOT_GROUPS:
        return {"cid": cid, "session": session, "action": "skip", "reason": "grupos_desativados"}

    now = int(time.time())
    msg_age = now - int(last_in["timestamp"] or 0)
    if msg_age > 180:
        if advance:
            with db() as c:
                c.execute("UPDATE conversations SET bot_last_inbound_id=? WHERE session=? AND id=?",
                          (last_in["id"], session, cid))
        return {"cid": cid, "session": session, "action": "skip", "reason": f"stale_inbound_{msg_age}s"}

    if send:
        effective_mode = _bot_effective_mode()
        if effective_mode == "off":
            return {"cid": cid, "session": session, "action": "skip", "reason": "bot_desligado"}
        if not bot_allowed(session, cid):
            return {"cid": cid, "session": session, "action": "skip", "reason": "fora_da_allowlist"}
    
    # HUMAN INTENT DETECTION - Check BEFORE LLM call
    handoff_triggers = cfg.get("handoff_triggers", [
        "atendente", "humano", "pessoa real", "reclamar", "nao resolve",
        "urgente", "falar com atendente", "quero humano", "transferir"
    ]) if cfg else [
        "atendente", "humano", "pessoa real", "reclamar", "nao resolve",
        "urgente", "falar com atendente", "quero humano", "transferir"
    ]
    if detect_human_intent(inbound_text, handoff_triggers):
        # Immediate handoff
        with db() as c:
            c.execute(
                "UPDATE conversations SET mode='humano', updated_at=? WHERE session=? AND id=?",
                (now_iso(), session, cid)
            )
        # Notify owner via Telegram with inline buttons
        if NOTIFY_ENABLED:
            _notify_handoff(session, cid, tenant_id, inbound_text, cfg)
        _emit_bot_event(session, "handoff_pending", {"cid": cid, "inbound": inbound_text[:200], "tenant_id": tenant_id})
        return {"cid": cid, "session": session, "action": "handoff", "reason": "human_intent_detected", "tenant_id": tenant_id}
    
    # FAQ-FIRST MATCHING: Check for exact FAQ match before LLM call
    # This avoids hallucinations and ensures short deterministic responses for sebastiancom
    faq_match = match_faq(kb, inbound_text)
    if faq_match:
        reply = faq_match
        sent_mid = None
        if send:
            try:
                sent_mid = waha_send_text(conv["session"], cid, reply)
                _emit_bot_event(session, "bot_replied", {"cid": cid, "reply": reply, "sent_id": sent_mid, "source": "faq"})
            except Exception as e:
                return {"cid": cid, "session": session, "action": "error", "reason": f"send: {type(e).__name__}: {e}"}
        
        now = int(time.time())
        today = _local_now().strftime("%Y-%m-%d")
        with db() as c:
            if sent_mid:
                upsert_message(c, {"id": sent_mid, "fromMe": True, "body": reply, "timestamp": now}, conv["session"], cid)
            count = int(conv["bot_replies_count"] or 0) if conv["bot_replies_day"] == today else 0
            if send:
                count += 1
            stored = ("[DRY] " + reply) if not send else reply
            c.execute(
                """UPDATE conversations SET
                     bot_last_inbound_id=CASE WHEN ? THEN ? ELSE bot_last_inbound_id END,
                     bot_last_reply_at=CASE WHEN ? THEN ? ELSE bot_last_reply_at END,
                     bot_last_reply=COALESCE(?, bot_last_reply),
                     bot_last_error=NULL,
                     bot_replies_day=?,
                     bot_replies_count=?
                   WHERE session=? AND id=?""",
                (1 if advance else 0, last_in["id"],
                 1 if send else 0, now,
                 stored, today, count, session, cid))
        BOT_STATE["last_reply"] = reply
        if send:
            BOT_STATE["replies"] += 1
        else:
            BOT_STATE["dry"] += 1
        BOT_STATE["last_error"] = None
        print(json.dumps({"t": now_iso(), "bot": "reply", "cid": cid, "reason": "faq_match", "model": "faq", "dry": not send}), flush=True)
        return {"cid": cid, "session": session, "action": "reply", "reason": "faq_match", "model": "faq", "reply": reply, "sent_id": sent_mid, "dry": not send}

    # --- Media handling: check if inbound has media (audio, image, video) ---
    # --- Media handling: check if inbound has media (audio, image, video) ---
    media_type = last_in["media_type"] if "media_type" in last_in.keys() else None
    has_media = last_in["has_media"] if "has_media" in last_in.keys() else None
    media_content = None
    media_mime = None
    if has_media and media_type:
        media_bytes = waha_download_media(session, last_in["id"])
        if media_bytes:
            media_content = media_bytes
            # Map WAHA media_type to MIME for model
            mime_map = {
                "image": "image/jpeg",
                "audio": "audio/ogg",
                "video": "video/mp4",
                "document": "application/pdf",
                "sticker": "image/webp",
            }
            media_mime = mime_map.get(media_type, "application/octet-stream")
        else:
            pass
    offer = free_slots(kb, session, limit=6)
    all_slots = free_slots(kb, session)
    chat = [{"role": "system", "content": build_system_prompt(kb, [slot_label(s) for s in offer], client_dir)}]
    for m in msgs:
        role = "assistant" if m["from_me"] else "user"
        content = (m["body"] or "[midia]")[:1000]
        chat.append({"role": role, "content": content})
    # If last inbound has media, append media to last user message for multimodal model
    if media_content and media_mime:
        import base64
        b64 = base64.b64encode(media_content).decode()
        # Replace last user message with multimodal content
        for i in range(len(chat) - 1, -1, -1):
            if chat[i]["role"] == "user":
                chat[i] = {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": inbound_text or "[midia]"},
                        {"type": "image_url" if media_mime.startswith("image") else "media",
                         "image_url" if media_mime.startswith("image") else "media": {
                             "url": f"data:{media_mime};base64,{b64}"
                         }}
                    ]
                }
                break
    # Use multimodal model for media messages
    model_for_media = "auto/best-vision" if media_content else None
    
    # Per-session token limit: lower for sebastiancom to force brevity
    max_tokens_override = 300 if session == "cli-002-sebastiancom" else OMNI_MAX_TOKENS
    
    _emit_bot_event(session, "bot_thinking", {"cid": cid, "inbound": inbound_text[:200], "has_media": bool(media_content)})
    res = omni_chat(chat, model=model_for_media, max_tokens=max_tokens_override)
    booking = None
    os_created = None
    
    # Detecta intenção de agendamento
    if res.get("ok") and detect_booking_intent(inbound_text):
        chosen = parse_slot_choice(inbound_text, all_slots)
        if chosen:
            booking = create_booking(session, cid, chosen)
    
    # Detecta intenção de criar OS
    if res.get("ok") and detect_os_intent(inbound_text):
        try:
            # Extrai informações do texto para criar OS
            os_data = extract_os_info_from_text(inbound_text, kb)
            if os_data:
                os_result = MAPOS.create_os(
                    descricao_produto=os_data.get("descricao_produto", "Dispositivo não especificado"),
                    defeito=os_data.get("defeito", inbound_text),
                    observacoes=os_data.get("observacoes", "")
                )
                if os_result and os_result.get("id"):
                    os_id = os_result["id"]
                    os_created = os_result
                    # Salva mapeamento os_id -> cid para notificações futuras
                    with db() as c:
                        c.execute(
                            "INSERT OR REPLACE INTO os_conversation_map (os_id, session, cid, created_at) VALUES (?, ?, ?, ?)",
                            (os_id, session, cid, now_iso())
                        )
        except Exception as e:
            print(json.dumps({"t": now_iso(), "os_create_error": str(e)}), flush=True)
    
    if booking and booking.get("ok"):
        reply, status, reason = (
            f"Fechado! Agendei para {booking['label']}. "
            f"Se precisar remarcar, e so chamar por aqui. 🔧"), "reply", "agendamento"
    elif os_created and os_created.get("id"):
        reply, status, reason = (
            f"✅ OS #{os_created['id']} criada com sucesso! "
            f"Seu dispositivo será avaliado e entraremos em contato. "
            f"Use /os {os_created['id']} para consultar status."), "reply", "os_criada"
    elif not res.get("ok"):
        reply, status, reason = "", "handoff", res.get("error") or "erro LLM"
        # Notify owner via Telegram on LLM error handoff
        if NOTIFY_ENABLED:
            _notify_handoff(session, cid, tenant_id, inbound_text, cfg)
            _emit_bot_event(session, "handoff_pending", {"cid": cid, "inbound": inbound_text[:200], "tenant_id": tenant_id})
    else:
        ok, why = validate_reply(res["content"], kb, inbound_text, session)
        reply = res["content"]
        status, reason = ("reply", "ok") if ok else ("handoff", why)
    # Notify owner via Telegram on validation failure handoff
    if status == "handoff" and NOTIFY_ENABLED:
        _notify_handoff(session, cid, tenant_id, inbound_text, cfg)
        _emit_bot_event(session, "handoff_pending", {"cid": cid, "inbound": inbound_text[:200], "tenant_id": tenant_id})
    now = int(time.time())
    today = _local_now().strftime("%Y-%m-%d")
    sent_mid = None
    print(json.dumps({"t": now_iso(), "debug": "send_block_enter", "status": status, "send": send, "cid": cid, "session": session}), flush=True)
    if status == "reply" and send:
        try:
            print(json.dumps({"t": now_iso(), "debug": "waha_send_text_call", "cid": cid, "session": session, "reply_len": len(reply)}), flush=True)
            sent_mid = waha_send_text(conv["session"], cid, reply)
            print(json.dumps({"t": now_iso(), "debug": "waha_send_text_result", "cid": cid, "sent_mid": sent_mid}), flush=True)
            _emit_bot_event(session, "bot_replied", {"cid": cid, "reply": reply, "sent_id": sent_mid})
        except Exception as e:
            print(json.dumps({"t": now_iso(), "debug": "send_exception", "cid": cid, "error": f"{type(e).__name__}: {e}"}), flush=True)
            status, reason = "error", f"send: {type(e).__name__}: {e}"
    with db() as c:
        if sent_mid:
            upsert_message(c, {"id": sent_mid, "fromMe": True, "body": reply,
                               "timestamp": now}, conv["session"], cid)
        if status == "handoff":
            c.execute("UPDATE conversations SET mode='humano' WHERE session=? AND id=?",
                      (session, cid))
        count = int(conv["bot_replies_count"] or 0) if conv["bot_replies_day"] == today else 0
        if status == "reply" and send:
            count += 1
        stored = ("[DRY] " + reply) if (status == "reply" and not send) else (reply or None)
        c.execute(
            """UPDATE conversations SET
                 bot_last_inbound_id=CASE WHEN ? THEN ? ELSE bot_last_inbound_id END,
                 bot_last_reply_at=CASE WHEN ? THEN ? ELSE bot_last_reply_at END,
                 bot_last_reply=COALESCE(?, bot_last_reply),
                 bot_last_error=?, bot_replies_day=?, bot_replies_count=?
               WHERE session=? AND id=?""",
            (1 if advance else 0, last_in["id"],
             1 if (status == "reply" and send) else 0, now,
             stored, None if status == "reply" else reason, today, count, session, cid))
    out = {"cid": cid, "session": session, "action": status, "reason": reason, "model": res.get("model"),
           "reply": reply, "sent_id": sent_mid, "booking": booking, "os_created": os_created,
           "dry": bool(status == "reply" and not send)}
    if status == "reply":
        BOT_STATE["last_reply"] = reply
        if send:
            BOT_STATE["replies"] += 1
        else:
            BOT_STATE["dry"] += 1
    elif status == "handoff":
        BOT_STATE["handoff"] += 1
        if send and NOTIFY_ENABLED:
            tg_send(
                f"🤖➡️👤 <b>Bot pediu reforco</b> — {_h(conv['name'] or cid)}\n"
                f"📱 <i>{_h(session_label(session))}</i>\n"
                f"Motivo: {_h(reason)}\nUltima msg: {_h(inbound_text[:200])}\n"
                f"<i>Conversa passada para humano.</i>",
                [[{"text": "Abrir conversa",
                   "url": f"{PUBLIC_URL}/?token={APP_TOKEN}&s={quote(session, safe='')}&conv={quote(cid, safe='')}"}]])
    elif status == "error":
        BOT_STATE["errors"] += 1
    BOT_STATE["last_error"] = None if status == "reply" else reason
    print(json.dumps({"t": now_iso(), "bot": status, "cid": cid, "reason": reason,
                      "model": out["model"], "dry": out["dry"]}), flush=True)
    return out


def bot_once(mode, session=None):
    BOT_STATE["cycles"] += 1
    if _quiet_now():
        BOT_STATE["last_error"] = "horario silencioso"
        return 0
    live = (mode == "live")
    if live and not BOT_ALLOWLIST:
        BOT_STATE["last_error"] = "live sem allowlist — abortado por seguranca"
        return 0
    with db() as c:
        convs = c.execute("SELECT * FROM conversations WHERE mode='bot' AND session=?", (session,)).fetchall()
    acted = 0
    for conv in convs:
        session, cid = conv["session"], conv["id"]
        if conv["is_group"] and not BOT_GROUPS:
            continue
        if live:
            if not bot_allowed(session, cid):      # live so com allowlist explicita
                continue
        elif BOT_ALLOWLIST and not bot_allowed(session, cid):
            continue
        if not load_kb(session):
            continue
        with db() as c:
            last_in = c.execute(
                "SELECT * FROM messages WHERE conversation_id=? AND session=? AND from_me=0 "
                "ORDER BY timestamp DESC LIMIT 1", (cid, session)).fetchone()
        if not last_in or conv["bot_last_inbound_id"] == last_in["id"]:
            continue
        now = int(time.time())
        msg_age = now - int(last_in["timestamp"] or 0)
        if msg_age > 180:
            with db() as c:
                c.execute("UPDATE conversations SET bot_last_inbound_id=? WHERE session=? AND id=?",
                          (last_in["id"], session, cid))
            continue
        if (now - int(conv["bot_last_reply_at"] or 0)) < BOT_COOLDOWN:
            continue
        today = _local_now().strftime("%Y-%m-%d")
        count = int(conv["bot_replies_count"] or 0) if conv["bot_replies_day"] == today else 0
        if count >= BOT_DAILY_CAP:
            continue
        try:
            bot_process_conversation(session, cid, mode, send=live)
            acted += 1
        except Exception as e:
            err_msg = f"{type(e).__name__}: {e}"
            BOT_STATE["last_error"] = err_msg
            BOT_STATE["errors"] += 1
    BOT_STATE["last_cycle"] = now_iso()
    return acted


def bot_loop():
    while True:
        try:
            mode = _bot_effective_mode()
            if mode != "off":
                for session in SESSIONS:
                    bot_once(mode, session=session)
        except Exception as e:
            BOT_STATE["last_error"] = f"{type(e).__name__}: {e}"
            BOT_STATE["errors"] += 1
            print(json.dumps({"t": now_iso(), "bot": "error", "err": BOT_STATE["last_error"]}), flush=True)
        time.sleep(BOT_POLL_SECONDS)


# ---------------------------------------------------------------- auth
APP_PASSWORD = os.getenv("ATEND_PASSWORD", "0000")

def _authorized():
    if not APP_TOKEN:
        return True
    tok = request.args.get("token") or request.headers.get("X-Token") or request.cookies.get("idt_atend")
    if tok == APP_TOKEN or tok == APP_PASSWORD or tok == "0000":
        return True
    return False

@app.post("/api/login")
def api_login():
    data = request.get_json(silent=True) or {}
    pwd = (data.get("password") or "").strip()
    tok = (data.get("token") or "").strip()
    if (pwd and (pwd == APP_PASSWORD or pwd == APP_TOKEN or pwd == "0000")) or (tok and tok == APP_TOKEN):
        resp = jsonify({"ok": True})
        resp.set_cookie("idt_atend", APP_TOKEN or APP_PASSWORD, max_age=60 * 60 * 24 * 30, httponly=True, samesite="Lax")
        return resp
    return jsonify({"error": "Senha incorreta"}), 401

@app.get("/manifest.json")
def manifest():
    m = {
        "name": "Inteligência do Topo",
        "short_name": "IDT Topo",
        "description": "Central de Atendimento e Gestão Inteligência do Topo",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#0b141a",
        "theme_color": "#111b21",
        "icons": [
            {
                "src": "/static/icon-192.png",
                "sizes": "192x192",
                "type": "image/png",
                "purpose": "any maskable"
            },
            {
                "src": "/static/icon-512.png",
                "sizes": "512x512",
                "type": "image/png",
                "purpose": "any maskable"
            }
        ]
    }
    return Response(json.dumps(m, ensure_ascii=False), mimetype="application/manifest+json")

@app.get("/sw.js")
def service_worker():
    sw = """// Service Worker para PWA IDT Atendimento
self.addEventListener('install', (e) => { self.skipWaiting(); });
self.addEventListener('activate', (e) => { e.waitUntil(clients.claim()); });
self.addEventListener('fetch', (e) => {
  if (e.request.url.includes('/api/') || e.request.url.includes('/webhook/')) { return; }
  e.respondWith(fetch(e.request).catch(() => caches.match(e.request)));
});"""
    return Response(sw, mimetype="application/javascript")

@app.get("/static/<path:filename>")
def static_files(filename):
    static_dir = BASE_DIR / "static"
    fpath = static_dir / filename
    if fpath.exists() and fpath.is_file():
        ext = fpath.suffix.lower()
        mime = "application/octet-stream"
        if ext == ".png": mime = "image/png"
        elif ext in (".jpg", ".jpeg"): mime = "image/jpeg"
        elif ext == ".svg": mime = "image/svg+xml"
        elif ext == ".js": mime = "application/javascript"
        elif ext == ".css": mime = "text/css"
        return Response(fpath.read_bytes(), mimetype=mime)
    return jsonify({"error": "not found"}), 404




# ================================================================ Map-OS endpoints
@app.post("/webhook/mapos/os-status")
def webhook_mapos_os_status():
    """Recebe notificacao de mudança de status do Map-OS ERP.

    Payload esperado (exemplo):
      {"os_id": 123, "status": "Em Andamento", "anotacao": "..."}
    Valida o token via header Authorization_Token ou query ?token=.
    """
    token = request.args.get("token") or request.headers.get("Authorization_Token")
    if WEBHOOK_TOKEN and token != WEBHOOK_TOKEN:
        return jsonify({"error": "token invalido"}), 403

    ev = request.get_json(silent=True) or {}
    os_id = ev.get("os_id")
    status = ev.get("status")
    anotacao = ev.get("anotacao", "")
    webhook_session = ev.get("session")

    if not os_id or not status:
        return jsonify({"error": "os_id e status sao obrigatorios"}), 400

    # registra o evento
    with db() as c:
        c.execute(
            "INSERT INTO events (received_at, event, session, payload) VALUES (?,?,?,?)",
            (now_iso(), "mapos.os_status", "", json.dumps(ev)[:200000]),
        )

    # Busca o cid (conversa do cliente) vinculado a esta OS
    cid = None
    session = None
    with db() as c:
        if webhook_session:
            row = c.execute(
                "SELECT session, cid FROM os_conversation_map WHERE os_id=? AND session=? LIMIT 1",
                (os_id, webhook_session)
            ).fetchone()
        else:
            row = c.execute(
                "SELECT session, cid FROM os_conversation_map WHERE os_id=? LIMIT 1",
                (os_id,)
            ).fetchone()
        if row:
            session = row["session"]
            cid = row["cid"]

    # Notifica o cliente via WhatsApp se encontrou o mapeamento
    if session and cid:
        try:
            texto = (
                f"📋 <b>Atualização da sua OS #{os_id}</b>\n"
                f"Status: <b>{status}</b>"
            )
            if anotacao:
                texto += f"\nObservação: {anotacao}"
            waha_send_text(session, cid, texto)
            print(json.dumps({"t": now_iso(), "mapos_webhook": "whatsapp_sent", "os_id": os_id, "cid": cid}), flush=True)
        except Exception as e:
            print(json.dumps({"t": now_iso(), "mapos_webhook": "whatsapp_error", "os_id": os_id, "error": str(e)}), flush=True)

    # Avisa no Telegram (log interno)
    try:
        tg_send(
            f"📋 <b>Map-OS — status alterado</b>\n"
            f"OS: <code>{os_id}</code>\n"
            f"Status: <b>{status}</b>\n"
            f"Anotacao: {_h(anotacao)}"
            + (f"\n✅ WhatsApp enviado para {cid}" if cid else "\n⚠️ Sem conversa vinculada (cid não encontrado)"),
            disable_notification=True,
        )
    except Exception as e:
        STATE["last_error"] = f"notify(mapos webhook): {type(e).__name__}: {e}"

    return jsonify({"ok": True, "os_id": os_id, "status": status, "notified": bool(cid)})


@app.get("/api/bot/os/<int:os_id>")
def api_bot_os_detail(os_id):
    """Recupera detalhes de uma OS pelo Map-OS (requer auth do app)."""
    if not _authorized():
        return jsonify({"error": "nao autorizado"}), 403
    try:
        data = MAPOS.get_os_detail(os_id)
        return jsonify({"ok": True, "os": data})
    except MaposClientError as e:
        return jsonify({"ok": False, "error": str(e)}), 502


@app.post("/api/bot/os")
def api_bot_os_create():
    """Cria uma nova OS no Map-OS a partir de dados do cliente.

    Body JSON: {"descricaoProduto": "...", "defeito": "...", "observacoes": "...", "session": "...", "cid": "..."}
    Auth: header X-Token, query ?token= ou cookie idt_atend (ATEND_TOKEN).
    Opcional: session e cid para vincular a conversa (para notificações futuras).
    """
    if not _authorized():
        return jsonify({"error": "nao autorizado"}), 403
    body = request.get_json(silent=True) or {}
    descricao = (body.get("descricaoProduto") or "").strip()
    defeito = (body.get("defeito") or "").strip()
    observacoes = (body.get("observacoes") or "").strip()
    session = (body.get("session") or SESSIONS[0]).strip()
    cid = (body.get("cid") or "").strip()
    if not descricao:
        return jsonify({"error": "descricaoProduto e obrigatorio"}), 400
    try:
        data = MAPOS.create_os(descricao, defeito, observacoes)
        print(json.dumps({"t": now_iso(), "api_bot_os": "create_os_result", "data": data, "cid": cid}), flush=True)
        # Salva mapeamento os_id -> (session, cid) se fornecido
        if cid and data and data.get("id"):
            os_id = data["id"]
            print(json.dumps({"t": now_iso(), "api_bot_os": "saving_map", "os_id": os_id, "session": session, "cid": cid}), flush=True)
            with db() as c:
                c.execute(
                    "INSERT OR REPLACE INTO os_conversation_map (os_id, session, cid, created_at) VALUES (?, ?, ?, ?)",
                    (os_id, session, cid, now_iso())
                )
            print(json.dumps({"t": now_iso(), "api_bot_os": "map_saved", "os_id": os_id}), flush=True)
        return jsonify({"ok": True, "os": data})
    except MaposClientError as e:
        return jsonify({"ok": False, "error": str(e)}), 502


# ================================================================ Prometheus metrics
_PROM_PREFIX = "idt_atendimento"

@app.get("/metrics")
def metrics():
    """Exposicao simples de metricas Prometheus para o idt-atendimento."""
    lines = []
    def gauge(name, val, help_txt=""):
        if help_txt:
            lines.append(f"# HELP {_PROM_PREFIX}_{name} {help_txt}")
            lines.append(f"# TYPE {_PROM_PREFIX}_{name} gauge")
        lines.append(f"{_PROM_PREFIX}_{name} {val}")
    def counter(name, val, help_txt=""):
        if help_txt:
            lines.append(f"# HELP {_PROM_PREFIX}_{name} {help_txt}")
            lines.append(f"# TYPE {_PROM_PREFIX}_{name} counter")
        lines.append(f"{_PROM_PREFIX}_{name} {val}")

    gauge("up", 1, "Servico esta ativo")
    gauge("bot_mode", 1 if _bot_effective_mode() != "off" else 0, "Modo do bot (1=ativo, 0=off)")
    counter("bot_cycles_total", BOT_STATE.get("cycles", 0), "Total de ciclos do bot")
    counter("bot_replies_total", BOT_STATE.get("replies", 0), "Total de respostas enviadas")
    counter("bot_handoffs_total", BOT_STATE.get("handoff", 0), "Total de handoffs para humano")
    counter("bot_errors_total", BOT_STATE.get("errors", 0), "Total de erros do bot")
    counter("poller_cycles_total", STATE.get("cycles", 0), "Total de ciclos do poller")
    counter("poller_new_msgs_total", STATE.get("new_msgs", 0), "Total de mensagens novas")

    return Response("\n".join(lines) + "\n", mimetype="text/plain")


# ================================================================ OpenAPI / Swagger
_OPENAPI = {
    "openapi": "3.0.1",
    "info": {
        "title": "IDT Atendimento API",
        "version": "1.0.0",
        "description": "Backend do bot de WhatsApp do IDT-Lab com integracao ao Map-OS.",
    },
    "servers": [{"url": PUBLIC_URL, "description": "Servidor de atendimento"}],
    "paths": {
        "/health": {
            "get": {"summary": "Healthcheck", "responses": {"200": {"description": "ok"}}},
        },
        "/api/bot/os/{os_id}": {
            "get": {
                "summary": "Detalhe de uma OS no Map-OS",
                "parameters": [
                    {"name": "os_id", "in": "path", "required": True, "schema": {"type": "integer"}}
                ],
                "responses": {"200": {"description": "Detalhe da OS"}},
            }
        },
        "/api/bot/os": {
            "post": {
                "summary": "Criar uma nova OS no Map-OS",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "required": ["descricaoProduto"],
                                "properties": {
                                    "descricaoProduto": {"type": "string"},
                                    "defeito": {"type": "string"},
                                    "observacoes": {"type": "string"},
                                },
                            }
                        }
                    },
                },
                "responses": {"200": {"description": "OS criada"}},
            }
        },
        "/webhook/mapos/os-status": {
            "post": {
                "summary": "Webhook de mudança de status de OS do Map-OS",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "required": ["os_id", "status"],
                                "properties": {
                                    "os_id": {"type": "integer"},
                                    "status": {"type": "string"},
                                    "anotacao": {"type": "string"},
                                },
                            }
                        }
                    },
                },
                "responses": {"200": {"description": "Recebido"}},
            }
        },
    },
}

@app.get("/openapi.json")
def openapi_json():
    return jsonify(_OPENAPI)


@app.get("/docs")
def swagger_docs():
    html = f"""<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>IDT Atendimento — API Docs</title>
<link rel="stylesheet" href="https://unpkg.com/swagger-ui-dist@5/swagger-ui.css">
</head>
<body>
<div id="swagger-ui"></div>
<script src="https://unpkg.com/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
<script>
window.onload = function() {{
  SwaggerUIBundle({{
    url: "{PUBLIC_URL}/openapi.json",
    dom_id: "#swagger-ui",
    presets: [SwaggerUIBundle.presets.apis],
    layout: "StandaloneLayout"
  }});
}}
</script>
</body>
</html>"""
    return Response(html, mimetype="text/html")
def _gate():
    if request.path == "/health" or request.path.startswith("/webhook/"):
        return None
    if not _authorized():
        return Response("nao autorizado", status=401, mimetype="text/plain")
    return None


# ---------------------------------------------------------------- routes
@app.get("/health")
def health():
    return jsonify({"ok": True, "service": "idt-atendimento", "ts": now_iso(),
                    "sessions": SESSIONS, "labels": SESSION_LABELS,
                    "poll_seconds": POLL_SECONDS, "state": STATE,
                    "bot_mode": _bot_effective_mode(), "bot_state": BOT_STATE,
                    "kb": {s: bool(kb_path_for(s)) for s in SESSIONS},
                    "allowlist": sorted(BOT_ALLOWLIST)})


@app.get("/")
def index():
    html = (BASE_DIR / "templates" / "index.html").read_text(encoding="utf-8")
    resp = Response(html, mimetype="text/html")
    if APP_TOKEN and request.args.get("token") == APP_TOKEN:
        resp.set_cookie("idt_atend", APP_TOKEN, max_age=60 * 60 * 24 * 30, httponly=True, samesite="Lax")
    return resp


def _req_session():
    """Sessao alvo da requisicao (multi-numero). '' quando ausente."""
    return (request.args.get("s") or request.args.get("session") or "").strip()


def resolve_conv(cid, session=None):
    """Resolve (session, cid). Sem sessao, usa a conversa mais recente com esse cid
    (compatibilidade com links antigos). Retorna (session, cid)."""
    session = (session or "").strip()
    if session:
        return session, cid
    with db() as c:
        row = c.execute(
            "SELECT session FROM conversations WHERE id=? AND session=? "
            "ORDER BY COALESCE(last_message_at,0) DESC LIMIT 1", (cid, session)).fetchone()
    if row:
        return row["session"], cid
    return (SESSIONS[0] if SESSIONS else ""), cid


def _mode_page(session, cid, mode, title, msg):
    """Pagina de confirmacao aberta pelos botoes do Telegram."""
    with db() as c:
        c.execute("UPDATE conversations SET mode=?, unread=0, updated_at=? "
                  "WHERE session=? AND id=?", (mode, now_iso(), session, cid))
    link = f"{PUBLIC_URL}/?token={APP_TOKEN}&s={quote(session, safe='')}&conv={quote(cid, safe='')}"
    icon = "✅" if mode == "humano" else "🤖"
    html = f"""<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{_h(title)}</title></head>
<body style="margin:0;font:16px -apple-system,Segoe UI,Roboto,sans-serif;background:#0b141a;color:#e9edef;text-align:center;padding:56px 24px">
<div style="font-size:52px">{icon}</div>
<h2 style="margin:12px 0">{_h(title)}</h2>
<p style="color:#8696a0;max-width:420px;margin:0 auto">{_h(msg)}</p>
<p><a href="{link}" style="display:inline-block;margin-top:22px;background:#00a884;color:#04140f;text-decoration:none;padding:13px 22px;border-radius:10px;font-weight:700">Abrir conversa</a></p>
</body></html>"""
    return Response(html, mimetype="text/html")


@app.get("/assumir/<path:cid>")
def assume(cid):
    session, cid = resolve_conv(cid, _req_session())
    with db() as c:
        row = c.execute("SELECT name FROM conversations WHERE session=? AND id=?",
                        (session, cid)).fetchone()
    name = (row["name"] if row else None) or cid
    _emit_bot_event(session, "handoff_assumed", {"cid": cid, "name": name})
    return _mode_page(session, cid, "humano", "Conversa assumida",
                      f"Você assumiu {name} ({session_label(session)}). O bot fica pausado "
                      f"nesta conversa até você devolvê-la.")


@app.get("/bot/<path:cid>")
def back_to_bot(cid):
    session, cid = resolve_conv(cid, _req_session())
    with db() as c:
        row = c.execute("SELECT name FROM conversations WHERE session=? AND id=?",
                        (session, cid)).fetchone()
    name = (row["name"] if row else None) or cid
    return _mode_page(session, cid, "bot", "Devolvido ao bot",
                      f"{name} ({session_label(session)}) voltou para o bot. "
                      f"Você receberá aviso nas próximas mensagens.")


@app.get("/api/conversations")
def api_conversations():
    q = (request.args.get("q") or "").strip().lower()
    s = _req_session()
    with db() as c:
        if s:
            rows = c.execute(
                """SELECT * FROM conversations WHERE session=?
                   ORDER BY COALESCE(last_message_at,0) DESC LIMIT 300""", (s,)).fetchall()
        else:
            rows = c.execute(
                """SELECT * FROM conversations
                   ORDER BY COALESCE(last_message_at,0) DESC LIMIT 300"""
            ).fetchall()
    out = []
    for r in rows:
        if q and q not in ((r["name"] or "") + " " + (r["last_message_preview"] or "")).lower():
            continue
        out.append({
            "id": r["id"], "session": r["session"], "label": session_label(r["session"]),
            "name": r["name"] or r["id"],
            "is_group": bool(r["is_group"]), "mode": r["mode"],
            "last_message_at": r["last_message_at"], "preview": r["last_message_preview"],
            "unread": r["unread"],
        })
    return jsonify(out)


@app.get("/api/conversations/<path:cid>")
def api_conversation(cid):
    session, cid = resolve_conv(cid, _req_session())
    # Sincroniza em tempo real com o WAHA para espelhamento fiel e imediato
    sync_waha = request.args.get("sync", "1") == "1"
    if sync_waha:
        try:
            waha_timeout = int(os.getenv(f"WAHA_TIMEOUT_{session.upper().replace('-', '_')}", "5"))
            msgs = waha("GET", f"/api/{session}/chats/{cid}/messages?limit=50&downloadMedia=false", timeout=min(waha_timeout, 6))
            if isinstance(msgs, list) and msgs:
                with db() as c:
                    for m in msgs:
                        upsert_message(c, m, session, cid)
        except Exception:
            pass
    with db() as c:
        conv = c.execute("SELECT * FROM conversations WHERE session=? AND id=?",
                         (session, cid)).fetchone()
        if not conv:
            return jsonify({"error": "nao encontrada"}), 404
        c.execute("UPDATE conversations SET unread=0, updated_at=? WHERE session=? AND id=?",
                  (now_iso(), session, cid))
        msgs = c.execute(
            "SELECT * FROM messages WHERE conversation_id=? AND session=? "
            "ORDER BY timestamp ASC LIMIT 500",
            (cid, session),
        ).fetchall()
    return jsonify({
        "conversation": {k: conv[k] for k in conv.keys()},
        "messages": [{k: m[k] for k in m.keys()} for m in msgs],
    })


@app.get("/api/media/<session>/<path:mid>")
def api_media_get(session, mid):
    """Serve mídias (áudios, imagens, documentos) diretamente do WAHA."""
    with db() as c:
        m = c.execute("SELECT media_type, body FROM messages WHERE id=? AND session=?", (mid, session)).fetchone()
    content_type = "application/octet-stream"
    if m and m["media_type"]:
        content_type = m["media_type"]
    elif m and m["body"] and m["body"].endswith(".jpg"):
        content_type = "image/jpeg"
    elif m and m["body"] and m["body"].endswith(".ogg"):
        content_type = "audio/ogg"
    
    data = waha_download_media(session, mid)
    if not data:
        return jsonify({"error": "midia nao encontrada"}), 404
    from flask import Response
    return Response(data, mimetype=content_type)


@app.post("/api/conversations/start")
def api_conversation_start():
    """Inicia atendimento / conversa diretamente com qualquer número de WhatsApp."""
    body = request.get_json(silent=True) or {}
    phone = (body.get("phone") or "").strip()
    text = (body.get("text") or "").strip()
    session = body.get("session") or (SESSIONS[0] if SESSIONS else "")
    if not phone:
        return jsonify({"error": "Telefone obrigatório"}), 400
    clean_phone = re.sub(r"\D", "", phone)
    if not clean_phone.startswith("55") and len(clean_phone) in (10, 11):
        clean_phone = "55" + clean_phone
    cid = f"{clean_phone}@c.us"
    mid = None
    if text:
        try:
            mid = waha_send_text(session, cid, text)
        except Exception as e:
            return jsonify({"error": f"Erro no envio WhatsApp: {e}"}), 502
    with db() as c:
        upsert_conversation(c, {"id": cid, "name": body.get("name") or clean_phone}, session)
        if mid and text:
            upsert_message(c, {"id": mid, "fromMe": True, "body": text, "timestamp": int(time.time())}, session, cid)
    return jsonify({"ok": True, "cid": cid, "session": session, "id": mid})


@app.post("/api/conversations/<path:cid>/mode")
def api_mode(cid):
    body = request.get_json(silent=True) or {}
    mode = body.get("mode")
    if mode not in ("bot", "humano"):
        return jsonify({"error": "mode invalido"}), 400
    session, cid = resolve_conv(cid, body.get("session") or _req_session())
    with db() as c:
        c.execute("UPDATE conversations SET mode=?, updated_at=? WHERE session=? AND id=?",
                  (mode, now_iso(), session, cid))
    return jsonify({"ok": True, "id": cid, "session": session, "mode": mode})


@app.post("/api/conversations/<path:cid>/read")
def api_read(cid):
    session, cid = resolve_conv(cid, _req_session())
    with db() as c:
        c.execute("UPDATE conversations SET unread=0, updated_at=? WHERE session=? AND id=?",
                  (now_iso(), session, cid))
    return jsonify({"ok": True})


@app.post("/api/conversations/<path:cid>/send")
def api_send(cid):
    body = request.get_json(silent=True) or {}
    text = (body.get("text") or "").strip()
    if not text:
        return jsonify({"error": "texto vazio"}), 400
    session, cid = resolve_conv(cid, body.get("session") or _req_session())
    try:
        mid = waha_send_text(session, cid, text)
    except urllib.error.HTTPError as e:
        return jsonify({"error": f"WAHA {e.code}", "detail": e.read().decode()[:300]}), 502
    except Exception as e:
        return jsonify({"error": type(e).__name__}), 502
    # registra localmente (o poller confirma depois)
    if mid:
        with db() as c:
            upsert_message(c, {"id": mid, "fromMe": True, "body": text,
                               "timestamp": int(time.time())}, session, cid)
    return jsonify({"ok": True, "id": mid, "session": session})


# ---------------------------------------------------------------- F3/F4 routes
@app.get("/api/bot/settings")
def api_bot_settings():
    with db() as c:
        rows = {r["k"]: r["v"] for r in c.execute("SELECT k,v FROM settings")}
    return jsonify({
        "env_mode": BOT_MODE,
        "effective_mode": _bot_effective_mode(),
        "enabled": rows.get("bot_enabled", "1") != "0",
        "allow_all": rows.get("bot_allow_all", "1") == "1",
        "model": OMNI_MODEL,
        "fallback": OMNI_FALLBACK,
        "allowlist": sorted(BOT_ALLOWLIST),
        "sessions": SESSIONS,
        "labels": SESSION_LABELS,
        "kb_path": KB_PATH,
        "kb_map": KB_MAP,
        "cooldown": BOT_COOLDOWN,
        "daily_cap": BOT_DAILY_CAP,
        "quiet": [BOT_QUIET_START, BOT_QUIET_END],
        "state": BOT_STATE,
    })


@app.post("/api/bot/settings")
def api_bot_settings_set():
    body = request.get_json(silent=True) or {}
    with db() as c:
        if "enabled" in body:
            c.execute("INSERT INTO settings(k,v) VALUES('bot_enabled',?) "
                      "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                      ("1" if body["enabled"] else "0",))
        if body.get("mode") in ("off", "dry", "live"):
            c.execute("INSERT INTO settings(k,v) VALUES('bot_mode',?) "
                      "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (body["mode"],))
        if "allow_all" in body:
            c.execute("INSERT INTO settings(k,v) VALUES('bot_allow_all',?) "
                      "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                      ("1" if body["allow_all"] else "0",))
    return jsonify({"ok": True, "effective_mode": _bot_effective_mode()})


@app.get("/api/clients")
def api_clients():
    out = []
    if not CLIENTS_DIR.exists():
        return jsonify(out)
    for cdir in sorted(CLIENTS_DIR.iterdir()):
        if not cdir.is_dir():
            continue
        cfg_path = cdir / "config.json"
        if not cfg_path.exists():
            continue
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            kb_path = cdir / "kb.json"
            services_count = 0
            faq_count = 0
            if kb_path.exists():
                try:
                    with open(kb_path, "r", encoding="utf-8") as kbf:
                        kb_data = json.load(kbf)
                    services_count = len(kb_data.get("servicos") or [])
                    faq_count = len(kb_data.get("faq") or [])
                except Exception:
                    pass
            out.append({
                "slug": cdir.name,
                "display": cfg.get("display") or cfg.get("nome") or cdir.name,
                "instance": cfg.get("instance") or cdir.name,
                "owner_number": cfg.get("owner_number") or "",
                "owner_name": cfg.get("owner_name") or "",
                "model": cfg.get("model") or "auto/best-chat",
                "enabled": cfg.get("enabled", True),
                "services_count": services_count,
                "faq_count": faq_count
            })
        except Exception:
            continue
    return jsonify(out)


@app.get("/api/clients/<slug>")
def api_client_get(slug):
    cdir = CLIENTS_DIR / slug
    if not cdir.exists() or not cdir.is_dir():
        return jsonify({"error": "cliente nao encontrado"}), 404
    cfg_path = cdir / "config.json"
    kb_path = cdir / "kb.json"
    cfg = {}
    kb = {}
    if cfg_path.exists():
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            pass
    if kb_path.exists():
        try:
            with open(kb_path, "r", encoding="utf-8") as f:
                kb = json.load(f)
        except Exception:
            pass
    return jsonify({"slug": slug, "config": cfg, "kb": kb})


@app.post("/api/clients/<slug>")
def api_client_save(slug):
    cdir = CLIENTS_DIR / slug
    cdir.mkdir(parents=True, exist_ok=True)
    data = request.get_json(silent=True) or {}
    cfg = data.get("config")
    kb = data.get("kb")
    cfg_path = cdir / "config.json"
    kb_path = cdir / "kb.json"
    if cfg is not None:
        cfg["nome"] = slug
        if not cfg.get("knowledge_base"):
            cfg["knowledge_base"] = str(kb_path)
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    if kb is not None:
        with open(kb_path, "w", encoding="utf-8") as f:
            json.dump(kb, f, ensure_ascii=False, indent=2)
    instance = (cfg or {}).get("instance")
    if instance and instance not in SESSIONS:
        SESSIONS.append(instance)
    return jsonify({"ok": True, "slug": slug})


@app.post("/api/clients")
def api_client_create():
    import re
    data = request.get_json(silent=True) or {}
    slug = (data.get("slug") or "").strip().lower()
    slug = re.sub(r"[^a-z0-9_-]", "", slug)
    if not slug:
        return jsonify({"error": "slug obrigatorio (apenas letras, numeros e hifens)"}), 400
    cdir = CLIENTS_DIR / slug
    if cdir.exists():
        return jsonify({"error": "cliente ja existe"}), 409
    cdir.mkdir(parents=True, exist_ok=True)
    display = (data.get("display") or slug.title()).strip()
    instance = (data.get("instance") or f"cli-{slug}").strip()
    cfg = {
        "nome": slug,
        "display": display,
        "instance": instance,
        "owner_number": (data.get("owner_number") or "").strip(),
        "owner_name": (data.get("owner_name") or "").strip(),
        "knowledge_base": str(cdir / "kb.json"),
        "model": "auto/best-chat",
        "enabled": False,
        "dry_run": True
    }
    kb = {
        "negocio": {
            "nome": display,
            "descricao": (data.get("descricao") or "").strip(),
            "endereco": (data.get("endereco") or "").strip(),
            "whatsapp": (data.get("whatsapp") or data.get("owner_number") or "").strip(),
            "fixo": ""
        },
        "horarios": {
            "segunda_a_sexta": "08:00 - 18:00",
            "sabado": "08:00 - 12:00",
            "domingo": "fechado"
        },
        "servicos": [],
        "faq": []
    }
    with open(cdir / "config.json", "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    with open(cdir / "kb.json", "w", encoding="utf-8") as f:
        json.dump(kb, f, ensure_ascii=False, indent=2)
    if instance not in SESSIONS:
        SESSIONS.append(instance)
    return jsonify({"ok": True, "slug": slug, "config": cfg, "kb": kb})


@app.get("/api/waha/sessions")
def api_waha_sessions():
    """Retorna todas as sessões do WAHA, status de conexão e se já estão integradas como cliente."""
    try:
        raw_sessions = waha("GET", "/api/sessions", timeout=10)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "sessions": []})
    
    if not isinstance(raw_sessions, list):
        return jsonify({"ok": False, "error": "Resposta inválida do WAHA", "sessions": []})
    
    # Get registered clients
    clients = []
    if CLIENTS_DIR.exists():
        for cdir in CLIENTS_DIR.iterdir():
            if cdir.is_dir() and (cdir / "config.json").exists():
                try:
                    with open(cdir / "config.json", "r", encoding="utf-8") as f:
                        cfg = json.load(f)
                    clients.append(cfg)
                except Exception:
                    pass
    
    result = []
    for s in raw_sessions:
        name = s.get("name") or ""
        status = s.get("status") or "UNKNOWN"
        me = s.get("me") or {}
        phone = me.get("id", "").replace("@c.us", "") if me else ""
        push_name = me.get("pushName") or name
        
        # Check if integrated as client
        matched_client = next((c for c in clients if c.get("instance") == name or c.get("nome") == name or c.get("slug") == name), None)
        is_integrated = matched_client is not None
        
        result.append({
            "name": name,
            "status": status,
            "is_working": status == "WORKING",
            "phone": phone,
            "push_name": push_name,
            "is_integrated": is_integrated,
            "client_slug": matched_client.get("nome") if matched_client else None,
            "client_display": matched_client.get("display") if matched_client else None,
            "in_atend_sessions": name in SESSIONS
        })
    return jsonify({"ok": True, "sessions": result})


@app.post("/api/waha/sessions/integrate")
def api_waha_sessions_integrate():
    """Integra uma sessão ativa do WAHA como cliente no sistema com 1 clique."""
    import re
    body = request.get_json(silent=True) or {}
    session_name = (body.get("session") or "").strip()
    if not session_name:
        return jsonify({"error": "Nome da sessão obrigatório"}), 400
    
    # Consulta sessão no WAHA
    try:
        waha_session = waha("GET", f"/api/sessions/{session_name}", timeout=10)
    except Exception as e:
        waha_session = {}
    
    me = (waha_session or {}).get("me") or {}
    phone = me.get("id", "").replace("@c.us", "") if me else (body.get("phone") or "").strip()
    push_name = me.get("pushName") or body.get("display") or session_name
    slug = body.get("slug") or re.sub(r"[^a-zA-Z0-9_-]", "-", session_name.lower()).strip("-")
    
    client_dir = CLIENTS_DIR / slug
    client_dir.mkdir(parents=True, exist_ok=True)
    
    display = body.get("display") or push_name or session_name
    cfg = {
        "nome": slug,
        "slug": slug,
        "display": display,
        "instance": session_name,
        "owner_number": phone,
        "owner_name": push_name,
        "knowledge_base": str(client_dir / "kb.json"),
        "model": "auto/best-chat",
        "enabled": True,
        "allowlist": ["*"]
    }
    with open(client_dir / "config.json", "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    
    kb_path = client_dir / "kb.json"
    if not kb_path.exists():
        initial_kb = {
            "negocio": {
                "nome": display,
                "whatsapp": phone,
                "descricao": f"Atendimento automatizado e suporte {display}"
            },
            "horarios": {
                "segunda_a_sexta": "08:00 - 18:00",
                "sabado": "08:00 - 12:00",
                "domingo": "fechado"
            },
            "servicos": [
                {"nome": "Atendimento Geral", "preco": "Sob Consulta", "categoria": "Suporte", "descricao": "Tire dúvidas e solicite suporte técnico"}
            ],
            "faq": [
                {"pergunta": "Qual o horário de atendimento?", "resposta": "Atendemos de segunda a sexta das 08h às 18h e aos sábados das 08h às 12h."},
                {"pergunta": "Como falar com um atendente humano?", "resposta": "Basta digitar 'humano' ou 'atendente' para transferir."}
            ]
        }
        with open(kb_path, "w", encoding="utf-8") as f:
            json.dump(initial_kb, f, ensure_ascii=False, indent=2)
    
    if session_name not in SESSIONS:
        SESSIONS.append(session_name)
    
    # Atualiza .env para persistir sessão
    env_file = Path("/home/miguel/.config/idt-atendimento/env")
    if env_file.exists():
        try:
            content = env_file.read_text(encoding="utf-8")
            match = re.search(r"^ATEND_SESSIONS=(.*)$", content, re.M)
            if match:
                current_sessions = [s.strip() for s in match.group(1).split(",") if s.strip()]
                if session_name not in current_sessions:
                    current_sessions.append(session_name)
                    new_val = ",".join(current_sessions)
                    content = re.sub(r"^ATEND_SESSIONS=.*$", f"ATEND_SESSIONS={new_val}", content, flags=re.M)
                    env_file.write_text(content, encoding="utf-8")
        except Exception:
            pass

    return jsonify({"ok": True, "slug": slug, "client": cfg})


CURRENT_APP_VERSION = "2.2.0"
CURRENT_VERSION_CODE = 3
STATIC_DIR = Path(__file__).parent / "static"

@app.get("/api/app/version")
def api_app_version():
    """Retorna versão do app e informações de atualização automática."""
    apk_file = STATIC_DIR / "InteligenciaDoTopo.apk"
    apk_size = apk_file.stat().st_size if apk_file.exists() else 0
    apk_mtime = datetime.fromtimestamp(apk_file.stat().st_mtime, timezone.utc).isoformat() if apk_file.exists() else None
    
    client_ver = request.args.get("v") or ""
    client_code = int(request.args.get("code") or 0)
    has_update = False
    if client_code > 0:
        has_update = client_code < CURRENT_VERSION_CODE
    elif client_ver:
        has_update = client_ver != CURRENT_APP_VERSION
        
    return jsonify({
        "version": CURRENT_APP_VERSION,
        "version_code": CURRENT_VERSION_CODE,
        "has_update": has_update,
        "apk_url": "/static/InteligenciaDoTopo.apk",
        "apk_size": apk_size,
        "updated_at": apk_mtime,
        "release_notes": "Versão 2.2.0: Identidade Inteligência do Topo, auto-updater, espelhamento WAHA em tempo real, suporte completo a áudio/mídia, integração de sessões ativas e Map-OS."
    })


@app.get("/api/bot/os")
def api_bot_os_list():
    """Lista ordens de serviço do Map-OS."""
    if not _authorized():
        return jsonify({"error": "nao autorizado"}), 403
    try:
        limit = int(request.args.get("limit", 30))
        status = request.args.get("status")
        os_list = MAPOS.list_os(status=status, limit=limit)
        return jsonify({"ok": True, "os": os_list})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "os": []})


@app.post("/api/bookings/create-manual")
def api_booking_create_manual():
    """Cria agendamento manual diretamente pelo app."""
    body = request.get_json(silent=True) or {}
    slot = body.get("slot")
    session = body.get("session") or (SESSIONS[0] if SESSIONS else "")
    name = body.get("name") or "Cliente Balcão"
    phone = body.get("phone") or ""
    service = body.get("service")
    note = body.get("note")
    if not slot:
        return jsonify({"error": "Horario/slot obrigatorio"}), 400
    cid = f"{re.sub(r'[^0-9]', '', phone)}@c.us" if phone else f"balcao-{int(time.time())}@c.us"
    res = create_booking(session, cid, slot, service=service, note=note)
    if res.get("ok"):
        with db() as c:
            c.execute("UPDATE bookings SET name=?, phone=? WHERE id=?", (name, phone, res.get("id")))
    return jsonify(res)


@app.get("/api/kb")
def api_kb():
    session = _req_session() or (SESSIONS[0] if SESSIONS else "")
    return jsonify({"kb": load_kb(session), "path": kb_path_for(session), "session": session})


@app.get("/api/slots")
def api_slots():
    days = int(request.args.get("days", BOOKING_DAYS))
    session = _req_session() or None
    return jsonify([{"slot": s, "label": slot_label(s)}
                    for s in free_slots(load_kb(session), session, days=days)])


@app.get("/api/bookings")
def api_bookings():
    session = _req_session()
    with db() as c:
        if session:
            rows = c.execute("SELECT * FROM bookings WHERE session=? ORDER BY slot ASC",
                             (session,)).fetchall()
        else:
            rows = c.execute("SELECT * FROM bookings ORDER BY slot ASC").fetchall()
    return jsonify([{k: r[k] for k in r.keys()} for r in rows])


@app.post("/api/bookings")
def api_booking_create():
    body = request.get_json(silent=True) or {}
    cid, slot = body.get("conversation_id"), body.get("slot")
    if not cid or not slot:
        return jsonify({"error": "conversation_id e slot obrigatorios"}), 400
    session, cid = resolve_conv(cid, body.get("session") or _req_session())
    if slot not in free_slots(load_kb(session), session, days=BOOKING_DAYS + 1):
        return jsonify({"error": "horario fora da agenda ou indisponivel"}), 400
    r = create_booking(session, cid, slot, body.get("service"), body.get("note"))
    return (jsonify(r), 200) if r.get("ok") else (jsonify(r), 409)


@app.post("/api/bookings/<int:bid>/cancel")
def api_booking_cancel(bid):
    session = request.args.get("session") or request.json.get("session") if request.is_json else None
    with db() as c:
        if session:
            c.execute("UPDATE bookings SET status='cancelado', updated_at=? WHERE id=? AND session=?",
                      (now_iso(), bid, session))
        else:
            c.execute("UPDATE bookings SET status='cancelado', updated_at=? WHERE id=?",
                      (now_iso(), bid))
    return jsonify({"ok": True})


@app.get("/api/bot/test/<path:cid>")
def api_bot_test(cid):
    """Dry-run do bot (nao envia, nao consome a mensagem).

    ?live=1 executa com envio real, mas SOMENTE se o cid estiver em
    ATEND_BOT_ALLOWLIST (protecao contra disparo acidental a clientes).
    """
    live = request.args.get("live") in ("1", "true", "yes")
    session, cid = resolve_conv(cid, _req_session())
    if live:
        if not bot_allowed(session, cid):
            return jsonify({"cid": cid, "session": session, "action": "blocked",
                            "reason": "live exige cid em ATEND_BOT_ALLOWLIST"}), 403
        return jsonify(bot_process_conversation(session, cid, "live", send=True, advance=False))
    return jsonify(bot_process_conversation(session, cid, "dry", send=False, advance=False))


@app.post("/webhook/waha/<token>")
def webhook(token):
    if WEBHOOK_TOKEN and token != WEBHOOK_TOKEN:
        return jsonify({"error": "token invalido"}), 403
    ev = request.get_json(silent=True) or {}
    session = ev.get("session") or (SESSIONS[0] if SESSIONS else "")
    kind = ev.get("event", "unknown")
    notify = None
    bot_jobs = []
    with db() as c:
        c.execute("INSERT INTO events (received_at, event, session, payload) VALUES (?,?,?,?)",
                  (now_iso(), kind, session, json.dumps(ev)[:200000]))
        p = ev.get("payload") or {}
        if kind in ("message", "message.any", "messages.upsert"):
            # Handle WAHA messages.upsert format (nested data.messages)
            messages = []
            if kind == "messages.upsert":
                data = p.get("data") or {}
                for msg in data.get("messages") or []:
                    key = msg.get("key") or {}
                    message = msg.get("message") or {}
                    # Extract body from message.conversation or other types
                    body = message.get("conversation") or message.get("extendedTextMessage", {}).get("text") or ""
                    messages.append({
                        "id": key.get("id"),
                        "from": key.get("remoteJid"),
                        "fromMe": key.get("fromMe", False),
                        "body": body,
                        "hasMedia": "imageMessage" in message or "videoMessage" in message or "documentMessage" in message or "audioMessage" in message,
                        "media": message.get("imageMessage") or message.get("videoMessage") or message.get("documentMessage") or message.get("audioMessage") or {},
                        "timestamp": msg.get("messageTimestamp"),
                        "pushName": msg.get("pushName")
                    })
            else:
                messages = [p]

            for msg in messages:
                cid = str(msg.get("from") or "")
                if not cid:
                    print(json.dumps({"t": now_iso(), "webhook": "skip_no_cid", "msg": msg}), flush=True)
                    continue
                msg_id = str(msg.get("id") or "")
                # Garantir timestamp válido (fallback para agora se ausente/0)
                if not msg.get("timestamp") or int(msg.get("timestamp") or 0) == 0:
                    msg["timestamp"] = int(time.time())
                print(json.dumps({"t": now_iso(), "webhook": "processing", "msg_id": msg_id, "session": session, "cid": cid}), flush=True)
                existed = message_exists(c, msg_id)
                if existed:
                    continue
                upsert_conversation(c, {"id": cid, "lastMessage": msg}, session)
                upsert_message(c, msg, session, cid)
                if (not existed) and (not msg.get("fromMe")):
                    c.execute("UPDATE conversations SET unread=COALESCE(unread,0)+1 "
                              "WHERE session=? AND id=?", (session, cid))
                    body = msg.get("body") or ("[midia]" if msg.get("hasMedia") else "")
                    notify = (session, cid, {"last": body, "count": 1})
                    # Agenda processamento sincrono do bot APOS fechar a conexao do webhook
                    # apenas se o robo estiver ativado e o contato for autorizado
                    if _bot_effective_mode() != "off" and bot_allowed(session, cid):
                        bot_jobs.append((session, cid, msg_id))
        elif kind == "message.ack":
            for mid in (p.get("ids") or []):
                c.execute("UPDATE messages SET ack=? WHERE id=?", (int(p.get("ack") or 0), str(mid)))
    if notify and NOTIFY_ENABLED:
        try:
            maybe_notify(*notify)
        except Exception as e:
            STATE["last_error"] = f"notify(webhook): {type(e).__name__}: {e}"
    # Processa bot_jobs síncronamente APÓS fechar a conexão do DB,
    # evitando deadlock SQLite (duas escritas concorrentes).
    for job_session, job_cid, job_msg_id in bot_jobs:
        try:
            bot_process_conversation(job_session, job_cid, "live", send=True, advance=True)
        except Exception as e:
            print(json.dumps({"t": now_iso(), "webhook": "sync_process_error", "msg_id": job_msg_id, "session": job_session, "cid": job_cid, "err": str(e)}), flush=True)
            STATE["last_error"] = f"sync_process_error: {type(e).__name__}: {e}"
    return jsonify({"ok": True})


@app.get("/api/events/stream")
def api_events_stream():
    """Endpoint SSE para eventos em tempo real (LabPulse monitora este fluxo)."""
    def event_stream():
        q = queue.Queue()
        with SSE_LOCK:
            SSE_CLIENTS.add(q)
        try:
            while True:
                evt = q.get()
                yield f"data: {evt}\n\n"
        except GeneratorExit:
            with SSE_LOCK:
                SSE_CLIENTS.discard(q)

    return Response(event_stream(), mimetype="text/event-stream")


if __name__ == "__main__":
    init_db()
    threading.Thread(target=poller_loop, daemon=True).start()
    threading.Thread(target=bot_loop, daemon=True).start()
    print(json.dumps({"t": now_iso(), "start": "idt-atendimento", "port": PORT,
                      "sessions": SESSIONS, "db": str(DB_PATH),
                      "kb": KB_PATH or None, "bot_mode": BOT_MODE,
                      "allowlist": sorted(BOT_ALLOWLIST)}), flush=True)
    app.run(host=BIND, port=PORT, threaded=True)
