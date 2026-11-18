"""
dashboard.py -- Read-only portfolio snapshot for the tracking dashboard.

Reads ONLY the collections the paper engine already maintains (`strategies`, `positions`,
`daily_nav`, `trades`, `orders`); it never writes and never fetches prices. "Current" prices
are therefore the `last_close` stored on each position by the last post-market pipeline run,
and the page says so.

Valuation is recomputed from the live books (cash + shares * last_close), not copied from a
stored NAV, so the page can never silently show a stale headline number. The date of the
newest `daily_nav` row is reported next to it so the viewer can see how fresh the marks are.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

VALID_MODES = ("paper", "real")


def _num(value: Any) -> Optional[float]:
    """Finite float or None. Never turns missing/garbage data into a silent 0."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _position_row(
    doc: Dict[str, Any],
    pending_by_sym: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    shares = int(_num(doc.get("shares")) or 0)
    entry_px = _num(doc.get("entry_fill_price"))
    if entry_px is None or entry_px <= 0:
        entry_px = _num(doc.get("entry_price"))
    last_close = _num(doc.get("last_close"))

    market_value = _num(doc.get("market_value"))
    if last_close is not None:
        # Always re-derive: shares * last_close is the definition, the stored field can lag a share-count change.
        market_value = shares * last_close
    outlay = _num(doc.get("entry_outlay"))
    if outlay is None and entry_px is not None:
        outlay = shares * entry_px

    pnl = _num(doc.get("unrealized_pnl"))
    if last_close is not None and outlay is not None:
        pnl = market_value - outlay  # net of entry fees, before exit costs

    gain_pct = None
    if entry_px and last_close is not None and entry_px > 0:
        gain_pct = (last_close / entry_px - 1.0) * 100.0

    peak_px = _num(doc.get("peak_price"))
    if peak_px is None and last_close is not None and entry_px is not None:
        peak_px = max(entry_px, last_close)

    peak_gain_pct = None
    if entry_px and peak_px is not None and entry_px > 0:
        peak_gain_pct = (peak_px / entry_px - 1.0) * 100.0

    stop_px = _num(doc.get("stop_loss_price"))
    stop_buf = _num(doc.get("stop_buffer_pct"))
    if (
        stop_buf is None
        and stop_px is not None
        and last_close is not None
        and last_close > 0
    ):
        stop_buf = round(((last_close - stop_px) / last_close) * 100.0, 2)

    sym = doc.get("symbol")
    pending = (pending_by_sym or {}).get(sym)

    return {
        "symbol": sym,
        "shares": shares,
        "entry_date": doc.get("entry_date"),
        "entry_price": entry_px,
        "last_close": last_close,
        "peak_price": peak_px,
        "peak_gain_pct": peak_gain_pct,
        "gain_pct": gain_pct,
        "pnl": pnl,
        "market_value": market_value,
        "days_held": doc.get("days_held"),
        "stop_loss_price": stop_px,
        "stop_buffer_pct": stop_buf,
        "pending_order": pending,
    }


def _trade_row(doc: Dict[str, Any]) -> Dict[str, Any]:
    raw_ret = _num(doc.get("return_pct"))
    ret_pct = None
    if raw_ret is not None:
        ret_pct = raw_ret * 100.0 if abs(raw_ret) < 2.0 else raw_ret

    return {
        "symbol": doc.get("symbol"),
        "entry_date": doc.get("entry_date"),
        "entry_price": _num(doc.get("entry_price")),
        "exit_date": doc.get("exit_date"),
        "exit_price": _num(doc.get("exit_price")),
        "shares": int(_num(doc.get("shares")) or 0),
        "hold_days": doc.get("hold_days"),
        "gross_pnl": _num(doc.get("gross_pnl")),
        "net_pnl": _num(doc.get("net_pnl")),
        "return_pct": ret_pct,
        "exit_reason": doc.get("exit_reason") or "EXIT",
    }


def _order_row(doc: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "order_id": doc.get("order_id"),
        "symbol": doc.get("symbol"),
        "action": doc.get("action") or doc.get("side") or "BUY",
        "shares": int(_num(doc.get("shares")) or 0),
        "price": _num(doc.get("price")),
        "order_type": doc.get("order_type") or "MARKET_OPEN",
        "status": doc.get("status"),
        "reason": doc.get("reason") or "-",
        "signal_date": doc.get("signal_date"),
    }


def _strategy_block(db, sdoc: Dict[str, Any], mode: str) -> Dict[str, Any]:
    sid = sdoc["strategy_id"]

    orders_col = getattr(db, "orders", None)
    pending_orders = []
    pending_by_sym = {}
    if orders_col is not None:
        try:
            o_docs = list(
                orders_col.find({
                    "strategy_id": sid,
                    "mode": mode,
                    "status": "PENDING_OPEN",
                })
            )
            for o in o_docs:
                row = _order_row(o)
                pending_orders.append(row)
                if row.get("symbol"):
                    pending_by_sym[row["symbol"]] = row
        except Exception:
            pass

    pos_docs = list(db.positions.find({"strategy_id": sid, "mode": mode}))
    rows = [_position_row(p, pending_by_sym) for p in pos_docs]

    trades_col = getattr(db, "trades", None)
    closed_trades = []
    if trades_col is not None:
        try:
            t_docs = list(
                trades_col.find({"strategy_id": sid, "mode": mode})
                .sort("exit_date", -1)
                .limit(20)
            )
            closed_trades = [_trade_row(t) for t in t_docs]
        except Exception:
            pass

    unpriced = [r["symbol"] for r in rows if r["market_value"] is None]
    holdings = sum(
        r["market_value"] for r in rows if r["market_value"] is not None
    )
    pnl_total = sum(r["pnl"] for r in rows if r["pnl"] is not None)

    cash = _num(sdoc.get("cash"))
    nav_rows = list(
        db.daily_nav.find({"strategy_id": sid, "mode": mode})
        .sort("date", -1)
        .limit(1)
    )
    last_nav = nav_rows[0] if nav_rows else None
    if cash is None and last_nav is not None:
        cash = _num(last_nav.get("cash"))

    initial = _num(sdoc.get("initial_capital"))
    total = (cash + holdings) if cash is not None else None
    ret_pct = (
        ((total / initial - 1.0) * 100.0)
        if (total is not None and initial)
        else None
    )

    for r in rows:
        r["weight_pct"] = (
            (r["market_value"] / total * 100.0)
            if (total and r["market_value"] is not None)
            else None
        )
    rows.sort(key=lambda r: (r["gain_pct"] is None, -(r["gain_pct"] or 0.0)))

    realized = _num(sdoc.get("realized_pnl"))
    if realized is None and closed_trades:
        realized = sum(
            t["net_pnl"] for t in closed_trades if t["net_pnl"] is not None
        )

    total_pnl = None
    if realized is not None or pnl_total is not None:
        total_pnl = (realized or 0.0) + (pnl_total or 0.0)

    return {
        "strategy_id": sid,
        "name": sdoc.get("name") or sid,
        "status": sdoc.get("status", "ACTIVE"),
        "initial_capital": initial,
        "cash": cash,
        "cash_pct": (cash / total * 100.0)
        if (cash is not None and total)
        else None,
        "holdings_value": holdings,
        "total_value": total,
        "return_pct": ret_pct,
        "unrealized_pnl": pnl_total,
        "realized_pnl": realized,
        "total_pnl": total_pnl,
        "open_positions": len(rows),
        "last_nav_date": last_nav.get("date") if last_nav else None,
        "last_signal_date": sdoc.get("last_signal_date"),
        "last_execution_date": sdoc.get("last_execution_date"),
        "unpriced_symbols": unpriced,
        "positions": rows,
        "closed_trades": closed_trades,
        "pending_orders": pending_orders,
    }


def build_snapshot(db, mode: str = "paper") -> Dict[str, Any]:
    """Whole-dashboard payload (JSON-serialisable). `db` needs .strategies/.positions/.daily_nav."""
    if mode not in VALID_MODES:
        raise ValueError(f"mode must be one of {VALID_MODES}, got {mode!r}")

    sdocs = [
        s for s in db.strategies.find({"mode": mode}) if s.get("strategy_id")
    ]
    blocks = [_strategy_block(db, s, mode) for s in sdocs]
    # Live accounts first, archived last, then by id for a stable order.
    blocks.sort(
        key=lambda b: (
            b["status"] == "ARCHIVED",
            b["status"] != "ACTIVE",
            b["strategy_id"],
        )
    )
    visible = [
        b
        for b in blocks
        if b["status"] != "ARCHIVED"
        or b["open_positions"] > 0
        or len(b.get("closed_trades", [])) > 0
    ]

    def _sum(key: str) -> Optional[float]:
        vals = [b[key] for b in visible if b.get(key) is not None]
        return sum(vals) if vals else None

    total = _sum("total_value")
    initial = _sum("initial_capital")
    realized_sum = _sum("realized_pnl")
    unrealized_sum = _sum("unrealized_pnl")
    total_pnl_sum = None
    if realized_sum is not None or unrealized_sum is not None:
        total_pnl_sum = (realized_sum or 0.0) + (unrealized_sum or 0.0)

    totals = {
        "initial_capital": initial,
        "cash": _sum("cash"),
        "holdings_value": _sum("holdings_value"),
        "total_value": total,
        "realized_pnl": realized_sum,
        "unrealized_pnl": unrealized_sum,
        "total_pnl": total_pnl_sum,
        "open_positions": sum(b["open_positions"] for b in visible),
        "closed_trades_count": sum(
            len(b.get("closed_trades", [])) for b in visible
        ),
        "pending_orders_count": sum(
            len(b.get("pending_orders", [])) for b in visible
        ),
        "return_pct": ((total / initial - 1.0) * 100.0)
        if (total is not None and initial)
        else None,
    }
    return {
        "mode": mode,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "totals": totals,
        "strategies": visible,
    }


PAGE_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>NSE Quant Lab - Portfolio</title>
<style>
:root{--bg:#0f1419;--card:#171d25;--line:#273040;--fg:#e6edf3;--mut:#8b98a8;--up:#3fb950;--down:#f85149;--warn:#d29922}
@media (prefers-color-scheme:light){:root{--bg:#f4f6f8;--card:#fff;--line:#d8dee6;--fg:#1b2430;--mut:#5d6b7c;--up:#1a7f37;--down:#cf222e;--warn:#9a6700}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,Segoe UI,Roboto,sans-serif}
main{max-width:1240px;margin:0 auto;padding:16px}
h1{font-size:18px;margin:0 0 4px}
.sub{color:var(--mut);font-size:12px;margin-bottom:14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:14px;margin-bottom:14px}
.card h2{font-size:15px;margin:0 0 10px;display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.badge{font-size:11px;color:var(--mut);border:1px solid var(--line);border-radius:10px;padding:1px 8px;font-weight:400}
.badge.exit{color:var(--down);border-color:var(--down);background:rgba(248,81,73,0.12);font-weight:600}
.badge.buy{color:var(--up);border-color:var(--up);background:rgba(63,185,80,0.12);font-weight:600}
.badge.warn{color:var(--warn);border-color:var(--warn);background:rgba(210,153,34,0.12);font-weight:600}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(125px,1fr));gap:10px;margin-bottom:12px}
.kpi .l{color:var(--mut);font-size:11px;text-transform:uppercase;letter-spacing:.04em}
.kpi .v{font-size:16px;font-weight:600;font-variant-numeric:tabular-nums}
.sect-title{font-size:12px;font-weight:600;color:var(--mut);text-transform:uppercase;letter-spacing:.04em;margin:12px 0 6px}
.tw{overflow-x:auto}
table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums;margin-bottom:10px}
th,td{padding:5px 8px;border-bottom:1px solid var(--line);white-space:nowrap}
th{color:var(--mut);font-weight:500;font-size:12px;text-align:right;cursor:pointer;user-select:none}
td{text-align:right}
th:first-child,td:first-child{text-align:left}
.up{color:var(--up)}.down{color:var(--down)}.warn{color:var(--warn)}.mut{color:var(--mut)}
.note{color:var(--mut);font-size:12px;margin-top:8px}
#err{display:none;border:1px solid var(--down);color:var(--down);border-radius:8px;padding:10px;margin-bottom:14px}
</style>
</head>
<body>
<main>
<h1>NSE Quant Lab - Portfolio tracker</h1>
<div class="sub" id="meta">loading...</div>
<div id="err"></div>
<div id="root"></div>
<div class="note">Read-only view of the database. "Last close" is the price stored by the last post-market pipeline run
(about 18:00 IST on trading days), not a live quote. Peak is the highest intraday high reached since entry.
Stop cushion = distance above structural stop-loss (amber warning when &le; 2.5%). Gain % = last close vs entry fill price.
P&amp;L = market value minus entry outlay. Realised P&amp;L comes from closed trade ledgers.</div>
</main>
<script>
const MODE = new URLSearchParams(location.search).get("mode") === "real" ? "real" : "paper";
const inr = v => v == null ? "-" : v.toLocaleString("en-IN", {maximumFractionDigits: 0});
const px  = v => v == null ? "-" : v.toLocaleString("en-IN", {minimumFractionDigits: 2, maximumFractionDigits: 2});
const pct = v => v == null ? "-" : (v >= 0 ? "+" : "") + v.toFixed(2) + "%";
const cls = v => v == null ? "mut" : (v > 0 ? "up" : (v < 0 ? "down" : ""));
const sortState = {};

function el(tag, props, ...kids) {
  const e = document.createElement(tag);
  if (props) for (const [k, v] of Object.entries(props)) { if (k === "class") e.className = v; else e[k] = v; }
  for (const k of kids) if (k != null) e.append(k);
  return e;
}
function kpi(label, text, c) {
  return el("div", {class: "kpi"}, el("div", {class: "l"}, label), el("div", {class: "v " + (c || "")}, text));
}

const POS_COLS = [
  ["symbol", "Symbol", r => r.symbol, null, true],
  ["shares", "Shares", r => r.shares.toLocaleString("en-IN"), null],
  ["entry_date", "Entry date", r => r.entry_date || "-", null],
  ["entry_price", "Entry price", r => px(r.entry_price), null],
  ["last_close", "Last close", r => px(r.last_close), null],
  ["peak_price", "Peak (high)", r => px(r.peak_price), null],
  ["peak_gain_pct", "Peak %", r => pct(r.peak_gain_pct), r => cls(r.peak_gain_pct)],
  ["gain_pct", "Gain %", r => pct(r.gain_pct), r => cls(r.gain_pct)],
  ["pnl", "P&L (Rs)", r => (r.pnl == null ? "-" : (r.pnl >= 0 ? "+" : "") + inr(r.pnl)), r => cls(r.pnl)],
  ["stop_loss_price", "Stop loss", r => px(r.stop_loss_price), null],
  ["stop_buffer_pct", "Stop cushion", r => (r.stop_buffer_pct == null ? "-" : (r.stop_buffer_pct <= 2.5 ? "⚠️ " : "") + r.stop_buffer_pct.toFixed(1) + "%"), r => (r.stop_buffer_pct != null && r.stop_buffer_pct <= 2.5 ? "warn" : "")],
  ["market_value", "Value (Rs)", r => inr(r.market_value), null],
  ["weight_pct", "Weight", r => r.weight_pct == null ? "-" : r.weight_pct.toFixed(1) + "%", null],
  ["days_held", "Days", r => r.days_held == null ? "-" : String(r.days_held), null],
];

function posTable(s) {
  const key = sortState[s.strategy_id] || {k: "gain_pct", dir: -1};
  const rows = s.positions.slice().sort((a, b) => {
    const x = a[key.k], y = b[key.k];
    if (x == null && y == null) return 0;
    if (x == null) return 1;
    if (y == null) return -1;
    return (x < y ? -1 : x > y ? 1 : 0) * key.dir;
  });
  const head = el("tr");
  for (const [k, label] of POS_COLS) {
    const th = el("th", null, label + (key.k === k ? (key.dir > 0 ? " ^" : " v") : ""));
    th.onclick = () => { sortState[s.strategy_id] = {k, dir: key.k === k ? -key.dir : -1}; render(window.__data); };
    head.append(th);
  }
  const tb = el("tbody");
  for (const r of rows) {
    const tr = el("tr");
    for (const [k, , f, c, isSym] of POS_COLS) {
      if (isSym) {
        const td = el("td", null, r.symbol);
        if (r.pending_order) {
          const act = r.pending_order.action || "SELL";
          const rsn = r.pending_order.reason || "EXIT";
          const bClass = act === "SELL" ? "badge exit" : "badge buy";
          const text = act === "SELL" ? ("🛑 EXIT (" + rsn + ")") : "🟢 BUY";
          td.append(" ", el("span", {class: bClass, title: "Triggered at EOD, queued for next open"}, text));
        }
        tr.append(td);
      } else {
        tr.append(el("td", {class: c ? c(r) : ""}, f(r)));
      }
    }
    tb.append(tr);
  }
  return el("div", {class: "tw"}, el("table", null, el("thead", null, head), tb));
}

const ORDER_COLS = [
  ["symbol", "Symbol", r => r.symbol, null],
  ["action", "Action", r => r.action, r => r.action === "SELL" ? "down" : "up"],
  ["shares", "Shares", r => r.shares ? r.shares.toLocaleString("en-IN") : "-", null],
  ["price", "Price / Limit", r => px(r.price), null],
  ["reason", "Reason", r => r.reason || "-", null],
  ["signal_date", "Signal Date", r => r.signal_date || "-", null],
  ["timing", "Execution Timing", () => "Queued for next Open (09:16 IST)", () => "warn"],
];

function pendingOrdersTable(orders) {
  const head = el("tr");
  for (const [, label] of ORDER_COLS) head.append(el("th", null, label));
  const tb = el("tbody");
  for (const r of orders) {
    const tr = el("tr");
    for (const [, , f, c] of ORDER_COLS) tr.append(el("td", {class: c ? c(r) : ""}, f(r)));
    tb.append(tr);
  }
  return el("div", {class: "tw"}, el("table", null, el("thead", null, head), tb));
}

const TRADE_COLS = [
  ["symbol", "Symbol", r => r.symbol, null],
  ["exit_date", "Exit Date", r => r.exit_date || "-", null],
  ["exit_price", "Exit Price", r => px(r.exit_price), null],
  ["shares", "Shares", r => r.shares.toLocaleString("en-IN"), null],
  ["entry_date", "Entry Date", r => r.entry_date || "-", null],
  ["entry_price", "Entry Price", r => px(r.entry_price), null],
  ["hold_days", "Days", r => r.hold_days == null ? "-" : String(r.hold_days), null],
  ["net_pnl", "Net P&L (Rs)", r => (r.net_pnl == null ? "-" : (r.net_pnl >= 0 ? "+" : "") + inr(r.net_pnl)), r => cls(r.net_pnl)],
  ["return_pct", "Return %", r => pct(r.return_pct), r => cls(r.return_pct)],
  ["exit_reason", "Exit Reason", r => r.exit_reason || "EXIT", null],
];

function closedTradesTable(trades) {
  const head = el("tr");
  for (const [, label] of TRADE_COLS) head.append(el("th", null, label));
  const tb = el("tbody");
  for (const r of trades) {
    const tr = el("tr");
    for (const [, , f, c] of TRADE_COLS) tr.append(el("td", {class: c ? c(r) : ""}, f(r)));
    tb.append(tr);
  }
  return el("div", {class: "tw"}, el("table", null, el("thead", null, head), tb));
}

function render(d) {
  window.__data = d;
  const root = document.getElementById("root");
  root.replaceChildren();
  const t = d.totals;
  const sum = el("div", {class: "card"}, el("h2", null, "All strategies (" + d.mode + ")"),
    el("div", {class: "kpis"},
      kpi("Total value", "Rs " + inr(t.total_value)),
      kpi("Cash", "Rs " + inr(t.cash)),
      kpi("Holdings", "Rs " + inr(t.holdings_value)),
      kpi("Return vs capital", pct(t.return_pct), cls(t.return_pct)),
      kpi("Realised P&L", "Rs " + inr(t.realized_pnl), cls(t.realized_pnl)),
      kpi("Unrealised P&L", "Rs " + inr(t.unrealized_pnl), cls(t.unrealized_pnl)),
      kpi("Total P&L", "Rs " + inr(t.total_pnl), cls(t.total_pnl)),
      kpi("Open positions", String(t.open_positions)),
      kpi("Pending orders", String(t.pending_orders_count || 0)),
      kpi("Closed exits", String(t.closed_trades_count || 0))));
  root.append(sum);

  for (const s of d.strategies) {
    const h = el("h2", null, s.name, el("span", {class: "badge"}, s.strategy_id), el("span", {class: "badge"}, s.status));
    const k = el("div", {class: "kpis"},
      kpi("Total value", "Rs " + inr(s.total_value)),
      kpi("Cash", "Rs " + inr(s.cash) + (s.cash_pct == null ? "" : " (" + s.cash_pct.toFixed(1) + "%)")),
      kpi("Holdings", "Rs " + inr(s.holdings_value)),
      kpi("Return vs capital", pct(s.return_pct), cls(s.return_pct)),
      kpi("Realised P&L", "Rs " + inr(s.realized_pnl), cls(s.realized_pnl)),
      kpi("Unrealised P&L", "Rs " + inr(s.unrealized_pnl), cls(s.unrealized_pnl)),
      kpi("Total P&L", "Rs " + inr(s.total_pnl), cls(s.total_pnl)),
      kpi("Positions", String(s.open_positions)),
      kpi("Last EOD run", s.last_nav_date || "-"));
    const card = el("div", {class: "card"}, h, k);

    if (s.unpriced_symbols.length)
      card.append(el("div", {class: "warn"}, "No price for: " + s.unpriced_symbols.join(", ") + " (excluded from holdings value)"));

    card.append(el("div", {class: "sect-title"}, "Open Positions (" + s.positions.length + ")"));
    card.append(s.positions.length ? posTable(s) : el("div", {class: "mut"}, "No open positions - all cash."));

    if (s.pending_orders && s.pending_orders.length) {
      card.append(el("div", {class: "sect-title"}, "Queued Orders For Next Open (" + s.pending_orders.length + ")"));
      card.append(pendingOrdersTable(s.pending_orders));
    }

    if (s.closed_trades && s.closed_trades.length) {
      card.append(el("div", {class: "sect-title"}, "Recent Exits / Closed Trades (" + s.closed_trades.length + ")"));
      card.append(closedTradesTable(s.closed_trades));
    }

    root.append(card);
  }
  document.getElementById("meta").textContent = "Updated " + new Date(d.generated_at).toLocaleString("en-IN") + " - auto-refresh every 60s";
}

async function load() {
  const err = document.getElementById("err");
  try {
    const r = await fetch("api/portfolio?mode=" + MODE, {cache: "no-store"});
    if (!r.ok) throw new Error("HTTP " + r.status + ": " + (await r.text()).slice(0, 200));
    render(await r.json());
    err.style.display = "none";
  } catch (e) {
    err.textContent = "Could not load data: " + e.message;
    err.style.display = "block";
  }
}
load();
setInterval(load, 60000);
</script>
</body>
</html>
"""
