#!/usr/bin/env python3
"""
cortex_engine.py — Módulo Cognitivo Multi-Tenant para Bots de Atendimento
Baseado nos princípios do AI Agent Book (Cap. 3: Memória Contínua, Cap. 2: KV-Cache).

Gerencia o ciclo de vida cognitivo, maturação e sincronização contínua de cada cliente:
  /mnt/storage/client-bots/clients/<client-slug>/cortex/
    ├── cortex.json       (Estado cognitivo, maturidade e regras ativas)
    ├── learnings.jsonl   (Log atômico de aprendizados extraídos)
    └── insights.json     (Inteligência agregada de negócio)
"""

import os
import json
import time
import datetime
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import requests

# Executor assíncrono para extração de aprendizado sem bloquear respostas de chat
CORTEX_POOL = ThreadPoolExecutor(max_workers=3, thread_name_prefix="cortex_worker")

OMNI_URL = os.getenv("ATEND_OMNI_URL", "http://127.0.0.1:20128/v1").rstrip("/")
OMNI_KEY = os.getenv("ATEND_OMNI_KEY", "")
OMNI_MODEL = os.getenv("ATEND_OMNI_MODEL", "best-chat")


def now_iso() -> str:
    return datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=-3))).isoformat()


def get_cortex_dir(client_dir: Path) -> Path:
    c_dir = client_dir / "cortex"
    c_dir.mkdir(parents=True, exist_ok=True)
    return c_dir


def calculate_maturity(data: dict) -> dict:
    """
    Calcula o nível e score de maturidade do bot com base no volume de conversas,
    regras aprendidas, taxa de handoff e qualidade das interações.
    
    Estágios:
      - Nível 0: Cold-Start / Nascimento (0 a 24 pontos)
      - Nível 1: Calibração & Coleta Ativa (25 a 59 pontos)
      - Nível 2: Estabilização & Convergência Contextual (60 a 84 pontos)
      - Nível 3: Autonomia Madura & Evolução Incremental (85 a 100 pontos)
    """
    convs = data.get("conversations_analyzed", 0)
    rules = data.get("learned_rules", [])
    rules_count = len(rules)
    learnings_count = data.get("learnings_count", 0)
    handoff_rate = data.get("handoff_rate", 0.0)

    # Cálculo ponderado
    # 1. Volume de conversas (até 35 pts: 100 convs = 35 pts)
    score_convs = min(35, int((convs / 100) * 35))
    
    # 2. Regras consolidadas (até 35 pts: 7 regras bem definidas = 35 pts)
    score_rules = min(35, int((rules_count / 7) * 35))
    
    # 3. Penalidade de handoff alto (máximo 15 pts bônus por baixa taxa de transbordo)
    score_handoff = int(max(0, 15 * (1.0 - handoff_rate)))
    
    # 4. Fatos e histórico registrado (até 15 pts)
    score_learnings = min(15, int((learnings_count / 30) * 15))

    total_score = max(0, min(100, score_convs + score_rules + score_handoff + score_learnings))

    if total_score < 25:
        level = 0
        stage = "Fase 0: Cold-Start / Nascimento"
        summary = "Bot recém-criado com base de regras inicial. Foco em testes controlados e absorção de rotina básica."
    elif total_score < 60:
        level = 1
        stage = "Fase 1: Calibração & Coleta Ativa"
        summary = "Bot assimilando correções do lojista, alinhamento de horários, endereço e postura de balcão."
    elif total_score < 85:
        level = 2
        stage = "Fase 2: Estabilização & Convergência Contextual"
        summary = "Bot com vocabulário leigo dominado, alta assertividade para conversão de balcão e baixo transbordo humano."
    else:
        level = 3
        stage = "Fase 3: Autonomia Madura & Evolução Incremental"
        summary = "Bot operando com máxima fluidez e autonomia, histórico recorrente e insights de demanda de balcão."

    return {
        "level": level,
        "score": total_score,
        "stage": stage,
        "summary": summary,
        "metrics": {
            "conversations_analyzed": convs,
            "learned_rules_count": rules_count,
            "learnings_count": learnings_count,
            "handoff_rate": round(handoff_rate, 2),
            "score_convs": score_convs,
            "score_rules": score_rules,
            "score_handoff": score_handoff,
            "score_learnings": score_learnings
        }
    }


def get_or_create_client_cortex(client_dir: Path, client_name: str = "") -> dict:
    """Carrega ou inicializa a estrutura do Cortex para um cliente."""
    c_dir = get_cortex_dir(client_dir)
    cortex_file = c_dir / "cortex.json"

    if cortex_file.exists():
        try:
            with open(cortex_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            # Recalcula maturidade para garantir consistência
            data["maturity"] = calculate_maturity(data)
            return data
        except Exception:
            pass

    slug = client_dir.name
    display_name = client_name or slug.replace("-", " ").title()

    initial_cortex = {
        "client_slug": slug,
        "client_name": display_name,
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "conversations_analyzed": 0,
        "learnings_count": 0,
        "handoff_rate": 0.05,
        "learned_rules": [],
        "vocabulary": {
            "termos_leigos": []
        },
        "owner_preferences": [],
        "insights": {
            "servicos_frequentes": [],
            "duvidas_recorrentes": []
        }
    }

    # Se for a SebastianCom, já inclui as regras calibradas verificadas
    if "sebastian" in slug.lower():
        initial_cortex["conversations_analyzed"] = 18
        initial_cortex["learnings_count"] = 6
        initial_cortex["learned_rules"] = [
            {
                "id": "rule-seb-001",
                "rule": "O endereço oficial e atual da loja é Avenida Vereador Francisco Sabino da Costa, 921 - Centro, Maricá - RJ (o número 398 era o endereço antigo).",
                "category": "localizacao",
                "confidence": 1.0,
                "source": "owner_verified"
            },
            {
                "id": "rule-seb-002",
                "rule": "Horário de funcionamento: Segunda a Sexta das 08h30 às 17h30 | Sábados das 08h30 às 12h00. JAMAIS fechar às 18h nem às 13h.",
                "category": "horario",
                "confidence": 1.0,
                "source": "owner_verified"
            },
            {
                "id": "rule-seb-003",
                "rule": "Foco comercial: somos assistência técnica privada e comercial (NÃO filantrópicos). JAMAIS passar diagnósticos no escuro nem tutoriais caseiros por WhatsApp. O foco primordial é trazer o aparelho para avaliação de bancada e abertura de OS.",
                "category": "politica_comercial",
                "confidence": 1.0,
                "source": "owner_verified"
            },
            {
                "id": "rule-seb-004",
                "rule": "Clientes leigos: quando o cliente diz que não entende ou não sabe explicar, interromper imediatamente perguntas técnicas e acolher com gentileza, convidando para a bancada.",
                "category": "atendimento",
                "confidence": 1.0,
                "source": "owner_verified"
            },
            {
                "id": "rule-seb-005",
                "rule": "Tratamento de mídias: reconhecer sempre que ouviu o áudio, viu a foto ou assistiu ao vídeo enviado, utilizando a evidência para justificar a necessidade da bancada.",
                "category": "multimodal",
                "confidence": 1.0,
                "source": "owner_verified"
            }
        ]
        initial_cortex["vocabulary"]["termos_leigos"] = [
            {"termo": "computador apitando", "significado": "falha na inicialização / memória / placa"},
            {"termo": "luz da gota piscando", "significado": "erro de suprimento / tinta na impressora"},
            {"termo": "notebook lerdo", "significado": "necessidade de upgrade de SSD ou otimização de sistema"}
        ]

    initial_cortex["maturity"] = calculate_maturity(initial_cortex)
    save_client_cortex(client_dir, initial_cortex)
    return initial_cortex


def save_client_cortex(client_dir: Path, cortex_data: dict):
    c_dir = get_cortex_dir(client_dir)
    cortex_data["updated_at"] = now_iso()
    cortex_file = c_dir / "cortex.json"
    with open(cortex_file, "w", encoding="utf-8") as f:
        json.dump(cortex_data, f, ensure_ascii=False, indent=2)


def get_cortex_prompt_injection(client_dir: Path) -> str:
    """Retorna o bloco dinâmico do Cortex para injetar no System Prompt."""
    if not client_dir or not client_dir.exists():
        return ""
    
    try:
        data = get_or_create_client_cortex(client_dir)
        mat = data.get("maturity", {})
        rules = data.get("learned_rules", [])
        vocab = data.get("vocabulary", {}).get("termos_leigos", [])
        prefs = data.get("owner_preferences", [])

        lines = [
            f"\n🧠 CÓRTEX COGNITIVO DO CLIENTE ({mat.get('stage', 'Ativo')} - Nível {mat.get('level', 1)}):",
            f"Diretrizes consolidadas e aprendizados acumulados para este negócio:"
        ]

        if rules:
            for r in rules:
                lines.append(f"- [REGRA]: {r.get('rule')}")

        if prefs:
            for p in prefs:
                lines.append(f"- [PREFERÊNCIA DO PROPRIETÁRIO]: {p}")

        if vocab:
            vocab_items = [f"'{v.get('termo')}' ({v.get('significado')})" for v in vocab[:4]]
            lines.append(f"- [VOCABULÁRIO LOCAL]: Entenda gírias comuns como: {', '.join(vocab_items)}.")

        return "\n".join(lines)
    except Exception as e:
        return ""


def append_learning(client_dir: Path, learning: dict):
    """Grava um aprendizado atômico no log do cliente."""
    c_dir = get_cortex_dir(client_dir)
    log_file = c_dir / "learnings.jsonl"
    learning["timestamp"] = now_iso()
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(learning, ensure_ascii=False) + "\n")


def record_owner_feedback(client_dir: Path, text: str, source: str = "owner"):
    """Permite registrar uma instrução direta do proprietário ou operador."""
    data = get_or_create_client_cortex(client_dir)
    rule_id = f"rule-{int(time.time())}"
    new_rule = {
        "id": rule_id,
        "rule": text.strip(),
        "category": "owner_direct",
        "confidence": 1.0,
        "source": source,
        "created_at": now_iso()
    }
    data["learned_rules"].append(new_rule)
    data["learnings_count"] = data.get("learnings_count", 0) + 1
    data["maturity"] = calculate_maturity(data)
    save_client_cortex(client_dir, data)
    append_learning(client_dir, {
        "type": "owner_direct",
        "rule": text.strip(),
        "source": source
    })
    return new_rule


def _extract_worker(session: str, cid: str, inbound_text: str, bot_reply: str, client_dir: Path, cfg: dict):
    """Executado em segundo plano pela thread pool para extrair aprendizado contínuo."""
    try:
        data = get_or_create_client_cortex(client_dir, cfg.get("display", ""))
        data["conversations_analyzed"] = data.get("conversations_analyzed", 0) + 1

        # Não analisa saídas mínimas ou mensagens puramente de teste
        if len(inbound_text.strip()) < 4 or "[midia]" == inbound_text.strip():
            save_client_cortex(client_dir, data)
            return

        # Prompt de extração cognitiva leve
        prompt = (
            "Você é o módulo Cortex Cognitive Learner. Analise esta troca no atendimento:\n"
            f"Mensagem do Cliente: {inbound_text}\n"
            f"Resposta do Atendente: {bot_reply}\n\n"
            "Identifique se há:\n"
            "1. Uma nova regra de negócio, correção ou objeção importante;\n"
            "2. Um termo ou gíria leiga do cliente;\n"
            "3. O serviço solicitado pelo cliente.\n\n"
            "Responda estritamente em JSON no formato:\n"
            "{\n"
            '  "has_learning": true/false,\n'
            '  "learned_rule": "regra resumida ou vazio",\n'
            '  "category": "duvida_frequente | objecao | vocabulario | operacao",\n'
            '  "confidence": 0.0 a 1.0,\n'
            '  "servico_detectado": "nome do servico ou vazio"\n'
            "}"
        )

        headers = {"Content-Type": "application/json"}
        if OMNI_KEY:
            headers["Authorization"] = f"Bearer {OMNI_KEY}"

        payload = {
            "model": "auto/best-chat",
            "messages": [
                {"role": "system", "content": "Extraia aprendizados de atendimento em formato JSON puro."},
                {"role": "user", "content": prompt}
            ],
            "max_tokens": 200,
            "temperature": 0.1
        }

        resp = requests.post(f"{OMNI_URL}/chat/completions", json=payload, headers=headers, timeout=20)
        if resp.status_code == 200:
            res_json = resp.json()
            raw_content = res_json.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
            if "```json" in raw_content:
                raw_content = raw_content.split("```json")[1].split("```")[0].strip()
            elif "```" in raw_content:
                raw_content = raw_content.split("```")[1].split("```")[0].strip()

            parsed = json.loads(raw_content)
            if parsed.get("has_learning") and parsed.get("confidence", 0) >= 0.85:
                rule_text = parsed.get("learned_rule", "").strip()
                if rule_text and len(rule_text) > 8:
                    # Evita duplicatas aproximadas
                    existing = [r.get("rule", "").lower() for r in data.get("learned_rules", [])]
                    if not any(rule_text.lower() in ex or ex in rule_text.lower() for ex in existing):
                        new_r = {
                            "id": f"rule-{int(time.time())}",
                            "rule": rule_text,
                            "category": parsed.get("category", "geral"),
                            "confidence": parsed.get("confidence", 0.9),
                            "source": "autonomous_dialogue",
                            "created_at": now_iso()
                        }
                        data["learned_rules"].append(new_r)
                        data["learnings_count"] = data.get("learnings_count", 0) + 1
                        append_learning(client_dir, {
                            "cid": cid,
                            "inbound": inbound_text[:100],
                            "rule": rule_text,
                            "category": parsed.get("category"),
                            "confidence": parsed.get("confidence")
                        })

            servico = parsed.get("servico_detectado")
            if servico and len(servico) > 3:
                servs = data.get("insights", {}).get("servicos_frequentes", [])
                if servico not in servs:
                    servs.append(servico)
                    data.setdefault("insights", {})["servicos_frequentes"] = servs[-10:]

        data["maturity"] = calculate_maturity(data)
        save_client_cortex(client_dir, data)
    except Exception as e:
        # Erro cognitivo em background nunca afeta a operação principal
        pass


def cortex_learn_async(session: str, cid: str, inbound_text: str, bot_reply: str, client_dir: Path, cfg: dict):
    """Enfileira a extração de aprendizado em segundo plano."""
    if not client_dir or not client_dir.exists():
        return
    CORTEX_POOL.submit(_extract_worker, session, cid, inbound_text, bot_reply, client_dir, cfg)


def init_all_clients_cortex(clients_base_dir: Path):
    """Inicializa o Cortex para todos os clientes existentes em /mnt/storage/client-bots/clients/"""
    results = {}
    if not clients_base_dir.exists():
        return results
    for c_dir in clients_base_dir.iterdir():
        if c_dir.is_dir() and not c_dir.name.startswith("."):
            cortex = get_or_create_client_cortex(c_dir)
            results[c_dir.name] = cortex.get("maturity", {})
    return results


if __name__ == "__main__":
    import sys
    base = Path("/mnt/storage/client-bots/clients")
    res = init_all_clients_cortex(base)
    print(json.dumps(res, ensure_ascii=False, indent=2))
