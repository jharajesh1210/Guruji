import os,sys,json,urllib.request
from datetime import datetime,timezone,timedelta
def main():
    mode=sys.argv[1]; path=sys.argv[2]
    text=open(path,encoding="utf-8",errors="replace").read()
    message=None
    if mode=="coindcx":
        import re
        m=re.search(r"Generated Signal:\s*(BUY|SELL|HOLD)",text)
        if not m: raise SystemExit("No valid signal line; Telegram not sent")
        if m.group(1)!="HOLD":
            message="GURUJI CoinDCX BTC Futures SIGNAL: "+m.group(1)+"\n"+next((x for x in text.splitlines() if "Current BTC Price:" in x),"")
    elif mode=="vwap":
        if "Setup confirmed: next candle OPEN pe SELL" in text:
            message="GURUJI Arvind VWAP SHORT: Setup confirmed for next candle OPEN (paper signal).\n"+next((x for x in text.splitlines() if "VWAP" in x and "Close" in x),"")
    elif mode=="backtest":
        message="GURUJI Arvind BTC/PAXG historical backtest completed. This is NOT a live trading signal. See GitHub Actions artifacts."
    else: raise SystemExit("Unknown mode")
    if not message:
        print("No new signal; Telegram not sent")
        return
    token=os.getenv("BOT_TOKEN",""); chat=os.getenv("CHAT_ID","")
    if not token or not chat: raise SystemExit("Missing BOT_TOKEN/CHAT_ID")
    req=urllib.request.Request("https://api.telegram.org/bot"+token+"/sendMessage",
        data=json.dumps({"chat_id":chat,"text":message}).encode(),
        headers={"Content-Type":"application/json"},method="POST")
    with urllib.request.urlopen(req,timeout=20) as r:
        response=json.load(r)
    if not response.get("ok"): raise RuntimeError("Telegram API rejected message")
    print("TELEGRAM SENT")
if __name__=="__main__": main()
