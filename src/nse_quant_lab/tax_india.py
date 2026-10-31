"""
tax_india.py -- Indian resident-individual capital-gains tax on listed equity (STT paid), by financial year.

Why this exists: the engine used flat 20% STCG / 12.5% LTCG with a Rs 1.25 lakh exemption for EVERY year. The real
regime changed several times, and in particular LTCG on listed equity was fully exempt until 31-Mar-2018.

Rates modelled (rate is set by the SALE date; cess added on top; surcharge ignored, i.e. assumes total income
below the Rs 50 lakh surcharge threshold, and other income already above the basic exemption so gains are taxed at
the full special rate):

  STCG (s.111A)   sale <  2008-04-01 : 10%      sale 2008-04-01 .. 2024-07-22 : 15%      sale >= 2024-07-23 : 20%
  LTCG (s.112A)   sale <  2018-04-01 : exempt   2018-04-01 .. 2024-07-22 : 10% on gains > Rs 1,00,000 per FY
                  sale >= 2024-07-23 : 12.5% on gains > Rs 1,25,000 per FY (limit applies to the whole FY)
  Cess            3% before 2018-04-01, 4% from 2018-04-01.

Set-off (s.70/71/74): a short-term loss is set off against short-term gain, then long-term gain; a long-term loss only
against long-term gain; unabsorbed losses carry forward 8 assessment years. A long-term loss in a year in which LTCG
is exempt is not allowed and is discarded.

Grandfathering of pre-31-Jan-2018 purchases is NOT modelled (hold periods here are months, effect negligible).
Securities Transaction Tax is not deductible against capital gains, so it is added back to the engine's net P&L.
Treating frequent delivery trading as capital gains (not business income) is an assumption that the taxpayer must
make consistently; it is not tax advice.
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Tuple

import pandas as pd

D_STCG_15 = pd.Timestamp("2008-04-01")
D_LTCG_START = pd.Timestamp("2018-04-01")
D_BUDGET_2024 = pd.Timestamp("2024-07-23")
CARRY_YEARS = 8


def stcg_rate(sale: pd.Timestamp) -> float:
    if sale < D_STCG_15:
        return 0.10
    if sale < D_BUDGET_2024:
        return 0.15
    return 0.20


def ltcg_rate_and_exemption(sale: pd.Timestamp) -> Tuple[float, float]:
    if sale < D_LTCG_START:
        return 0.0, float("inf")
    if sale < D_BUDGET_2024:
        return 0.10, 100_000.0
    return 0.125, 125_000.0


def cess(sale: pd.Timestamp) -> float:
    return 0.03 if sale < D_LTCG_START else 0.04


def taxable_pnl(trade: Dict) -> float:
    """Engine net_pnl plus the non-deductible STT on both legs."""
    stt = 0.001 * trade["shares"] * (trade["entry_fill_price"] + trade["exit_fill_price"])
    return float(trade["net_pnl"]) + stt


def compute_fy_tax(
    fy_end_year: int,
    trades: Iterable[Dict],
    carry_st: List[Tuple[int, float]],
    carry_lt: List[Tuple[int, float]],
) -> Tuple[float, List[Tuple[int, float]], List[Tuple[int, float]], Dict]:
    """
    Tax for the financial year ending 31-Mar-fy_end_year.
    carry_st / carry_lt: lists of (fy_end_year_of_loss, remaining_loss) with positive amounts.
    Returns (tax, new_carry_st, new_carry_lt, detail).
    """
    st_by_rate: Dict[float, float] = {}
    lt_gross = 0.0
    lt_pre = lt_post = 0.0  # LTCG realised before / after the 2024 budget (only used to blend rates in FY2024-25)
    st_total = 0.0
    ref_sale = pd.Timestamp(f"{fy_end_year}-03-31")
    for t in trades:
        sale = pd.Timestamp(t["exit_date"])
        ref_sale = sale
        p = taxable_pnl(t)
        if int(t["hold_days"]) >= 365:
            lt_gross += p
            if sale < D_BUDGET_2024:
                lt_pre += p
            else:
                lt_post += p
        else:
            st_total += p
            r = stcg_rate(sale)
            st_by_rate[r] = st_by_rate.get(r, 0.0) + p

    fy_end = pd.Timestamp(f"{fy_end_year}-03-31")
    ltcg_exempt_year = fy_end < D_LTCG_START

    # age out carried losses
    carry_st = [(y, a) for y, a in carry_st if fy_end_year - y <= CARRY_YEARS and a > 0]
    carry_lt = [(y, a) for y, a in carry_lt if fy_end_year - y <= CARRY_YEARS and a > 0]

    # --- short-term bucket: current-year result net of carried STCL (oldest first)
    st_net = st_total
    new_carry_st: List[Tuple[int, float]] = []
    if st_net < 0:
        new_carry_st = carry_st + [(fy_end_year, -st_net)]
        st_net = 0.0
    else:
        for y, a in carry_st:
            use = min(a, st_net)
            st_net -= use
            if a - use > 1e-9:
                new_carry_st.append((y, a - use))

    # --- long-term bucket
    lt_net = 0.0 if ltcg_exempt_year else lt_gross
    new_carry_lt: List[Tuple[int, float]] = []
    if ltcg_exempt_year:
        new_carry_lt = []  # long-term loss in an exempt regime is not allowed / carried
    else:
        if lt_net < 0:
            new_carry_lt = carry_lt + [(fy_end_year, -lt_net)]
            lt_net = 0.0
        else:
            for y, a in carry_lt:
                use = min(a, lt_net)
                lt_net -= use
                if a - use > 1e-9:
                    new_carry_lt.append((y, a - use))
        # unabsorbed short-term losses (carried, including this year's) offset long-term gains
        if lt_net > 0 and new_carry_st:
            rem = []
            for y, a in sorted(new_carry_st):
                use = min(a, lt_net)
                lt_net -= use
                if a - use > 1e-9:
                    rem.append((y, a - use))
            new_carry_st = rem

    # --- tax
    st_tax = 0.0
    if st_total > 0 and st_net > 0:
        # allocate the post-set-off positive base across rates pro rata to positive gains
        pos = {r: v for r, v in st_by_rate.items() if v > 0}
        tot_pos = sum(pos.values())
        for r, v in pos.items():
            st_tax += st_net * (v / tot_pos) * r
    lt_tax = 0.0
    if not ltcg_exempt_year and lt_net > 0:
        denom = max(lt_pre, 0.0) + max(lt_post, 0.0)
        w_post = (max(lt_post, 0.0) / denom) if denom > 0 else 1.0
        rate_pre, ex_pre = ltcg_rate_and_exemption(pd.Timestamp("2020-01-01"))
        rate_post, ex_post = ltcg_rate_and_exemption(pd.Timestamp("2025-01-01"))
        if fy_end >= pd.Timestamp("2025-03-31"):
            rate, exemption = rate_post, ex_post
        elif fy_end < D_BUDGET_2024:
            rate, exemption = rate_pre, ex_pre
        else:  # FY2024-25: blended rate, exemption Rs 1.25 lakh for the year
            rate, exemption = (1 - w_post) * rate_pre + w_post * rate_post, ex_post
        lt_tax = max(0.0, lt_net - exemption) * rate
    c = cess(ref_sale if ref_sale > pd.Timestamp("1990-01-01") else fy_end)
    tax = (st_tax + lt_tax) * (1.0 + c)
    detail = {
        "stcg_gross": round(st_total, 2),
        "ltcg_gross": round(lt_gross, 2),
        "taxable_stcg": round(st_net, 2),
        "taxable_ltcg": round(max(0.0, lt_net - (0.0 if ltcg_exempt_year else exemption)) if lt_net > 0 and not ltcg_exempt_year else 0.0, 2),
        "tax": round(tax, 2),
    }
    return tax, new_carry_st, new_carry_lt, detail
