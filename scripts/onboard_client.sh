#!/bin/bash
# onboard_client.sh — Cria novo cliente IDT-Atendimento em 1 comando
# Uso: ./onboard_client.sh <slug> "Display Name" <owner_number> <vertical> [telegram_chat_id]

set -euo pipefail

# --- Cores ---
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

log()  { echo -e "${GREEN}[OK]${NC} $*"; }
warn() { echo -e "${YELLOW}[AVISO]${NC} $*"; }
err()  { echo -e "${RED}[ERRO]${NC} $*"; }
info() { echo -e "${BLUE}[INFO]${NC} $*"; }

# --- Validação ---
if [[ $# -lt 4 ]]; then
    echo "Uso: $0 <slug> \"Display Name\" <owner_number> <vertical> [telegram_chat_id]"
    echo ""
    echo "Exemplos:"
    echo "  $0 sebastiancom \"SebastianCom Informática\" 5521988743980 assistencia_tecnica 1008088231"
    echo "  $0 clinica_sorriso \"Clínica Sorriso\" 5511999998888 clinica_odontologia"
    echo "  $0 imobiliaria_abc \"Imobiliária ABC\" 5511888887777 imobiliaria"
    echo ""
    echo "Verticais disponíveis: assistencia_tecnica, clinica_odontologia, imobiliaria, advocacia, generico"
    exit 1
fi

SLUG="$1"
DISPLAY_NAME="$2"
OWNER_NUMBER="$3"
VERTICAL="$4"
TELEGRAM_CHAT="${5:-}"

# Diretórios base
BASE_DIR="/mnt/storage/client-bots/clients"
CLIENT_DIR="$BASE_DIR/$SLUG"
LEADS_DIR="/mnt/storage/client-bots/leads"
PROMPTS_DIR="$CLIENT_DIR/prompts"
ENV_FILE="/home/miguel/.config/idt-atendimento/env"
SESSIONS_FILE="/home/miguel/services/idt-atendimento/.env"

# --- Templates por Vertical ---
get_system_prompt() {
    local v="$1"
    case "$v" in
        assistencia_tecnica)
            cat <<'EOF'
Você é técnico da {empresa} com 25+ anos. Responda no WhatsApp:
- 2-3 frases máx, direto, gírias leves (tá, pra, aí)
- NUNCA: "Olá! Como posso ajudar?", "Estou aqui para ajudar"
- SEMPRE: pergunte equipamento + sintoma se não tiver info
- Emojis só 🔧💻🖥️ quando ajudar
- Não invente preço/prazo. Não dê diagnóstico remoto.
- Se cliente pedir atendente/humano → diga "Chama o pessoal lá que eu passo 👍"
EOF
            ;;
        clinica_odontologia)
            cat <<'EOF'
Você é atendente da {empresa}. Responda no WhatsApp:
- 2-3 frases, acolhedor, profissional
- Pergunte: nome, convênio, sintoma/urgência
- Horários: confirme disponibilidade antes de prometer
- Emergência (dor forte, trauma) → "Vem direto que a gente encaixa 🚨"
- Não diagnostique. Não prometa cobertura de convênio sem checar.
EOF
            ;;
        imobiliaria)
            cat <<'EOF'
Você é corretor da {empresa}. Responda no WhatsApp:
- 2-3 frases, direto, foco em qualificação
- Pergunte: tipo (compra/aluguel), bairro, valor, quartos
- Agende visita só com dados completos
- Não invente preço de imóvel não visitado
- Lead quente → "Te mando opções agora, qual melhor horário pra falar? 🏠"
EOF
            ;;
        advocacia)
            cat <<'EOF'
Você é assistente jurídico da {empresa}. Responda no WhatsApp:
- 2-3 frases, formal mas acessível
- Pergunte: área (cível/trabalhista/família), urgência, docs que tem
- Não dê parecer jurídico. Não prometa resultado.
- Urgência (prazo correndo) → "Manda os docs que o advogado analisa hoje ⚖️"
- LGPD: não peça CPF/docs sensíveis no chat
EOF
            ;;
        *)
            cat <<'EOF'
Você é atendente da {empresa}. Responda no WhatsApp:
- 2-3 frases, direto, educado
- Pergunte o necessário para encaminhar
- Não invente informações. Não prometa o que não controla.
- Se não souber → "Vou verificar com a equipe e te retorno 👍"
EOF
            ;;
    esac
}

get_handoff_triggers() {
    local v="$1"
    case "$v" in
        assistencia_tecnica)
            echo '["atendente", "humano", "pessoa real", "reclamar", "não resolve", "urgente", "problema grave", "falar com alguém"]'
            ;;
        clinica_odontologia)
            echo '["atendente", "humano", "pessoa real", "emergência", "dor forte", "trauma", "convênio não aceito", "reclamar"]'
            ;;
        imobiliaria)
            echo '["atendente", "humano", "pessoa real", "corretor", "visita", "reclamar", "não gostei", "outro imóvel"]'
            ;;
        advocacia)
            echo '["atendente", "humano", "pessoa real", "advogado", "urgente", "prazo", "reclamar", "não entendi"]'
            ;;
        *)
            echo '["atendente", "humano", "pessoa real", "reclamar", "não resolve", "falar com alguém"]'
            ;;
    esac
}

get_multimodal_config() {
    local v="$1"
    case "$v" in
        assistencia_tecnica)
            echo '{"audio": true, "image": true, "video": false, "transcription_model": "whisper-base", "vision_model": "auto/best-vision"}'
            ;;
        clinica_odontologia)
            echo '{"audio": true, "image": true, "video": false, "transcription_model": "whisper-base", "vision_model": "auto/best-vision"}'
            ;;
        imobiliaria)
            echo '{"audio": true, "image": true, "video": true, "transcription_model": "whisper-base", "vision_model": "auto/best-vision"}'
            ;;
        advocacia)
            echo '{"audio": true, "image": true, "video": false, "transcription_model": "whisper-base", "vision_model": "auto/best-vision"}'
            ;;
        *)
            echo '{"audio": true, "image": true, "video": false, "transcription_model": "whisper-base", "vision_model": "auto/best-vision"}'
            ;;
    esac
}

get_kb_template() {
    local v="$1"
    local display="$2"
    case "$v" in
        assistencia_tecnica)
            cat <<EOF
{
  "negocio": {
    "nome": "$display",
    "descricao": "Descreva o negócio aqui",
    "endereco": "Endereço completo",
    "whatsapp": "55XXXXXXXXXXX",
    "fixo": "(XX) XXXX-XXXX",
    "email": "contato@dominio.com",
    "website": "https://site.com",
    "experiencia": "Anos de experiência"
  },
  "horarios": {
    "segunda_a_sexta": "08:30 - 18:00",
    "sabado": "08:30 - 13:00",
    "domingo": "fechado"
  },
  "servicos": [
    {"nome": "Serviço 1", "descricao": "Descrição", "categoria": "categoria"},
    {"nome": "Serviço 2", "descricao": "Descrição", "categoria": "categoria"}
  ],
  "marcas": ["Marca1", "Marca2"],
  "protocolo": {
    "titulo": "Protocolo de Atendimento",
    "passos": [
      "Passo 1",
      "Passo 2",
      "Passo 3",
      "Passo 4"
    ],
    "dicas": "Dicas para agilizar atendimento"
  },
  "faq": [
    {"pergunta": "Pergunta 1?", "resposta": "Resposta 1"},
    {"pergunta": "Pergunta 2?", "resposta": "Resposta 2"}
  ],
  "tom": "Tom de voz personalizado para $display",
  "restricoes": [
    "Nunca invente preços, prazos ou promoções que não estejam nesta base",
    "Se não houver resposta na base, encaminhe para atendente humano",
    "Não inclua contatos na resposta; o cliente já está no WhatsApp"
  ],
  "exemplos_reais": [
    {"cliente": "Exemplo de mensagem do cliente", "bot": "Resposta ideal do bot"}
  ]
}
EOF
            ;;
        *)
            cat <<EOF
{
  "negocio": {
    "nome": "$display",
    "descricao": "Descreva o negócio aqui",
    "endereco": "Endereço completo",
    "whatsapp": "55XXXXXXXXXXX",
    "fixo": "(XX) XXXX-XXXX",
    "email": "contato@dominio.com",
    "website": "https://site.com",
    "experiencia": "Anos de experiência"
  },
  "horarios": {
    "segunda_a_sexta": "09:00 - 18:00",
    "sabado": "fechado",
    "domingo": "fechado"
  },
  "servicos": [
    {"nome": "Serviço Principal", "descricao": "Descrição", "categoria": "principal"}
  ],
  "marcas": [],
  "protocolo": {
    "titulo": "Protocolo de Atendimento",
    "passos": ["Passo 1", "Passo 2", "Passo 3"],
    "dicas": "Dicas para agilizar"
  },
  "faq": [
    {"pergunta": "Como funciona?", "resposta": "Explicação básica"}
  ],
  "tom": "Tom de voz para $display",
  "restricoes": [
    "Nunca invente informações",
    "Encaminhe para humano se não souber"
  ],
  "exemplos_reais": []
}
EOF
            ;;
    esac
}

# --- Execução Principal ---
info "=== Onboarding cliente: $SLUG ($DISPLAY_NAME) ==="
info "Vertical: $VERTICAL | Dono: $OWNER_NUMBER"

# 1. Criar diretórios
mkdir -p "$CLIENT_DIR" "$PROMPTS_DIR" "$LEADS_DIR"
log "Diretórios criados: $CLIENT_DIR"

# 2. Gerar config.json
CONFIG_FILE="$CLIENT_DIR/config.json"
cat > "$CONFIG_FILE" <<EOF
{
  "cliente": "$SLUG",
  "vertical": "$VERTICAL",
  "display": "$DISPLAY_NAME",
  "instance": "client_$SLUG",
  "telegram_bot_token": "",
  "owner_number": "$OWNER_NUMBER",
  "owner_name": "Dono",
  "telegram_chat": "$TELEGRAM_CHAT",
  "knowledge_base": "$CLIENT_DIR/kb.json",
  "prompts_path": "$PROMPTS_DIR/",
  "model": "auto/best-chat",
  "max_tokens": 400,
  "temperature": 0.3,
  "tom": "${VERTICAL}_whatsapp",
  "handoff_triggers": $(get_handoff_triggers "$VERTICAL"),
  "multimodal": $(get_multimodal_config "$VERTICAL"),
  "kb_path": "$CLIENT_DIR/kb.json",
  "leads_db": "$LEADS_DIR/$SLUG.db",
  "limits": {
    "cooldown_seconds": 60,
    "daily_cap": 500,
    "quiet_start": 22,
    "quiet_end": 8
  },
  "features": {
    "agendamento_f4": true,
    "mapos_integration": false,
    "status_os": false,
    "orcamento_rapido": false
  },
  "enabled": true,
  "dry_run": false
}
EOF
log "config.json criado: $CONFIG_FILE"

# 3. Gerar kb.json template
KB_FILE="$CLIENT_DIR/kb.json"
get_kb_template "$VERTICAL" "$DISPLAY_NAME" > "$KB_FILE"
log "kb.json template criado: $KB_FILE"

# 4. Gerar system prompt
SYSTEM_FILE="$PROMPTS_DIR/system.md"
get_system_prompt "$VERTICAL" | sed "s/{empresa}/$DISPLAY_NAME/g" > "$SYSTEM_FILE"
log "system.md criado: $SYSTEM_FILE"

# 5. Gerar few-shot template
FEWSHOT_FILE="$PROMPTS_DIR/fewshot.json"
cat > "$FEWSHOT_FILE" <<EOF
[
  {"cliente": "Oi", "bot": "Oi! Qual equipamento tá com problema e o que ele tá fazendo? 🔧"},
  {"cliente": "Quero agendar", "bot": "Pra agendar preciso saber: qual o equipamento e o defeito? Assim a gente vê o melhor horário 📅"},
  {"cliente": "Quanto custa?", "bot": "Preço só vendo o equipamento. Manda marca, modelo e o que tá acontecendo que a gente orienta 💰"},
  {"cliente": "Falar com atendente", "bot": "Chama o pessoal lá que eu passo 👍"}
]
EOF
log "fewshot.json template criado: $FEWSHOT_FILE"

# 6. Criar leads.db (SQLite vazio com schema)
LEADS_DB="$LEADS_DIR/$SLUG.db"
sqlite3 "$LEADS_DB" <<'SQL'
CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cliente_slug TEXT NOT NULL,
    wa_id TEXT NOT NULL,
    wa_name TEXT,
    primeiro_contato TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    ultimo_contato TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    status TEXT DEFAULT 'novo',
    origem TEXT,
    observacoes TEXT,
    UNIQUE(cliente_slug, wa_id)
);
CREATE INDEX IF NOT EXISTS idx_leads_wa ON leads(wa_id);
CREATE INDEX IF NOT EXISTS idx_leads_status ON leads(status);
SQL
log "leads.db criado com schema: $LEADS_DB"

# 7. Atualizar .env do idt-atendimento (SESSIONS)
if [[ -f "$SESSIONS_FILE" ]]; then
    # Verifica se já existe
    if grep -q "ATEND_SESSIONS=" "$SESSIONS_FILE"; then
        # Adiciona à lista se não estiver lá
        if ! grep -q "client_$SLUG" "$SESSIONS_FILE"; then
            sed -i "s/^ATEND_SESSIONS=.*/&,client_$SLUG/" "$SESSIONS_FILE"
            log "Sessão client_$SLUG adicionada em $SESSIONS_FILE"
        else
            warn "Sessão client_$SLUG já existe em ATEND_SESSIONS"
        fi
    else
        echo "ATEND_SESSIONS=idt-lab-wpp,client_$SLUG" >> "$SESSIONS_FILE"
        log "ATEND_SESSIONS criado em $SESSIONS_FILE"
    fi
else
    warn "Arquivo $SESSIONS_FILE não encontrado — adicione manualmente: ATEND_SESSIONS=idt-lab-wpp,client_$SLUG"
fi

# 8. Instruções finais
echo ""
echo -e "${GREEN}✅ Cliente $SLUG criado com sucesso!${NC}"
echo ""
echo "📁 Estrutura:"
echo "   $CLIENT_DIR/"
echo "   ├── config.json"
echo "   ├── kb.json          ← PREENCHA com dados reais"
echo "   ├── prompts/"
echo "   │   ├── system.md"
echo "   │   └── fewshot.json  ← ADICIONE conversas reais"
echo "   └── leads.db"
echo ""
echo "⚠️  PRÓXIMOS PASSOS OBRIGATÓRIOS:"
echo "   1. Edite $KB_FILE com dados reais do cliente"
echo "   2. Adicione exemplos reais em $FEWSHOT_FILE (mínimo 10)"
echo "   3. Configure WAHA: crie sessão 'client_$SLUG' na porta 3003"
echo "   4. Reinicie idt-atendimento: systemctl restart idt-atendimento (ou docker compose restart)"
echo "   5. Teste: GET /api/bot/test/{cid}?session=client_$SLUG"
echo ""
if [[ -n "$TELEGRAM_CHAT" ]]; then
    echo "📱 Telegram Chat ID configurado: $TELEGRAM_CHAT"
    echo "   Notificações de handoff irão para este chat."
fi
echo ""
echo -e "${BLUE}Vertical: $VERTICAL — triggers de handoff:$(get_handoff_triggers "$VERTICAL" | jq -r '.[]' | paste -sd '|' -)${NC}"