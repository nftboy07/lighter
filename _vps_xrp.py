import json
import urllib.request

import paramiko
import _vps_deploy as d

acc = json.load(urllib.request.urlopen(
    "https://mainnet.zklighter.elliot.ai/api/v1/account?by=index&value=737649", timeout=20
))
a = (acc.get("accounts") or [acc])[0]
for p in a.get("positions") or []:
    if float(p.get("position") or 0) == 0:
        continue
    print(
        p.get("symbol"),
        "sign", p.get("sign"),
        "size", p.get("position"),
        "orders", p.get("open_order_count"),
        "entry", p.get("avg_entry_price"),
        "upnl", p.get("unrealized_pnl"),
    )

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect(d.HOST, username=d.USER, password=d.password(), timeout=20, allow_agent=False, look_for_keys=False)
d.run(c, "cmd /c findstr /C:\"Placing TP/SL\" C:\\LighterBot\\sniper.log")
c.close()
