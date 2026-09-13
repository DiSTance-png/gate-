from okx_runtime import replace_cli_prefix as okx_private_command
import subprocess
import json
import datetime

res = subprocess.run(okx_private_command("okx account bills --limit 100 --json"), shell=True, capture_output=True, text=True)
bills = json.loads(res.stdout) if res.stdout else []

orders_by_id = {}
for b in reversed(bills):
    ts = int(b.get("ts", 0))/1000.0
    dt = datetime.datetime.fromtimestamp(ts, tz=datetime.timezone(datetime.timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
    if dt >= "2026-08-29 01:11:20":
        sub_type = str(b.get("subType"))
        if sub_type in ["5", "6"]:
            ord_id = b.get("ordId") or b.get("billId")
            inst = b.get("instId", "").replace("-USDT-SWAP", "")
            pnl = float(b.get("pnl", 0) or 0)
            fee = float(b.get("fee", 0) or 0)
            
            if ord_id not in orders_by_id:
                orders_by_id[ord_id] = {
                    "ordId": ord_id,
                    "time": dt,
                    "inst": inst,
                    "gross_pnl": 0.0,
                    "fee": 0.0,
                    "net_pnl": 0.0
                }
            orders_by_id[ord_id]["gross_pnl"] += pnl
            orders_by_id[ord_id]["fee"] += fee
            orders_by_id[ord_id]["net_pnl"] += (pnl + fee)

print("=== AGGREGATED REAL ORDERS (SINCE 01:11:20 REBOOT) ===")
wins = 0
losses = 0
for ord_id, o in orders_by_id.items():
    p = o["net_pnl"]
    if p > 0: wins += 1
    elif p < 0: losses += 1
    print(f"[{o['time']}] {o['inst']:<5} | ordId={ord_id} | gross={o['gross_pnl']:+.4f} | fee={o['fee']:+.4f} | net={p:+.4f} | {'🟢 胜' if p>0 else '🔴 负'}")

print("----------------------------------------------------")
print(f"真实订单级平仓战绩: {wins} 胜 / {losses} 负 (总订单数: {len(orders_by_id)}) | 真实胜率 = {wins/(wins+losses)*100:.1f}%")
