#!/usr/bin/env python3
# registro: 2026-11-16 | autor: opencode | modelo: big-pickle
"""Merge de bancos do IDT Atendimento (multi-numero) num unico DB.

Objetivo: uma instancia passa a monitorar varios numeros de WhatsApp. Cada banco
de origem ja carrega a coluna `session` em conversations/messages/bookings, entao
o merge e um INSERT OR IGNORE sobre a PK composta (session, id).

Garantias:
  - Nao escreve nos bancos de origem (apenas leitura via ATTACH).
  - Reutiliza a migracao do proprio app.py (schema composite) para nao haver drift.
  - Idempotente: rodar de novo nao duplica (INSERT OR IGNORE).
  - Valida contagens e aborta se houver colisao inesperada de (session,id).

Uso:
  python3 scripts/merge_atendimento_dbs.py \
      --out /mnt/storage/lab/data/idt-atendimento-unificado \
      --main /mnt/storage/lab/data/idt-atendimento/atendimento.db \
      --extra /mnt/storage/lab/data/idt-atendimento-sebastiancom/atendimento.db
"""
import argparse
import importlib.util
import shutil
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
APP_PY = REPO / "app.py"

CONV_COLS = [
    "session", "id", "name", "is_group", "mode", "last_message_at",
    "last_message_preview", "last_message_id", "unread", "updated_at",
    "last_notified_at", "muted", "bot_last_reply_at", "bot_last_inbound_id",
    "bot_replies_day", "bot_replies_count", "bot_last_reply", "bot_last_error",
]
MSG_COLS = [
    "id", "conversation_id", "session", "from_me", "sender", "body",
    "has_media", "media_type", "timestamp", "ack", "created_at",
]
BOOK_COLS = [
    "conversation_id", "session", "name", "phone", "service", "slot",
    "status", "note", "created_at", "updated_at",
]


def load_app(out_dir):
    """Importa app.py com ATEND_DATA apontando para o diretorio de saida."""
    import os
    os.environ["ATEND_DATA"] = str(out_dir)
    spec = importlib.util.spec_from_file_location("atend_app", str(APP_PY))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def counts(path):
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        out = {}
        for t in ("conversations", "messages", "bookings"):
            try:
                out[t] = con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
            except sqlite3.OperationalError:
                out[t] = 0
        out["by_session"] = dict(con.execute(
            "SELECT session, count(*) FROM conversations GROUP BY session").fetchall())
        return out
    finally:
        con.close()


def insert_from(con, table, cols):
    ph = ",".join("?" for _ in cols)
    collist = ",".join(cols)
    cur = con.execute(
        f"INSERT OR IGNORE INTO {table} ({collist}) "
        f"SELECT {collist} FROM src.{table}")
    return cur.rowcount


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, help="diretorio de saida (recebe atendimento.db)")
    ap.add_argument("--main", required=True, help="banco principal (base do merge)")
    ap.add_argument("--extra", action="append", default=[], help="banco(s) adicional(is)")
    args = ap.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_db = out_dir / "atendimento.db"

    for src in [args.main] + args.extra:
        if not Path(src).exists():
            sys.exit(f"ERRO: banco nao encontrado: {src}")

    before = {p: counts(p) for p in [args.main] + args.extra}
    print("== origem ==")
    for p, c in before.items():
        print(f"  {p}: {c['conversations']} convs, {c['messages']} msgs, "
              f"{c['bookings']} bookings {c['by_session']}")

    shutil.copy2(args.main, out_db)
    print(f"== copiado {args.main} -> {out_db} ==")

    # migra o banco de saida para o schema composite reutilizando o app
    app = load_app(out_dir)
    app.init_db()

    con = sqlite3.connect(out_db)
    try:
        added = {"conversations": 0, "messages": 0, "bookings": 0}
        for extra in args.extra:
            con.execute("ATTACH DATABASE ? AS src", (str(Path(extra).resolve()),))
            added["conversations"] += insert_from(con, "conversations", CONV_COLS)
            added["messages"] += insert_from(con, "messages", MSG_COLS)
            added["bookings"] += insert_from(con, "bookings", BOOK_COLS)
            con.commit()
            con.execute("DETACH DATABASE src")
            print(f"== merge de {extra}: +{added} ==")
        con.commit()
    finally:
        con.close()

    after = counts(out_db)
    exp_conv = before[args.main]["conversations"] + sum(before[p]["conversations"] for p in args.extra)
    exp_msg = before[args.main]["messages"] + sum(before[p]["messages"] for p in args.extra)
    print("== resultado ==")
    print(f"  {out_db}: {after['conversations']} convs, {after['messages']} msgs, "
          f"{after['bookings']} bookings {after['by_session']}")

    ok = (after["conversations"] == exp_conv and after["messages"] == exp_msg)
    # colisao inesperada: (session,id) duplicado seria impossivel (PK); msg id unico
    if not ok:
        sys.exit(f"ERRO: contagens inesperadas (convs {after['conversations']}/{exp_conv}, "
                 f"msgs {after['messages']}/{exp_msg})")
    print("== OK: contagens batem, sem colisoes ==")


if __name__ == "__main__":
    main()
