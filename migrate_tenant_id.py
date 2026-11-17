#!/usr/bin/env python3
"""
Migration script to add tenant_id column to conversations and messages tables.
Maps sessions to tenant_ids based on client config files.
"""

import json
import os
import sqlite3
from pathlib import Path

DB_PATH = Path("/mnt/storage/lab/data/idt-atendimento/atendimento.db")
CLIENTS_DIR = Path("/mnt/storage/client-bots/clients")

def get_session_to_tenant_mapping():
    """Build mapping from session name to tenant_id by reading client configs."""
    mapping = {}
    if not CLIENTS_DIR.exists():
        return mapping
    
    for client_dir in CLIENTS_DIR.iterdir():
        if not client_dir.is_dir():
            continue
        config_file = client_dir / "config.json"
        if not config_file.exists():
            continue
        try:
            with open(config_file, 'r', encoding='utf-8') as f:
                config = json.load(f)
            tenant_id = config.get("nome", client_dir.name)
            instance = config.get("instance", "")
            if instance:
                mapping[instance] = tenant_id
            # Also map by client name
            mapping[client_dir.name] = tenant_id
        except Exception as e:
            print(f"Warning: Could not read {config_file}: {e}")
    return mapping

def migrate():
    session_to_tenant = get_session_to_tenant_mapping()
    print(f"Found {len(session_to_tenant)} session->tenant mappings")
    for s, t in session_to_tenant.items():
        print(f"  {s} -> {t}")
    
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        
        # Check if tenant_id column exists in conversations
        cols = {r["name"] for r in c.execute("PRAGMA table_info(conversations)")}
        
        if "tenant_id" not in cols:
            print("Adding tenant_id column to conversations...")
            c.execute("ALTER TABLE conversations ADD COLUMN tenant_id TEXT")
            c.execute("CREATE INDEX IF NOT EXISTS idx_conv_tenant ON conversations(tenant_id)")
        else:
            print("tenant_id column already exists in conversations")
        
        # Check messages table
        cols_msg = {r["name"] for r in c.execute("PRAGMA table_info(messages)")}
        if "tenant_id" not in cols_msg:
            print("Adding tenant_id column to messages...")
            c.execute("ALTER TABLE messages ADD COLUMN tenant_id TEXT")
            c.execute("CREATE INDEX IF NOT EXISTS idx_msg_tenant ON messages(tenant_id)")
        else:
            print("tenant_id column already exists in messages")
        
        # Check bookings table
        cols_book = {r["name"] for r in c.execute("PRAGMA table_info(bookings)")}
        if "tenant_id" not in cols_book:
            print("Adding tenant_id column to bookings...")
            c.execute("ALTER TABLE bookings ADD COLUMN tenant_id TEXT")
            c.execute("CREATE INDEX IF NOT EXISTS idx_book_tenant ON bookings(tenant_id)")
        else:
            print("tenant_id column already exists in bookings")
        
        # Check os_conversation_map table
        cols_os = {r["name"] for r in c.execute("PRAGMA table_info(os_conversation_map)")}
        if "tenant_id" not in cols_os:
            print("Adding tenant_id column to os_conversation_map...")
            c.execute("ALTER TABLE os_conversation_map ADD COLUMN tenant_id TEXT")
        else:
            print("tenant_id column already exists in os_conversation_map")
        
        # Populate tenant_id for existing conversations based on session
        print("\nPopulating tenant_id for existing conversations...")
        for session, tenant_id in session_to_tenant.items():
            result = c.execute(
                "UPDATE conversations SET tenant_id=? WHERE session=? AND (tenant_id IS NULL OR tenant_id='')",
                (tenant_id, session)
            )
            print(f"  {session} -> {tenant_id}: {result.rowcount} rows updated")
        
        # Populate tenant_id for existing messages based on session
        print("\nPopulating tenant_id for existing messages...")
        for session, tenant_id in session_to_tenant.items():
            result = c.execute(
                "UPDATE messages SET tenant_id=? WHERE session=? AND (tenant_id IS NULL OR tenant_id='')",
                (tenant_id, session)
            )
            print(f"  {session} -> {tenant_id}: {result.rowcount} rows updated")
        
        # Populate tenant_id for existing bookings based on session
        print("\nPopulating tenant_id for existing bookings...")
        for session, tenant_id in session_to_tenant.items():
            result = c.execute(
                "UPDATE bookings SET tenant_id=? WHERE session=? AND (tenant_id IS NULL OR tenant_id='')",
                (tenant_id, session)
            )
            print(f"  {session} -> {tenant_id}: {result.rowcount} rows updated")
        
        # Populate tenant_id for os_conversation_map based on session
        print("\nPopulating tenant_id for existing os_conversation_map...")
        for session, tenant_id in session_to_tenant.items():
            result = c.execute(
                "UPDATE os_conversation_map SET tenant_id=? WHERE session=? AND (tenant_id IS NULL OR tenant_id='')",
                (tenant_id, session)
            )
            print(f"  {session} -> {tenant_id}: {result.rowcount} rows updated")
        
        conn.commit()
        print("\n✅ Migration complete!")
        
        # Verify
        print("\nVerification:")
        for table in ["conversations", "messages", "bookings", "os_conversation_map"]:
            result = c.execute(f"SELECT tenant_id, COUNT(*) as cnt FROM {table} GROUP BY tenant_id").fetchall()
            print(f"  {table}:")
            for row in result:
                print(f"    {row['tenant_id'] or 'NULL'}: {row['cnt']}")

if __name__ == "__main__":
    migrate()