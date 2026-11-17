#!/usr/bin/env python3
import os, sys, json, urllib.request

sys.path.insert(0, '.')
from app import tg_send

msg = """<b>🤖 IDT-Atendimento — Status & Acessos</b>

<b>🌐 Web UI (Dashboard)</b>
• Local: <code>http://127.0.0.1:8138/?token=SEU_TOKEN</code>
• Tailscale: <code>http://100.104.13.42:8138/?token=SEU_TOKEN</code>
• Ver todas conversas, histórico, assumir/devolver bot, enviar manual

<b>📱 LabPulse Android v1.2.2</b>
• APK entregue no Telegram (versão anterior)
• Abas: Conversas, Automações, Docker, Orca, Downloads, Mídia
• Launcher 1-toque p/ 6 apps, monitor acervo, assistente IA
• Notificação push no handoff

<b>🤖 Bot sebastiancom (cli-002-sebastiancom)</b>
• Sessão WAHA: WORKING (porta 3003)
• Modo: bot automático + handoff por validação KB
• Respostas: 2-3 frases, tom WhatsApp natural
• Multimídia: áudio/foto/vídeo → auto/best-vision
• Allowlist: 5521988743980@c.us

<b>📊 APIs úteis</b>
• <code>GET /api/conversations</code> — lista todas
• <code>GET /api/conversation/{cid}</code> — histórico completo
• <code>GET /api/bot/test/{cid}?session=cli-002-sebastiancom</code> — dry-run
• <code>GET /api/bot/test/{cid}?session=cli-002-sebastiancom&live=1</code> — envio real

<b>🧠 IA Local Verticalizada — Opções</b>
• <b>OmniRoute (atual):</b> kilo-auto — menos de 2s (cloud) — System prompt + KB
• <b>Ollama (local CPU):</b> Qwen 2.5 7B — 8-30s — Prompt + KB contexto
• <b>Ollama (GPU 12GB+):</b> Nemotron 3B — 1-3s — Prompt + KB contexto
• <b>llama.cpp (CPU):</b> GGUF 4-bit — 3-10s — Leve, CPU only
• <b>Fine-tune LoRA:</b> Llama 3.1 8B — menos de 1s (GPU) — Treino p/ domínio

<b>⚠️ 3-5 min é MUITO lento</b> — UX WhatsApp exige menos de 5s (ideal menos de 2s).
<code>Recomendação: mantém OmniRoute cloud ou sobe Ollama com GPU.</code>

<b>🔧 Próximos passos sugeridos</b>
1. <code>detect_human_intent()</code> — cliente pede atendente → handoff imediato
2. Script <code>onboard_client.sh</code> — novo cliente em 1 comando
3. <code>config.json</code> por cliente (model, tokens, tone, handoff triggers)
4. Dedup poller + webhook (message_id)
5. RLS/tenant_id nas tabelas p/ multi-tenant real

<b>📂 KB & Config sebastiancom</b>
• <code>/mnt/storage/client-bots/clients/sebastiancom/kb.json</code>
• <code>/mnt/storage/client-bots/clients/sebastiancom/config.json</code> (criar)"""

buttons = [
    [{"text": "🌐 Web UI (Tailscale)", "url": "http://100.104.13.42:8138/?token=IDT_ATEND_TOKEN"}],
    [{"text": "📊 Ver Conversas API", "url": "http://100.104.13.42:8138/api/conversations?token=IDT_ATEND_TOKEN"}],
    [{"text": "🤖 Test Bot (dry)", "url": "http://100.104.13.42:8138/api/bot/test/5521988743980@c.us?session=cli-002-sebastiancom"}],
    [{"text": "📱 LabPulse APK", "url": "https://github.com/IDT-Lab/LabPulse/releases"}],
]

result = tg_send(msg, buttons=buttons)
print(json.dumps(result, indent=2, ensure_ascii=False))