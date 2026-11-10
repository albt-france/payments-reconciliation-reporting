# Assumptions of the simulation (`src/generate_sim.py`, seed 42)

All transactions are **SIMULATED**. Only the daily prices are real. Every number below is an assumption chosen to look plausible, not a fact about any company.

## Period and volume
- 24 full months: 2024-11-01 to 2026-10-31. All sources extracted on 2026-11-01 06:00 UTC.
- ~600 invoices a day at the start, +3.5 % a month (Poisson noise), evening peak around 20:00 UTC.
- About 4.2 million rows across the six sources (ledger 2.5 M, invoices 0.67 M, chain 0.56 M, partner 0.35 M, payouts 0.14 M, merchants 1.5 k).

## Merchants (CRM)
- 1,500 merchants: 550 already live before Nov 2024, 950 signing up during the period.
- Segments: iGaming 40 %, E-commerce 25 %, SaaS 13 %, Forex 12 %, Marketplace 10 %.
- Size follows a Pareto law (a few merchants carry a large share of volume), capped.
- Each merchant churns after an exponential lifetime (mean ~1,000 days); 60-day ramp-up after signup.
- **Planted trend:** the largest Forex merchant (M0172, ~12 % of revenue at the start) declines from March 2026 to ~0 by October 2026.
- Fee plan (% of EUR value): deposit fee 0.7-1.3 % by segment (-0.25 pt for the top 5 % merchants), conversion fee 0.5-1.0 %, payout fee 0.15-0.4 %.
- Share of deposits auto-converted to EUR: SaaS 95 %, E-commerce 90 %, Marketplace 80 %, Forex 50 %, iGaming 35 %.

## Coins and networks
- Stablecoin share of invoices rises from 32 % to 62 % (planted trend).
- Stablecoins: USDT on TRON 50 %, Ethereum 18 %, BSC 17 %; USDC on Ethereum 9 %, Solana 6 %.
- Volatile: BTC 45 %, ETH 25 %, LTC 10 %, TRX 8 %, BNB 6 %, SOL 6 %.
- Typical network fee per transfer (EUR): BTC 1.50, USDT-ERC20 1.80, USDT-TRC20 1.60, ETH 0.80, TRX 0.30, BSC 0.05-0.06, LTC 0.02, Solana < 0.01, multiplied by a daily congestion index (AR(1) random walk around 1).
- Confirmations required before crediting: Bitcoin 2, Litecoin 6, Ethereum 12, BSC 15, TRON 19, Solana 32.
- Invoice expiry: 60 min (Bitcoin, Litecoin), 30 min (others).

## Ticket size and prices
- Median ticket (EUR, lognormal): iGaming 60, Marketplace 90, E-commerce 120, SaaS 250, Forex 300.
- E-commerce, SaaS and Marketplace price in EUR (amount in coin = EUR / rate).
- iGaming and Forex players paying in a volatile coin send a "familiar" number of coins (EUR target / 90-day average price). **So when the price falls, the EUR value of their deposits falls** (the February 2026 story: BTC fell ~25 % in a month).
- Invoice rate = real daily price +/- small intraday noise (0.6 % volatile, 0.05 % stablecoins).
- February 2026: +8 % payment activity (customers react to the price drop).

## Normal flows
- 83 % of invoices are paid, 17 % abandoned (expire with no payment).
- 0.3 % of paid invoices are paid in two transfers before expiry (split payment: **not** an error).
- The merchant is credited at the invoice rate. Conversions go through an exchange partner (statement in **Europe/Tallinn local time**), with normal slippage of -0.1 % +/- 0.2 % and a partner fee of 0.10 %.
- Payouts: every Monday 10:00 UTC for the previous week, in EUR (converted funds) or in coin; 1 % fail and are retried the next day.
- Network costs we pay (address sweeps, Bitcoin sweeps batched at 25 %, crypto payouts) are booked once a day per network.

## Planted breaks (answer key in `planted_issues`, never read by the pipeline)
| Break | Rate | How it is fixed (ledger evidence) |
|---|---|---|
| Underpayment (more likely when the network fee is high vs the ticket) | 0.2 % + 25 % x fee/ticket, max 5 % | refund of the amount received |
| Overpayment (+1 to +10 %) | 0.25 % of invoices | refund of the excess |
| Late payment (5 min to 8 h after expiry) | 0.5 % | manual credit (deposit booked late) |
| Wrong network (EVM coin sent on the other EVM chain) | 0.25 % of Ethereum/BSC invoices | manual credit |
| Address reuse (customer pays an old invoice address 2-60 days later) | 0.1 % of paid invoices | manual credit |
| Duplicate deposit in the ledger | 0.4 % of deposits | reversal |
| Premature credit (credited while the tx is still unconfirmed) | 0.12 % of paid invoices | reversal |
| Rate gap (conversion executed 2-8 % away from the invoice rate) | 0.5 % of conversions | fx_adjustment |
| Partner fee missing in the ledger | 0.4 % of conversions | fee booked late (or never) |
| Month-end cut-off (partner local date in the next month) | natural, ~0.5 % of conversions | cleared by converting to UTC |

- Time to fix: exponential, mean 3 to 12 days by type; **8 % of breaks are never fixed** (they become the > 30-day backlog).
- Data-quality noise: networks named differently by the gateway ("TRC20", "ERC20", "BEP20"...) and the explorer ("tron", "ethereum"...); EVM tx hashes stored in upper case by a legacy ledger connector before March 2025.

## Known simplifications
- USDT = 1 USD. Daily prices only (no intraday data). Network costs are allocated to deposits by count.
- Deposit fees are kept on refunded or reversed deposits.
- Merchant names are generic ("Merchant 0172"); account managers are codes ("AM 03").
