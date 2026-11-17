> Registro: 2026-11-14 · Autor: opencode · Modelo: big-pickle
> Estado: ativo
> Origem: decisão do operador (14/11/2026) pela Opção B — "app próprio sobre WAHA", "queremos algo nosso".
> Continua: F3 (bot IA com KB de serviços via OmniRoute, allowlist + dry-run) e F4 (agenda).

# IDT Atendimento

App **próprio** de atendimento WhatsApp do IDT-Lab, sobre o **WAHA** já existente. Sem
plataforma externa (nada de Chatwoot). É o produto descrito pelo operador: espelho das
conversas, handoff humano (✓ assumir / ✕ devolver ao bot), preparado para a aba chatbot
e o menu de serviços.

## Estado

### F1 concluído

| Item | Prova |
|------|-------|
| Backend no ar (systemd user) | `idt-atendimento.service` active; ~21–33 MB de RAM |
| Espelho de conversas | 34 conversas e 560+ mensagens reais ingeridas da sessão `idt-lab-wpp` |
| Interface | desktop 1366×768 e celular 390×844, **0 erros de console** |
| Handoff ✓/✕ | `POST /api/conversations/<id>/mode` persiste `bot`↔`humano` |
| Ingestão por webhook | `POST /webhook/waha/<token>` grava evento (token errado → 403) |
| Acesso remoto | responde em `http://100.104.13.42:8138` (Tailscale) |
| Auth | token obrigatório (sem token → 401) |
| Envio real | `POST /api/sendText` (session no corpo) — enviado `3EB06D9060BE5DC4F68756` |

### F2 concluído (notificação ao dono + ✓/✕)

Mensagem **recebida nova** → incrementa `unread` → notifica o dono no **Telegram** com botões
de URL: **✓ Assumir** (pausa o bot), **✕ Deixar no bot** e **Abrir conversa**.

| Item | Prova |
|------|-------|
| Detecção de inbound novo | caminho **poller** real: mensagem do WAHA re-detectada → `poller new=1` |
| Notificação Telegram | `notify: sent`, `message_id` 1849 (webhook) e 1850 (poller) |
| Botões ✓/✕ (URL) | `GET /assumir/<id>` → 200 e `mode=humano`; `GET /bot/<id>` → 200 e `mode=bot` |
| Silêncio após assumir | em `humano`, nova mensagem **não** notifica |
| Auth nas rotas | sem token → **401** |
| Cooldown | 1 aviso por conversa a cada `ATEND_NOTIFY_COOLDOWN` s (padrão 120) |
| Grupos | **não** notificam por padrão (`ATEND_NOTIFY_GROUPS=0`) |
| Warmup | 1º ciclo após restart não notifica (evita rajada) |

> Nota: botões são de **URL** (não callback) para não consumir os `getUpdates` do bot do
> Hermes, que é compartilhado.

## Arquitetura

```
WhatsApp → WAHA (127.0.0.1:3003, sessão por cliente)
             │  poller (chats/overview + chats/{id}/messages, 20 s)
             │  e/ou webhook POST /webhook/waha/<token>
             ▼
        idt-atendimento (Flask + SQLite)  ← este serviço
             │  espelho + modo bot/humano + envio (sendText)
             ▼
        App do dono (navegador/PWA) em :8138
```

Banco: `/mnt/storage/lab/data/idt-atendimento/atendimento.db`
(conversations, messages, events, settings).

## Rodar

```bash
systemctl --user status idt-atendimento.service
systemctl --user restart idt-atendimento.service
journalctl --user -u idt-atendimento.service -f
```

## Acesso

- Local: `http://127.0.0.1:8138/?token=<ATEND_TOKEN>`
- Tailscale: `http://100.104.13.42:8138/?token=<ATEND_TOKEN>`
- Token em `/home/miguel/.config/idt-atendimento/env` (chmod 600).

## API

| Método | Rota | Função |
|--------|------|--------|
| GET | `/health` | healthcheck (sem auth) |
| GET | `/api/conversations?q=` | lista conversas |
| GET | `/api/conversations/<id>` | conversa + mensagens |
| POST | `/api/conversations/<id>/mode` | `{"mode":"bot"\|"humano"}` (✓/✕) |
| POST | `/api/conversations/<id>/read` | marca lida |
| POST | `/api/conversations/<id>/send` | `{"text":"..."}` envia pelo WAHA (`POST /api/sendText`) |
| GET | `/assumir/<id>?token=` | botão ✓ do Telegram (pausa o bot na conversa) |
| GET | `/bot/<id>?token=` | botão ✕ do Telegram (devolve ao bot) |
| POST | `/webhook/waha/<token>` | ingestão de eventos WAHA |

## Próximos passos (F3+)

1. **F3 — bot IA**: worker que lê conversas em modo `bot`, consulta o OmniRoute com a KB de
   serviços do cliente (`client-bots/clients/<slug>/kb.json`) e responde pelo WAHA, com
   allowlist e **dry-run** antes de ligar de verdade.
2. **F4 — agenda**: disponibilidade determinística (Google Calendar via n8n ou Cal.com) + confirmação.
3. **F5 — piloto 1 cliente** (ex.: SebastianCom, sessão WAHA já WORKING) por 30 dias.
4. **Multi-sessão**: hoje 1 instância monitora 1 sessão (`ATEND_SESSIONS` aceita lista, mas a PK
   de `conversations` é só `id`). Antes de monitorar 2 sessões com chat ids iguais, migrar a chave
   para `(session, id)`.

## Riscos

- **Ban**: WAHA é não-oficial. QR na conta do cliente, cooldown/cap, nunca disparo em massa.
- **LGPD**: mensagens são dado pessoal — retenção definida e IA com provedor zero-retention.
- **Webhook x restart**: habilitar webhook via `PUT /api/sessions/{s}` pode reiniciar a sessão;
  hoje usamos **poller** justamente para não tocar na sessão de produção.
- **Multi-sessão**: PK de `conversations` é só `id` (sem `session`) — colisão possível se duas
  sessões tiverem o mesmo chat id. Ver item 4 acima.

## F3 — IA de resposta (KB + fallback)

O bot de atendimento (porta 8138; piloto na porta 8147) responde usando a base de conhecimento do cliente (`KB` JSON) com provedor zero-retention via OmniRoute:

- `build_system_prompt` monta o prompt do sistema a partir da KB.
- `omni_chat` chama o LLM com fallback ordenado: `kilo-free` (primário) → `poll/openai-fast` → `auto/best-chat` → `lfm2-lab:latest` (último recurso).
- `OMNI_MAX_TOKENS=900`; `finish_reason=length` é tratado como falha.
- `validate_reply` checa a resposta antes de enviar; falha de validação vira handoff (não envia ao vivo sem aprovação).
- `bot_process_conversation` roda em dry-run ou live; modo gravado em `bot_mode`.
- Live comprovado com `sent_id=3EB0F236A40BE081ADED46` no número `5521988743980@c.us`.
- `downloadMedia=false` obrigatório no poller do WAHA; `?live=1` só envia se `cid` em `ATEND_BOT_ALLOWLIST` (senão 403, não envia).
- Nunca enviar live sem aprovação e allowlist (chats são clientes reais).

## F4 — Agenda de horários

- `kb_open_intervals`, `free_slots`, `slot_label` calculam disponibilidade determinística.
- `detect_booking_intent` + `parse_slot_choice` interpretam a escolha do cliente.
- `create_booking` cria o agendamento: 49 slots, cria 200, duplicado 400, inválido 400, lista 1, slots 49→48, cancelar → 49.

## F5 — Piloto SebastianCom (cli-002-sebastiancom)

- Serviço `idt-atendimento-sebastiancom.service` porta 8147, sessão WAHA `cli-002-sebastiancom` WORKING.
- KB carregada, `bot_mode=dry` (não envia ao vivo sem aprovação).
- 100 conversas / 1537 mensagens ingeridas pelo poller (`cycles>0`).
- `downloadMedia=false` aplicado em `poll_once()`; `waha_send_text()` helper.
- Mensagens de teste da conversa `5521998871811@c.us` apagadas localmente (DELETE removeu 7 mensagens, 0 restantes).
- Booking id=1 já ausente (0 linhas).
- Ambos os serviços confirmados `active` via `systemctl --user is-active`.
