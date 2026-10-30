import csv, os, json
from pathlib import Path
from urllib.request import Request, urlopen
from datetime import datetime, timezone, timedelta

REPO=os.getenv("GITHUB_REPOSITORY","jharajesh1210/Guruji")
TOKEN=os.getenv("GITHUB_TOKEN","")
HEAD={"Accept":"application/vnd.github+json","User-Agent":"Guruji-Report"}
if TOKEN: HEAD["Authorization"]="Bearer "+TOKEN
def api(path):
    with urlopen(Request("https://api.github.com/repos/"+REPO+path,headers=HEAD),timeout=30) as r: return json.load(r)
def number(x):
    try: return float(str(x).replace(",","").replace("%",""))
    except (ValueError,TypeError): return None
def performance(name):
    prefix=Path(name).stem.lower().replace(".yml","")
    matches=[p for p in Path(".").rglob("*trades.csv") if p.is_file() and (prefix in p.stem.lower() or p.stem.lower().startswith(prefix.replace("_paper","")))]
    if not matches: return ("N/A","N/A","N/A","N/A","N/A","No matching trade CSV")
    wins=losses=0; pnl=0.; pnl_count=0
    for p in matches:
        with p.open(encoding="utf-8-sig",newline="") as f:
            for row in csv.DictReader(f):
                r={k.lower().strip():v for k,v in row.items() if k}
                status=str(next((r[k] for k in ("status","result","outcome","exit_reason") if k in r),"")).lower()
                val=next((number(r[k]) for k in ("pnl_usdt","net_pnl","realized_pnl","pnl","profit_usdt","profit") if k in r),None)
                if status in ("open","active","pending","entry","running"): continue
                if val is not None:
                    pnl+=val; pnl_count+=1
                    if val>0: wins+=1
                    elif val<0: losses+=1
                elif status in ("win","won","tp","take_profit"): wins+=1
                elif status in ("loss","lost","sl","stop_loss"): losses+=1
    total=wins+losses
    return (total,wins,losses,round(100*wins/total,2) if total else "N/A",round(pnl,4) if pnl_count else "N/A", "; ".join(str(p) for p in matches))
def main():
    workflows=[]; page=1
    while True:
        batch=api(f"/actions/workflows?per_page=100&page={page}").get("workflows",[])
        workflows+=batch
        if len(batch)<100: break
        page+=1
    rows=[]
    for w in workflows:
        try: run=api(f"/actions/workflows/{w['id']}/runs?per_page=1").get("workflow_runs",[])
        except Exception as e: run=[]; print("Run lookup error",w["name"],e)
        r=run[0] if run else {}
        dt=r.get("run_started_at") or r.get("created_at")
        ist=(datetime.fromisoformat(dt.replace("Z","+00:00")).astimezone(timezone(timedelta(hours=5,minutes=30))).strftime("%Y-%m-%d %H:%M:%S") if dt else "")
        perf=performance(Path(w.get("path","")).stem)
        rows.append(dict(scanner=w["name"],workflow=w.get("path",""),enabled=w.get("state",""),last_run_ist=ist,run_status=r.get("status","never_run"),conclusion=r.get("conclusion",""),run_url=r.get("html_url",""),total_closed_trades=perf[0],wins=perf[1],losses=perf[2],win_percent=perf[3],net_pnl=perf[4],trade_csv=perf[5],telegram="Not verified"))
    with open("ALL_SCANNER_REPORT.csv","w",newline="",encoding="utf-8-sig") as f:
        writer=csv.DictWriter(f,fieldnames=list(rows[0]) if rows else ["scanner"])
        writer.writeheader();writer.writerows(rows)
    print("Report saved for",len(rows),"workflows")
if __name__=="__main__": main()
