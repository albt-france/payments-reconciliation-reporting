"""
Generate SIMULATED crypto-payment data (fixed seed 42) on top of REAL daily prices.

Nothing here is Coinspaid data. Every assumption is listed in data/ASSUMPTIONS.md.

Six source tables (what each one imitates):
  merchants           CRM
  invoices            internal payment-gateway system
  chain_tx            blockchain explorer extract (incoming payments to our deposit addresses)
  ledger              internal general ledger (UTC)
  partner_statements  exchange partner statement (partner LOCAL time, Europe/Tallinn)
  payouts             payouts to merchants
plus one hidden answer key:
  planted_issues      every break planted on purpose (the pipeline never reads it; used only for validation)

Business flow simulated:
  invoice created -> customer pays on-chain -> confirmations -> ledger deposit (+ deposit fee)
  -> optional conversion to EUR through the partner (merchant gets the invoice rate, we bear FX)
  -> weekly payout to the merchant.

Run:  python src/generate_sim.py          (Parquet only)
      python src/generate_sim.py --csv    (also writes CSV copies)
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

SEED = 42
rng = np.random.default_rng(SEED)

ROOT = Path(__file__).resolve().parents[1]
PRICES = ROOT / "data" / "raw" / "prices" / "prices_daily.csv"
OUT = ROOT / "data" / "clean"

START = pd.Timestamp("2024-11-01")
END = pd.Timestamp("2026-10-31")
SNAPSHOT = pd.Timestamp("2026-11-01 06:00")  # moment all sources were extracted (UTC)
DAYS = pd.date_range(START, END, freq="D")
N_DAYS = len(DAYS)

# ---------------------------------------------------------------------------
# Network rules: seconds per block, confirmations required before crediting
# ---------------------------------------------------------------------------
NETWORKS = {
    "BITCOIN":  {"block_s": 600, "req_conf": 2},
    "LITECOIN": {"block_s": 150, "req_conf": 6},
    "ETHEREUM": {"block_s": 12,  "req_conf": 12},
    "BSC":      {"block_s": 3,   "req_conf": 15},
    "TRON":     {"block_s": 3,   "req_conf": 19},
    "SOLANA":   {"block_s": 0.4, "req_conf": 32},
}
EVM = ["ETHEREUM", "BSC"]  # same address format: the classic "wrong network" mistake

# (currency, network): share inside its group, typical network fee in EUR for one transfer
VOLATILE = {("BTC", "BITCOIN"): (0.45, 1.50), ("ETH", "ETHEREUM"): (0.25, 0.80),
            ("LTC", "LITECOIN"): (0.10, 0.02), ("TRX", "TRON"): (0.08, 0.30),
            ("BNB", "BSC"): (0.06, 0.05), ("SOL", "SOLANA"): (0.06, 0.002)}
STABLE = {("USDT", "TRON"): (0.50, 1.60), ("USDT", "ETHEREUM"): (0.18, 1.80),
          ("USDT", "BSC"): (0.17, 0.06), ("USDC", "ETHEREUM"): (0.09, 1.80),
          ("USDC", "SOLANA"): (0.06, 0.003)}
PAIRS = list(VOLATILE) + list(STABLE)
PAIR_FEE = {**{k: v[1] for k, v in VOLATILE.items()}, **{k: v[1] for k, v in STABLE.items()}}
STABLES = {"USDT", "USDC"}

SEGMENTS = {  # share of merchants, median ticket EUR, deposit fee %, auto-convert share
    "iGaming":     (0.40, 60,  1.30, 0.35),
    "E-commerce":  (0.25, 120, 0.90, 0.90),
    "SaaS":        (0.13, 250, 0.70, 0.95),
    "Forex":       (0.12, 300, 1.10, 0.50),
    "Marketplace": (0.10, 90,  1.00, 0.80),
}
COUNTRIES = ["MT", "CY", "EE", "LT", "DE", "FR", "ES", "GB", "PL", "BR", "CA", "CW"]

# planted break rates and how long ops take to fix them (mean days)
RESOLVE_MEAN_DAYS = {"underpayment": 4, "overpayment": 6, "late_payment": 3, "wrong_network": 10,
                     "address_reuse": 12, "ledger_duplicate": 8, "premature_credit": 5,
                     "rate_gap": 7, "missing_partner_fee": 9}
NEVER_RESOLVED = 0.08           # share of breaks nobody ever fixes (the stale ones)
PARTNER_FEE_PCT = 0.10          # exchange partner fee, % of converted EUR
FEB_2026_ACTIVITY_BOOST = 1.08  # more deposits during the BTC crash month


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
HEX = "0123456789abcdef"
B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def rand_str(n: int, length: int, alphabet: str) -> np.ndarray:
    """n random strings of a given length (fast: built as bytes)."""
    codes = np.frombuffer(alphabet.encode(), dtype=np.uint8)
    idx = rng.integers(0, len(codes), size=(n, length))
    return codes[idx].view(f"S{length}").ravel().astype(str)


def make_address(networks: np.ndarray) -> np.ndarray:
    out = np.empty(len(networks), dtype=object)
    for net, prefix, length, alpha in [("BITCOIN", "bc1q", 38, HEX), ("LITECOIN", "ltc1q", 38, HEX),
                                       ("ETHEREUM", "0x", 40, HEX), ("BSC", "0x", 40, HEX),
                                       ("TRON", "T", 33, B58), ("SOLANA", "", 44, B58)]:
        m = networks == net
        out[m] = np.char.add(prefix, rand_str(m.sum(), length, alpha))
    return out


def make_tx_hash(networks: np.ndarray) -> np.ndarray:
    out = np.empty(len(networks), dtype=object)
    for net in NETWORKS:
        m = networks == net
        if net == "SOLANA":
            out[m] = rand_str(m.sum(), 88, B58)
        elif net in EVM:
            out[m] = np.char.add("0x", rand_str(m.sum(), 64, HEX))
        else:
            out[m] = rand_str(m.sum(), 64, HEX)
    return out


def ar1_index(n: int, sigma: float = 0.25, phi: float = 0.92) -> np.ndarray:
    """Network fee index around 1 (calm days ~1, congested days 2-4x)."""
    x = np.zeros(n)
    eps = rng.normal(0, sigma, n)
    for t in range(1, n):
        x[t] = phi * x[t - 1] + eps[t]
    return np.exp(x)


def lag_days(issue_type: str, n: int) -> np.ndarray:
    return rng.exponential(RESOLVE_MEAN_DAYS[issue_type], n) + 0.2


def resolution_ts(issue_type: str, event_ts: pd.Series) -> pd.Series:
    """When ops fixed the break; NaT if never fixed or not yet fixed at extraction time."""
    n = len(event_ts)
    ts = event_ts + pd.to_timedelta(lag_days(issue_type, n), unit="D")
    never = rng.random(n) < NEVER_RESOLVED
    ts = ts.where(~never & (ts < SNAPSHOT))
    return ts


# ---------------------------------------------------------------------------
# 1. Real prices
# ---------------------------------------------------------------------------
prices = pd.read_csv(PRICES, parse_dates=["date"])
px = prices.pivot(index="date", columns="currency", values="price_eur").reindex(DAYS)
px_trail = px.rolling(90, min_periods=1).mean()  # what customers "remember" a coin is worth


def price_on(dates: pd.Series, currencies: pd.Series, table: pd.DataFrame = px) -> np.ndarray:
    d = dates.dt.normalize().clip(START, END)
    rows = table.index.get_indexer(d)
    cols = table.columns.get_indexer(currencies)
    return table.to_numpy()[rows, cols]


# network fee in EUR per pair and day
fee_idx = {net: ar1_index(N_DAYS) for net in NETWORKS}
pair_fee_day = pd.DataFrame({f"{c}|{n}": PAIR_FEE[(c, n)] * fee_idx[n] for c, n in PAIRS}, index=DAYS)


def network_fee_eur(dates: pd.Series, currencies: pd.Series, networks: pd.Series) -> np.ndarray:
    rows = pair_fee_day.index.get_indexer(dates.dt.normalize().clip(START, END))
    cols = pair_fee_day.columns.get_indexer(currencies + "|" + networks)
    return pair_fee_day.to_numpy()[rows, cols]


# ---------------------------------------------------------------------------
# 2. Merchants (CRM)
# ---------------------------------------------------------------------------
N_EXISTING, N_NEW = 550, 950
n_m = N_EXISTING + N_NEW
seg_names = list(SEGMENTS)
seg = rng.choice(seg_names, n_m, p=[SEGMENTS[s][0] for s in seg_names])
signup = np.concatenate([
    pd.to_datetime(rng.integers(pd.Timestamp("2022-01-01").value // 10**9, START.value // 10**9, N_EXISTING), unit="s"),
    pd.to_datetime(rng.integers(START.value // 10**9, END.value // 10**9, N_NEW), unit="s"),
]).astype("datetime64[ns]")
weight = np.minimum(rng.pareto(1.1, n_m) + 1, 120)  # a few merchants carry most of the volume
merchants = pd.DataFrame({
    "merchant_id": [f"M{i:04d}" for i in range(1, n_m + 1)],
    "merchant_name": [f"Merchant {i:04d}" for i in range(1, n_m + 1)],
    "segment": seg,
    "country": rng.choice(COUNTRIES, n_m),
    "signup_date": pd.to_datetime(signup).normalize(),
    "account_manager": rng.choice([f"AM {i:02d}" for i in range(1, 9)], n_m),
})
big = weight >= np.quantile(weight, 0.95)
base_dep = merchants["segment"].map({s: v[2] for s, v in SEGMENTS.items()}).to_numpy()
merchants["deposit_fee_pct"] = np.round(base_dep - 0.25 * big + rng.choice([-0.1, 0, 0.1], n_m), 2)
merchants["conversion_fee_pct"] = np.round(np.where(big, 0.5, rng.choice([0.6, 0.8, 1.0], n_m)), 2)
merchants["payout_fee_pct"] = np.round(np.where(big, 0.15, rng.choice([0.2, 0.3, 0.4], n_m)), 2)
merchants["auto_convert_share"] = merchants["segment"].map({s: v[3] for s, v in SEGMENTS.items()})

# lifecycle: churn date (exponential, median ~2 years), ramp-up in the first 60 days
churn = merchants["signup_date"] + pd.to_timedelta(rng.exponential(1000, n_m), unit="D")
merchants["_churn"] = churn

# the "big merchant that drops off": the largest Forex merchant already live at the start
fx_existing = np.where((seg == "Forex") & (np.arange(n_m) < N_EXISTING))[0]
decliner = fx_existing[np.argmax(weight[fx_existing])]
weight[decliner] = 110.0
merchants.loc[decliner, "_churn"] = pd.Timestamp("2030-01-01")

# activity matrix (days x merchants)
day_num = DAYS.to_numpy()[:, None]
age = (day_num - merchants["signup_date"].to_numpy()[None, :]) / np.timedelta64(1, "D")
alive = (age >= 0) & (day_num < merchants["_churn"].to_numpy()[None, :])
ramp = np.where(age < 30, 0.3, np.where(age < 60, 0.7, 1.0))
w = weight[None, :] * alive * ramp
months_since_mar26 = ((DAYS - pd.Timestamp("2026-03-01")).days / 30.4).to_numpy()
w[:, decliner] *= np.clip(1 - 0.22 * np.clip(months_since_mar26, 0, None), 0.06, 1)

# ---------------------------------------------------------------------------
# 3. Invoices (gateway)
# ---------------------------------------------------------------------------
month_idx = (DAYS.year - START.year) * 12 + DAYS.month - START.month
base = 600 * 1.035 ** month_idx.to_numpy()
base = base * np.where((DAYS.year == 2026) & (DAYS.month == 2), FEB_2026_ACTIVITY_BOOST, 1.0)
n_per_day = rng.poisson(base)

hours_w = 1 + 1.2 * np.exp(-((np.arange(24) - 20) ** 2) / (2 * 3 ** 2))  # evening peak (UTC)
hours_w /= hours_w.sum()

parts = []
for d, (day, n) in enumerate(zip(DAYS, n_per_day)):
    p = w[d] / w[d].sum()
    m_idx = rng.choice(n_m, n, p=p)
    secs = rng.choice(24, n, p=hours_w) * 3600 + rng.integers(0, 3600, n)
    parts.append(pd.DataFrame({"m_idx": m_idx, "created_ts": day + pd.to_timedelta(secs, unit="s"),
                               "month_idx": month_idx[d]}))
inv = pd.concat(parts, ignore_index=True).sort_values("created_ts", ignore_index=True)
n_inv = len(inv)
inv["invoice_id"] = [f"INV{i:08d}" for i in range(1, n_inv + 1)]
inv["merchant_id"] = merchants["merchant_id"].to_numpy()[inv["m_idx"]]
inv["segment"] = merchants["segment"].to_numpy()[inv["m_idx"]]

# stablecoin share rises from 32% to 62% over the 24 months
p_stable = 0.32 + 0.30 * inv["month_idx"] / 23
is_stable = rng.random(n_inv) < p_stable
vol_keys, vol_p = list(VOLATILE), np.array([v[0] for v in VOLATILE.values()])
st_keys, st_p = list(STABLE), np.array([v[0] for v in STABLE.values()])
pick_v = rng.choice(len(vol_keys), n_inv, p=vol_p / vol_p.sum())
pick_s = rng.choice(len(st_keys), n_inv, p=st_p / st_p.sum())
inv["currency"] = np.where(is_stable, [st_keys[i][0] for i in pick_s], [vol_keys[i][0] for i in pick_v])
inv["network"] = np.where(is_stable, [st_keys[i][1] for i in pick_s], [vol_keys[i][1] for i in pick_v])

# ticket size: e-commerce/SaaS price in EUR; iGaming/Forex players send a familiar number of COINS
median_ticket = inv["segment"].map({s: v[1] for s, v in SEGMENTS.items()}).to_numpy()
target_eur = np.minimum(median_ticket * rng.lognormal(0, 1.0, n_inv), 50_000)
spot = price_on(inv["created_ts"], inv["currency"])
noise = np.where(inv["currency"].isin(STABLES), rng.normal(0, 0.0005, n_inv), rng.normal(0, 0.006, n_inv))
inv["rate_used"] = spot * (1 + noise)  # EUR per coin, locked on the invoice
crypto_denominated = inv["segment"].isin(["iGaming", "Forex"]) & ~inv["currency"].isin(STABLES)
trail = price_on(inv["created_ts"], inv["currency"], px_trail)
amount_crypto = np.where(crypto_denominated, target_eur / trail, target_eur / inv["rate_used"])
decimals = np.where(inv["currency"].isin(STABLES | {"TRX"}), 2, 6)
inv["amount_crypto"] = np.round(amount_crypto * 10.0 ** decimals) / 10.0 ** decimals
inv["amount_crypto"] = np.maximum(inv["amount_crypto"], 10.0 ** -decimals)
inv["amount_eur_at_rate"] = (inv["amount_crypto"] * inv["rate_used"]).round(2)
expiry_min = np.where(inv["network"].isin(["BITCOIN", "LITECOIN"]), 60, 30)
inv["expiry_ts"] = inv["created_ts"] + pd.to_timedelta(expiry_min, unit="m")
inv["pay_address"] = make_address(inv["network"].to_numpy())

# ---- outcome of each invoice --------------------------------------------
fee_eur_now = network_fee_eur(inv["created_ts"], inv["currency"], inv["network"])
u = rng.random(n_inv)
is_evm = inv["network"].isin(EVM).to_numpy()
p_under = np.clip(0.002 + 0.25 * fee_eur_now / inv["amount_eur_at_rate"].clip(lower=1), 0, 0.05)
outcome = np.full(n_inv, "paid", dtype=object)
cut = 0.17
outcome[u < cut] = "abandoned"
m = (u >= cut) & (u < cut + 0.0025) & is_evm
outcome[m] = "wrong_network"
cut += 0.0025
outcome[(u >= cut) & (u < cut + 0.005)] = "late_payment"
cut += 0.005
outcome[(u >= cut) & (u < cut + 0.0025)] = "overpayment"
cut += 0.0025
outcome[(u >= cut) & (u < cut + p_under)] = "underpayment"
inv["_outcome"] = outcome

# amount actually sent by the customer
shortfall = np.clip(fee_eur_now / inv["amount_eur_at_rate"].clip(lower=1) * rng.uniform(0.8, 1.5, n_inv), 0.01, 0.30)
excess = rng.uniform(0.01, 0.10, n_inv)
received = inv["amount_crypto"].to_numpy().copy()
received = np.where(outcome == "underpayment", received * (1 - shortfall), received)
received = np.where(outcome == "overpayment", received * (1 + excess), received)
inv["_received"] = np.round(received * 10.0 ** decimals) / 10.0 ** decimals

# when the customer's transaction is first seen on-chain
in_window = rng.uniform(0.05, 0.95, n_inv) * expiry_min
late_min = expiry_min + rng.uniform(5, 480, n_inv)
seen_min = np.where(outcome == "late_payment", late_min, in_window)
inv["_seen_ts"] = inv["created_ts"] + pd.to_timedelta(seen_min, unit="m")
inv = inv[inv["_seen_ts"] < SNAPSHOT - pd.Timedelta(hours=1)].reset_index(drop=True)  # keep extract consistent
n_inv = len(inv)

# premature credit: internal says paid, chain tx still stuck in the mempool
premature = (inv["_outcome"] == "paid").to_numpy() & (rng.random(n_inv) < 0.0012)
inv.loc[premature, "_outcome"] = "premature_credit"

status_map = {"paid": "paid", "premature_credit": "paid", "underpayment": "underpaid",
              "overpayment": "overpaid", "late_payment": "expired", "wrong_network": "expired",
              "abandoned": "expired"}
inv["status"] = inv["_outcome"].map(status_map)

# ---------------------------------------------------------------------------
# 4. On-chain transactions (explorer extract)
# ---------------------------------------------------------------------------
payers = inv[inv["_outcome"] != "abandoned"].copy()
tx = pd.DataFrame({
    "invoice_ref": payers["invoice_id"].to_numpy(),  # helper only, dropped before saving
    "merchant_id": payers["merchant_id"].to_numpy(),
    "currency": payers["currency"].to_numpy(),
    "network": payers["network"].to_numpy(),
    "to_address": payers["pay_address"].to_numpy(),
    "amount_crypto": payers["_received"].to_numpy(),
    "seen_ts": payers["_seen_ts"].to_numpy(),
    "rate_used": payers["rate_used"].to_numpy(),
    "_outcome": payers["_outcome"].to_numpy(),
    "_expiry": payers["expiry_ts"].to_numpy(),
})
# wrong network: USDT/USDC/ETH/BNB sent on the other EVM chain to the same address
wn = tx["_outcome"] == "wrong_network"
tx.loc[wn, "network"] = np.where(tx.loc[wn, "network"] == "ETHEREUM", "BSC", "ETHEREUM")

# split payments (NOT an error): the customer pays one invoice in two transfers before expiry.
# The pipeline must add them up instead of flagging an underpayment + an extra payment.
split = tx[tx["_outcome"] == "paid"].sample(frac=0.003, random_state=SEED + 2).index
first_share = rng.uniform(0.3, 0.7, len(split))
second = tx.loc[split].copy()
second["amount_crypto"] = (tx.loc[split, "amount_crypto"] * (1 - first_share)).round(8)
tx.loc[split, "amount_crypto"] = (tx.loc[split, "amount_crypto"] - second["amount_crypto"]).round(8)
room = (second["_expiry"] - second["seen_ts"]).dt.total_seconds()
second["seen_ts"] = second["seen_ts"] + pd.to_timedelta(room * rng.uniform(0.2, 0.9, len(split)), unit="s")
tx = pd.concat([tx, second], ignore_index=True)

# address reuse: a customer pays again, weeks later, to an old invoice address
paid_like = tx[tx["_outcome"] == "paid"]
reuse = paid_like.sample(frac=0.0010, random_state=SEED).copy()
reuse["seen_ts"] = reuse["seen_ts"] + pd.to_timedelta(rng.uniform(2, 60, len(reuse)), unit="D")
reuse = reuse[reuse["seen_ts"] < SNAPSHOT - pd.Timedelta(hours=1)]
reuse["amount_crypto"] = (reuse["amount_crypto"] * rng.uniform(0.5, 2.0, len(reuse))).round(6)
reuse["_outcome"] = "address_reuse"
reuse["rate_used"] = price_on(reuse["seen_ts"], reuse["currency"])
tx = pd.concat([tx, reuse], ignore_index=True)

tx["tx_hash"] = make_tx_hash(tx["network"].to_numpy())
block_s = tx["network"].map({k: v["block_s"] for k, v in NETWORKS.items()}).to_numpy()
req = tx["network"].map({k: v["req_conf"] for k, v in NETWORKS.items()}).to_numpy()
tx["block_ts"] = tx["seen_ts"] + pd.to_timedelta(rng.uniform(0.2, 1.5, len(tx)) * block_s, unit="s")
stuck = tx["_outcome"] == "premature_credit"
tx.loc[stuck, "block_ts"] = pd.NaT
conf = np.floor((SNAPSHOT - tx["block_ts"]).dt.total_seconds() / block_s) + 1
tx["confirmations"] = conf.fillna(0).astype(np.int64)
fee_eur_tx = network_fee_eur(tx["seen_ts"], tx["currency"], tx["network"])
tx["network_fee"] = (fee_eur_tx / price_on(tx["seen_ts"], tx["currency"])).round(8)  # paid by sender, in coin
tx["_fee_eur"] = fee_eur_tx
tx["_req_conf"] = req
tx["_block_s"] = block_s

# ---------------------------------------------------------------------------
# 5. Ledger (UTC), built in blocks; entry ids assigned in build order
# ---------------------------------------------------------------------------
fee_pct = merchants.set_index("merchant_id")
_eid = [0]


def new_ids(n: int) -> list:
    ids = [f"LE{i:09d}" for i in range(_eid[0] + 1, _eid[0] + n + 1)]
    _eid[0] += n
    return ids


planted = []  # rows of the hidden answer key


def add_planted(issue_type, source_table, record_id, merchant_id, amount_eur, event_ts, resolved_ts):
    planted.append(pd.DataFrame({"issue_type": issue_type, "source_table": source_table,
                                 "record_id": np.asarray(record_id), "merchant_id": np.asarray(merchant_id),
                                 "amount_eur": np.round(np.asarray(amount_eur, dtype=float), 2),
                                 "event_ts": np.asarray(event_ts), "resolved_ts": np.asarray(resolved_ts)}))


# --- 5a. which transactions get credited, and when ------------------------
confirm_delay = pd.to_timedelta(tx["_req_conf"] * tx["_block_s"] + rng.uniform(10, 180, len(tx)), unit="s")
tx["_credit_ts"] = tx["block_ts"] + confirm_delay
normal_credit = tx["_outcome"].isin(["paid", "underpayment", "overpayment"])
tx.loc[stuck, "_credit_ts"] = tx.loc[stuck, "seen_ts"] + pd.to_timedelta(rng.uniform(30, 180, stuck.sum()), unit="s")

# breaks fixed by a manual credit (late / wrong network / address reuse)
for t in ["late_payment", "wrong_network", "address_reuse"]:
    mt = tx["_outcome"] == t
    res = resolution_ts(t, tx.loc[mt, "seen_ts"])
    tx.loc[mt, "_credit_ts"] = res
    add_planted(t, "chain_tx", tx.loc[mt, "tx_hash"], tx.loc[mt, "merchant_id"],
                tx.loc[mt, "amount_crypto"] * tx.loc[mt, "rate_used"], tx.loc[mt, "seen_ts"], res)
    if t != "late_payment":  # manual credit valued at the day's price, not an invoice rate
        tx.loc[mt, "rate_used"] = price_on(tx.loc[mt, "seen_ts"], tx.loc[mt, "currency"])

credited = tx[tx["_credit_ts"].notna() & (tx["_credit_ts"] < SNAPSHOT)].copy()
credited["invoice_id"] = np.where(credited["_outcome"] == "address_reuse", None, credited["invoice_ref"])
credited["amount_eur"] = (credited["amount_crypto"] * credited["rate_used"]).round(2)
credited["entry_id"] = new_ids(len(credited))
dep_pct = credited["merchant_id"].map(fee_pct["deposit_fee_pct"]).to_numpy() / 100

deposits = pd.DataFrame({
    "entry_id": credited["entry_id"], "invoice_id": credited["invoice_id"], "tx_hash": credited["tx_hash"],
    "ref": None, "merchant_id": credited["merchant_id"], "entry_type": "deposit", "fee_type": None,
    "amount": credited["amount_crypto"], "currency": credited["currency"], "amount_eur": credited["amount_eur"],
    "booked_ts": credited["_credit_ts"],
})
dep_fees = pd.DataFrame({
    "entry_id": new_ids(len(credited)), "invoice_id": credited["invoice_id"], "tx_hash": credited["tx_hash"],
    "ref": None, "merchant_id": credited["merchant_id"], "entry_type": "fee", "fee_type": "deposit",
    "amount": (credited["amount_crypto"] * dep_pct).round(8), "currency": credited["currency"],
    "amount_eur": (credited["amount_eur"] * dep_pct).round(2),
    "booked_ts": credited["_credit_ts"] + pd.Timedelta(seconds=1),
})

# --- 5b. under / over payments: refund closes the exception ---------------
refund_parts = []
for t in ["underpayment", "overpayment"]:
    c = credited[credited["_outcome"] == t]
    inv_amt = c["invoice_ref"].map(inv.set_index("invoice_id")["amount_crypto"])
    gap = (c["amount_crypto"] - inv_amt).abs()  # what is missing or extra, in coin
    res = resolution_ts(t, c["seen_ts"])
    add_planted(t, "invoices", c["invoice_ref"], c["merchant_id"], gap * c["rate_used"], c["seen_ts"], res)
    done = res.notna()
    refund_amt = np.where(t == "underpayment", c["amount_crypto"], gap)  # under: send it all back
    refund_parts.append(pd.DataFrame({
        "entry_id": new_ids(int(done.sum())), "invoice_id": c.loc[done, "invoice_ref"], "tx_hash": c.loc[done, "tx_hash"],
        "ref": c.loc[done, "invoice_ref"], "merchant_id": c.loc[done, "merchant_id"], "entry_type": "refund",
        "fee_type": None, "amount": np.round(refund_amt[done.to_numpy()], 8), "currency": c.loc[done, "currency"],
        "amount_eur": np.round(refund_amt[done.to_numpy()] * c.loc[done, "rate_used"], 2), "booked_ts": res[done],
    }))

# --- 5c. premature credits: reversed once ops notices --------------------
pc = deposits[deposits["tx_hash"].isin(tx.loc[stuck, "tx_hash"])]
res = resolution_ts("premature_credit", pc["booked_ts"])
add_planted("premature_credit", "ledger", pc["entry_id"], pc["merchant_id"], pc["amount_eur"], pc["booked_ts"], res)
done = res.notna()
reversals_pc = pc[done].assign(entry_id=new_ids(int(done.sum())), ref=pc.loc[done, "entry_id"],
                               entry_type="reversal", booked_ts=res[done])

# --- 5d. duplicate deposit lines (a retry that posted twice) -------------
dup_src = deposits[~deposits["tx_hash"].isin(pc["tx_hash"])].sample(frac=0.004, random_state=SEED + 1)
dups = dup_src.assign(entry_id=new_ids(len(dup_src)),
                      booked_ts=dup_src["booked_ts"] + pd.to_timedelta(rng.uniform(1, 30, len(dup_src)), unit="s"))
res = resolution_ts("ledger_duplicate", dups["booked_ts"])
add_planted("ledger_duplicate", "ledger", dups["entry_id"], dups["merchant_id"], dups["amount_eur"], dups["booked_ts"], res)
done = res.notna()
reversals_dup = dups[done].assign(entry_id=new_ids(int(done.sum())), ref=dups.loc[done, "entry_id"],
                                  entry_type="reversal", booked_ts=res[done])

# --- 5e. conversions to EUR through the exchange partner -----------------
conv_share = credited["merchant_id"].map(fee_pct["auto_convert_share"]).to_numpy()
to_convert = (rng.random(len(credited)) < conv_share) & ~credited["_outcome"].eq("premature_credit").to_numpy() \
    & ~credited["currency"].eq("EUR").to_numpy()
cv = credited[to_convert].copy()
cv["net_crypto"] = (cv["amount_crypto"] * (1 - cv["merchant_id"].map(fee_pct["deposit_fee_pct"]) / 100)).round(8)
cv["ref"] = [f"CNV{i:08d}" for i in range(1, len(cv) + 1)]
cv["conv_ts"] = cv["_credit_ts"] + pd.to_timedelta(rng.uniform(60, 1200, len(cv)), unit="s")
cv["booked_eur"] = (cv["net_crypto"] * cv["rate_used"]).round(2)  # merchant keeps the invoice rate
slip = rng.normal(-0.001, 0.002, len(cv))  # normal execution slippage
gap_flag = rng.random(len(cv)) < 0.005  # planted: conversion executed far from the invoice rate
gap_size = rng.uniform(0.02, 0.08, len(cv)) * np.where(rng.random(len(cv)) < 0.75, -1, 1)
cv["exec_eur"] = (cv["booked_eur"] * (1 + np.where(gap_flag, gap_size, slip))).round(2)
cv["partner_fee"] = (cv["exec_eur"] * PARTNER_FEE_PCT / 100).round(2)
conv_pct = cv["merchant_id"].map(fee_pct["conversion_fee_pct"]).to_numpy() / 100

conversions = pd.DataFrame({
    "entry_id": new_ids(len(cv)), "invoice_id": cv["invoice_id"], "tx_hash": cv["tx_hash"], "ref": cv["ref"],
    "merchant_id": cv["merchant_id"], "entry_type": "conversion", "fee_type": None, "amount": cv["net_crypto"],
    "currency": cv["currency"], "amount_eur": cv["booked_eur"], "booked_ts": cv["conv_ts"],
})
conv_fees = pd.DataFrame({
    "entry_id": new_ids(len(cv)), "invoice_id": cv["invoice_id"], "tx_hash": cv["tx_hash"], "ref": cv["ref"],
    "merchant_id": cv["merchant_id"], "entry_type": "fee", "fee_type": "conversion",
    "amount": (cv["booked_eur"] * conv_pct).round(2), "currency": "EUR",
    "amount_eur": (cv["booked_eur"] * conv_pct).round(2), "booked_ts": cv["conv_ts"] + pd.Timedelta(seconds=1),
})

# rate gaps: fixed later by an FX adjustment entry
g = cv[gap_flag]
res = resolution_ts("rate_gap", g["conv_ts"])
add_planted("rate_gap", "partner_statements", g["ref"], g["merchant_id"], g["exec_eur"] - g["booked_eur"], g["conv_ts"], res)
done = res.notna()
fx_adj = pd.DataFrame({
    "entry_id": new_ids(int(done.sum())), "invoice_id": g.loc[done, "invoice_id"], "tx_hash": g.loc[done, "tx_hash"],
    "ref": g.loc[done, "ref"], "merchant_id": g.loc[done, "merchant_id"], "entry_type": "fx_adjustment",
    "fee_type": None, "amount": (g.loc[done, "exec_eur"] - g.loc[done, "booked_eur"]).round(2), "currency": "EUR",
    "amount_eur": (g.loc[done, "exec_eur"] - g.loc[done, "booked_eur"]).round(2), "booked_ts": res[done],
})

# partner fee booked as a cost; some are missing and only booked late (or never)
missing = rng.random(len(cv)) < 0.004
pf_ts = cv["conv_ts"] + pd.to_timedelta(rng.uniform(60, 7200, len(cv)), unit="s")
mres = resolution_ts("missing_partner_fee", cv.loc[missing, "conv_ts"])
add_planted("missing_partner_fee", "partner_statements", cv.loc[missing, "ref"], cv.loc[missing, "merchant_id"],
            cv.loc[missing, "partner_fee"], cv.loc[missing, "conv_ts"], mres)
pf_ts[missing] = mres
keep = pf_ts.notna()
partner_fees = pd.DataFrame({
    "entry_id": new_ids(int(keep.sum())), "invoice_id": None, "tx_hash": None, "ref": cv.loc[keep, "ref"],
    "merchant_id": None, "entry_type": "partner_fee", "fee_type": None, "amount": cv.loc[keep, "partner_fee"],
    "currency": "EUR", "amount_eur": cv.loc[keep, "partner_fee"], "booked_ts": pf_ts[keep],
})

# --- 6. Partner statements (LOCAL time Europe/Tallinn) -------------------
local = cv["conv_ts"].dt.tz_localize("UTC").dt.tz_convert("Europe/Tallinn").dt.tz_localize(None)
partner = pd.DataFrame({
    "stmt_id": [f"PS{i:08d}" for i in range(1, len(cv) + 1)],
    "ref": cv["ref"].to_numpy(),
    "value_ts_local": local.dt.floor("s").to_numpy(),
    "value_date": local.dt.date.astype(str).to_numpy(),
    "currency_sold": cv["currency"].to_numpy(),
    "amount_crypto": cv["net_crypto"].to_numpy(),
    "amount_eur": cv["exec_eur"].to_numpy(),
    "fees_eur": cv["partner_fee"].to_numpy(),
})
# month-end cut-off: the partner books it in the NEXT month (local time) while our UTC ledger books it this month
tz_shift = local.dt.to_period("M").to_numpy() != cv["conv_ts"].dt.to_period("M").to_numpy()
add_planted("timezone_cutoff", "partner_statements", cv.loc[tz_shift, "ref"], cv.loc[tz_shift, "merchant_id"],
            cv.loc[tz_shift, "exec_eur"], cv.loc[tz_shift, "conv_ts"], cv.loc[tz_shift, "conv_ts"])

# --- 7. Weekly payouts (Monday 10:00 UTC, for the previous week) ---------
def next_monday(ts: pd.Series) -> pd.Series:
    d = ts.dt.normalize()
    return d + pd.to_timedelta(7 - d.dt.day_of_week, unit="D")


eur_flow = pd.DataFrame({"merchant_id": cv["merchant_id"], "currency": "EUR",
                         "amount": cv["booked_eur"] - conv_fees["amount"].to_numpy(),
                         "pay_day": next_monday(cv["conv_ts"])})
nc = credited[~to_convert]
crypto_flow = pd.DataFrame({"merchant_id": nc["merchant_id"], "currency": nc["currency"],
                            "amount": nc["amount_crypto"] * (1 - nc["merchant_id"].map(fee_pct["deposit_fee_pct"]) / 100),
                            "pay_day": next_monday(nc["_credit_ts"])})
po = pd.concat([eur_flow, crypto_flow]).groupby(["merchant_id", "currency", "pay_day"], as_index=False)["amount"].sum()
po = po[po["pay_day"] <= END].sort_values(["pay_day", "merchant_id", "currency"], ignore_index=True)
po["ts"] = po["pay_day"] + pd.Timedelta(hours=10) + pd.to_timedelta(rng.integers(0, 3600, len(po)), unit="s")
po["amount"] = np.where(po["currency"] == "EUR", po["amount"].round(2), po["amount"].round(8))
eur_rate = np.where(po["currency"] == "EUR", 1.0, price_on(po["ts"], po["currency"].where(po["currency"] != "EUR", "USDT")))
po["amount_eur"] = (po["amount"] * eur_rate).round(2)
failed = rng.random(len(po)) < 0.01
po["status"] = np.where(failed, "failed", "completed")
retry = po[failed].copy()
retry["ts"] = retry["ts"] + pd.Timedelta(days=1)
retry["status"] = "completed"
payouts = pd.concat([po, retry], ignore_index=True).sort_values("ts", ignore_index=True)
payouts["payout_id"] = [f"PO{i:07d}" for i in range(1, len(payouts) + 1)]
payouts = payouts[["payout_id", "merchant_id", "ts", "amount", "currency", "amount_eur", "status"]]

done_po = payouts[payouts["status"] == "completed"]
po_pct = done_po["merchant_id"].map(fee_pct["payout_fee_pct"]).to_numpy() / 100
payout_entries = pd.DataFrame({
    "entry_id": new_ids(len(done_po)), "invoice_id": None, "tx_hash": None, "ref": done_po["payout_id"],
    "merchant_id": done_po["merchant_id"], "entry_type": "payout", "fee_type": None, "amount": done_po["amount"],
    "currency": done_po["currency"], "amount_eur": done_po["amount_eur"], "booked_ts": done_po["ts"],
})
payout_fees = pd.DataFrame({
    "entry_id": new_ids(len(done_po)), "invoice_id": None, "tx_hash": None, "ref": done_po["payout_id"],
    "merchant_id": done_po["merchant_id"], "entry_type": "fee", "fee_type": "payout",
    "amount": (done_po["amount_eur"] * po_pct).round(2), "currency": "EUR",
    "amount_eur": (done_po["amount_eur"] * po_pct).round(2), "booked_ts": done_po["ts"] + pd.Timedelta(seconds=1),
})

# --- 8. Network costs we pay (address sweeps + crypto payouts), booked daily per network
sweep_factor = np.where(credited["network"] == "BITCOIN", 0.25, 1.0)  # BTC sweeps are batched
sweep = pd.DataFrame({"day": credited["_credit_ts"].dt.normalize(), "network": credited["network"],
                      "cost": network_fee_eur(credited["_credit_ts"], credited["currency"], credited["network"]) * sweep_factor})
cp = done_po[done_po["currency"] != "EUR"]
cp_net = cp["currency"].map({"BTC": "BITCOIN", "ETH": "ETHEREUM", "LTC": "LITECOIN", "TRX": "TRON", "BNB": "BSC",
                             "SOL": "SOLANA", "USDT": "TRON", "USDC": "ETHEREUM"})
payout_cost = pd.DataFrame({"day": cp["ts"].dt.normalize(), "network": cp_net,
                            "cost": network_fee_eur(cp["ts"], cp["currency"], cp_net)})
nc_daily = pd.concat([sweep, payout_cost]).groupby(["day", "network"], as_index=False)["cost"].sum()
nc_daily = nc_daily[nc_daily["day"] <= END]
network_costs = pd.DataFrame({
    "entry_id": new_ids(len(nc_daily)), "invoice_id": None, "tx_hash": None,
    "ref": "NC-" + nc_daily["day"].dt.strftime("%Y%m%d") + "-" + nc_daily["network"], "merchant_id": None,
    "entry_type": "network_cost", "fee_type": None, "amount": nc_daily["cost"].round(2), "currency": "EUR",
    "amount_eur": nc_daily["cost"].round(2), "booked_ts": nc_daily["day"] + pd.Timedelta(hours=23, minutes=59),
})

ledger = pd.concat([deposits, dep_fees, *refund_parts, reversals_pc, dups, reversals_dup, conversions, conv_fees,
                    fx_adj, partner_fees, payout_entries, payout_fees, network_costs], ignore_index=True)
ledger = ledger[ledger["booked_ts"] < SNAPSHOT].sort_values(["booked_ts", "entry_id"], ignore_index=True)
ledger["booked_ts"] = ledger["booked_ts"].dt.floor("ms")

# ---------------------------------------------------------------------------
# 9. Save
# ---------------------------------------------------------------------------
invoices = inv[["invoice_id", "merchant_id", "created_ts", "expiry_ts", "currency", "network", "pay_address",
                "amount_crypto", "amount_eur_at_rate", "rate_used", "status"]].copy()
invoices["rate_used"] = invoices["rate_used"].round(6)
chain_tx = tx[["tx_hash", "network", "currency", "to_address", "amount_crypto", "seen_ts", "block_ts",
               "confirmations", "network_fee"]].sort_values("seen_ts", ignore_index=True)

# Each system names networks its own way (the cleaning step must map them to one standard):
#   gateway  -> token-standard labels a merchant sees ("USDT TRC20")
#   explorer -> lowercase chain names
GATEWAY_NET = {("BTC", "BITCOIN"): "BTC", ("LTC", "LITECOIN"): "LTC", ("ETH", "ETHEREUM"): "ETH",
               ("TRX", "TRON"): "TRX", ("BNB", "BSC"): "BEP20", ("SOL", "SOLANA"): "SOL",
               ("USDT", "TRON"): "TRC20", ("USDT", "ETHEREUM"): "ERC20", ("USDT", "BSC"): "BEP20",
               ("USDC", "ETHEREUM"): "ERC20", ("USDC", "SOLANA"): "SPL"}
invoices["network"] = [GATEWAY_NET[(c, n)] for c, n in zip(invoices["currency"], invoices["network"])]
chain_tx["network"] = chain_tx["network"].str.lower()
# a legacy ledger connector stores EVM hashes in upper case (same hash, different text)
legacy = ledger["tx_hash"].fillna("").str.startswith("0x") & (ledger["booked_ts"] < pd.Timestamp("2025-03-01"))
ledger.loc[legacy, "tx_hash"] = "0x" + ledger.loc[legacy, "tx_hash"].str[2:].str.upper()
merchants_out = merchants.drop(columns=["_churn"])
planted_issues = pd.concat(planted, ignore_index=True)
planted_issues.insert(0, "issue_id", [f"PI{i:06d}" for i in range(1, len(planted_issues) + 1)])

OUT.mkdir(parents=True, exist_ok=True)
tables = {"merchants": merchants_out, "invoices": invoices, "chain_tx": chain_tx, "ledger": ledger,
          "partner_statements": partner, "payouts": payouts, "planted_issues": planted_issues}
for name, df in tables.items():
    df.to_parquet(OUT / f"{name}.parquet", index=False)
    if "--csv" in sys.argv:
        df.to_csv(OUT / f"{name}.csv", index=False)
    print(f"{name:20s} {len(df):>10,d} rows")
print(f"{'TOTAL (6 sources)':20s} {sum(len(d) for k, d in tables.items() if k != 'planted_issues'):>10,d} rows")
print(planted_issues.groupby("issue_type").agg(n=("issue_id", "size"), open=("resolved_ts", lambda s: s.isna().sum())))
