#!/usr/bin/env python3
from __future__ import annotations
import csv, json
from collections import defaultdict
from datetime import timedelta
from pathlib import Path
import chatgpt_backtest as base

BMIN = 0.01  # STRICTLY greater than 1.00%


def main():
    daily = {}; quality = []
    d = base.START
    while d <= base.END:
        if d.weekday() < 5:
            raw = base.download(d)
            if raw:
                try:
                    rows, status = base.read_day(d, raw)
                    daily[d] = rows
                    quality.append({'date': d.isoformat(), 'status': status, 'symbols': len(rows)})
                    print(d, status, len(rows), flush=True)
                except Exception as e:
                    quality.append({'date': d.isoformat(), 'status': 'parse_error', 'error': repr(e)})
            else:
                quality.append({'date': d.isoformat(), 'status': 'missing_or_holiday', 'symbols': 0})
        d += timedelta(days=1)
    if not daily:
        raise SystemExit('No NSE data downloaded')

    positions = {}; trades = []
    available_days = defaultdict(int); qualifying_days = defaultdict(int)

    for d in sorted(daily):
        rows = daily[d]
        for sym, (exp, spot, fut, settle, vol) in rows.items():
            available_days[sym] += 1
            basis = (fut - spot) / spot
            if basis > BMIN and d < exp:
                qualifying_days[sym] += 1

            p = positions.get(sym)
            if p is None:
                # Exact strategy rule: stay flat until strict B > 1%.
                if d < exp and basis > BMIN:
                    positions[sym] = base.Pos(sym, d, exp, spot, fut)
                continue

            # Keep monitoring the originally opened contract until exit/expiry.
            if exp != p.expiry and d < p.expiry:
                continue

            exit_fut = fut
            reason = None
            if d >= p.expiry:
                if settle and settle > 0:
                    exit_fut = settle
                reason = 'expiry'

            ret = ((spot - p.s0) + (p.f0 - exit_fut)) / p.s0
            p.min_ret = min(p.min_ret, ret)
            p.max_ret = max(p.max_ret, ret)
            p.peak_ret = max(p.peak_ret, ret)
            p.max_dd = min(p.max_dd, ret - p.peak_ret)
            if ret >= base.TARGET:
                reason = 'target'

            if reason:
                entry_basis = (p.f0 - p.s0) / p.s0
                trades.append({
                    'symbol': sym, 'entry_date': p.entry.isoformat(), 'exit_date': d.isoformat(),
                    'expiry': p.expiry.isoformat(), 'entry_basis_pct': round(entry_basis * 100, 6),
                    'cash_entry': round(p.s0, 6), 'future_short_entry': round(p.f0, 6),
                    'cash_exit': round(spot, 6), 'future_exit': round(exit_fut, 6),
                    'exit_reason': reason, 'gross_return_pct': round(ret * 100, 6),
                    'mae_pct': round(p.min_ret * 100, 6), 'mfe_pct': round(p.max_ret * 100, 6),
                    'max_drawdown_pct': round(p.max_dd * 100, 6),
                    'holding_calendar_days': (d - p.entry).days,
                })
                del positions[sym]

    # Mark positions still open at the end of the test.
    for sym, p in list(positions.items()):
        last = None
        for d in sorted(daily, reverse=True):
            if sym in daily[d]:
                exp, spot, fut, settle, vol = daily[d][sym]
                if exp == p.expiry:
                    last = (d, spot, fut); break
        if last:
            d, spot, fut = last
            ret = ((spot - p.s0) + (p.f0 - fut)) / p.s0
            p.min_ret = min(p.min_ret, ret); p.max_ret = max(p.max_ret, ret)
            p.peak_ret = max(p.peak_ret, ret); p.max_dd = min(p.max_dd, ret - p.peak_ret)
            entry_basis = (p.f0 - p.s0) / p.s0
            trades.append({
                'symbol': sym, 'entry_date': p.entry.isoformat(), 'exit_date': d.isoformat(),
                'expiry': p.expiry.isoformat(), 'entry_basis_pct': round(entry_basis * 100, 6),
                'cash_entry': round(p.s0, 6), 'future_short_entry': round(p.f0, 6),
                'cash_exit': round(spot, 6), 'future_exit': round(fut, 6),
                'exit_reason': 'open', 'gross_return_pct': round(ret * 100, 6),
                'mae_pct': round(p.min_ret * 100, 6), 'mfe_pct': round(p.max_ret * 100, 6),
                'max_drawdown_pct': round(p.max_dd * 100, 6),
                'holding_calendar_days': (d - p.entry).days,
            })

    by = defaultdict(list)
    for t in trades: by[t['symbol']].append(t)
    summary = []
    for sym, ts in by.items():
        closed = [t for t in ts if t['exit_reason'] != 'open']
        hits = [t for t in closed if t['exit_reason'] == 'target']
        exps = [t for t in closed if t['exit_reason'] == 'expiry']
        summary.append({
            'symbol': sym, 'available_days': available_days[sym], 'qualifying_days_b_gt_1pct': qualifying_days[sym],
            'trades': len(ts), 'closed_trades': len(closed), 'target_hits': len(hits), 'expiry_exits': len(exps),
            'open_trades': sum(t['exit_reason'] == 'open' for t in ts),
            'hit_rate_pct': round(100 * len(hits) / len(closed), 4) if closed else None,
            'avg_entry_basis_pct': round(sum(t['entry_basis_pct'] for t in ts) / len(ts), 6),
            'avg_holding_days_targets': round(sum(t['holding_calendar_days'] for t in hits) / len(hits), 3) if hits else None,
            'worst_mae_pct': round(min(t['mae_pct'] for t in ts), 6),
            'worst_max_drawdown_pct': round(min(t['max_drawdown_pct'] for t in ts), 6),
            'best_trade_pct': round(max(t['gross_return_pct'] for t in ts), 6),
            'cumulative_closed_gross_return_pct': round(sum(t['gross_return_pct'] for t in closed), 6),
        })
    summary.sort(key=lambda x: (x['target_hits'], x['hit_rate_pct'] or -1, x['cumulative_closed_gross_return_pct']), reverse=True)

    def write_csv(path, rows):
        if not rows: return
        with open(path, 'w', newline='', encoding='utf-8') as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys())); w.writeheader(); w.writerows(rows)

    write_csv('nse_bmin1_summary.csv', summary)
    write_csv('nse_bmin1_trades.csv', trades)
    write_csv('nse_bmin1_quality.csv', quality)

    closed = [t for t in trades if t['exit_reason'] != 'open']
    hits = [t for t in closed if t['exit_reason'] == 'target']
    agg = {
        'start': base.START.isoformat(), 'end': base.END.isoformat(), 'target_pct': base.TARGET * 100,
        'entry_rule': 'strict (future-close - underlying-value) / underlying-value > 1.00%',
        'symbols_with_at_least_one_trade': len(summary), 'trades': len(trades), 'closed_trades': len(closed),
        'target_hits': len(hits), 'expiry_exits': sum(t['exit_reason']=='expiry' for t in closed),
        'open_trades': sum(t['exit_reason']=='open' for t in trades),
        'overall_hit_rate_pct': round(100 * len(hits) / len(closed), 4) if closed else None,
        'avg_closed_gross_return_pct': round(sum(t['gross_return_pct'] for t in closed)/len(closed), 6) if closed else None,
        'median_closed_gross_return_pct': sorted(t['gross_return_pct'] for t in closed)[len(closed)//2] if closed else None,
        'avg_target_return_pct': round(sum(t['gross_return_pct'] for t in hits)/len(hits), 6) if hits else None,
        'avg_entry_basis_pct': round(sum(t['entry_basis_pct'] for t in trades)/len(trades), 6) if trades else None,
        'avg_target_holding_days': round(sum(t['holding_calendar_days'] for t in hits)/len(hits), 3) if hits else None,
        'worst_trade_mae_pct': min((t['mae_pct'] for t in trades), default=None),
        'worst_trade_drawdown_pct': min((t['max_drawdown_pct'] for t in trades), default=None),
        'total_closed_gross_return_pct_sum': round(sum(t['gross_return_pct'] for t in closed), 6),
        'symbols_with_target_hit': len({t['symbol'] for t in hits}),
        'note': 'Gross price-differential backtest; dividends, financing, brokerage, taxes, slippage and margin cost excluded.'
    }
    Path('nse_bmin1_aggregate.json').write_text(json.dumps(agg, indent=2), encoding='utf-8')
    print('AGGREGATE', json.dumps(agg), flush=True)
    print('TOP20')
    for r in summary[:20]: print(json.dumps(r), flush=True)

if __name__ == '__main__': main()
