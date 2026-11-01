import os, time, requests, threading, json
from flask import Flask, request

# ==== SECRETS: set these in Render > Environment, never in the code ====
SOL_PRIVATE_KEY = os.getenv("SOL_PRIVATE_KEY", "2NVYNwqt1tQM3p5J6kXzi5BbZ8H1MyNzeK1344MEuBGPoWGGHGVdm5JKsFn3XsVdAcarpeP8xTG13VTLXVKNMFEW")
EVM_PRIVATE_KEY = os.getenv("EVM_PRIVATE_KEY", "0x372f9c7cbe15fa79365a10587b137253d599d03e48fe0fd6903fcb7e76f88cf5")
HELIUS_RPC = os.getenv("HELIUS_RPC", "https://mainnet.helius-rpc.com/?api-key=1942eb38-a995-4415-a5dd-00794df2676c")
ALCHEMY_RPC = os.getenv("ALCHEMY_RPC", "https://rpc.ankr.com/polygon")
GEMINI_KEY = os.getenv("GEMINI_KEY", "AIzaSyDLbT5Kt-GjHij2B12fgZaBPBLxCUb7NQg")
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN", "EAAP7755w8FwBSZB44MuMzJwKjHmNpxq0kaHccQb1oM5b68phjrf2ZBCRI9JlsV924cS6hmDohdd0bAejqK3PqYhqYH8hp5J6Ypa7BFEmgWVZCPkiumst3SIuvUin3hQZAIir4DxGbo6L6doEDx1MuRZA0ffq2svCZAJPfkyfKK1DPjDLVvvxbCDN6qDVFV1QDRe9Szh8u3yJuiqKYFWZAAQ9avq77pAX42WSrgEzsKHe3w8EP1jeQZDZD")
PHONE_ID = os.getenv("PHONE_ID", "1401521863047276")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "halalbot123")
MY_NUMBER = os.getenv("MY_NUMBER", "923309355323")
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "halalbot-269e58ea7930")
TRADING_MODE = os.getenv("TRADING_MODE", "paper")

app = Flask(__name__)
seen = set()

# ==== PAPER TRADING SETTINGS ====
START_BAL = float(os.getenv("START_BAL", "10"))        # fake $ per chain
TRADE_FRACTION = float(os.getenv("TRADE_FRACTION", "0.5"))  # 50% of chain balance per trade
TP_PCT = float(os.getenv("TP_PCT", "110"))             # take profit at +110%
SL_PCT = float(os.getenv("SL_PCT", "90"))              # stop loss at -90%
FEE_PCT = float(os.getenv("FEE_PCT", "2"))             # fake fees/slippage per round trip
MAX_HOLD_HOURS = float(os.getenv("MAX_HOLD_HOURS", "24"))
CHAINS = ["solana", "polygon", "bsc", "base", "ethereum"]
STATE_FILE = "paper_state.json"
lock = threading.Lock()

state = {"balances": {c: START_BAL for c in CHAINS}, "positions": [], "closed": [], "last_summary": time.time()}
try:
    with open(STATE_FILE) as f:
        state.update(json.load(f))
except Exception:
    pass


def save_state():
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f)
    except Exception:
        pass


def send_whatsapp(text):
    # Sends the alert to the ntfy app (name kept so the rest of the code works)
    try:
        requests.post(f"https://ntfy.sh/{NTFY_TOPIC}", data=text.encode("utf-8"), timeout=10)
    except Exception:
        pass


def is_halal(name, desc, website):
    try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GEMINI_KEY}"
        prompt = f"Token: {name} Desc: {desc} Website: {website}. No gambling, alcohol, riba, adult. Answer HALAL or HARAM only"
        r = requests.post(url, json={"contents": [{"parts": [{"text": prompt}]}]}, timeout=15).json()
        txt = r['candidates'][0]['content']['parts'][0]['text']
        return "HALAL" in txt.upper() and "HARAM" not in txt.upper()
    except Exception:
        return False


def check_all_rules(pair):
    created_ms = pair.get('pairCreatedAt', 0)
    if not created_ms or time.time() - created_ms / 1000 > 120:
        return False, ""
    info = pair.get('info', {})
    webs = info.get('websites', [])
    if not webs:
        return False, ""
    website = webs[0].get('url', '')
    if not info.get('imageUrl'):
        return False, ""
    liq = pair.get('liquidity', {}).get('usd', 0)
    if liq < 1000:
        return False, ""
    token_addr = pair['baseToken']['address']
    try:
        res = requests.get(f"https://api.dexscreener.com/latest/dex/tokens/{token_addr}", timeout=10).json()
        if len(res.get('pairs', [])) > 1:
            return False, ""
    except Exception:
        pass
    return True, website


# ==== GET 50% BALANCE FUNCTIONS ====
def get_sol_50_percent():
    try:
        from solders.keypair import Keypair
        kp = Keypair.from_base58_string(SOL_PRIVATE_KEY)
        pubkey = str(kp.pubkey())
        payload = {"jsonrpc": "2.0", "id": 1, "method": "getBalance", "params": [pubkey]}
        r = requests.post(HELIUS_RPC, json=payload, timeout=10).json()
        lamports = r['result']['value']
        sol = lamports / 1e9
        return sol * 0.5  # 50%
    except Exception as e:
        print("SOL balance error:", e)
        return 0


def get_evm_50_percent():
    try:
        from eth_account import Account
        acct = Account.from_key(EVM_PRIVATE_KEY)
        address = acct.address
        # Uses ALCHEMY_RPC for Polygon (add a separate RPC for BSC if needed)
        payload = {"jsonrpc": "2.0", "id": 1, "method": "eth_getBalance", "params": [address, "latest"]}
        r = requests.post(ALCHEMY_RPC, json=payload, timeout=10).json()
        wei = int(r['result'], 16)
        native = wei / 1e18
        return native * 0.5  # 50%
    except Exception as e:
        print("EVM balance error:", e)
        return 0


def get_price(chain, pair_addr):
    try:
        r = requests.get(f"https://api.dexscreener.com/latest/dex/pairs/{chain}/{pair_addr}", timeout=10).json()
        p = r.get("pair") or ((r.get("pairs") or [None])[0])
        return float(p["priceUsd"]) if p else None
    except Exception:
        return None


def paper_buy(pair):
    chain = pair["chainId"]
    symbol = pair["baseToken"]["symbol"]
    price = float(pair["priceUsd"])
    link = f"https://dexscreener.com/{chain}/{pair['pairAddress']}"
    with lock:
        bal = state["balances"].setdefault(chain, START_BAL)
        size = bal * TRADE_FRACTION
        if size < 0.5:
            send_whatsapp(f"SKIPPED ${symbol} on {chain.upper()}: balance too low (${bal:.2f})")
            return
        state["balances"][chain] = bal - size
        state["positions"].append({
            "chain": chain, "symbol": symbol, "pair": pair["pairAddress"],
            "entry": price, "size": size, "time": time.time(), "fails": 0})
        save_state()
    send_whatsapp(f"PAPER BUY ${symbol} on {chain.upper()}\nSize ${size:.2f} (50% of ${bal:.2f})\nEntry ${price}\nTP +{TP_PCT:.0f}% | SL -{SL_PCT:.0f}%\n{link}")


def close_position(pos, exit_price, reason):
    with lock:
        if pos not in state["positions"]:
            return
        value = pos["size"] * (exit_price / pos["entry"]) * (1 - FEE_PCT / 100)
        pnl = value - pos["size"]
        pct = (value / pos["size"] - 1) * 100
        state["balances"][pos["chain"]] = state["balances"].get(pos["chain"], 0) + value
        state["positions"].remove(pos)
        state["closed"].append({"symbol": pos["symbol"], "chain": pos["chain"], "pnl": pnl, "pct": pct, "reason": reason})
        bal = state["balances"][pos["chain"]]
        save_state()
    send_whatsapp(f"PAPER SELL ${pos['symbol']} on {pos['chain'].upper()} ({reason})\n{pct:+.1f}% | PnL {pnl:+.2f} USD\n{pos['chain'].upper()} balance ${bal:.2f}")


def summary_text():
    with lock:
        closed = list(state["closed"])
        wins = sum(1 for c in closed if c["pnl"] > 0)
        losses = len(closed) - wins
        total = sum(c["pnl"] for c in closed)
        lines = [f"SUMMARY: {len(closed)} trades, {wins} wins, {losses} losses", f"Total PnL {total:+.2f} USD"]
        for ch, b in state["balances"].items():
            lines.append(f"{ch.upper()}: ${b:.2f}")
        lines.append(f"Open positions: {len(state['positions'])}")
    return "\n".join(lines)


def monitor():
    while True:
        try:
            with lock:
                open_pos = list(state["positions"])
            for pos in open_pos:
                price = get_price(pos["chain"], pos["pair"])
                if price is None:
                    pos["fails"] = pos.get("fails", 0) + 1
                    if pos["fails"] >= 15:
                        close_position(pos, pos["entry"] * 0.0001, "NO DATA / RUG")
                    continue
                pos["fails"] = 0
                change = (price / pos["entry"] - 1) * 100
                if change >= TP_PCT:
                    close_position(pos, price, "TAKE PROFIT")
                elif change <= -SL_PCT:
                    close_position(pos, price, "STOP LOSS")
                elif time.time() - pos["time"] > MAX_HOLD_HOURS * 3600:
                    close_position(pos, price, "TIME EXIT")
            if time.time() - state.get("last_summary", 0) > 24 * 3600:
                state["last_summary"] = time.time()
                send_whatsapp(summary_text())
                save_state()
        except Exception as e:
            print("monitor error:", e)
        time.sleep(20)


def buy_token(pair):
    chain = pair['chainId']
    symbol = pair['baseToken']['symbol']
    price = float(pair['priceUsd'])
    link = f"https://dexscreener.com/{chain}/{pair['pairAddress']}"

    # --- PAPER MODE ---
    if TRADING_MODE == "paper":
        paper_buy(pair)
        return

    # --- REAL MODE (50% OF WALLET) ---
    try:
        if chain == "solana":
            amount = get_sol_50_percent()
            if amount < 0.001:
                send_whatsapp(f"LOW SOL BALANCE for ${symbol}")
                return
            send_whatsapp(f"REAL BUY ${symbol} on SOLANA\nAmount: {amount:.4f} SOL (50% of wallet)\nPrice ${price}\n{link}")
            # TODO: Add your Jupiter swap code using amount
        else:
            amount = get_evm_50_percent()
            if amount < 0.001:
                send_whatsapp(f"LOW {chain.upper()} BALANCE for ${symbol}")
                return
            send_whatsapp(f"REAL BUY ${symbol} on {chain.upper()}\nAmount: {amount:.4f} {chain.upper()} (50% of wallet)\nPrice ${price}\n{link}")
            # TODO: Add your QuickSwap/PancakeSwap code using amount
    except Exception as e:
        send_whatsapp(f"BUY FAILED ${symbol}: {e}")


def handle_pair(pair):
    if pair['pairAddress'] in seen:
        return
    seen.add(pair['pairAddress'])
    ok, website = check_all_rules(pair)
    if not ok:
        return
    symbol = pair['baseToken']['symbol']
    desc = pair.get('info', {}).get('header', '') or pair['baseToken']['name']
    if not is_halal(symbol, desc, website):
        return
    buy_token(pair)


def scanner():
    while True:
        try:
            for q in ['solana', 'polygon', 'bsc', 'base', 'ethereum']:
                try:
                    r = requests.get(f"https://api.dexscreener.com/latest/dex/search/?q={q}", timeout=15).json()
                    for pair in r.get('pairs', [])[:15]:
                        handle_pair(pair)
                except Exception:
                    continue
            time.sleep(5)
        except Exception:
            time.sleep(5)


@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    global TRADING_MODE
    if request.method == "GET":
        if request.args.get("hub.verify_token") == VERIFY_TOKEN:
            return request.args.get("hub.challenge")
        return "forbidden", 403
    try:
        data = request.json
        m = data['entry'][0]['changes'][0]['value']['messages'][0]
        # Only accept commands from your own number
        if m.get('from') != MY_NUMBER:
            return "ok", 200
        msg = m['text']['body'].lower().strip()
        if msg == "paper":
            TRADING_MODE = "paper"
            send_whatsapp("Switched to PAPER (no real money)")
        elif msg == "real":
            TRADING_MODE = "real"
            send_whatsapp("Switched to REAL (50% wallet per trade!)")
    except Exception:
        pass
    return "ok", 200


@app.route("/")
def home():
    return f"Bot LIVE {TRADING_MODE.upper()} - REAL uses 50%"


if __name__ == "__main__":
    send_whatsapp(f"Bot started in {TRADING_MODE.upper()} mode")
    threading.Thread(target=scanner, daemon=True).start()
    threading.Thread(target=monitor, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
