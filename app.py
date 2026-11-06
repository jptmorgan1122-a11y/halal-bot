import os, time, json, threading, requests
from flask import Flask

# ================= SETTINGS (set in Render > Environment) =================
GEMINI_KEY = os.getenv("GEMINI_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "halalbot-269e58ea7930")
NTFY_TOKEN = os.getenv("NTFY_TOKEN", "")            # optional ntfy login token

START_BAL = float(os.getenv("START_BAL", "10"))     # fake USD per chain
TRADE_FRACTION = float(os.getenv("TRADE_FRACTION", "0.5"))  # 50% of chain balance per trade
TP_PCT = float(os.getenv("TP_PCT", "110"))          # take profit +110%
SL_PCT = float(os.getenv("SL_PCT", "90"))           # stop loss -90%
FEE_PCT = float(os.getenv("FEE_PCT", "2"))          # fake fees/slippage per round trip
MAX_AGE_SEC = float(os.getenv("MAX_AGE_SEC", "120"))  # pair must be younger than this
MIN_LIQ_USD = float(os.getenv("MIN_LIQ_USD", "1000"))
MAX_HOLD_HOURS = float(os.getenv("MAX_HOLD_HOURS", "24"))
COOLDOWN_SEC = float(os.getenv("COOLDOWN_SEC", "90"))  # pause after a 429
AUDIT_ON = os.getenv("AUDIT_ON", "1") == "1"                 # set AUDIT_ON=0 to disable audit checks
AUDIT_ON_ERROR = os.getenv("AUDIT_ON_ERROR", "skip")          # "skip" = don't buy if audit can't be checked, "allow" = buy anyway
MAX_TAX_PCT = float(os.getenv("MAX_TAX_PCT", "10"))           # EVM buy/sell tax limit
GOPLUS_CHAIN_IDS = {"ethereum": "1", "bsc": "56", "polygon": "137", "base": "8453"}
SCAN_PAUSE = float(os.getenv("SCAN_PAUSE", "2"))  # seconds between scan rounds

CHAINS = ["solana", "polygon", "bsc", "base", "ethereum"]
STATE_FILE = "paper_state.json"

HALAL_PROMPT = ("The data I provide to you . Does it have anything Haram mentioned in the writing only . "
                "Consider Lack of purpose is also haram. don't talk about uncertainty or gahar etc ? "
                "One word ans only . YES OR NO no bullshit only one word ans")

# ================= STATE =================
app = Flask(__name__)
lock = threading.Lock()
seen = set()
stats = {"scanned": 0, "pumpfun": 0, "too_old": 0, "no_website": 0, "no_logo": 0, "low_liq": 0,
         "multi_pair": 0, "audit_fail": 0, "audit_error": 0, "gemini_error": 0, "haram_or_unclear": 0, "bought": 0}
state = {"balances": {c: START_BAL for c in CHAINS}, "positions": [], "closed": [], "last_summary": time.time()}

try:
    with open(STATE_FILE) as f:
        state.update(json.load(f))
except Exception:
    pass


def log(*a):
    print(*a, flush=True)


def save_state():
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f)
    except Exception:
        pass


cooldown_until = 0  # set when the data API answers HTTP 429
GECKO = "https://api.geckoterminal.com/api/v2"
GECKO_NET = {"solana": "solana", "polygon": "polygon_pos", "bsc": "bsc", "base": "base", "ethereum": "eth"}
GECKO_PER_MIN = int(os.getenv("GECKO_PER_MIN", "26"))   # free limit is about 30 calls/min
_calls = []
_rate_lock = threading.Lock()


def gecko_json(path, timeout=15):
    """GeckoTerminal GET with a built-in rate limiter. Returns None on 404, raises on other errors."""
    global cooldown_until
    if time.time() < cooldown_until:
        raise Exception("cooling down after HTTP 429")
    with _rate_lock:
        now = time.time()
        while _calls and now - _calls[0] > 60:
            _calls.pop(0)
        if len(_calls) >= GECKO_PER_MIN:
            time.sleep(max(0.0, 60 - (now - _calls[0]) + 0.2))
        _calls.append(time.time())
    r = requests.get(GECKO + path, headers={"Accept": "application/json;version=20230302"}, timeout=timeout)
    if r.status_code == 404:
        return None
    if r.status_code == 429:
        cooldown_until = time.time() + COOLDOWN_SEC
        raise Exception(f"HTTP 429 (rate limited), pausing {COOLDOWN_SEC:.0f}s")
    if r.status_code != 200:
        raise Exception(f"HTTP {r.status_code} from {path.split('?')[0]}")
    return r.json()


def iso_to_ms(t):
    try:
        import datetime
        return datetime.datetime.fromisoformat(t.replace("Z", "+00:00")).timestamp() * 1000
    except Exception:
        return 0


def pool_to_pair(chain, pool):
    a = pool.get("attributes", {})
    rel = pool.get("relationships", {})
    base_id = rel["base_token"]["data"]["id"]          # e.g. "solana_<mint>"
    token = base_id.split("_", 1)[1]
    symbol = (a.get("name") or "").split(" / ")[0] or "?"
    dex = rel.get("dex", {}).get("data", {}).get("id", "")
    return {"chainId": chain, "pairAddress": a.get("address", ""), "dexId": dex.replace("-", "").lower(),
            "pairCreatedAt": iso_to_ms(a.get("pool_created_at", "")),
            "priceUsd": a.get("base_token_price_usd"),
            "liquidity": {"usd": float(a.get("reserve_in_usd") or 0)},
            "baseToken": {"address": token, "symbol": symbol, "name": symbol}}


# ================= ALERTS (ntfy) =================
def notify(text):
    log("ALERT:", text.replace("\n", " | "))
    headers = {"Title": "Halal Bot"}
    if NTFY_TOKEN:
        headers["Authorization"] = f"Bearer {NTFY_TOKEN}"
    for _ in range(3):
        try:
            r = requests.post(f"https://ntfy.sh/{NTFY_TOPIC}", data=text.encode("utf-8"), headers=headers, timeout=15)
            log("ntfy status:", r.status_code, r.text[:200])
            if r.status_code == 200 or r.status_code == 429:
                return  # 429 = quota reached, retrying won't help (set NTFY_TOKEN)
        except Exception as e:
            log("ntfy error:", e)
        time.sleep(3)


# ================= HALAL CHECK (Gemini: NO = halal, YES = haram) =================
def is_halal(name, desc, website):
    try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_KEY}"
        prompt = f"{HALAL_PROMPT}\n\nToken name: {name}\nDescription: {desc}\nWebsite: {website}"
        r = requests.post(url, json={"contents": [{"parts": [{"text": prompt}]}]}, timeout=20).json()
        word = r["candidates"][0]["content"]["parts"][0]["text"].strip().upper().strip(" .!\n\t\"'")
        return word == "NO"
    except Exception as e:
        log("gemini error:", e)
        stats["gemini_error"] += 1
        return False


# ================= FILTERS =================
def check_rules(pair):
    stats["scanned"] += 1
    if pair["dexId"] == "pumpfun":   # block pump.fun bonding curve, allow pumpswap
        stats["pumpfun"] += 1
        return False, ""
    created = pair.get("pairCreatedAt", 0)
    if not created or time.time() - created / 1000 > MAX_AGE_SEC:
        stats["too_old"] += 1
        return False, ""
    if (pair.get("liquidity") or {}).get("usd", 0) < MIN_LIQ_USD or not pair.get("priceUsd"):
        stats["low_liq"] += 1
        return False, ""
    net = GECKO_NET.get(pair["chainId"])
    token = pair["baseToken"]["address"]
    try:
        info = (gecko_json(f"/networks/{net}/tokens/{token}/info") or {}).get("data", {}).get("attributes", {})
    except Exception as e:
        log("info error:", e)
        return False, ""
    webs = [w for w in (info.get("websites") or []) if w]
    if not webs:
        stats["no_website"] += 1
        return False, ""
    img = info.get("image_url") or ""
    if not img or "missing" in img:
        stats["no_logo"] += 1
        return False, ""
    try:
        pools = (gecko_json(f"/networks/{net}/tokens/{token}/pools") or {}).get("data", [])
        if len(pools) > 1:
            stats["multi_pair"] += 1
            return False, ""
    except Exception:
        pass
    pair["baseToken"]["name"] = info.get("name") or pair["baseToken"]["name"]
    pair["baseToken"]["symbol"] = info.get("symbol") or pair["baseToken"]["symbol"]
    pair["info"] = {"websites": [{"url": w} for w in webs], "imageUrl": img, "header": info.get("description") or ""}
    return True, webs[0]


# ================= AUDIT (RugCheck for Solana, GoPlus for EVM) =================
def audit_check(pair):
    """returns (True, '') if clean, (False, reason) if risky, (None, reason) if it could not be checked"""
    chain = pair["chainId"]
    token = pair["baseToken"]["address"]
    try:
        if chain == "solana":
            r = requests.get(f"https://api.rugcheck.xyz/v1/tokens/{token}/report/summary", timeout=15)
            if r.status_code != 200:
                return None, f"rugcheck HTTP {r.status_code}"
            data = r.json()
            danger = [x.get("name", "?") for x in (data.get("risks") or []) if str(x.get("level", "")).lower() == "danger"]
            if danger:
                return False, "RugCheck danger: " + ", ".join(danger[:3])
            return True, ""
        cid = GOPLUS_CHAIN_IDS.get(chain)
        if not cid:
            return None, "chain not supported for audit"
        r = requests.get(f"https://api.gopluslabs.io/api/v1/token_security/{cid}?contract_addresses={token}", timeout=15)
        if r.status_code != 200:
            return None, f"goplus HTTP {r.status_code}"
        res = (r.json().get("result") or {}).get(token.lower())
        if not res:
            return None, "goplus has no data yet"
        bad = []
        for key, label in [("is_honeypot", "honeypot"), ("cannot_sell_all", "cannot sell all"), ("hidden_owner", "hidden owner"),
                           ("is_mintable", "mintable"), ("transfer_pausable", "pausable"), ("can_take_back_ownership", "ownership takeback")]:
            if str(res.get(key, "0")) == "1":
                bad.append(label)
        if str(res.get("is_open_source", "1")) == "0":
            bad.append("not open source")
        for key in ("buy_tax", "sell_tax"):
            try:
                if float(res.get(key) or 0) * 100 > MAX_TAX_PCT:
                    bad.append(f"{key} {float(res[key]) * 100:.0f}%")
            except Exception:
                pass
        return (False, "GoPlus: " + ", ".join(bad)) if bad else (True, "")
    except Exception as e:
        return None, f"audit error: {e}"


# ================= PAPER TRADING =================
def paper_buy(pair):
    chain = pair["chainId"]
    symbol = pair["baseToken"]["symbol"]
    price = float(pair["priceUsd"])
    link = f"https://dexscreener.com/{chain}/{pair['pairAddress']}"
    with lock:
        bal = state["balances"].setdefault(chain, START_BAL)
        size = bal * TRADE_FRACTION
        if size < 0.5:
            notify(f"SKIPPED ${symbol} on {chain.upper()}: balance too low (${bal:.2f})")
            return
        state["balances"][chain] = bal - size
        state["positions"].append({"chain": chain, "symbol": symbol, "pair": pair["pairAddress"],
                                   "entry": price, "size": size, "time": time.time(), "fails": 0})
        save_state()
    stats["bought"] += 1
    notify(f"PAPER BUY ${symbol} on {chain.upper()}\nSize ${size:.2f} ({TRADE_FRACTION * 100:.0f}% of ${bal:.2f})\n"
           f"Entry ${price}\nTP +{TP_PCT:.0f}% | SL -{SL_PCT:.0f}%\n{link}")


def paper_sell(pos, exit_price, reason):
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
    notify(f"PAPER SELL ${pos['symbol']} on {pos['chain'].upper()} ({reason})\n"
           f"{pct:+.1f}% | PnL {pnl:+.2f} USD\n{pos['chain'].upper()} balance ${bal:.2f}")


def get_price(chain, pair_addr):
    # returns a price, None if the pool truly has no data, or "ERR" if the request itself failed
    try:
        r = gecko_json(f"/networks/{GECKO_NET[chain]}/pools/{pair_addr}", 10)
    except Exception as e:
        log("price error:", e)
        return "ERR"
    try:
        return float(r["data"]["attributes"]["base_token_price_usd"]) if r else None
    except Exception:
        return None


def summary_text():
    with lock:
        closed = list(state["closed"])
        wins = sum(1 for c in closed if c["pnl"] > 0)
        lines = [f"SUMMARY: {len(closed)} trades, {wins} wins, {len(closed) - wins} losses",
                 f"Total PnL {sum(c['pnl'] for c in closed):+.2f} USD"]
        lines += [f"{ch.upper()}: ${b:.2f}" for ch, b in state["balances"].items()]
        lines.append(f"Open positions: {len(state['positions'])}")
    return "\n".join(lines)


def monitor():
    while True:
        try:
            with lock:
                open_pos = list(state["positions"])
            for pos in open_pos:
                price = get_price(pos["chain"], pos["pair"])
                if price == "ERR":
                    continue
                if price is None:
                    pos["fails"] = pos.get("fails", 0) + 1
                    if pos["fails"] >= 15:
                        paper_sell(pos, pos["entry"] * 0.0001, "NO DATA / RUG")
                    continue
                pos["fails"] = 0
                change = (price / pos["entry"] - 1) * 100
                if change >= TP_PCT:
                    paper_sell(pos, price, "TAKE PROFIT")
                elif change <= -SL_PCT:
                    paper_sell(pos, price, "STOP LOSS")
                elif time.time() - pos["time"] > MAX_HOLD_HOURS * 3600:
                    paper_sell(pos, price, "TIME EXIT")
            if time.time() - state.get("last_summary", 0) > 24 * 3600:
                state["last_summary"] = time.time()
                notify(summary_text())
                save_state()
        except Exception as e:
            log("monitor error:", e)
        time.sleep(20)


# ================= SCANNER =================
def handle_pair(pair):
    addr = pair.get("pairAddress")
    if not addr or addr in seen:
        return
    seen.add(addr)
    ok, website = check_rules(pair)
    if not ok:
        return
    if AUDIT_ON:
        ok_audit, why = audit_check(pair)
        if ok_audit is False:
            stats["audit_fail"] += 1
            log("AUDIT FAIL:", pair["baseToken"].get("symbol"), why)
            return
        if ok_audit is None:
            stats["audit_error"] += 1
            log("AUDIT UNKNOWN:", pair["baseToken"].get("symbol"), why)
            if AUDIT_ON_ERROR != "allow":
                return
    name = pair["baseToken"].get("name", "")
    symbol = pair["baseToken"].get("symbol", "")
    desc = (pair.get("info") or {}).get("header", "") or name
    if not is_halal(symbol, desc, website):
        stats["haram_or_unclear"] += 1
        return
    paper_buy(pair)


def scanner():
    while True:
        if time.time() < cooldown_until:
            time.sleep(5)
            continue
        for chain in CHAINS:
            if time.time() < cooldown_until:
                break
            try:
                r = gecko_json(f"/networks/{GECKO_NET[chain]}/new_pools?page=1") or {}
                for pool in r.get("data", []):
                    try:
                        handle_pair(pool_to_pair(chain, pool))
                    except Exception as e:
                        log("pool error:", e)
            except Exception as e:
                log("scan error:", chain, e)
        time.sleep(SCAN_PAUSE)


# ================= WEB PAGE (also keeps Render awake via UptimeRobot) =================
@app.route("/")
def home():
    lines = [f"Bot LIVE PAPER - max pair age {MAX_AGE_SEC:.0f}s"]
    lines += [f"{k}: {v}" for k, v in stats.items()]
    lines.append(f"open positions: {len(state['positions'])}")
    lines.append("")
    lines.append(summary_text())
    lines.append("")
    lines.append("open trades:")
    for p in state["positions"]:
        lines.append(f"  {p['symbol']} on {p['chain']} entry {p['entry']} size ${p['size']:.2f}")
    lines.append("last 10 closed trades:")
    for c in state["closed"][-10:]:
        lines.append(f"  {c['symbol']} {c['chain']} {c['reason']} {c['pct']:+.1f}% ({c['pnl']:+.2f} USD)")
    return "<pre>" + "\n".join(lines) + "</pre>"


@app.route("/test")
def test():
    notify("Test alert from the bot")
    return "sent - check ntfy and the Render logs"


if __name__ == "__main__":
    notify("Bot started in PAPER mode")
    threading.Thread(target=scanner, daemon=True).start()
    threading.Thread(target=monitor, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
                                                                              
