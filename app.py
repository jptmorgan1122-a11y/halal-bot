import os, time, requests, threading, json
from flask import Flask, request

# ==== SECRETS: set these in Render > Environment, never in the code ====
SOL_PRIVATE_KEY = os.getenv("SOL_PRIVATE_KEY", "2NVYNwqt1tQM3p5J6kXzi5BbZ8H1MyNzeK1344MEuBGPoWGGHGVdm5JKsFn3XsVdAcarpeP8xTG13VTLXVKNMFEW")
EVM_PRIVATE_KEY = os.getenv("EVM_PRIVATE_KEY", "0x372f9c7cbe15fa79365a10587b137253d599d03e48fe0fd6903fcb7e76f88cf5")
HELIUS_RPC = os.getenv("HELIUS_RPC", "https://mainnet.helius-rpc.com/?api-key=1942eb38-a995-4415-a5dd-00794df2676c")
ALCHEMY_RPC = os.getenv("ALCHEMY_RPC", "https://rpc.ankr.com/polygon")
GEMINI_KEY = os.getenv("GEMINI_KEY", "AIzaSyDLbT5Kt-GjHij2B12fgZaBPBLxCUb7NQg")
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN", "EAAP7755w8FwBS0ZBwVL1E3IxOEGbrqfUWIcZCxkMvEw6ym88AFXgSHRn2l3Ul3NmhoeK0iGC3TLNURYiPSuKhZCNXaoe4qyuJB92eO99KZBzBzM0VX6xrs9n9AZBe1zLbT364ZCpWfq7HTBpZBKmhrmkymJczX4iNerDvuJ5XcA4Ko3gmsYawqhzaQTaOZALIG1wF2vFMhrqTIhrZALhOh3w6UZC68sg5csR3Mtu7nZCZAKE13zpJolAMgZDZD" )
PHONE_ID = os.getenv("PHONE_ID", "1401521863047276")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "halalbot123")
MY_NUMBER = os.getenv("MY_NUMBER", "923309355323")
TRADING_MODE = os.getenv("TRADING_MODE", "paper")

app = Flask(__name__)
seen = set()


def send_whatsapp(text):
    url = f"https://graph.facebook.com/v19.0/{PHONE_ID}/messages"
    h = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    data = {"messaging_product": "whatsapp", "to": MY_NUMBER, "type": "text", "text": {"body": text}}
    try:
        requests.post(url, json=data, headers=h, timeout=10)
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


def buy_token(pair):
    chain = pair['chainId']
    symbol = pair['baseToken']['symbol']
    price = float(pair['priceUsd'])
    link = f"https://dexscreener.com/{chain}/{pair['pairAddress']}"

    # --- PAPER MODE ---
    if TRADING_MODE == "paper":
        send_whatsapp(f"PAPER BUY ${symbol} on {chain.upper()}\nPrice ${price}\nTP +110% SL 90%\n{link}")
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
    threading.Thread(target=scanner, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
