"""
mtf_pullback/strategy.py — マルチタイムフレーム押し目戦略 (M5 ベース)。

エントリー条件:
  1. H4 / H1 / M30 / M15 のトレンドが全て同方向 (= 大局トレンド成立)
  2. M5 トレンドが直近 lookback_bars 以内で反対方向だった (= 押し戻し)
  3. M5 トレンドが大局方向に転換 (= 押し戻し終わり、再加速の入り口)

SL:
  ロング → M15 直近 Z1 安値
  ショート → M15 直近 Z1 高値
  (M15 構造に anchor された損切、= スイングが崩れたら撤退)

TP:
  entry ± sl_dist (= 1:1 RR、同じ価格距離)

ポジションサイズ:
  証拠金 × 1% / sl_dist で逆算 (= SL ヒット = 1% 損失)

データ要件:
  M5 ベース + H1/H4 MTF が ctx 経由で来る前提。
  M15/M30 は M5 から内部集計 (3 本/6 本)。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from src.core.indicators import Pivot, ZigZagTracker, atr
from src.core.strategy_base import Bar, Context, Strategy, StrategyParams


# run_backtest が読む宣言
SAMPLE_PERIOD_SECONDS = 300  # M5
DEFAULT_CSV = "sample_M5.csv"
MTF_PERIODS = [3600, 14400]  # H1, H4 は外部 MTF として受け取る
INITIAL_BALANCE = 10_000.0


@dataclass
class Params(StrategyParams):
    # ZigZag (M5 ベース。上位足は ZigZag depth を上げて長期視点に)
    zz_depth_m5: int = 5
    zz_depth_m15: int = 8
    zz_depth_m30: int = 10
    zz_depth_h1: int = 12
    zz_depth_h4: int = 12
    zz_dev_pips: float = 3.0
    # ATR
    atr_period: int = 14
    # M5 押し戻し検出: 過去 N 本以内に「反対方向トレンド」があった必要あり
    pullback_lookback_bars: int = 30  # M5 30本 = 150 分 = 2.5h
    # リスク
    risk_pct: float = 0.01
    # SL の妥当性: 直近 M15 安値 / 高値が現在価格から離れすぎてないか
    # min: 0 だと無効、max: sl_dist > price × max_sl_ratio なら skip
    min_sl_dist_atr: float = 0.3   # ATR の 30% 以上の SL を要求 (= スプレッド分余裕)
    max_sl_dist_atr: float = 5.0   # ATR の 5 倍超は遠すぎ skip
    # ★ 絶対最小SL (pips)。SL がこの幅未満ならエントリー skip。0=無効。
    #   タイトSL=コスト(スプレッド+コミッション)で負ける層。net 検証で 15-20pips が最良。
    min_sl_dist_pips: float = 0.0
    # 換算
    pip_size: float = 0.0001
    pip_value: float = 10.0
    # 同方向の連続再発火を防ぐクールダウン (M5 バー数)
    cooldown_bars: int = 6
    # ★ v2: H4/H1 トレンドラインを破ったらスキップ
    # 上昇トレンド時、price が H4/H1 の昇トレンドライン (= 2 つの安値を結んだ線) より下なら skip
    # 下降トレンド時、price が H4/H1 の降トレンドライン (= 2 つの高値を結んだ線) より上なら skip
    skip_on_trendline_break: bool = False
    # ★ v3: H4 から集計した D1/W1 ピボットがエントリー方向の "壁" として近接していたら skip
    # 「近接」= entry_price から daily_wall_max_atr × ATR 以内
    skip_on_daily_line: bool = False
    daily_wall_max_atr: float = 2.0   # 進行方向にこの距離以内に D1/W1 ピボットあれば skip
    # ★ 頻度調整: 大局アラインメントに M30 を含めるか (False なら H4=H1=M15 の 3 階層)
    require_m30_alignment: bool = True
    # ★ アラインメント TF を明示指定 (空=既定ロジック)。例 "h4,m15" / "h1,m15" で 1 上位足のみ一致。
    align_tfs: str = ""
    # ★ SL バッファ: M15 ピボットから ATR×この分さらに離す (ノイズで刈られるのを防ぐ)
    sl_buffer_atr: float = 0.0
    # ★ 構造インタクト確認: 直近で M15 の higher-low(LL) を割っていたら
    #   = 上昇構造ブレイク=転換とみなし entry skip (ダウ理論的に押し目でなく転換)
    require_structure_intact: bool = False
    structure_lookback_bars: int = 30        # 安値ベース判定で見る直近 M5 本数
    # 終値ベース判定: True なら「直近 K 本の確定 M15 バーが anchor を終値で割ったか」で判定
    #   (ヒゲ=ストップ狩りは許容し、終値で割った本物の転換だけ skip)
    structure_use_close: bool = False
    structure_lookback_m15: int = 10
    # ★ SL アンカーを「直近の実際の安値/高値(ヒゲ含む)」にする。
    #   ストップ狩りのヒゲを許容して入る以上、SL はそのヒゲの下(上)に置くのが筋。
    sl_anchor_recent_low: bool = False
    # ★ TP の RR 倍率。1.0=1:1、1.5=リスクの1.5倍をTPに (SLリスクは1%のまま)
    tp_rr: float = 1.0
    # ★ TP が直近 M15 スイング内に収まる設定のみエントリー。
    #   long: TP <= 直近 M15 高値、short: TP >= 直近 M15 安値 (= 届きやすい目標に限定)
    require_tp_within_swing: bool = False
    # ★ SL アンカーを「ダウ的に確定したスイング高安」にする (ZigZag の未確定極値を使わない)。
    #   未確定の小さな揉み合い天井/底に SL を置いてノイズで刈られるのを防ぐ。
    sl_dow_confirmed_swing: bool = False
    # ★ room_R フィルタ: 直近 M15 スイング高安までの余地 ÷ sl_dist が大きすぎる
    #   (= SL がスイングに対してタイトすぎ、ノイズで刈られやすい) エントリーを除外。
    #   0 = 無効。例 2.5 で room_R>=2.5 を skip。
    room_R_max: float = 0.0
    # ★ 時間帯フィルタ: [start, end) (UTC時) のエントリーを skip。-1 = 無効。
    block_hour_start: int = -1
    block_hour_end: int = -1
    # ★ ボラ regime フィルタ: ATR/price*100 がこの値超なら skip (高ボラ=ダマシ多)。0=無効。
    max_atr_pct: float = 0.0
    # ★ ①コンフルエンス: 押し目の実際の安値/高値が直近 H1 スイング(支持/抵抗)の
    #   ±confluence_atr×ATR 以内であること (= 構造的な足場で反発するエントリーのみ)。
    require_h1_confluence: bool = False
    confluence_atr: float = 1.5
    # ★ ②トリガーの質: 転換(エントリー)足が強い推進であること = 方向一致 + レンジ >= impulse_atr×ATR。
    require_impulse: bool = False
    impulse_atr: float = 1.0
    # ★ ③相対ボラ regime: 現在 ATR が直近中央値の max_atr_ratio 倍超なら skip
    #   (絶対値でなくペア相対 = そのペアの平常比で異常高ボラ=ダマシ)。0=無効。
    max_atr_ratio: float = 0.0
    # ★ M5 転換時、M15 のトレンド方向の極値が未更新であること (= 直近 M15 ピボットが
    #   逆側 = 押し目の極値。M5 再転換が M15 新高値/新安値より前に起きているエントリーのみ)。
    require_m15_unupdated_extreme: bool = False
    # ★ タスク1: M15 の推進波(レッグ)が拡大しているとき(=トレンド加速)のみエントリー。
    require_expanding_legs: bool = False
    # ★ タスク2: 1 つ上の時間足モード。H4=H1 一致 + M15 トレンド転換でエントリー、SL は H1 構造。
    htf_mode: bool = False
    # ★ NEW: トレンド品質フィルタ = Kaufman 効率比(ER) on H1。
    #   ER = |close[t]-close[t-N]| / Σ|close[i]-close[i-1]| (0..1, 高=綺麗なトレンド)。
    #   平均回帰押し目はクリーンなトレンドでのみ機能する仮説 → ER>=閾値 のみエントリー。0=off。
    min_h1_er: float = 0.0
    er_period: int = 10        # ER の H1 本数
    # ★ NEW: トレンド方向 ER のみ要求 (ER は方向不問だが、進行方向に効率的か net displacement の符号で確認)
    er_directional: bool = True


def _dow_trend(pivots: list[Pivot]) -> Optional[str]:
    """直近 4 ピボットから Dow トレンド ('up' / 'down' / None) を返す。"""
    if len(pivots) < 4:
        if len(pivots) >= 2:
            last = pivots[-1]
            prev = pivots[-2]
            if last.kind == "high" and prev.kind == "low":
                return "up" if last.price > prev.price else None
            if last.kind == "low" and prev.kind == "high":
                return "down" if last.price < prev.price else None
        return None
    last4 = pivots[-4:]
    highs = [p for p in last4 if p.kind == "high"]
    lows = [p for p in last4 if p.kind == "low"]
    if len(highs) >= 2 and len(lows) >= 2:
        hh = highs[-1].price > highs[-2].price
        hl = lows[-1].price > lows[-2].price
        ll = lows[-1].price < lows[-2].price
        lh = highs[-1].price < highs[-2].price
        if hh and hl:
            return "up"
        if ll and lh:
            return "down"
    return None


def _opposite(d: Optional[str]) -> Optional[str]:
    if d == "up":
        return "down"
    if d == "down":
        return "up"
    return None


def _legs_expanding(pivots: list[Pivot], trend: str) -> bool:
    """トレンド方向の推進波(レッグ)が拡大しているか (= トレンド加速)。
    up: 上昇レッグ(安値→高値)の直近 2 本を比較し、最新が大きければ True。
    down: 下降レッグ(高値→安値)で同様。レッグが 2 本未満なら False。
    """
    legs = []
    if trend == "up":
        for i in range(1, len(pivots)):
            if pivots[i - 1].kind == "low" and pivots[i].kind == "high":
                legs.append(pivots[i].price - pivots[i - 1].price)
    elif trend == "down":
        for i in range(1, len(pivots)):
            if pivots[i - 1].kind == "high" and pivots[i].kind == "low":
                legs.append(pivots[i - 1].price - pivots[i].price)
    else:
        return False
    if len(legs) < 2:
        return False
    return legs[-1] > legs[-2]


def _last_confirmed_swing(pivots: list[Pivot], kind: str) -> Optional[Pivot]:
    """ダウ理論で「確定した」スイング高値/安値を返す (ZigZag は極値探索の補助としてのみ使う)。

    - high: その高値の後に、高値の直前の安値を下回る安値ができたら確定
      (= 下落で構造が壊れ、その高値がスイング天井として確定した)。
    - low:  その安値の後に、安値の直前の高値を上回る高値ができたら確定。

    確定が無ければ None。ZigZag が拾っただけの未確定の極値 (まだ構造が壊れていない) は返さない。
    """
    n = len(pivots)
    want = "high" if kind == "high" else "low"
    opp = "low" if kind == "high" else "high"
    for i in range(n - 1, -1, -1):
        if pivots[i].kind != want:
            continue
        # 直前の逆側ピボット
        before = None
        for j in range(i - 1, -1, -1):
            if pivots[j].kind == opp:
                before = pivots[j]
                break
        if before is None:
            continue
        # 後続の逆側ピボットが before を突破したか (high なら下回る / low なら上回る)
        for k in range(i + 1, n):
            if pivots[k].kind != opp:
                continue
            broke = (pivots[k].price < before.price) if kind == "high" else (pivots[k].price > before.price)
            if broke:
                return pivots[i]
    return None


def _trendline_dist_atr(
    pivots: list[Pivot],
    cur_idx_in_tf: int,
    cur_price: float,
    atr_val: float,
    trend: Optional[str],
) -> Optional[float]:
    """直近 2 つの同種ピボットからトレンドラインを引き、現在価格との距離 (ATR 単位) を返す。

    trend=='up' → ascending support (2 lows) を引く。戻り値 > 0 なら price は線上、< 0 なら下抜け。
    trend=='down' → descending resistance (2 highs)。戻り値 < 0 なら price は線下、> 0 なら上抜け。
    """
    if trend not in ("up", "down"):
        return None
    if trend == "up":
        same = [p for p in pivots if p.kind == "low"]
    else:
        same = [p for p in pivots if p.kind == "high"]
    if len(same) < 2:
        return None
    P1, P2 = same[-2], same[-1]
    if P2.index <= P1.index:
        return None
    slope = (P2.price - P1.price) / (P2.index - P1.index)
    line_now = P2.price + slope * (cur_idx_in_tf - P2.index)
    if atr_val <= 0:
        return None
    return (cur_price - line_now) / atr_val


def _aggregate(bars: list[Bar], period_sec: int, target_period_sec: int) -> list[Bar]:
    """period_sec 単位の bars を target_period_sec の bars に集計。

    target / period 本まとめて 1 本にする。完全に区切れた grouping のみ。
    """
    n = target_period_sec // period_sec
    if n <= 1:
        return list(bars)
    out: list[Bar] = []
    # 集計開始は target_period_sec のグリッドに沿う
    # bars[0].time が必ずしも border ではないので、border にぴったりのところから始める
    if not bars:
        return out
    # 最初に target_period_sec の境界に乗る index を探す
    start = 0
    while start < len(bars) and bars[start].time % target_period_sec != 0:
        start += 1
    i = start
    while i + n <= len(bars):
        chunk = bars[i:i + n]
        out.append(Bar(
            time=chunk[0].time,
            open=chunk[0].open,
            high=max(b.high for b in chunk),
            low=min(b.low for b in chunk),
            close=chunk[-1].close,
            volume=sum(b.volume for b in chunk),
        ))
        i += n
    return out


class MtfPullbackStrategy(Strategy):
    """マルチTF押し目戦略。"""

    def __init__(self, params: Params) -> None:
        super().__init__(params)
        self.p: Params = params
        dev = params.zz_dev_pips * params.pip_size
        # 各 TF の ZigZag tracker
        self.zz_m5  = ZigZagTracker(params.zz_depth_m5,  dev)
        self.zz_m15 = ZigZagTracker(params.zz_depth_m15, dev)
        self.zz_m30 = ZigZagTracker(params.zz_depth_m30, dev)
        self.zz_h1  = ZigZagTracker(params.zz_depth_h1,  dev)
        self.zz_h4  = ZigZagTracker(params.zz_depth_h4,  dev)
        # MTF dedup
        self._last_m15_time: int = -1
        self._last_m30_time: int = -1
        self._last_h1_time: int = -1
        self._last_h4_time: int = -1
        # M5 trend 履歴 (= 押し戻し検出用)
        # 各 bar での "trend at that bar" を short ring buffer に保存
        self._m5_trend_history: list[Optional[str]] = []
        self._m5_trend_history_max = max(params.pullback_lookback_bars + 5, 50)
        # HTF モード用: M15 trend 履歴 (M15 close ごとに 1 件)
        self._m15_trend_history: list[Optional[str]] = []
        self._m15_closed_this_bar = False
        # 相対ボラ用: ATR 履歴 (中央値計算)
        self._atr_hist: list[float] = []
        # クールダウン
        self._last_entry_bar_idx: int = -10**9
        self._bar_idx: int = -1
        # session.py 連携 (なくても動く)
        self.symbol: str = "UNKNOWN"

    def on_bar(self, ctx: Context) -> None:
        p = self.p
        self._bar_idx += 1
        self._m15_closed_this_bar = False
        # ctx.bars() = M5 のシリーズ
        bars = ctx.bars(max(p.atr_period + 10, p.structure_lookback_bars + 2))
        if len(bars) < p.atr_period + 1:
            return
        cur = bars[-1]

        # === ZigZag 更新 ===
        # M5
        self.zz_m5.update(cur)
        # M15 (= M5 を 3 本まとめる、新しい M15 境界の終了で 1 本)
        # 完了した M15 を判定: cur.time が M15 境界の直前 (例: cur.time % 900 == 600 = 10:10 → 10:00-10:15 は cur で完了)
        # 簡易実装: 最後の bar の time +300 が次の M15 境界なら、その前の 3 本で構成
        # → cur.time + 300 が 900 の倍数なら、今の bar で M15 区切りが完了
        if (cur.time + 300) % 900 == 0:
            # 直近 3 本 (cur 含む) で M15 を作る
            m15_chunk = bars[-3:]
            if len(m15_chunk) == 3:
                m15_bar = Bar(
                    time=m15_chunk[0].time,
                    open=m15_chunk[0].open,
                    high=max(b.high for b in m15_chunk),
                    low=min(b.low for b in m15_chunk),
                    close=m15_chunk[-1].close,
                    volume=sum(b.volume for b in m15_chunk),
                )
                if m15_bar.time > self._last_m15_time:
                    self.zz_m15.update(m15_bar)
                    self._last_m15_time = m15_bar.time
                    self._m15_closed_this_bar = True
        # M30 (= M5 を 6 本まとめる)
        if (cur.time + 300) % 1800 == 0:
            m30_chunk = bars[-6:]
            if len(m30_chunk) == 6:
                m30_bar = Bar(
                    time=m30_chunk[0].time,
                    open=m30_chunk[0].open,
                    high=max(b.high for b in m30_chunk),
                    low=min(b.low for b in m30_chunk),
                    close=m30_chunk[-1].close,
                    volume=sum(b.volume for b in m30_chunk),
                )
                if m30_bar.time > self._last_m30_time:
                    self.zz_m30.update(m30_bar)
                    self._last_m30_time = m30_bar.time
        # H1
        try:
            h1_bars = ctx.bars_mtf(3600, 1)
        except (NotImplementedError, KeyError):
            h1_bars = []
        if h1_bars and h1_bars[-1].time > self._last_h1_time:
            self.zz_h1.update(h1_bars[-1])
            self._last_h1_time = h1_bars[-1].time
        # H4
        try:
            h4_bars = ctx.bars_mtf(14400, 1)
        except (NotImplementedError, KeyError):
            h4_bars = []
        if h4_bars and h4_bars[-1].time > self._last_h4_time:
            self.zz_h4.update(h4_bars[-1])
            self._last_h4_time = h4_bars[-1].time

        # === ATR ===
        highs = [b.high for b in bars]
        lows = [b.low for b in bars]
        closes = [b.close for b in bars]
        atr_line = atr(highs, lows, closes, p.atr_period)
        atr_val = atr_line[-1]
        if atr_val is None or atr_val <= 0:
            return
        self._atr_hist.append(atr_val)
        if len(self._atr_hist) > 300:
            self._atr_hist.pop(0)

        # === 各 TF のトレンド ===
        m5_trend = _dow_trend(self.zz_m5.pivots)
        m15_trend = _dow_trend(self.zz_m15.pivots)
        m30_trend = _dow_trend(self.zz_m30.pivots)
        h1_trend = _dow_trend(self.zz_h1.pivots)
        h4_trend = _dow_trend(self.zz_h4.pivots)

        # 診断用: 直近バーの各 TF トレンドを控える (ロジックには無影響)
        self._dbg_h4 = h4_trend
        self._dbg_h1 = h1_trend
        self._dbg_m30 = m30_trend

        # M5 トレンド履歴を ring buffer に
        self._m5_trend_history.append(m5_trend)
        if len(self._m5_trend_history) > self._m5_trend_history_max:
            self._m5_trend_history.pop(0)
        # M15 trend 履歴 (M15 close ごと)
        if self._m15_closed_this_bar:
            self._m15_trend_history.append(m15_trend)
            if len(self._m15_trend_history) > self._m5_trend_history_max:
                self._m15_trend_history.pop(0)

        # === 既にポジションあればここで終了 (TP/SL/反転 close は EA/サーバ任せ) ===
        if ctx.position() is not None:
            return

        # === タスク2: HTF モード (H4=H1 一致 + M15 転換でエントリー、SL は H1 構造) ===
        if p.htf_mode:
            self._try_htf_entry(ctx, cur, atr_val, h4_trend, h1_trend)
            return

        # === エントリー条件 ===
        # 1. 大局アラインメント (既定 H4 = H1 = (M30) = M15。align_tfs 指定で TF を任意に)
        if p.align_tfs:
            _tfmap = {"h4": h4_trend, "h1": h1_trend, "m30": m30_trend,
                      "m15": m15_trend, "m5": m5_trend}
            major = [_tfmap[k] for k in p.align_tfs.split(",")]
        elif p.require_m30_alignment:
            major = [h4_trend, h1_trend, m30_trend, m15_trend]
        else:
            major = [h4_trend, h1_trend, m15_trend]  # M30 を外した 3 階層
        if any(t is None for t in major):
            return
        if not all(t == major[0] for t in major):
            return
        major_dir = major[0]  # "up" or "down"

        # 2. M5 トレンドが直近 lookback_bars 以内で逆方向だった
        recent_m5 = self._m5_trend_history[-p.pullback_lookback_bars:]
        opposite_dir = _opposite(major_dir)
        had_opposite = any(t == opposite_dir for t in recent_m5)
        if not had_opposite:
            return

        # 3. M5 が major 方向に転換 (= 現在が major、直前バーは opposite or None)
        if m5_trend != major_dir:
            return
        # 直近の trend が major と違う、と確認 (= "今まさに転換した")
        if len(self._m5_trend_history) >= 2:
            prev_m5 = self._m5_trend_history[-2]
            if prev_m5 == major_dir:
                # 既に転換済み (= さっき入るべきだった)。今は遅い。
                return

        # 3b. M15 のトレンド方向の極値が未更新 (= 直近 M15 ピボットが逆側=押し目の極値)。
        #     M5 再転換が M15 新高値/新安値より「前」に起きているエントリーのみ採用。
        if p.require_m15_unupdated_extreme:
            if not self.zz_m15.pivots:
                return
            want_kind = "low" if major_dir == "up" else "high"
            if self.zz_m15.pivots[-1].kind != want_kind:
                return

        # 3c. タスク1: M15 推進波が拡大 (トレンド加速) しているエントリーのみ
        if p.require_expanding_legs:
            if not _legs_expanding(self.zz_m15.pivots, major_dir):
                return

        # 3d. ②トリガーの質: 転換足が強い推進か (方向一致 + 大きめレンジ)
        if p.require_impulse:
            rng = cur.high - cur.low
            if rng < p.impulse_atr * atr_val:
                return
            if major_dir == "up" and cur.close <= cur.open:
                return
            if major_dir == "down" and cur.close >= cur.open:
                return

        # 4. クールダウン
        if self._bar_idx - self._last_entry_bar_idx < p.cooldown_bars:
            return

        # 時間帯フィルタ (UTC 時)
        if p.block_hour_start >= 0:
            hour = (cur.time // 3600) % 24
            if p.block_hour_start <= hour < p.block_hour_end:
                return

        # ボラ regime フィルタ (絶対値: 高ボラ除外)
        if p.max_atr_pct > 0 and cur.close > 0:
            if atr_val / cur.close * 100.0 > p.max_atr_pct:
                return

        # ③相対ボラ regime: 現在 ATR が直近中央値の max_atr_ratio 倍超なら skip
        if p.max_atr_ratio > 0 and len(self._atr_hist) >= 50:
            srt = sorted(self._atr_hist)
            med = srt[len(srt) // 2]
            if med > 0 and atr_val > p.max_atr_ratio * med:
                return

        # ★ NEW: トレンド品質 = Kaufman 効率比(ER) on H1。クリーンなトレンドのみ押し目を取る。
        if p.min_h1_er > 0:
            h1c = [b.close for b in self.zz_h1.bars]
            if len(h1c) < p.er_period + 1:
                return
            seg = h1c[-(p.er_period + 1):]
            net = seg[-1] - seg[0]
            vol = sum(abs(seg[i] - seg[i - 1]) for i in range(1, len(seg)))
            if vol <= 0:
                return
            er = abs(net) / vol
            if er < p.min_h1_er:
                return
            # 進行方向に効率的か (up は net>0, down は net<0)
            if p.er_directional:
                if major_dir == "up" and net <= 0:
                    return
                if major_dir == "down" and net >= 0:
                    return

        # ①コンフルエンス: 押し目の安値/高値が直近 H1 スイング(支持/抵抗)に到達しているか
        if p.require_h1_confluence:
            tol = p.confluence_atr * atr_val
            lb = bars[-p.structure_lookback_bars:]
            if major_dir == "up":
                recent_low = min(b.low for b in lb)
                h1_lows = [q.price for q in self.zz_h1.pivots if q.kind == "low"]
                if not any(abs(recent_low - hl) <= tol for hl in h1_lows[-6:]):
                    return
            else:
                recent_high = max(b.high for b in lb)
                h1_highs = [q.price for q in self.zz_h1.pivots if q.kind == "high"]
                if not any(abs(recent_high - hh) <= tol for hh in h1_highs[-6:]):
                    return

        price = cur.close

        # 5. ★ v2: H4/H1 トレンドラインブレイク判定
        if p.skip_on_trendline_break:
            h4_idx = len(self.zz_h4.bars) - 1
            h1_idx = len(self.zz_h1.bars) - 1
            h4_d = _trendline_dist_atr(self.zz_h4.pivots, h4_idx, price, atr_val, h4_trend)
            h1_d = _trendline_dist_atr(self.zz_h1.pivots, h1_idx, price, atr_val, h1_trend)
            if major_dir == "up":
                # 上昇支持線を下抜けていたら skip
                if (h4_d is not None and h4_d < 0) or (h1_d is not None and h1_d < 0):
                    return
            else:
                # 下降抵抗線を上抜けていたら skip
                if (h4_d is not None and h4_d > 0) or (h1_d is not None and h1_d > 0):
                    return

        # 6. ★ v3: D1/W1 重要ラインがエントリー方向に近接 → skip
        if p.skip_on_daily_line:
            # H4 bars から D1 (6本) / W1 (30本) を集計
            h4_bars_all = self.zz_h4.bars
            d1_bars = _aggregate(h4_bars_all, 14400, 86400)
            w1_bars = _aggregate(h4_bars_all, 14400, 604800)
            # 簡易: それぞれの直近 30 本程度から pivot を抽出 (=ZigZag を流すと過剰なので、シンプルな swing high/low で)
            def _swings(bars: list[Bar], k: int = 3) -> tuple[list[float], list[float]]:
                his: list[float] = []
                los: list[float] = []
                for i in range(k, len(bars) - k):
                    h = bars[i].high
                    if all(bars[j].high < h for j in range(i - k, i)) and \
                       all(bars[j].high < h for j in range(i + 1, i + k + 1)):
                        his.append(h)
                    l = bars[i].low
                    if all(bars[j].low > l for j in range(i - k, i)) and \
                       all(bars[j].low > l for j in range(i + 1, i + k + 1)):
                        los.append(l)
                return his, los
            d_h, d_l = _swings(d1_bars, k=3)
            w_h, w_l = _swings(w1_bars, k=2)
            threshold = p.daily_wall_max_atr * atr_val
            if major_dir == "up":
                # 進行方向 = 上、price から上に近い D1/W1 high が壁
                walls_above = [x for x in (d_h + w_h) if x > price]
                if walls_above:
                    nearest = min(walls_above) - price
                    if nearest < threshold:
                        return
            else:
                walls_below = [x for x in (d_l + w_l) if x < price]
                if walls_below:
                    nearest = price - max(walls_below)
                    if nearest < threshold:
                        return

        # === SL/TP 計算 ===
        if major_dir == "up":
            # M15 直近の Z1 安値を取得
            m15_lows = [p_.price for p_ in self.zz_m15.pivots if p_.kind == "low"]
            if not m15_lows:
                return
            anchor = m15_lows[-1]
            # ダウ的に確定したスイング安値を SL アンカーに (未確定の極値を避ける)
            if p.sl_dow_confirmed_swing:
                cs = _last_confirmed_swing(self.zz_m15.pivots, "low")
                if cs is not None:
                    anchor = cs.price
            # 構造インタクト確認: 直近で higher-low を割っていたら転換 → skip
            if p.require_structure_intact:
                if p.structure_use_close:
                    # M15 終値ベース: 直近 K 本の確定 M15 が anchor を終値割りしたら skip
                    if any(b.close < anchor for b in self.zz_m15.bars[-p.structure_lookback_m15:]):
                        return
                else:
                    recent_low = min(b.low for b in bars[-p.structure_lookback_bars:])
                    if recent_low < anchor:
                        return
            # SL アンカー: 直近の実際の安値(ヒゲ含む)も考慮して、その下に置く
            if p.sl_anchor_recent_low:
                recent_low = min(b.low for b in bars[-p.structure_lookback_bars:])
                anchor = min(anchor, recent_low)
            sl = anchor - p.sl_buffer_atr * atr_val   # ノイズ回避バッファ
            sl_dist = price - sl
            if sl_dist <= 0:
                return
            tp = price + sl_dist * p.tp_rr  # RR = tp_rr (既定 1:1)
            # TP が直近 M15 高値を超えるなら skip (TP が前回スイング内に収まる設定のみ)
            if p.require_tp_within_swing:
                m15_highs_p = [q.price for q in self.zz_m15.pivots if q.kind == "high"]
                if m15_highs_p and tp > m15_highs_p[-1]:
                    return
            # room_R フィルタ: 直近 M15 高値までの余地/SL が大きすぎる (SL タイト) なら skip
            if p.room_R_max > 0:
                m15_highs_r = [q.price for q in self.zz_m15.pivots if q.kind == "high"]
                if m15_highs_r and (m15_highs_r[-1] - price) / sl_dist >= p.room_R_max:
                    return
            # sl_dist の妥当性
            if sl_dist < p.min_sl_dist_atr * atr_val:
                return  # 近すぎ (スプレッド吸われる)
            if sl_dist > p.max_sl_dist_atr * atr_val:
                return  # 遠すぎ
            # 絶対最小SL (pips): タイトSL=コスト負け層を除外
            if p.min_sl_dist_pips > 0:
                sym = (self.symbol or "").upper()
                pip = 0.01 if "JPY" in sym else (0.1 if "XAU" in sym else 0.0001)
                if sl_dist < p.min_sl_dist_pips * pip:
                    return
            vol = self._risk_lot(ctx, sl_dist)
            if vol <= 0:
                return
            ctx.buy(vol, sl=sl, tp=tp)
            ctx.log(
                f"[entry-mtf] LONG {self.symbol} price={price:.5f} "
                f"sl={sl:.5f} tp={tp:.5f} sl_dist={sl_dist:.5f} "
                f"atr={atr_val:.5f} vol={vol:.3f}"
            )
            self._attach_meta(ctx, price, sl_dist)
        else:
            m15_highs = [p_.price for p_ in self.zz_m15.pivots if p_.kind == "high"]
            if not m15_highs:
                return
            anchor = m15_highs[-1]
            # ダウ的に確定したスイング高値を SL アンカーに (未確定の極値を避ける)
            if p.sl_dow_confirmed_swing:
                cs = _last_confirmed_swing(self.zz_m15.pivots, "high")
                if cs is not None:
                    anchor = cs.price
            # 構造インタクト確認: 直近で lower-high を上抜けていたら転換 → skip
            if p.require_structure_intact:
                if p.structure_use_close:
                    if any(b.close > anchor for b in self.zz_m15.bars[-p.structure_lookback_m15:]):
                        return
                else:
                    recent_high = max(b.high for b in bars[-p.structure_lookback_bars:])
                    if recent_high > anchor:
                        return
            if p.sl_anchor_recent_low:
                recent_high = max(b.high for b in bars[-p.structure_lookback_bars:])
                anchor = max(anchor, recent_high)
            sl = anchor + p.sl_buffer_atr * atr_val
            sl_dist = sl - price
            if sl_dist <= 0:
                return
            tp = price - sl_dist * p.tp_rr
            # TP が直近 M15 安値を下回るなら skip (TP が前回スイング内に収まる設定のみ)
            if p.require_tp_within_swing:
                m15_lows_p = [q.price for q in self.zz_m15.pivots if q.kind == "low"]
                if m15_lows_p and tp < m15_lows_p[-1]:
                    return
            # room_R フィルタ: 直近 M15 安値までの余地/SL が大きすぎる (SL タイト) なら skip
            if p.room_R_max > 0:
                m15_lows_r = [q.price for q in self.zz_m15.pivots if q.kind == "low"]
                if m15_lows_r and (price - m15_lows_r[-1]) / sl_dist >= p.room_R_max:
                    return
            if sl_dist < p.min_sl_dist_atr * atr_val:
                return
            if sl_dist > p.max_sl_dist_atr * atr_val:
                return
            vol = self._risk_lot(ctx, sl_dist)
            if vol <= 0:
                return
            ctx.sell(vol, sl=sl, tp=tp)
            ctx.log(
                f"[entry-mtf] SHORT {self.symbol} price={price:.5f} "
                f"sl={sl:.5f} tp={tp:.5f} sl_dist={sl_dist:.5f} "
                f"atr={atr_val:.5f} vol={vol:.3f}"
            )
            self._attach_meta(ctx, price, sl_dist)
        self._last_entry_bar_idx = self._bar_idx

    def _try_htf_entry(self, ctx, cur, atr_val, h4_trend, h1_trend) -> None:
        """タスク2: 1 つ上の時間足。H4=H1 一致 + M15 トレンド転換でエントリー、SL は H1 構造。

        room_R / min-max_sl_dist は M5 基準で tuned のため HTF では適用しない (スケールが違う)。
        コア論理 (整合 + M15 押し目 + M15 転換 + cooldown + 時間帯) に絞る。
        """
        p = self.p
        if not self._m15_closed_this_bar:   # M15 転換の判定は M15 close 時のみ
            return
        if h4_trend is None or h1_trend is None or h4_trend != h1_trend:
            return
        major_dir = h4_trend
        hist = self._m15_trend_history
        if len(hist) < 2:
            return
        opp = _opposite(major_dir)
        if not any(t == opp for t in hist[-p.pullback_lookback_bars:]):
            return
        if hist[-1] != major_dir or hist[-2] == major_dir:   # 今まさに M15 が major へ転換
            return
        if self._bar_idx - self._last_entry_bar_idx < p.cooldown_bars:
            return
        if p.block_hour_start >= 0:
            hour = (cur.time // 3600) % 24
            if p.block_hour_start <= hour < p.block_hour_end:
                return
        price = cur.close
        if major_dir == "up":
            h1_lows = [q.price for q in self.zz_h1.pivots if q.kind == "low"]
            if not h1_lows:
                return
            sl = h1_lows[-1]
            sl_dist = price - sl
            if sl_dist <= 0:
                return
            tp = price + sl_dist * p.tp_rr
            vol = self._risk_lot(ctx, sl_dist)
            if vol <= 0:
                return
            ctx.buy(vol, sl=sl, tp=tp)
            self._attach_meta(ctx, price, sl_dist)
            self._last_entry_bar_idx = self._bar_idx
        else:
            h1_highs = [q.price for q in self.zz_h1.pivots if q.kind == "high"]
            if not h1_highs:
                return
            sl = h1_highs[-1]
            sl_dist = sl - price
            if sl_dist <= 0:
                return
            tp = price - sl_dist * p.tp_rr
            vol = self._risk_lot(ctx, sl_dist)
            if vol <= 0:
                return
            ctx.sell(vol, sl=sl, tp=tp)
            self._attach_meta(ctx, price, sl_dist)
            self._last_entry_bar_idx = self._bar_idx

    def _risk_lot(self, ctx: Context, sl_dist: float) -> float:
        balance = ctx.account_balance()
        if balance <= 0:
            return 0.0
        risk_amount = balance * self.p.risk_pct
        pips_at_risk = sl_dist / self.p.pip_size
        money_per_lot = pips_at_risk * self.p.pip_value
        if money_per_lot <= 0:
            return 0.0
        return risk_amount / money_per_lot

    def _attach_meta(self, ctx: Context, price: float, sl_dist: float) -> None:
        """EA / replay tool が trailing 等で使うメタ情報。"""
        if hasattr(ctx, "pending_commands") and ctx.pending_commands:
            cmd = ctx.pending_commands[-1]
            cmd["entry_price"] = float(price)
            cmd["sl_dist"] = float(sl_dist)
            # 戦略本体での trailing は使わない (= TP/SL hit に任せる)
            cmd["trail_activate_R"] = 0.0
            cmd["trail_stop_R"] = 0.0
