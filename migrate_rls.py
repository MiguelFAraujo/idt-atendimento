#!/usr/bin/env python3
"""
RLS Migration — Add tenant_id to conversations and messages tables for multi-tenancy.
Run once after creating onboard_client.sh to prepare the database.
"""

import sqlite3
import sys
from pathlib import Path

DB_PATH = Path("/mnt/storage/lab/data/idt-atendimento/atendimento.db")

def migrate():
    print(f"🔧 Migrating database: {DB_PATH}")
    
    with sqlite3.connect(DB_PATH, timeout=30) as conn:
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        
        # Check current schema
        conv_cols = {r["name"] for r in c.execute("PRAGMA table_info(conversations)")}
        msg_cols = {r["name"] for r in c.execute("PRAGMA table_info(messages)")}
        
        print(f"📋 conversations columns: {sorted(conv_cols)}")
        print(f"📋 messages columns: {sorted(msg_cols)}")
        
        # Add tenant_id to conversations
        if "tenant_id" not in conv_cols:
            print("➕ Adding tenant_id to conversations...")
            c.execute("ALTER TABLE conversations ADD COLUMN tenant_id TEXT")
            c.execute("CREATE INDEX IF NOT EXISTS idx_conv_tenant ON conversations(tenant_id)")
            print("✅ tenant_id added to conversations")
        else:
            print("✅ tenant_id already exists in conversations")
        
        # Add tenant_id to messages
        if "tenant_id" not in msg_cols:
            print("➕ Adding tenant_id to messages...")
            c.execute("ALTER TABLE messages ADD COLUMN tenant_id TEXT")
            c.execute("CREATE INDEX IF NOT EXISTS idx_msg_tenant ON messages(tenant_id)")
            print("✅ tenant_id added to messages")
        else:
            print("✅ tenant_id already exists in messages")
        
        # Populate tenant_id for existing sebastiancom data
        print("🔄 Populating tenant_id for existing data...")
        c.execute("""
            UPDATE conversations 
            SET tenant_id = 'sebastiancom' 
            WHERE tenant_id IS NULL AND session = 'cli-002-sebastiancom'
        """)
        conv_updated = c.rowcount
        
        c.execute("""
            UPDATE messages 
            SET tenant_id = 'sebastiancom' 
            WHERE tenant_id IS NULL AND session = 'cli-002-sebastiancom'
        """)
        msg_updated = c.rowcount
        
        print(f"✅ Updated {conv_updated} conversations, {msg_updated} messages for sebastiancom")
        
        # Verify
        count_conv = c.execute("SELECT COUNT(*) FROM conversations WHERE tenant_id = 'sebastiancom'").fetchone()[0]
        count_msg = c.execute("SELECT COUNT(*) FROM messages WHERE tenant_id = 'sebastiancom'").fetchone()[0]
        print(f"📊 Verified: {count_conv} conversations, {count_msg} messages with tenant_id='sebastiancom'")
        
        conn.commit()
    
    print("✅ RLS Migration complete!")

if __name__ == "__main__":
    migrate()