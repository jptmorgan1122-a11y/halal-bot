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


GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

HALAL_PROMPT = ("The data I provide to you . Does it have anything Haram mentioned in the writing only . "
                "Consider Lack of purpose is also haram. don't talk about uncertainty or gahar etc ? "
                "One word ans only . YES OR NO no bullshit only one word ans")


def is_halal(name, desc, website):
    # YES = haram, NO = halal. Anything else (error, long answer) is treated as not halal.
    try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_KEY}"
        prompt = f"{HALAL_PROMPT}\n\nToken name: {name}\nDescription: {desc}\nWebsite: {website}"
        r = requests.post(url, json={"contents": [{"parts": [{"text": prompt}]}]}, timeout=20).json()
        txt = r['candidates'][0]['content']['parts'][0]['text']
        word = txt.strip().upper().strip(" .!\n\t\"'")
        return word == "NO"
    except Exception as e:
        print("halal check error:", e)
        return False


def is_pumpfun(pair):
    # Block only the pump.fun bonding curve (dexId "pumpfun"). PumpSwap ("pumpswap") is allowed.
    return str(pair.get("dexId", "")).lower() == "pumpfun"


def check_all_rules(pair):
    if is_pumpfun(pair):
        return False, ""
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


# ==== REAL TRADING ====
import base64

SOL_MINT = "So11111111111111111111111111111111111111112"
JUP_BASE = os.getenv("JUP_BASE", "https://lite-api.jup.ag/swap/v1")
SLIPPAGE_PCT = min(float(os.getenv("SLIPPAGE_PCT", "30")), 30.0)  # max 30%
MAX_SELL_TRIES = int(os.getenv("MAX_SELL_TRIES", "5"))

# Routers/wrapped-native addresses are Uniswap-V2 style. Edit here if you use another DEX.
EVM_CFG = {
    "polygon": {"rpc": os.getenv("RPC_POLYGON", ALCHEMY_RPC), "sym": "MATIC", "explorer": "https://polygonscan.com/tx/",
                "router": "0xa5e0829caced8ffdd4de3c43696c57f7d7a678ff", "wn": "0x0d500b1d8e8ef31e21c99d1db9a6444d3adf1270"},
    "bsc": {"rpc": os.getenv("RPC_BSC", "https://bsc-dataseed.binance.org"), "sym": "BNB", "explorer": "https://bscscan.com/tx/",
            "router": "0x10ed43c718714eb63d5aa57d5f32bf4d6e86fa28", "wn": "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c"},
    "base": {"rpc": os.getenv("RPC_BASE", "https://mainnet.base.org"), "sym": "ETH", "explorer": "https://basescan.org/tx/",
             "router": "0x4752ba5dbc23f44d87826276bf6fd6b1c372ad24", "wn": "0x4200000000000000000000000000000000000006"},
    "ethereum": {"rpc": os.getenv("RPC_ETHEREUM", "https://eth.llamarpc.com"), "sym": "ETH", "explorer": "https://etherscan.io/tx/",
                 "router": "0x7a250d5630b4cf539739df2c5dacb4c659f2488d", "wn": "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"},
}

ROUTER_ABI = json.loads('[{"name":"getAmountsOut","type":"function","stateMutability":"view","inputs":[{"name":"amountIn","type":"uint256"},{"name":"path","type":"address[]"}],"outputs":[{"name":"amounts","type":"uint256[]"}]},{"name":"swapExactETHForTokensSupportingFeeOnTransferTokens","type":"function","stateMutability":"payable","inputs":[{"name":"amountOutMin","type":"uint256"},{"name":"path","type":"address[]"},{"name":"to","type":"address"},{"name":"deadline","type":"uint256"}],"outputs":[]},{"name":"swapExactTokensForETHSupportingFeeOnTransferTokens","type":"function","stateMutability":"nonpayable","inputs":[{"name":"amountIn","type":"uint256"},{"name":"amountOutMin","type":"uint256"},{"name":"path","type":"address[]"},{"name":"to","type":"address"},{"name":"deadline","type":"uint256"}],"outputs":[]}]')
ERC20_ABI = json.loads('[{"name":"balanceOf","type":"function","stateMutability":"view","inputs":[{"name":"a","type":"address"}],"outputs":[{"name":"","type":"uint256"}]},{"name":"allowance","type":"function","stateMutability":"view","inputs":[{"name":"o","type":"address"},{"name":"s","type":"address"}],"outputs":[{"name":"","type":"uint256"}]},{"name":"approve","type":"function","stateMutability":"nonpayable","inputs":[{"name":"s","type":"address"},{"name":"v","type":"uint256"}],"outputs":[{"name":"","type":"bool"}]}]')


# ---------- Solana (Jupiter) ----------
def sol_keypair():
    from solders.keypair import Keypair
    return Keypair.from_base58_string(SOL_PRIVATE_KEY)


def sol_rpc(method, params):
    r = requests.post(HELIUS_RPC, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=20).json()
    if "error" in r:
        raise Exception(str(r["error"])[:200])
    return r["result"]


def sol_token_balance(owner, mint):
    res = sol_rpc("getTokenAccountsByOwner", [owner, {"mint": mint}, {"encoding": "jsonParsed"}])
    return sum(int(a["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"]) for a in res["value"])


def sol_swap(input_mint, output_mint, amount):
    from solders.transaction import VersionedTransaction
    kp = sol_keypair()
    q = requests.get(f"{JUP_BASE}/quote", params={
        "inputMint": input_mint, "outputMint": output_mint, "amount": str(int(amount)),
        "slippageBps": int(SLIPPAGE_PCT * 100)}, timeout=15).json()
    if "outAmount" not in q:
        raise Exception("no route: " + str(q)[:150])
    sw = requests.post(f"{JUP_BASE}/swap", json={
        "quoteResponse": q, "userPublicKey": str(kp.pubkey()),
        "wrapAndUnwrapSol": True, "dynamicComputeUnitLimit": True}, timeout=20).json()
    if "swapTransaction" not in sw:
        raise Exception("swap build failed: " + str(sw)[:150])
    tx = VersionedTransaction.from_bytes(base64.b64decode(sw["swapTransaction"]))
    signed = VersionedTransaction(tx.message, [kp])
    sig = sol_rpc("sendTransaction", [base64.b64encode(bytes(signed)).decode(),
                                      {"encoding": "base64", "skipPreflight": False, "maxRetries": 3}])
    for _ in range(30):
        time.sleep(1.5)
        st = sol_rpc("getSignatureStatuses", [[sig]])["value"][0]
        if st:
            if st.get("err"):
                raise Exception("tx failed on-chain: " + str(st["err"]))
            if st.get("confirmationStatus") in ("confirmed", "finalized"):
                return sig, int(q["outAmount"])
    raise Exception("tx not confirmed in time: " + sig)


def sol_buy(token):
    owner = str(sol_keypair().pubkey())
    lamports = sol_rpc("getBalance", [owner])["value"]
    amount = int(lamports * TRADE_FRACTION)
    if amount < 1_000_000:
        raise Exception("SOL balance too low")
    sig, out = sol_swap(SOL_MINT, token, amount)
    got = out
    for _ in range(4):
        try:
            b = sol_token_balance(owner, token)
            if b > 0:
                got = b
                break
        except Exception:
            pass
        time.sleep(1.5)
    return {"spent": amount, "tokens": got, "tx": sig, "spent_txt": f"{amount / 1e9:.4f} SOL",
            "link": f"https://solscan.io/tx/{sig}"}


def sol_sell(pos):
    owner = str(sol_keypair().pubkey())
    bal = sol_token_balance(owner, pos["token"])
    if bal <= 0:
        raise Exception("no token balance to sell")
    sig, out = sol_swap(pos["token"], SOL_MINT, bal)
    pct = (out / pos["size"] - 1) * 100
    return pct, f"{out / 1e9:.4f} SOL", f"https://solscan.io/tx/{sig}"


# ---------- EVM (Uniswap-V2 style routers) ----------
def evm_ctx(chain):
    from web3 import Web3
    from eth_account import Account
    cfg = EVM_CFG[chain]
    w3 = Web3(Web3.HTTPProvider(cfg["rpc"], request_kwargs={"timeout": 20}))
    acct = Account.from_key(EVM_PRIVATE_KEY)
    router = w3.eth.contract(address=Web3.to_checksum_address(cfg["router"]), abi=ROUTER_ABI)
    return Web3, w3, acct, router, cfg


def evm_send(w3, acct, fn, value=0):
    tx = fn.build_transaction({"from": acct.address, "value": value,
                               "nonce": w3.eth.get_transaction_count(acct.address),
                               "gasPrice": w3.eth.gas_price})
    signed = acct.sign_transaction(tx)
    raw = getattr(signed, "raw_transaction", None) or getattr(signed, "rawTransaction")
    h = w3.eth.send_raw_transaction(raw)
    rc = w3.eth.wait_for_transaction_receipt(h, timeout=120)
    if rc.status != 1:
        raise Exception("tx reverted: " + h.hex())
    return h.hex()


def evm_buy(chain, token_addr):
    Web3, w3, acct, router, cfg = evm_ctx(chain)
    bal = w3.eth.get_balance(acct.address)
    amount = int(bal * TRADE_FRACTION)
    if amount < w3.to_wei(0.001, "ether"):
        raise Exception(f"{cfg['sym']} balance too low")
    token = Web3.to_checksum_address(token_addr)
    wn = Web3.to_checksum_address(cfg["wn"])
    out = router.functions.getAmountsOut(amount, [wn, token]).call()[-1]
    min_out = int(out * (1 - SLIPPAGE_PCT / 100))
    fn = router.functions.swapExactETHForTokensSupportingFeeOnTransferTokens(min_out, [wn, token], acct.address, int(time.time()) + 300)
    txh = evm_send(w3, acct, fn, amount)
    erc = w3.eth.contract(address=token, abi=ERC20_ABI)
    got = erc.functions.balanceOf(acct.address).call()
    return {"spent": amount, "tokens": got, "tx": txh, "spent_txt": f"{amount / 1e18:.5f} {cfg['sym']}",
            "link": cfg["explorer"] + txh}


def evm_sell(pos):
    chain = pos["chain"]
    Web3, w3, acct, router, cfg = evm_ctx(chain)
    token = Web3.to_checksum_address(pos["token"])
    wn = Web3.to_checksum_address(cfg["wn"])
    erc = w3.eth.contract(address=token, abi=ERC20_ABI)
    bal = erc.functions.balanceOf(acct.address).call()
    if bal <= 0:
        raise Exception("no token balance to sell")
    if erc.functions.allowance(acct.address, router.address).call() < bal:
        evm_send(w3, acct, erc.functions.approve(router.address, 2 ** 256 - 1))
    quote = router.functions.getAmountsOut(bal, [token, wn]).call()[-1]
    min_out = int(quote * (1 - SLIPPAGE_PCT / 100))
    fn = router.functions.swapExactTokensForETHSupportingFeeOnTransferTokens(bal, min_out, [token, wn], acct.address, int(time.time()) + 300)
    txh = evm_send(w3, acct, fn)
    pct = (quote / pos["size"] - 1) * 100
    return pct, f"{quote / 1e18:.5f} {cfg['sym']}", cfg["explorer"] + txh


def real_buy(pair):
    chain = pair["chainId"]
    symbol = pair["baseToken"]["symbol"]
    token = pair["baseToken"]["address"]
    price = float(pair["priceUsd"])
    try:
        if chain == "solana":
            info = sol_buy(token)
        elif chain in EVM_CFG:
            info = evm_buy(chain, token)
        else:
            send_whatsapp(f"SKIPPED ${symbol}: {chain} not supported for real trades")
            return
    except Exception as e:
        send_whatsapp(f"REAL BUY FAILED ${symbol} on {chain.upper()}: {str(e)[:200]}")
        return
    with lock:
        state["positions"].append({
            "real": True, "chain": chain, "symbol": symbol, "pair": pair["pairAddress"], "token": token,
            "entry": price, "size": info["spent"], "tokens": info["tokens"],
            "time": time.time(), "fails": 0, "sell_tries": 0})
        save_state()
    send_whatsapp(f"REAL BUY ${symbol} on {chain.upper()}\nSpent {info['spent_txt']} ({TRADE_FRACTION * 100:.0f}% of wallet)\nEntry ${price}\nTP +{TP_PCT:.0f}% | SL -{SL_PCT:.0f}%\n{info['link']}")


def close_real_position(pos, reason):
    try:
        if pos["chain"] == "solana":
            pct, got_txt, link = sol_sell(pos)
        else:
            pct, got_txt, link = evm_sell(pos)
    except Exception as e:
        pos["sell_tries"] = pos.get("sell_tries", 0) + 1
        n = pos["sell_tries"]
        send_whatsapp(f"REAL SELL FAILED ${pos['symbol']} ({reason}) try {n}/{MAX_SELL_TRIES}: {str(e)[:150]}")
        if n >= MAX_SELL_TRIES:
            with lock:
                if pos in state["positions"]:
                    state["positions"].remove(pos)
                save_state()
            send_whatsapp(f"STUCK ${pos['symbol']} on {pos['chain'].upper()} - gave up selling (possible honeypot). Token: {pos['token']}")
        return
    with lock:
        if pos in state["positions"]:
            state["positions"].remove(pos)
        state.setdefault("real_closed", []).append({"symbol": pos["symbol"], "pct": pct})
        save_state()
    send_whatsapp(f"REAL SELL ${pos['symbol']} on {pos['chain'].upper()} ({reason})\nReceived {got_txt} ({pct:+.1f}%)\n{link}")


def close_any(pos, price, reason):
    if pos.get("real"):
        close_real_position(pos, reason)
    else:
        close_position(pos, price, reason)


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
        rc = state.get("real_closed", [])
        if rc:
            rw = sum(1 for c in rc if c["pct"] > 0)
            lines.append(f"REAL: {len(rc)} trades, {rw} wins, {len(rc) - rw} losses")
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
                        close_any(pos, pos["entry"] * 0.0001, "NO DATA / RUG")
                    continue
                pos["fails"] = 0
                change = (price / pos["entry"] - 1) * 100
                if change >= TP_PCT:
                    close_any(pos, price, "TAKE PROFIT")
                elif change <= -SL_PCT:
                    close_any(pos, price, "STOP LOSS")
                elif time.time() - pos["time"] > MAX_HOLD_HOURS * 3600:
                    close_any(pos, price, "TIME EXIT")
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

    # --- REAL MODE ---
    real_buy(pair)


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
