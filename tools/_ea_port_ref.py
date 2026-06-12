"""_ea_port_ref.py — EA 移植検証用の Python リファレンス出力。

録音 M5 を N 本読み、Python の ZigZagTracker / _dow_trend で
M5・M15 ピボットと M5 トレンド系列を出して JSON で stdout に吐く。
tools/verify_ea_port.mjs が JS 版の出力とこれを突き合わせる。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.core.indicators import ZigZagTracker  # noqa: E402
from src.core.strategy_base import Bar  # noqa: E402
from strategies.mtf_pullback.strategy import _dow_trend  # noqa: E402

FILE = sys.argv[1]
N = int(sys.argv[2])

seen = {}
for line in open(FILE, encoding="utf-8"):
    if '"tick"' not in line:
        continue
    r = json.loads(line)
    if r.get("_type") != "tick":
        continue
    m = r.get("m15")
    if not m:
        continue
    t = int(m["time"])
    if t in seen:
        continue
    seen[t] = m
bars = [seen[t] for t in sorted(seen)][:N]

dev = 3.0 * 0.0001
zz5 = ZigZagTracker(5, dev)
zz15 = ZigZagTracker(8, dev)
last_m15 = -1
m5buf = []
m5trend = []
for m in bars:
    bar = Bar(time=int(m["time"]), open=m["open"], high=m["high"],
              low=m["low"], close=m["close"], volume=m.get("volume", 0))
    m5buf.append(bar)
    if len(m5buf) > 30:
        m5buf.pop(0)
    zz5.update(bar)
    if (bar.time + 300) % 900 == 0 and len(m5buf) >= 3:
        c = m5buf[-3:]
        m15 = Bar(time=c[0].time, open=c[0].open,
                  high=max(b.high for b in c), low=min(b.low for b in c),
                  close=c[-1].close, volume=0)
        if m15.time > last_m15:
            zz15.update(m15)
            last_m15 = m15.time
    m5trend.append(_dow_trend(zz5.pivots))


def tail(pivs, k=30):
    return [{"index": p.index, "kind": p.kind, "price": round(p.price, 6)} for p in pivs[-k:]]


print(json.dumps({
    "n": len(bars),
    "zz5": tail(zz5.pivots),
    "zz15": tail(zz15.pivots),
    "m5trend_tail": m5trend[-50:],
}))
