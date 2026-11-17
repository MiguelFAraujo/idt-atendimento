#!/usr/bin/env bash
# onboard_client.sh — Multi-tenant client onboarding for IDT Atendimento
# Uso: ./onboard_client.sh "nome" "Display Name" "owner_number" "vertical" [instance_name]
# Exemplo: ./onboard_client.sh "minhaloja" "Minha Loja" "5521999999999" "assistencia_tecnica" "cli-003-minhaloja"

set -euo pipefail

CLIENT_NAME="${1:-}"
DISPLAY_NAME="${2:-}"
OWNER_NUMBER="${3:-}"
VERTICAL="${4:-}"
INSTANCE_NAME="${5:-cli-003-${CLIENT_NAME}}"

if [[ -z "$CLIENT_NAME" || -z "$DISPLAY_NAME" || -z "$OWNER_NUMBER" || -z "$VERTICAL" ]]; then
    echo "Uso: $0 \"nome\" \"Display Name\" \"owner_number\" \"vertical\" [instance_name]"
    echo "Verticals: assistencia_tecnica, clinica, imobiliaria, advocacia, petshop, generico"
    exit 1
fi

BASE_DIR="/mnt/storage/client-bots/clients/${CLIENT_NAME}"
LEADS_DIR="/mnt/storage/client-bots/leads"
CONFIG_FILE="${BASE_DIR}/config.json"
KB_FILE="${BASE_DIR}/kb.json"
LEADS_DB="${LEADS_DIR}/${CLIENT_NAME}.db"
PROMPTS_DIR="${BASE_DIR}/prompts"
ENV_FILE="/home/miguel/services/idt-atendimento/.env"

echo "🚀 Onboarding client: ${CLIENT_NAME} (${DISPLAY_NAME})"
echo "   Owner: ${OWNER_NUMBER}"
echo "   Vertical: ${VERTICAL}"
echo "   Instance: ${INSTANCE_NAME}"

# Create directories
mkdir -p "${BASE_DIR}" "${LEADS_DIR}" "${PROMPTS_DIR}"

# Generate config.json per vertical
cat > "${CONFIG_FILE}" <<EOF
{
  "nome": "${CLIENT_NAME}",
  "display": "${DISPLAY_NAME}",
  "instance": "${INSTANCE_NAME}",
  "telegram_bot_token": "",
  "owner_number": "${OWNER_NUMBER}",
  "owner_name": "Dono",
  "knowledge_base": "${KB_FILE}",
  "model": "auto/best-chat",
  "fallback_models": [
    "auto/best-coding",
    "coding",
    "lfm2-lab:latest"
  ],
  "leads_db": "${LEADS_DB}",
  "limits": {
    "cooldown_seconds": 60,
    "daily_cap": 500,
    "quiet_start": 22,
    "quiet_end": 8
  },
  "enabled": true,
  "dry_run": false,
  "vertical": "${VERTICAL}",
  "handoff_triggers": [
    "atendente",
    "humano",
    "pessoa real",
    "reclamar",
    "nao resolve",
    "urgente",
    "falar com atendente",
    "quero humano",
    "transferir"
  ],
  "multimodal": {
    "audio_enabled": true,
    "image_enabled": true,
    "video_enabled": true
  },
  "features": {
    "appointments": true,
    "leads_capture": true,
    "os_integration": false
  }
}
EOF

echo "✅ config.json created at ${CONFIG_FILE}"

# Generate KB template per vertical
case "${VERTICAL}" in
    assistencia_tecnica)
        cat > "${KB_FILE}" <<'KB_EOF'
{
  "negocio": {
    "nome": "DISPLAY_NAME_PLACEHOLDER",
    "descricao": "Assistência técnica especializada em [equipamentos] em [cidade/região]",
    "endereco": "ENDERECO_PLACEHOLDER",
    "whatsapp": "WHATSAPP_PLACEHOLDER",
    "fixo": "FIXO_PLACEHOLDER",
    "email": "EMAIL_PLACEHOLDER",
    "website": "WEBSITE_PLACEHOLDER",
    "experiencia": "Anos de experiência: EXPERIENCIA_PLACEHOLDER"
  },
  "horarios": {
    "segunda_a_sexta": "HORARIO_SEMANA_PLACEHOLDER",
    "sabado": "HORARIO_SABADO_PLACEHOLDER",
    "domingo": "fechado"
  },
  "servicos": [
    {"nome": "Serviço 1", "descricao": "Descrição do serviço 1", "categoria": "hardware"},
    {"nome": "Serviço 2", "descricao": "Descrição do serviço 2", "categoria": "hardware"}
  ],
  "marcas": ["Marca 1", "Marca 2", "Marca 3"],
  "protocolo": {
    "titulo": "Protocolo de Atendimento",
    "passos": [
      "Envie uma mensagem com qual é o equipamento e o que está acontecendo",
      "A equipe indica se é caso de configuração, orçamento ou diagnóstico presencial",
      "O reparo só começa depois de explicar preço, prazo e necessidade de peças",
      "Você recebe o equipamento com recomendações para evitar nova falha"
    ],
    "dicas": "Para agilizar: envie marca/modelo, uma foto se ajudar, o sintoma principal e se o problema começou após queda, chuva, atualização, troca de peça ou falta de energia."
  },
  "faq": [
    {"pergunta": "Vocês fazem orçamento?", "resposta": "Faz sim! Manda marca, modelo e o defeito que a gente orienta. Preço só depois de ver o equipamento."},
    {"pergunta": "Precisa agendar?", "resposta": "Ideal agendar pelo WhatsApp pra garantir horário. Se tiver vaga, atende sem agendar também."},
    {"pergunta": "Qual o prazo de reparo?", "resposta": "Depende do problema e peças. Alguns no dia, outros 2-5 dias úteis. A gente avisa antes."},
    {"pergunta": "Vocês fazem atendimento a domicílio?", "resposta": "Só presencial na loja. Manda os dados que a gente orienta o melhor caminho."},
    {"pergunta": "Aceitam cartão?", "resposta": "Aceita tudo: crédito, débito, Pix e dinheiro."},
    {"pergunta": "Tem garantia?", "resposta": "Tem sim! Todo serviço tem garantia. A gente informa o prazo no orçamento."}
  ],
  "tom": "Fale como se fosse um técnico experiente conversando com cliente. Direto, sem enrolação, mas educado. Use gírias leves ('tá', 'pra', 'aí') quando natural. Demonstre que entende do assunto. Emojis só quando ajudar (🔧💻🖥️). NUNCA comece com 'Olá! Como posso ajudar?' - isso é genérico demais.",
  "restricoes": [
    "Nunca invente preços, prazos ou promoções que não estejam nesta base",
    "Se não houver resposta na base, encaminhe para atendente humano via WhatsApp",
    "Nunca dê diagnóstico definitivo sem ver o equipamento presencialmente",
    "Nunca prometa prazo específico sem confirmação da equipe",
    "Em caso de equipamento com risco (inchamento de bateria, cheiro de queimado, líquido derramado), oriente DESLIGAR IMEDIATAMENTE e trazer à loja",
    "Não responda sobre assuntos não relacionados ao negócio",
    "Não negocie descontos sem autorização do dono",
    "Nunca inclua números de telefone, WhatsApp, e-mail ou links na resposta; o cliente já está falando com a loja pelo WhatsApp.",
    "Não repita o número da loja nem convide o cliente a ligar; siga a conversa por aqui mesmo."
  ],
  "exemplos_reais": [
    {"cliente": "Meu notebook tá travando", "bot": "Pode ser SSD, memória, sistema ou aquecimento. Manda marca/modelo e o sintoma que a gente ajuda."},
    {"cliente": "Quanto custa montar um PC?", "bot": "Monta sim! Conta seu orçamento e o que quer jogar que a gente monta a config ideal."}
  ]
}
KB_EOF
        ;;
    clinica)
        cat > "${KB_FILE}" <<'KB_EOF'
{
  "negocio": {
    "nome": "DISPLAY_NAME_PLACEHOLDER",
    "descricao": "Clínica de [especialidades] em [cidade/região]",
    "endereco": "ENDERECO_PLACEHOLDER",
    "whatsapp": "WHATSAPP_PLACEHOLDER",
    "fixo": "FIXO_PLACEHOLDER",
    "email": "EMAIL_PLACEHOLDER",
    "website": "WEBSITE_PLACEHOLDER",
    "experiencia": "Anos de atuação: EXPERIENCIA_PLACEHOLDER"
  },
  "horarios": {
    "segunda_a_sexta": "HORARIO_SEMANA_PLACEHOLDER",
    "sabado": "HORARIO_SABADO_PLACEHOLDER",
    "domingo": "fechado"
  },
  "servicos": [
    {"nome": "Consulta", "descricao": "Atendimento médico com especialista", "categoria": "consulta"},
    {"nome": "Exames", "descricao": "Coleta e realização de exames laboratoriais", "categoria": "exames"}
  ],
  "marcas": [],
  "protocolo": {
    "titulo": "Protocolo de Agendamento",
    "passos": [
      "Informe a especialidade desejada e se tem convênio",
      "Verificamos disponibilidade e confirmamos horário",
      "Compareça com 15 min de antecedência e documentos",
      "Receba orientações pós-consulta e receitas se houver"
    ],
    "dicas": "Tenha em mãos: carteirinha do convênio, documento com foto e pedido médico se houver."
  },
  "faq": [
    {"pergunta": "Quais convênios aceitam?", "resposta": "Atendemos os principais convênios. Manda o nome do seu plano que a gente confirma."},
    {"pergunta": "Precisa agendar?", "resposta": "Sim, agendamento prévio é obrigatório. Chama aqui que a gente acha o melhor horário."},
    {"pergunta": "Atendem particular?", "resposta": "Atende sim! Valores e disponibilidade pelo WhatsApp."},
    {"pergunta": "Fazem telemedicina?", "resposta": "Disponível para algumas especialidades. Consulte disponibilidade."}
  ],
  "tom": "Fale como recepcionista de clínica: acolhedora, clara, profissional. Use 'você' e 'a gente'. Emojis discretos (🏥📅✅). NUNCA comece com 'Olá! Como posso ajudar?'.",
  "restricoes": [
    "Nunca informe diagnóstico médico pelo WhatsApp",
    "Não confirme cobertura de convênio sem checar no sistema",
    "Não prometa horário sem confirmar disponibilidade real",
    "Em emergência, oriente procurar pronto-socorro imediatamente",
    "Não compartilhe dados de outros pacientes (LGPD)",
    "Não inclua contatos da clínica na resposta; o cliente já está no WhatsApp."
  ],
  "exemplos_reais": [
    {"cliente": "Quero marcar cardiologista", "bot": "Temos vaga pro Dr. Carlos na 5ª feira 14h ou 6ª feira 9h. Qual prefere? Preciso do nome do convênio pra confirmar."},
    {"cliente": "Meu convênio é Unimed", "bot": "Unimed a gente atende! Confirma seu nome completo e CPF que eu travo o horário."}
  ]
}
KB_EOF
        ;;
    imobiliaria)
        cat > "${KB_FILE}" <<'KB_EOF'
{
  "negocio": {
    "nome": "DISPLAY_NAME_PLACEHOLDER",
    "descricao": "Imobiliária especializada em [venda/aluguel/temporada] em [cidade/região]",
    "endereco": "ENDERECO_PLACEHOLDER",
    "whatsapp": "WHATSAPP_PLACEHOLDER",
    "fixo": "FIXO_PLACEHOLDER",
    "email": "EMAIL_PLACEHOLDER",
    "website": "WEBSITE_PLACEHOLDER",
    "experiencia": "Anos no mercado: EXPERIENCIA_PLACEHOLDER"
  },
  "horarios": {
    "segunda_a_sexta": "HORARIO_SEMANA_PLACEHOLDER",
    "sabado": "HORARIO_SABADO_PLACEHOLDER",
    "domingo": "fechado"
  },
  "servicos": [
    {"nome": "Venda de Imóveis", "descricao": "Intermediação na compra e venda", "categoria": "venda"},
    {"nome": "Locação", "descricao": "Aluguel residencial e comercial", "categoria": "locacao"},
    {"nome": "Avaliação", "descricao": "Avaliação de mercado gratuita", "categoria": "avaliacao"}
  ],
  "marcas": [],
  "protocolo": {
    "titulo": "Protocolo de Atendimento Imobiliário",
    "passos": [
      "Cliente informa: tipo (comprar/alugar), região, quartos, valor máximo",
      "Enviamos opções filtradas com fotos e detalhes",
      "Agendamos visita presencial ou virtual",
      "Acompanhamos proposta, documentação e assinatura"
    ],
    "dicas": "Quanto mais detalhes você der (bairro, metragem, garagem, lazer), mais assertiva a busca."
  },
  "faq": [
    {"pergunta": "Como faço para alugar?", "resposta": "Manda: bairro, quartos, valor máximo, garagem e se aceita pet. A gente filtra e manda opções."},
    {"pergunta": "Precisa de fiador?", "resposta": "Depende do imóvel. Tem opções com seguro-fiança, título de capitalização ou fiador. A gente explica caso a caso."},
    {"pergunta": "Vocês cobram taxa de visita?", "resposta": "Não! Visitas são gratuitas e sem compromisso."},
    {"pergunta": "Fazem avaliação do meu imóvel?", "resposta": "Fazemos sim, sem custo. Manda endereço e fotos que a gente passa o valor de mercado."}
  ],
  "tom": "Fale como corretor experiente: direto, conhece a região, passa confiança. Use 'tá', 'pra'. Emojis: 🏠🔑📍. NUNCA 'Olá! Como posso ajudar?'.",
  "restricoes": [
    "Nunca invente valor de imóvel ou condição de pagamento",
    "Não prometa aprovação de crédito sem análise",
    "Não passe endereço exato sem agendar visita (segurança)",
    "Não negocie comissão sem autorização do gestor",
    "Não inclua contatos da imobiliária na resposta."
  ],
  "exemplos_reais": [
    {"cliente": "Procuro 2 quartos no Centro até 2000", "bot": "Temos 3 opções no Centro 2qts 1800-2000 com garagem. Quer ver fotos? Manda 'sim' que passo o link."},
    {"cliente": "Tenho imóvel pra alugar", "bot": "Topa! Manda endereço, fotos e quanto você quer. A gente avalia e cuida do resto."}
  ]
}
KB_EOF
        ;;
    advocacia)
        cat > "${KB_FILE}" <<'KB_EOF'
{
  "negocio": {
    "nome": "DISPLAY_NAME_PLACEHOLDER",
    "descricao": "Escritório de advocacia especializado em [áreas] em [cidade/região]",
    "endereco": "ENDERECO_PLACEHOLDER",
    "whatsapp": "WHATSAPP_PLACEHOLDER",
    "fixo": "FIXO_PLACEHOLDER",
    "email": "EMAIL_PLACEHOLDER",
    "website": "WEBSITE_PLACEHOLDER",
    "experiencia": "Anos de atuação: EXPERIENCIA_PLACEHOLDER"
  },
  "horarios": {
    "segunda_a_sexta": "HORARIO_SEMANA_PLACEHOLDER",
    "sabado": "fechado",
    "domingo": "fechado"
  },
  "servicos": [
    {"nome": "Consulta Jurídica", "descricao": "Análise de caso e orientação inicial", "categoria": "consulta"},
    {"nome": "Ação Judicial", "descricao": "Ingresso e acompanhamento de processos", "categoria": "contencioso"},
    {"nome": "Contratos", "descricao": "Elaboração e revisão de contratos", "categoria": "consultivo"}
  ],
  "marcas": [],
  "protocolo": {
    "titulo": "Protocolo de Atendimento Jurídico",
    "passos": [
      "Cliente resume o caso e objetivo",
      "Advogado indica viabilidade, custos e prazo estimado",
      "Formalização via procuração e honorários",
      "Acompanhamento processual com atualizações periódicas"
    ],
    "dicas": "Tenha documentos em mãos: contratos, notificações, prints, e-mails. Quanto mais completo, melhor a análise."
  },
  "faq": [
    {"pergunta": "Quanto custa uma consulta?", "resposta": "Consulta inicial tem valor fixo. Manda o resumo do caso que a gente passa o valor e agenda."},
    {"pergunta": "Vocês ganham se perder a causa?", "resposta": "Honorários são combinados antes. Tem opção de sucesso (percentual sobre ganho) + fixo reduzido. A gente explica no detalhe."},
    {"pergunta": "Atendem online?", "resposta": "Sim, consulta por videochamada ou WhatsApp. Presencial se preferir."},
    {"pergunta": "Prazo do processo?", "resposta": "Varia muito (meses a anos). A gente estima na consulta conforme o tipo de ação e comarca."}
  ],
  "tom": "Fale como advogado sênior: técnico mas acessível, passa segurança. Evite juridiquês desnecessário. Emojis mínimos (⚖️📄✍️). NUNCA 'Olá! Como posso ajudar?'.",
  "restricoes": [
    "NUNCA dê aconselhamento jurídico definitivo pelo WhatsApp (apenas orientação inicial)",
    "Não prometa resultado, ganho de causa ou prazo garantido",
    "Não oriente a destruir provas ou omitir informações",
    "Não atue em conflito de interesses; verifique antes",
    "Não inclua contatos do escritório na resposta.",
    "Respeite sigilo profissional (não cite outros clientes)."
  ],
  "exemplos_reais": [
    {"cliente": "Fui demitido sem justa causa", "bot": "Tem direito a aviso, férias + 1/3, 13º proporcional, FGTS + 40% e seguro-desemprego. Tem a carta de demissão e holerites? Manda que a gente analisa."},
    {"cliente": "Vizinho faz barulho todo dia", "bot": "Pode ser perturbação do sossego. Junta provas: gravações, testemunhas, reclamações no condomínio. A gente vê medida cabível (extrajudicial ou ação)."}
  ]
}
KB_EOF
        ;;
    petshop)
        cat > "${KB_FILE}" <<'KB_EOF'
{
  "negocio": {
    "nome": "DISPLAY_NAME_PLACEHOLDER",
    "descricao": "Pet shop com banho, tosa, veterinária e produtos em [cidade/região]",
    "endereco": "ENDERECO_PLACEHOLDER",
    "whatsapp": "WHATSAPP_PLACEHOLDER",
    "fixo": "FIXO_PLACEHOLDER",
    "email": "EMAIL_PLACEHOLDER",
    "website": "WEBSITE_PLACEHOLDER",
    "experiencia": "Anos cuidando de pets: EXPERIENCIA_PLACEHOLDER"
  },
  "horarios": {
    "segunda_a_sexta": "HORARIO_SEMANA_PLACEHOLDER",
    "sabado": "HORARIO_SABADO_PLACEHOLDER",
    "domingo": "fechado"
  },
  "servicos": [
    {"nome": "Banho e Tosa", "descricao": "Higiene completa com produtos hipoalergênicos", "categoria": "estetica"},
    {"nome": "Veterinária", "descricao": "Consulta, vacina, exames e cirurgias", "categoria": "clinica"},
    {"nome": "Pet Shop", "descricao": "Ração, acessórios, brinquedos e medicamentos", "categoria": "varejo"}
  ],
  "marcas": ["Premier", "Royal Canin", "Golden", "Farmina", "Bravecto", "NexGard"],
  "protocolo": {
    "titulo": "Protocolo de Atendimento Pet",
    "passos": [
      "Cliente informa: tipo de pet, porte, idade e serviço desejado",
      "Verificamos disponibilidade e agendamos",
      "No dia: traga cartão de vacina e medicações se houver",
      "Retirada com feedback e recomendações"
    ],
    "dicas": "Agende banho/tosa com antecedência (fins de semana lotam). Vacinas: traga carteirinha."
  },
  "faq": [
    {"pergunta": "Qual o valor do banho?", "resposta": "Depende do porte e pelagem. Manda raça/peso que a gente passa o valor na hora."},
    {"pergunta": "Precisa agendar vacina?", "resposta": "Sim, agendamos pra garantir a dose. Manda qual vacina e a idade do pet."},
    {"pergunta": "Fazem tosa higiênica?", "resposta": "Fazemos! Inclui olhos, patas, parte íntima e unha. Valor à parte do banho."},
    {"pergunta": "Atendem emergência?", "resposta": "Horário comercial: sim. Fora do horário: indicamos plantão 24h da região."}
  ],
  "tom": "Fale como quem ama pet: carinhoso, atento, passa confiança. 'Seu doguinho', 'seu gatinho'. Emojis: 🐕🐈🛁💉. NUNCA 'Olá! Como posso ajudar?'.",
  "restricoes": [
    "Nunca indique medicamento ou dose sem aval do veterinário",
    "Não prometa resultado estético específico na tosa",
    "Não atenda emergência grave sem encaminhar pra clínica 24h",
    "Não venda produto controlado sem receita",
    "Não inclua contatos do pet shop na resposta."
  ],
  "exemplos_reais": [
    {"cliente": "Meu shih tzu 3 anos precisa de banho", "bot": "Shih tzu 3 anos banho + tosa higiênica = R$ XX. Tem vaga 5ª feira 10h ou 6ª feira 14h. Confirma?"},
    {"cliente": "Gato 2 meses vacina", "bot": "Gatinho 2 meses: vacina quíntupla + vermífugo. Temos dose. Agenda? Preciso do nome dele."}
  ]
}
KB_EOF
        ;;
    generico|*)
        cat > "${KB_FILE}" <<'KB_EOF'
{
  "negocio": {
    "nome": "DISPLAY_NAME_PLACEHOLDER",
    "descricao": "Descrição do negócio em [cidade/região]",
    "endereco": "ENDERECO_PLACEHOLDER",
    "whatsapp": "WHATSAPP_PLACEHOLDER",
    "fixo": "FIXO_PLACEHOLDER",
    "email": "EMAIL_PLACEHOLDER",
    "website": "WEBSITE_PLACEHOLDER",
    "experiencia": "EXPERIENCIA_PLACEHOLDER"
  },
  "horarios": {
    "segunda_a_sexta": "HORARIO_SEMANA_PLACEHOLDER",
    "sabado": "HORARIO_SABADO_PLACEHOLDER",
    "domingo": "fechado"
  },
  "servicos": [
    {"nome": "Serviço Principal", "descricao": "Descrição", "categoria": "principal"}
  ],
  "marcas": [],
  "protocolo": {
    "titulo": "Protocolo de Atendimento",
    "passos": [
      "Cliente informa o que precisa",
      "A equipe orienta próximos passos",
      "Formalização se necessário",
      "Entrega/Conclusão com orientações"
    ],
    "dicas": "Quanto mais detalhes, melhor o atendimento."
  },
  "faq": [
    {"pergunta": "Como funciona?", "resposta": "Manda sua dúvida que a gente explica direitinho."},
    {"pergunta": "Quanto custa?", "resposta": "Depende do que você precisa. Manda detalhes que a gente passa orçamento."}
  ],
  "tom": "Fale como especialista do seu ramo: direto, natural, passa confiança. Use gírias leves se combinar. Emojis só quando ajudar. NUNCA 'Olá! Como posso ajudar?'.",
  "restricoes": [
    "Nunca invente preços ou prazos",
    "Se não souber, encaminhe para humano",
    "Não inclua contatos na resposta."
  ],
  "exemplos_reais": [
    {"cliente": "Quero saber mais", "bot": "Claro! O que exatamente você procura? Manda detalhes que eu te explico."}
  ]
}
KB_EOF
        ;;
esac

# Replace placeholder with actual display name
sed -i "s/DISPLAY_NAME_PLACEHOLDER/${DISPLAY_NAME//\//\\/}/g" "${KB_FILE}"

echo "✅ kb.json template created at ${KB_FILE} (vertical: ${VERTICAL})"

# Create empty leads database with schema
sqlite3 "${LEADS_DB}" <<'SQL_EOF'
CREATE TABLE IF NOT EXISTS leads (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session TEXT NOT NULL,
  cid TEXT NOT NULL,
  name TEXT,
  phone TEXT,
  source TEXT,
  status TEXT DEFAULT 'novo',
  notes TEXT,
  created_at TEXT DEFAULT (datetime('now')),
  updated_at TEXT DEFAULT (datetime('now')),
  UNIQUE(session, cid)
);
CREATE INDEX IF NOT EXISTS idx_leads_session ON leads(session);
CREATE INDEX IF NOT EXISTS idx_leads_status ON leads(status);
SQL_EOF

echo "✅ leads.db created at ${LEADS_DB}"

# Create few-shot prompts file
cat > "${PROMPTS_DIR}/fewshot.json" <<EOF
{
  "exemplos": [
    {"cliente": "Olá, tudo bem?", "bot": "Oi! Tudo bem. O que você precisa hoje?"},
    {"cliente": "Quanto custa?", "bot": "Depende do que você precisa. Manda detalhes que eu passo o valor."},
    {"cliente": "Quero falar com atendente", "bot": "Claro, vou transferir pra um atendente humano. Já já alguém te atende."}
  ]
}
EOF

echo "✅ fewshot.json created at ${PROMPTS_DIR}/fewshot.json"

# Update .env with KB_MAP and SESSION_LABELS entries
KB_MAP_ENTRY="ATEND_KB_MAP_${CLIENT_NAME^^}=${KB_FILE}"
SESSION_LABEL_ENTRY="ATEND_SESSION_LABELS_${CLIENT_NAME^^}=${DISPLAY_NAME}"

if [[ -f "${ENV_FILE}" ]]; then
    # Check if already exists
    if ! grep -q "ATEND_KB_MAP_${CLIENT_NAME^^}=" "${ENV_FILE}"; then
        echo "${KB_MAP_ENTRY}" >> "${ENV_FILE}"
        echo "✅ Added KB_MAP entry to .env"
    fi
    if ! grep -q "ATEND_SESSION_LABELS_${CLIENT_NAME^^}=" "${ENV_FILE}"; then
        echo "${SESSION_LABEL_ENTRY}" >> "${ENV_FILE}"
        echo "✅ Added SESSION_LABEL entry to .env"
    fi
else
    echo "⚠️  .env not found at ${ENV_FILE}, skipping env update"
fi

# Print summary
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "✅ ONBOARDING COMPLETO: ${CLIENT_NAME}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "📁 Diretório: ${BASE_DIR}"
echo "⚙️  Config:    ${CONFIG_FILE}"
echo "📚 KB:        ${KB_FILE}"
echo "💾 Leads DB:  ${LEADS_DB}"
echo "🎯 Prompts:   ${PROMPTS_DIR}/fewshot.json"
echo ""
echo "📋 PRÓXIMOS PASSOS:"
echo "1. Edite ${KB_FILE} e preencha os PLACEHOLDERS (endereço, WhatsApp, horários, serviços, FAQ)"
echo "2. Edite ${CONFIG_FILE} se quiser ajustar limites, modelo, handoff_triggers"
echo "3. Crie a sessão no WAHA com nome: ${INSTANCE_NAME}"
echo "4. Adicione a sessão no ATEND_SESSIONS do .env (ou reinicie o serviço)"
echo "5. Teste: curl \"http://100.104.13.42:8138/api/bot/test/5521988743980@c.us?session=${INSTANCE_NAME}\""
echo ""
echo "🔄 Para aplicar: reinicie o serviço idt-atendimento (systemctl restart idt-atendimento)"
echo "   ou recarregue config: curl -X POST http://100.104.13.42:8138/api/bot/settings -H 'Content-Type: application/json' -d '{\"mode\":\"live\"}'"