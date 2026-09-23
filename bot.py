#!/usr/bin/env python3
"""Spot Solana scanner and manual-trade alerts; never connects to wallets."""
import argparse
import datetime as dt
import json
import math
import os
from pathlib import Path
import re
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "config.json"
STATE = ROOT / "estado.json"
DEX = "https://api.dexscreener.com"
GECKO = "https://api.geckoterminal.com/api/v2"
ZEC = "A7bdiYdS5GjqGFtxf17ppRHtDKPkkRqbKtR27dxvQXaS"
QUOTE_MINTS = {"So11111111111111111111111111111111111111112",
               "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"}
MINT = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
DEFAULT = {
    "interval_minutes": 15,
    "max_discovery_pools": 30,
    "min_liquidity_usd": 50000,
    "min_volume_24h_usd": 40000,
    "min_trades_24h": 150,
    "min_pair_age_days": 5,
    "buy_score_threshold": 70,
    "take_profit_pct": 15,
    "stop_loss_pct": 5,
    "trailing_activation_pct": 5,
    "trailing_drop_pct": 3,
    "alert_cooldown_hours": 3,
    "watchlist": [{"address": ZEC, "entry_price_usd": None}],
    "whatsapp": {"phone_number_id": "", "recipient": "", "template_name": "alerta_trade_sol", "language": "pt_BR"}
}


def num(x):
    try:
        v = float(x)
        return v if math.isfinite(v) else 0.
    except (ValueError, TypeError):
        return 0.


def request_json(url, payload=None, bearer=None):
    headers = {"Accept": "application/json", "User-Agent": "SolSpotRadar/1.0"}
    if bearer:
        headers["Authorization"] = "Bearer " + bearer
    body = json.dumps(payload).encode() if payload is not None else None
    if body:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers)
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as exc:
            if exc.code not in (429, 500, 502, 503) or attempt == 2 or body:
                raise
        except (TimeoutError, urllib.error.URLError):
            if attempt == 2 or body:
                raise
        time.sleep(2 * (attempt + 1))


def candidate_addresses(cfg):
    addresses = []
    for source in ("trending_pools", "pools", "new_pools"):
        try:
            data = request_json(f"{GECKO}/networks/solana/{source}?page=1")
        except (urllib.error.URLError, ValueError):
            print(f"Fonte de descoberta indisponível: {source}")
            continue
        for item in data.get("data", []):
            addr = item.get("attributes", {}).get("address")
            if addr and addr not in addresses:
                addresses.append(addr)
    return addresses[:int(cfg["max_discovery_pools"])]


def get_pairs(cfg):
    found = {}
    for pool in candidate_addresses(cfg):
        url = f"{DEX}/latest/dex/pairs/solana/{urllib.parse.quote(pool)}"
        try:
            fetched = request_json(url).get("pairs") or []
        except (urllib.error.URLError, ValueError):
            continue
        for pair in fetched:
            if pair.get("pairAddress") == pool:
                found[pool] = pair
        time.sleep(.22)
    # Explicit watchlist remains in scope even if discovery cap excludes its pools.
    for entry in cfg["watchlist"]:
        token = entry["address"]
        url = f"{DEX}/token-pairs/v1/solana/{urllib.parse.quote(token)}"
        try:
            fetched = request_json(url)
        except (urllib.error.URLError, ValueError):
            print(f"Dados indisponíveis para token monitorado: {token}")
            continue
        for pair in fetched:
            if pair.get("baseToken", {}).get("address") == token:
                found[pair["pairAddress"]] = pair
        time.sleep(.22)
    return list(found.values())


def assess(pair, cfg, now=None, monitoring_only=False):
    now = now or time.time()
    base = pair.get("baseToken") or {}
    address = base.get("address", "")
    quote = pair.get("quoteToken") or {}
    if pair.get("chainId") != "solana" or not MINT.fullmatch(address):
        return None
    if quote.get("address") not in QUOTE_MINTS:
        return None
    price = num(pair.get("priceUsd"))
    liq = num((pair.get("liquidity") or {}).get("usd"))
    vol = num((pair.get("volume") or {}).get("h24"))
    transactions = pair.get("txns") or {}
    buys = num((transactions.get("h24") or {}).get("buys"))
    sells = num((transactions.get("h24") or {}).get("sells"))
    trades = buys + sells
    age = (now * 1000 - num(pair.get("pairCreatedAt"))) / 86400000
    change = pair.get("priceChange") or {}
    if price <= 0:
        return None
    if monitoring_only:
        return {"address": address, "symbol": base.get("symbol", "?"),
                "price": price, "liq": liq, "vol": vol, "trades": int(trades),
                "age": round(age, 1), "m5": num(change.get("m5")),
                "h1": num(change.get("h1")), "h6": num(change.get("h6")),
                "h24": num(change.get("h24")), "ratio": 0,
                "score": 0, "buy": False, "pair": pair.get("pairAddress", ""),
                "url": pair.get("url", "")}
    if (liq < cfg["min_liquidity_usd"] or
            vol < cfg["min_volume_24h_usd"] or trades < cfg["min_trades_24h"] or
            age < cfg["min_pair_age_days"] or num(pair.get("pairCreatedAt")) <= 0 or
            any(change.get(k) is None for k in ("m5", "h1", "h6", "h24")) or
            vol / liq > 12):
        return None
    m5, h1, h6, h24 = (num(change[k]) for k in ("m5", "h1", "h6", "h24"))
    ratio = buys / max(sells, 1)
    # Momentum with anti-chase rules; rank is not a probability of success.
    score = round(min(25, liq / 5000) + min(20, vol / 6000) +
                  min(15, trades / 25) + min(10, age / 2) +
                  (20 if 1.1 <= ratio <= 3 else 5) +
                  (10 if -2 <= m5 <= 3 else 0))
    buy = (score >= cfg["buy_score_threshold"] and -2 <= m5 <= 3 and
           2 <= h1 <= 12 and 3 <= h6 <= 28 and -10 <= h24 <= 45 and
           1.1 <= ratio <= 3)
    return {"address": address, "symbol": base.get("symbol", "?"),
            "price": price, "liq": liq, "vol": vol, "trades": int(trades),
            "age": round(age, 1), "m5": m5, "h1": h1, "h6": h6,
            "h24": h24, "ratio": ratio, "score": score, "buy": buy,
            "pair": pair.get("pairAddress", ""), "url": pair.get("url", "")}


def best_by_token(pairs, cfg):
    selected = {}
    watched = {x["address"] for x in cfg["watchlist"]}
    for pair in pairs:
        address = (pair.get("baseToken") or {}).get("address", "")
        row = assess(pair, cfg)
        if row is None and address in watched:
            row = assess(pair, cfg, monitoring_only=True)
        if row and (row["address"] not in selected or row["liq"] > selected[row["address"]]["liq"]):
            selected[row["address"]] = row
    return selected


def sell_signal(row, entry, state, cfg):
    """Trailing high since monitoring began; no position or cost basis is invented."""
    if not entry or num(entry.get("entry_price_usd")) <= 0:
        return None
    cost = num(entry["entry_price_usd"])
    # Preserve high only for the same configured entry price.
    if state.get("entry") != cost:
        state.clear()
        state["entry"] = cost
        state["peak"] = max(cost, row["price"])
    state["peak"] = max(num(state.get("peak")), row["price"], cost)
    gain = (row["price"] / cost - 1) * 100
    peak_gain = (state["peak"] / cost - 1) * 100
    drop = (1 - row["price"] / state["peak"]) * 100
    if gain <= -cfg["stop_loss_pct"]:
        return f"REVISAR VENDA: queda de {abs(gain):.1f}% desde a compra"
    if peak_gain >= cfg["trailing_activation_pct"] and drop >= cfg["trailing_drop_pct"]:
        return f"REVISAR VENDA: recuo de {drop:.1f}% do pico observado; resultado {gain:+.1f}%"
    if gain >= cfg["take_profit_pct"]:
        return f"REVISAR VENDA: alvo de {cfg['take_profit_pct']}% atingido; resultado {gain:+.1f}%"
    return None


def whatsapp(message, cfg):
    settings = cfg["whatsapp"]
    token = os.getenv("WHATSAPP_ACCESS_TOKEN", "")
    phone = settings.get("phone_number_id", "")
    to = settings.get("recipient", "")
    if not (token and phone and to):
        return False
    if not re.fullmatch(r"\d+", str(phone)) or not re.fullmatch(r"\d{10,15}", str(to)):
        raise ValueError("IDs e destinatário do WhatsApp inválidos")
    payload = {"messaging_product": "whatsapp", "to": to, "type": "template",
               "template": {"name": settings["template_name"],
                            "language": {"code": settings["language"]},
                            "components": [{"type": "body", "parameters": [
                                {"type": "text", "text": message[:900]}]}]}}
    # Version must be set to a version supported in the user's Meta app.
    version = os.getenv("WHATSAPP_GRAPH_VERSION", "v23.0")
    url = f"https://graph.facebook.com/{version}/{phone}/messages"
    data = request_json(url, payload=payload, bearer=token)
    return bool(data.get("messages"))


def telegram(message, state):
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if not token:
        return False
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "") or state.get("telegram_chat_id")
    if not chat_id:
        updates = request_json(f"https://api.telegram.org/bot{token}/getUpdates")
        if not updates.get("ok"):
            raise ValueError("Telegram não respondeu à busca da conversa")
        chats = set()
        for update in updates.get("result", []):
            incoming = update.get("message") or {}
            chat = incoming.get("chat") or {}
            sender = incoming.get("from") or {}
            command = (incoming.get("text") or "").split(maxsplit=1)[0]
            if (chat.get("type") == "private" and chat.get("id") == sender.get("id")
                    and command.split("@", 1)[0] == "/start"):
                chats.add(chat["id"])
        if len(chats) != 1:
            print("Telegram: envie /start ao bot; se houver várias conversas, configure TELEGRAM_CHAT_ID.")
            return False
        chat_id = chats.pop()
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    result = request_json(url, {"chat_id": chat_id, "text": message[:3800]})
    if not result.get("ok"):
        raise ValueError("Telegram recusou o alerta")
    state["telegram_chat_id"] = chat_id
    return True


def notify(kind, row, cfg, state, now):
    key = f'{kind}:{row["address"]}'
    last = num(state["last_alert"].get(key))
    if now - last < 3600 * cfg["alert_cooldown_hours"]:
        return
    observed = dt.datetime.fromtimestamp(now, dt.timezone.utc).strftime('%d/%m/%Y %H:%M UTC')
    condition = ('Entrada: confirmar manutenção do movimento, liquidez e volume antes de comprar.'
                 if kind == 'REVISAR COMPRA' else
                 'Saída: conferir preço executável e posição antes de vender; sem ordem automática.')
    message = (f'{kind} {row["symbol"]} | ${row["price"]:.8g} | '
               f'consulta {observed}\n'
               f'1h {row["h1"]:+.1f}% | 24h {row["h24"]:+.1f}% | '
               f'liquidez ${row["liq"]:,.0f} | volume 24h ${row["vol"]:,.0f} | '
               f'{row["trades"]} negócios 24h | par {row["age"]} dias\n'
               f'{condition}\nRisco: preço e liquidez podem mudar; slippage e manipulação.\n'
               f'Mint: {row["address"]}\n{row["url"]}')
    print("ALERTA:", message)
    try:
        if telegram(message, state):
            state["last_alert"][key] = now
            print("Telegram: requisição aceita.")
        elif whatsapp(message, cfg):
            state["last_alert"][key] = now
            print("WhatsApp: requisição aceita; entrega depende da Meta.")
        else:
            print("Envio não configurado; alerta exibido somente aqui.")
    except (urllib.error.URLError, ValueError, KeyError) as exc:
        print("Envio falhou; tentará novamente depois:", type(exc).__name__)


def scan(cfg, state, now=None):
    now = now or time.time()
    rows = best_by_token(get_pairs(cfg), cfg)
    watch = {x["address"]: x for x in cfg["watchlist"]}
    suggestions = sorted((x for x in rows.values() if x["buy"]),
                         key=lambda x: (x["score"], x["liq"]), reverse=True)
    print(f'\n{dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")} | '
          f'{len(rows)} tokens elegíveis; {len(suggestions)} alertas de compra potenciais')
    for row in suggestions[:3]:
        print(f'COMPRA PARA REVISAR {row["symbol"]}: ${row["price"]:.8g}, '
              f'nota {row["score"]}, 1h {row["h1"]:+.1f}%, endereço {row["address"]}')
        notify("REVISAR COMPRA", row, cfg, state, now)
    for token, entry in watch.items():
        row = rows.get(token)
        if not row:
            print(f"MONITORADO {token}: sem dados suficientes; nenhum sinal de venda.")
            continue
        signal = sell_signal(row, entry, state["positions"].setdefault(token, {}), cfg)
        print(f'MONITORADO {row["symbol"]}: ${row["price"]:.8g} | '
              f'24h {row["h24"]:+.1f}% | {signal or "aguardando condições"}')
        if signal:
            notify(signal, row, cfg, state, now)
    return rows


def main():
    parser = argparse.ArgumentParser(description="Radar de compra/venda à vista na Solana")
    parser.add_argument("--loop", action="store_true", help="monitorar continuamente")
    parser.add_argument("--test-telegram", action="store_true", help="enviar confirmação ao Telegram")
    args = parser.parse_args()
    if not CONFIG.exists():
        CONFIG.write_text(json.dumps(DEFAULT, ensure_ascii=False, indent=2) + "\n")
        print("Configuração criada: config.json")
    cfg = DEFAULT | json.loads(CONFIG.read_text())
    private_entry = os.getenv("ZEC_ENTRY_PRICE_USD", "").strip()
    if private_entry:
        if num(private_entry) <= 0:
            raise ValueError("ZEC_ENTRY_PRICE_USD precisa ser um preço unitário positivo")
        for item in cfg["watchlist"]:
            if item["address"] == ZEC:
                item["entry_price_usd"] = num(private_entry)
    for item in cfg["watchlist"]:
        if not MINT.fullmatch(item.get("address", "")):
            raise ValueError("Endereço inválido na watchlist")
    try:
        state = json.loads(STATE.read_text())
    except (OSError, ValueError):
        state = {}
    state.setdefault("last_alert", {})
    state.setdefault("positions", {})
    if args.test_telegram:
        if telegram("Radar de trades Solana conectado. Alertas serão enviados apenas quando houver sinal.", state):
            STATE.write_text(json.dumps(state, indent=2))
            print("Telegram: mensagem de teste aceita.")
        else:
            print("Telegram: teste não enviado; confira o token e envie /start ao bot.")
        return
    while True:
        try:
            scan(cfg, state)
            STATE.write_text(json.dumps(state, indent=2))
        except (urllib.error.URLError, ValueError, KeyError, TypeError) as exc:
            print("Atualização indisponível; não foi gerado sinal:", str(exc)[:180])
        if not args.loop:
            break
        time.sleep(max(5, int(cfg["interval_minutes"])) * 60)


if __name__ == "__main__":
    main()
