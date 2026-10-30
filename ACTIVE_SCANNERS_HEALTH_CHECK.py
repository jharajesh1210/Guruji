#!/usr/bin/env python3
"""Read-only GitHub Actions health check. No orders, no strategy changes."""
import json, os, re, sys, urllib.request, urllib.error
from datetime import datetime, timezone

REPO=os.environ["GITHUB_REPOSITORY"]
TOKEN=os.environ["GITHUB_TOKEN"]
BASE=f"https://api.github.com/repos/{REPO}"
HEAD={"Authorization":f"Bearer {TOKEN}","Accept":"application/vnd.github+json","User-Agent":"Guruji-health"}
EXCLUDE={"ALL_BOTS_STATUS.yml","ALL_SCANNER_REPORT.yml","ALL_6_SCANNERS_RESULTS.yml",
         "TELEGRAM_TEST.yml","TELEGRAM_DELIVERY_DIAGNOSTIC.yml",
         "THREE_SCANNERS_REPORT.yml","NEWS_ENGINE_DIAGNOSTIC.yml",
         "REPLAY_TODAY.yml","BTC_LOSS_ANALYSIS.yml"}
def api(path):
    with urllib.request.urlopen(urllib.request.Request(BASE+path,headers=HEAD),timeout=25) as r:
        return json.load(r)
def age_hours(value):
    return (datetime.now(timezone.utc)-datetime.fromisoformat(value.replace("Z","+00:00"))).total_seconds()/3600
def send(message):
    token=os.environ.get("BOT_TOKEN","")
    chat=os.environ.get("CHAT_ID","")
    if not token or not chat:
        raise RuntimeError("BOT_TOKEN/CHAT_ID missing")
    payload=json.dumps({"chat_id":chat,"text":message[:4000]}).encode()
    req=urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage",
        data=payload,headers={"Content-Type":"application/json"},method="POST")
    with urllib.request.urlopen(req,timeout=25) as r:
        if not json.load(r).get("ok"):
            raise RuntimeError("Telegram rejected health alert")
def main():
    workflows=api("/actions/workflows?per_page=100")["workflows"]
    issues=[]; healthy=0; skipped=0
    for wf in workflows:
        name=wf["path"].rsplit("/",1)[-1]
        if wf["state"]!="active" or name in EXCLUDE:
            skipped+=1
            continue
        runs=api(f"/actions/workflows/{wf['id']}/runs?per_page=1")["workflow_runs"]
        if not runs:
            issues.append(f"{wf['name']}: NEVER RUN")
            continue
        run=runs[0]; status=run["status"]; conclusion=run.get("conclusion")
        age=age_hours(run["created_at"])
        # Detect stale recurring scanners; daily/manual workflows are not required to run every 3h.
        try:
            import base64
            contents=api("/contents/"+wf["path"])
            yml=base64.b64decode(contents["content"]).decode()
            cron_lines=re.findall(r'cron:\s*["\']?([^"\'\n#]+)',yml)
            frequent=any(line.strip().split()[2:] == ["*","*","*"] for line in cron_lines if len(line.strip().split())==5)
        except Exception:
            frequent=False
        if status=="completed" and conclusion not in ("success","skipped"):
            issues.append(f"{wf['name']}: {conclusion} ({age:.1f}h ago)")
        elif frequent and age>3:
            issues.append(f"{wf['name']}: NO RECENT RUN ({age:.1f}h)")
        else:
            healthy+=1
        print(f"{wf['name']}: {status}/{conclusion}, last {age:.1f}h ago")
    print(f"Healthy={healthy}, Issues={len(issues)}, Excluded/disabled={skipped}")
    if issues:
        msg="GURUJI SCANNER HEALTH ALERT\n"+"\n".join("- "+i for i in issues[:35])
        msg+="\n\nNote: No trade signal is not a Telegram failure. No trading code changed."
        send(msg)
        print("Telegram health alert sent")
    else:
        print("No workflow failure/staleness detected; no Telegram alert needed")
if __name__=="__main__":
    main()
