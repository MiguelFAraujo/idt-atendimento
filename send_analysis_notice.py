#!/usr/bin/env python3
import os, sys, json
sys.path.insert(0, '/home/miguel/services/idt-atendimento')
from app import tg_send

# Enviar aviso dos arquivos de análise criados
msg = """<b>📄 Análises Completas Salvas</b>

Dois documentos detalhados foram criados no workspace:

<b>1. Análise SebastianCom Completa</b>
<code>/home/miguel/.gemini/antigravity-cli/brain/dcd880c8-fd6d-4546-b23d-f1eddb620753/analise-sebastiancom-completa.md</code>
• Arquitetura atual (poller + webhook + ARBITER)
• KB e config validados
• Comparativo IA local vs cloud (tempos reais)
• Próximos passos priorizados com código

<b>2. Verticalização por Cliente — Estratégia</b>
<code>/home/miguel/.gemini/antigravity-cli/brain/dcd880c8-fd6d-4546-b23d-f1eddb620753/verticalizacao-atendimento-por-cliente.md</code>
• Como tornar bot especialista por domínio
• Few-shot com conversas reais (segredo do "natural")
• config.json padronizado multi-tenant
• LabPulse: o que tem, o que falta
• Plano 4 semanas + custos

<b>🎯 Ação Imediata Recomendada:</b>
Implementar <code>onboard_client.sh</code> + <code>detect_human_intent()</code>
→ Desbloqueia escala real do produto IDT-Atendimento"""

buttons = [
    [{"text": "📊 Ver Conversas (API)", "url": "http://100.104.13.42:8138/api/conversations?token=IDT_ATEND_TOKEN"}],
    [{"text": "🤖 Testar Bot (dry-run)", "url": "http://100.104.13.42:8138/api/bot/test/5521988743980@c.us?session=cli-002-sebastiancom"}],
    [{"text": "🌐 Dashboard Tailscale", "url": "http://100.104.13.42:8138/?token=IDT_ATEND_TOKEN"}],
]

result = tg_send(msg, buttons=buttons)
print(json.dumps(result, indent=2, ensure_ascii=False))