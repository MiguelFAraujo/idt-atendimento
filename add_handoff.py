with open('app.py', 'rb') as f:
    content = f.read()

old_bytes = b'DEFAULT_FEWSHOT = [\n    {"role": "user", "content": "Oi, tudo bem?"},\n    {"role": "assistant", "content": "Oi! Tudo \xc3\xb3timo. Em que posso ajudar?"},\n    {"role": "user", "content": "Quanto custa montar um PC?"},\n    {"role": "assistant", "content": "Depende das pe\xc3\xa7as. Me passa seu or\xc3\xa7amento e quais jogos quer rodar que te oriento."},\n    {"role": "user", "content": "Tenho Ryzen 7 e RTX 4050"},\n    {"role": "assistant", "content": "Show! Qual Ryzen 7 exatamente (5700X, 5800X3D, 7700X)? A 4050 \xc3\xa9 desktop 6GB/8GB ou notebook? E o que j\xc3\xa1 tem: placa-m\xc3\xa3e, RAM, SSD, fonte, gabinete, cooler?"},\n    {"role": "user", "content": "Quero falar com atendente"},\n    {"role": "assistant", "content": "Entendido. Transferindo para um atendente humano..."},\n]\n\n\n\n'

new_function = '''def _notify_handoff(session, cid, tenant_id, inbound_text, cfg):
    """Notify owner via Telegram with inline buttons for handoff (Assumir / Devolver)."""
    owner_number = cfg.get("owner_number", "5521988743980")
    owner_name = cfg.get("owner_name", "Dono")
    display = cfg.get("display", session)

    txt = (f"🤝 <b>HANDOFF SOLICITADO</b>\\n"
           f"📱 <i>{display}</i>\\n"
           f"👤 {owner_name} ({owner_number})\\n"
           f"💬 Cliente: {cid}\\n"
           f"📝 \\"{inbound_text[:200]}\\""\\n"
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

'''

new_bytes = old_bytes + new_function.encode('utf-8')

if old_bytes in content:
    content = content.replace(old_bytes, new_bytes)
    with open('app.py', 'wb') as f:
        f.write(content)
    print('SUCCESS: _notify_handoff added at byte level')
else:
    print('FAILED: Bytes not found')
    idx = content.find(b'DEFAULT_FEWSHOT = [')
    print('Found at:', idx)
    print(repr(content[idx:idx+760]))