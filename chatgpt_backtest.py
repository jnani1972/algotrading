#!/usr/bin/env python3
"""NSE long-cash / short-near-future paired backtest, 18-May-2026..17-Aug-2026.
Uses only the official NSE F&O UDiFF bhavcopy.  Each FUTSTK/STF row contains
UndrlygPric (cash underlying value), so no separate CM download is required.
"""
from __future__ import annotations
import csv, io, json, math, os, subprocess, sys, time, urllib.request, zipfile
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

START = date(2026,5,18)
END = date(2026,8,17)
TARGET = 0.01
BASE = "https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{d}_F_0000.csv.zip"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131 Safari/537.36"
CACHE = Path("nse_fo_cache")
CACHE.mkdir(exist_ok=True)


def parse_date(s):
    s=(s or "").strip()
    for f in ("%Y-%m-%d","%d-%b-%Y","%d-%m-%Y","%d/%m/%Y"):
        try: return datetime.strptime(s,f).date()
        except ValueError: pass
    try: return datetime.fromisoformat(s[:10]).date()
    except Exception: return None


def download(d: date):
    ds=d.strftime("%Y%m%d")
    fp=CACHE/f"FO_{ds}.zip"
    if fp.exists() and fp.stat().st_size>100:
        return fp.read_bytes()
    url=BASE.format(d=ds)
    req=urllib.request.Request(url, headers={"User-Agent":UA,"Accept":"*/*","Referer":"https://www.nseindia.com/"})
    for k in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                b=r.read()
            if b[:2]==b"PK":
                fp.write_bytes(b); return b
        except Exception as e:
            if k==2:
                # fallback to runner's curl
                tmp=str(fp)+".tmp"
                p=subprocess.run(["curl","-L","--fail","--retry","2","-A",UA,"-e","https://www.nseindia.com/","-o",tmp,url],capture_output=True,text=True)
                if p.returncode==0 and Path(tmp).exists():
                    b=Path(tmp).read_bytes(); Path(tmp).unlink(missing_ok=True)
                    if b[:2]==b"PK": fp.write_bytes(b); return b
            time.sleep(1.2*(k+1))
    return None


def to_float(x):
    try:
        v=float(str(x).replace(",","").strip())
        return v if math.isfinite(v) else None
    except Exception: return None


def read_day(d: date, raw: bytes):
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        names=[n for n in z.namelist() if n.lower().endswith(".csv")]
        if not names: return {}, "no_csv"
        with z.open(names[0]) as f:
            text=io.TextIOWrapper(f,encoding="utf-8-sig",errors="replace",newline="")
            rd=csv.DictReader(text)
            fields={str(x).strip().upper():x for x in (rd.fieldnames or [])}
            def col(*names):
                for n in names:
                    if n.upper() in fields: return fields[n.upper()]
                return None
            c_sym=col("TckrSymb","SYMBOL")
            c_inst=col("FinInstrmTp","INSTRUMENT")
            c_exp=col("XpryDt","EXPIRY_DT")
            c_close=col("ClsPric","CLOSE")
            c_settle=col("SttlmPric","SETTLE_PR")
            c_under=col("UndrlygPric","UNDERLYING_VALUE","UNDERLYING")
            c_vol=col("TtlTradgVol","CONTRACTS","TOTTRDQTY")
            if not all([c_sym,c_exp,c_close,c_under]):
                return {}, "missing_cols:"+",".join(fields.keys())
            rows=defaultdict(list)
            for r in rd:
                inst=(r.get(c_inst,"") if c_inst else "").strip().upper()
                # Current UDiFF stock future code = STF; legacy datasets may say FUTSTK.
                if c_inst and inst not in ("STF","FUTSTK"):
                    continue
                exp=parse_date(r.get(c_exp,""))
                if exp is None or exp<d: continue
                sym=(r.get(c_sym,"") or "").strip().upper()
                fut=to_float(r.get(c_close,"")); settle=to_float(r.get(c_settle,"")) if c_settle else None
                spot=to_float(r.get(c_under,"")); vol=to_float(r.get(c_vol,"")) if c_vol else None
                if not sym or not spot or spot<=0: continue
                if not fut or fut<=0: fut=settle
                if not fut or fut<=0: continue
                rows[sym].append((exp,spot,fut,settle,vol or 0.0))
            # nearest expiry for each symbol/day
            return {s:sorted(v,key=lambda x:x[0])[0] for s,v in rows.items()}, "ok"


@dataclass
class Pos:
    sym: str; entry: date; expiry: date; s0: float; f0: float
    min_ret: float=0.0; peak_ret: float=0.0; max_dd: float=0.0; max_ret: float=0.0


def main():
    daily={}; quality=[]
    d=START
    while d<=END:
        if d.weekday()<5:
            raw=download(d)
            if raw:
                try:
                    rows,status=read_day(d,raw)
                    daily[d]=rows
                    quality.append({"date":d.isoformat(),"status":status,"symbols":len(rows)})
                    print(d, status, len(rows), flush=True)
                except Exception as e:
                    quality.append({"date":d.isoformat(),"status":"parse_error","error":repr(e)})
            else:
                quality.append({"date":d.isoformat(),"status":"missing_or_holiday","symbols":0})
                print(d,"missing_or_holiday",flush=True)
        d+=timedelta(days=1)
    if not daily:
        raise SystemExit("No NSE data downloaded")

    positions={}; trades=[]
    eligible=defaultdict(int)
    for d in sorted(daily):
        rows=daily[d]
        for sym in rows: eligible[sym]+=1
        for sym,(exp,spot,fut,settle,vol) in rows.items():
            p=positions.get(sym)
            if p is None:
                # Do not initiate at the closing print on expiry day.
                if d<exp:
                    positions[sym]=Pos(sym,d,exp,spot,fut)
                continue
            # Track only the exact contract opened. If near month rolled early for any reason,
            # wait for the opened expiry row; daily UDiFF normally keeps it until expiry.
            if exp!=p.expiry and d<p.expiry:
                continue
            exit_fut=fut
            reason=None
            if d>=p.expiry:
                if settle and settle>0: exit_fut=settle
                reason="expiry"
            ret=((spot-p.s0)+(p.f0-exit_fut))/p.s0
            p.min_ret=min(p.min_ret,ret); p.max_ret=max(p.max_ret,ret)
            p.peak_ret=max(p.peak_ret,ret); p.max_dd=min(p.max_dd,ret-p.peak_ret)
            if ret>=TARGET: reason="target"
            if reason:
                trades.append({
                    "symbol":sym,"entry_date":p.entry.isoformat(),"exit_date":d.isoformat(),
                    "expiry":p.expiry.isoformat(),"cash_entry":round(p.s0,6),"future_short_entry":round(p.f0,6),
                    "cash_exit":round(spot,6),"future_exit":round(exit_fut,6),"exit_reason":reason,
                    "gross_return_pct":round(ret*100,6),"mae_pct":round(p.min_ret*100,6),
                    "mfe_pct":round(p.max_ret*100,6),"max_drawdown_pct":round(p.max_dd*100,6),
                    "holding_calendar_days":(d-p.entry).days
                })
                del positions[sym]

    # Mark remaining positions to END using last matching observation if present.
    for sym,p in list(positions.items()):
        last=None
        for d in sorted(daily, reverse=True):
            if sym in daily[d]:
                exp,spot,fut,settle,vol=daily[d][sym]
                if exp==p.expiry:
                    last=(d,spot,fut); break
        if last:
            d,spot,fut=last; ret=((spot-p.s0)+(p.f0-fut))/p.s0
            p.min_ret=min(p.min_ret,ret); p.max_ret=max(p.max_ret,ret); p.peak_ret=max(p.peak_ret,ret); p.max_dd=min(p.max_dd,ret-p.peak_ret)
            trades.append({"symbol":sym,"entry_date":p.entry.isoformat(),"exit_date":d.isoformat(),"expiry":p.expiry.isoformat(),
                "cash_entry":round(p.s0,6),"future_short_entry":round(p.f0,6),"cash_exit":round(spot,6),"future_exit":round(fut,6),
                "exit_reason":"open","gross_return_pct":round(ret*100,6),"mae_pct":round(p.min_ret*100,6),"mfe_pct":round(p.max_ret*100,6),
                "max_drawdown_pct":round(p.max_dd*100,6),"holding_calendar_days":(d-p.entry).days})

    # summary by symbol
    by=defaultdict(list)
    for t in trades: by[t["symbol"]].append(t)
    summary=[]
    for sym,ts in by.items():
        closed=[t for t in ts if t["exit_reason"]!="open"]
        hits=[t for t in closed if t["exit_reason"]=="target"]
        exps=[t for t in closed if t["exit_reason"]=="expiry"]
        summary.append({
            "symbol":sym,"eligible_days":eligible[sym],"trades":len(ts),"closed_trades":len(closed),
            "target_hits":len(hits),"expiry_exits":len(exps),"open_trades":sum(t["exit_reason"]=="open" for t in ts),
            "hit_rate_pct":round(100*len(hits)/len(closed),4) if closed else None,
            "avg_holding_days_targets":round(sum(t["holding_calendar_days"] for t in hits)/len(hits),3) if hits else None,
            "worst_mae_pct":round(min(t["mae_pct"] for t in ts),6),
            "worst_max_drawdown_pct":round(min(t["max_drawdown_pct"] for t in ts),6),
            "best_trade_pct":round(max(t["gross_return_pct"] for t in ts),6),
            "cumulative_closed_gross_return_pct":round(sum(t["gross_return_pct"] for t in closed),6)
        })
    summary.sort(key=lambda x:(x["target_hits"], x["hit_rate_pct"] or -1, x["cumulative_closed_gross_return_pct"]), reverse=True)

    def write_csv(path,rows):
        if not rows: return
        with open(path,"w",newline="",encoding="utf-8") as f:
            w=csv.DictWriter(f,fieldnames=list(rows[0].keys())); w.writeheader(); w.writerows(rows)
    write_csv("nse_cash_futures_summary.csv",summary)
    write_csv("nse_cash_futures_trades.csv",trades)
    write_csv("nse_cash_futures_quality.csv",quality)
    agg={
        "start":START.isoformat(),"end":END.isoformat(),"target_pct":TARGET*100,
        "symbols":len(summary),"trades":len(trades),"closed_trades":sum(x["closed_trades"] for x in summary),
        "target_hits":sum(x["target_hits"] for x in summary),"expiry_exits":sum(x["expiry_exits"] for x in summary),
        "open_trades":sum(x["open_trades"] for x in summary),
        "overall_hit_rate_pct":round(100*sum(x["target_hits"] for x in summary)/max(1,sum(x["closed_trades"] for x in summary)),4),
        "worst_trade_mae_pct":min((t["mae_pct"] for t in trades),default=None),
        "worst_trade_drawdown_pct":min((t["max_drawdown_pct"] for t in trades),default=None),
        "total_closed_gross_return_pct_sum":round(sum(t["gross_return_pct"] for t in trades if t["exit_reason"]!="open"),6),
        "note":"Price-differential gross backtest; dividends, financing, brokerage, taxes, slippage and margin cost excluded. Equal share-equivalent legs."
    }
    Path("nse_cash_futures_aggregate.json").write_text(json.dumps(agg,indent=2),encoding="utf-8")
    print("AGGREGATE",json.dumps(agg),flush=True)
    print("TOP20")
    for r in summary[:20]: print(json.dumps(r),flush=True)

if __name__=="__main__": main()
