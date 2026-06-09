"""
zigzag_line_break/strategy.py — ZigZag + ライン + MTF (Dow 理論).

spec.md を実装したもの。データ参照・発注はすべて `ctx` 経由。
FTO 固有 API は一切書かない。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from src.core.indicators import Pivot, ZigZagTracker, atr
from src.core.strategy_base import Bar, Context, Strategy, StrategyParams


# run_backtest が読む宣言
SAMPLE_PERIOD_SECONDS = 900  # M15
DEFAULT_CSV = "sample_M15.csv"
MTF_PERIODS = [3600, 14400]  # H1, H4
INITIAL_BALANCE = 10_000.0


@dataclass
class Params(StrategyParams):
    # ZigZag
    z1_depth: int = 25
    z1_dev_pips: float = 5.0
    z2_depth: int = 5
    z2_dev_pips: float = 4.0
    # ATR と許容
    atr_period: int = 14
    line_atr_k: float = 1.5   # 「ライン付近」の判定許容 (転換時のみ使用)
    wall_atr_k: float = 0.3   # 上位足壁前の判定許容 (狭い方が阻害が少ない)
    # SL / TP
    sl_buffer_k: float = 0.5
    tp_rr: float = 1.5        # TP が近いほど WR は上がる (v5: 2.0 → 1.5)
    min_rr: float = 1.0
    # 状態遷移
    lookback_bars: int = 50   # Z2 トリガを待つ最大本数
    # リスク
    risk_pct: float = 0.01
    # 換算 (FTO 側でも同じ値を使う)
    pip_size: float = 0.0001
    pip_value: float = 10.0
    # ★ v5: 時間帯フィルタ (UTC ベース)
    # True で月曜オープン早朝 / 金曜クローズ後 / 土日を skip
    block_low_liquidity: bool = True
    # ★ v5/v6: AI confidence 連動の position size
    # AI conf >= ai_conf_size_high のとき lot を ai_conf_size_mult 倍にする
    # 当初 v5 では mult=1.5 (高確信時に攻める) で実装したが、データを見ると
    # 高 conf 群の WR が 22% (vs 中 conf 群 45%) と「逆相関」していた。
    # 「分かりやすい setup ほど狩られる」現象。v6 では mult=0.5 にして
    # 高 conf 時こそ薄く張る contrarian sizing にした。
    ai_conf_size_high: float = 0.85
    ai_conf_size_mult: float = 0.5
    # ★ v5: EA 側 trailing close (SL Modify が使えない FTO 制約への代替)
    # トレード保有中、含み益が +trail_activate_R 達成後、含み益が trail_stop_R
    # まで戻ったら EA 側で CloseOrder する (= BE 保護)
    trail_enabled: bool = True
    trail_activate_R: float = 1.0
    trail_stop_R: float = 0.0


def _is_low_liquidity_time(bar_time_unix: int) -> bool:
    """UTC で「流動性が薄く誤シグナルが出やすい時間帯」を判定。

    - 月曜 0-7 UTC: アジア序盤 (週初オープンノイズ)
    - 金曜 18 UTC 以降: NY クローズ後、週末跨ぎリスク
    - 土曜終日: マーケット閉場 (FTO データには出ないはずだが念のため)
    - 日曜終日: 同上
    """
    from datetime import datetime, timezone
    try:
        dt = datetime.fromtimestamp(int(bar_time_unix), tz=timezone.utc)
    except Exception:  # noqa: BLE001
        return False
    wd = dt.weekday()  # 0=Mon ... 6=Sun
    hr = dt.hour
    if wd == 0 and hr < 7:
        return True
    if wd == 4 and hr >= 18:
        return True
    if wd in (5, 6):
        return True
    return False


def _dow_trend_from_pivots(pivots: list[Pivot]) -> Optional[str]:
    """直近 2 つの同方向ピボットから Dow トレンドを返す: "up" / "down" / None。

    上昇 = 直近の高値ピボット > その前の高値ピボット かつ
           直近の安値ピボット > その前の安値ピボット。判定がブレないよう
           最後の 4 ピボットを見て HH/HL or LL/LH を確認する。
    """
    if len(pivots) < 4:
        # ピボットが少ないときは直近の極の向きで暫定判定
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


class ZigZagLineBreakStrategy(Strategy):
    def __init__(self, params: Params) -> None:
        super().__init__(params)
        self.p: Params = params
        # ZigZag トラッカ
        dev_z1 = params.z1_dev_pips * params.pip_size
        dev_z2 = params.z2_dev_pips * params.pip_size
        self.z1 = ZigZagTracker(params.z1_depth, dev_z1)
        self.z2 = ZigZagTracker(params.z2_depth, dev_z2)
        self.z1_h1 = ZigZagTracker(params.z1_depth, dev_z1)
        self.z1_h4 = ZigZagTracker(params.z1_depth, dev_z1)
        # MTF dedup
        self._last_h1_time: int = -1
        self._last_h4_time: int = -1
        # Dow 状態管理
        self._dow_trend: Optional[str] = None
        # 「直前の Z1 反対側ピボット」を覚えておく (反転判定用)
        self._last_reversal_bar_idx: int = -10**9
        self._reversal_direction: Optional[str] = None  # "up" / "down"
        self._reversal_z1_pivot_price: Optional[float] = None
        self._bar_idx: int = -1
        # 最後に発火した転換方向 (連続再発火と pivot 由来の巻き戻り防止)
        self._last_fired_reversal_dir: Optional[str] = None

        # ★ AI フィルタ (オプション)
        # 設定すると、エントリ条件が揃った瞬間にこの関数で「本当に入るか」判断する。
        # シグネチャ: filter(direction, features_dict) -> bool
        #   direction: "up" or "down"
        #   features_dict: server/ai/features.py の build_zigzag_features の戻り値
        #   戻り値: True=enter, False=skip
        # None なら従来通り (フィルタなし、全エントリ実行)。
        self.entry_filter = None

    # ---- ヘルパ ----
    def _all_lines(self) -> list[tuple[float, str]]:
        """M15 / H1 / H4 すべての Z1 ピボット価格 + 出所タグ。"""
        out: list[tuple[float, str]] = []
        for p in self.z1.pivots:
            out.append((p.price, "m15"))
        for p in self.z1_h1.pivots:
            out.append((p.price, "h1"))
        for p in self.z1_h4.pivots:
            out.append((p.price, "h4"))
        return out

    def _upper_lines_above(self, price: float) -> list[float]:
        out = [p.price for p in self.z1_h1.pivots if p.price > price]
        out += [p.price for p in self.z1_h4.pivots if p.price > price]
        return out

    def _upper_lines_below(self, price: float) -> list[float]:
        out = [p.price for p in self.z1_h1.pivots if p.price < price]
        out += [p.price for p in self.z1_h4.pivots if p.price < price]
        return out

    def _near_any_line(self, price: float, atr_val: float) -> bool:
        tol = self.p.line_atr_k * atr_val
        for line, _ in self._all_lines():
            if abs(price - line) <= tol:
                return True
        return False

    def _wall_above(self, price: float, atr_val: float) -> bool:
        tol = self.p.wall_atr_k * atr_val
        for line in self._upper_lines_above(price):
            if (line - price) <= tol:
                return True
        return False

    def _wall_below(self, price: float, atr_val: float) -> bool:
        tol = self.p.wall_atr_k * atr_val
        for line in self._upper_lines_below(price):
            if (price - line) <= tol:
                return True
        return False

    def _next_line_above(self, price: float) -> Optional[float]:
        # 優先: H4 > H1 > M15
        for tracker in (self.z1_h4, self.z1_h1, self.z1):
            above = [p.price for p in tracker.pivots if p.price > price]
            if above:
                return min(above)
        return None

    def _next_line_below(self, price: float) -> Optional[float]:
        for tracker in (self.z1_h4, self.z1_h1, self.z1):
            below = [p.price for p in tracker.pivots if p.price < price]
            if below:
                return max(below)
        return None

    def _last_z1_low(self) -> Optional[Pivot]:
        for p in reversed(self.z1.pivots):
            if p.kind == "low":
                return p
        return None

    def _last_z1_high(self) -> Optional[Pivot]:
        for p in reversed(self.z1.pivots):
            if p.kind == "high":
                return p
        return None

    def _last_z2_high_after(self, bar_idx: int) -> Optional[Pivot]:
        for p in reversed(self.z2.pivots):
            if p.kind == "high" and p.index >= bar_idx:
                return p
        return None

    def _last_z2_low_after(self, bar_idx: int) -> Optional[Pivot]:
        for p in reversed(self.z2.pivots):
            if p.kind == "low" and p.index >= bar_idx:
                return p
        return None

    # ---- メイン ----
    def on_bar(self, ctx: Context) -> None:
        p = self.p
        self._bar_idx += 1
        bars = ctx.bars(p.atr_period + 5)
        if len(bars) < p.atr_period + 2:
            return
        cur = bars[-1]

        # ZigZag を更新 (M15)
        prev_z1_count = len(self.z1.pivots)
        self.z1.update(cur)
        self.z2.update(cur)
        if len(self.z1.pivots) > prev_z1_count:
            np = self.z1.pivots[-1]
            ctx.log(
                f"[z1] new {np.kind}@{np.price:.5f} bar_idx={np.index} "
                f"confirmed_at={self._bar_idx} total={len(self.z1.pivots)}"
            )

        # MTF を更新 (新しい H1 / H4 確定足が来たときだけ)
        try:
            h1_bars = ctx.bars_mtf(3600, 1)
        except (NotImplementedError, KeyError):
            h1_bars = []
        if h1_bars and h1_bars[-1].time > self._last_h1_time:
            self.z1_h1.update(h1_bars[-1])
            self._last_h1_time = h1_bars[-1].time
        try:
            h4_bars = ctx.bars_mtf(14400, 1)
        except (NotImplementedError, KeyError):
            h4_bars = []
        if h4_bars and h4_bars[-1].time > self._last_h4_time:
            self.z1_h4.update(h4_bars[-1])
            self._last_h4_time = h4_bars[-1].time

        # ATR を計算 (直近 atr_period+5 本のみで)
        highs = [b.high for b in bars]
        lows = [b.low for b in bars]
        closes = [b.close for b in bars]
        atr_line = atr(highs, lows, closes, p.atr_period)
        atr_val = atr_line[-1]
        if atr_val is None or atr_val <= 0:
            return

        # 現在の Dow トレンド状態を更新
        # 注: 一度転換を発火させたら、pivot 由来の更新は「同じ方向」の場合だけ採用する。
        # これをしないと、転換直後の足で旧 Z1 パターン (まだ HH/HL のまま) が
        # 巻き戻りを起こし、同方向の転換が毎足再発火してしまう。
        prev_trend = self._dow_trend
        new_trend = _dow_trend_from_pivots(self.z1.pivots)
        if new_trend is not None:
            if (
                self._last_fired_reversal_dir is None
                or new_trend == self._last_fired_reversal_dir
            ):
                self._dow_trend = new_trend

        price = cur.close
        pos = ctx.position()

        # --- ダウ転換検出 (確定足 close ベース) ---
        # 上昇中: 直近 Z1 安値を close で下抜け → 下降転換
        # 下降中: 直近 Z1 高値を close で上抜け → 上昇転換
        last_high = self._last_z1_high()
        last_low = self._last_z1_low()
        reversal_now: Optional[str] = None
        if prev_trend == "up" and last_low is not None and price < last_low.price:
            reversal_now = "down"
            self._reversal_z1_pivot_price = last_high.price if last_high else None
        elif prev_trend == "down" and last_high is not None and price > last_high.price:
            reversal_now = "up"
            self._reversal_z1_pivot_price = last_low.price if last_low else None

        # ★ 同方向の連続再発火を防ぐ (last_fired_reversal_dir と同じ方向は無視)
        if reversal_now is not None and reversal_now != self._last_fired_reversal_dir:
            self._dow_trend = reversal_now
            self._last_fired_reversal_dir = reversal_now
            # 保有中 & 反対方向の転換 → 即クローズ (ラインの有無に関わらず安全装置)
            if pos == "long" and reversal_now == "down":
                ctx.log(f"[reversal-close] LONG closed at bar={self._bar_idx}")
                ctx.close()
                return
            if pos == "short" and reversal_now == "up":
                ctx.log(f"[reversal-close] SHORT closed at bar={self._bar_idx}")
                ctx.close()
                return
            # ライン付近の Z1 転換だけがエントリ候補 (仕様: Step 1-2 の文脈)
            if self._near_any_line(price, atr_val):
                self._reversal_direction = reversal_now
                self._last_reversal_bar_idx = self._bar_idx
                ctx.log(
                    f"[reversal-armed] dir={reversal_now} bar={self._bar_idx} "
                    f"price={price:.5f}"
                )
            else:
                ctx.log(
                    f"[reversal-skip] dir={reversal_now} bar={self._bar_idx} "
                    f"price={price:.5f} (not near any line)"
                )

        # 既にポジションを持っていれば、ここから先の新規エントリ判定はしない
        if pos is not None:
            return

        # 転換が有効か (lookback_bars 以内か)
        if self._reversal_direction is None:
            return
        age = self._bar_idx - self._last_reversal_bar_idx
        if age > p.lookback_bars:
            self._reversal_direction = None  # 期限切れリセット
            return

        # 注: ライン付近判定は転換時に済んでいるため、エントリ判定では再チェックしない。

        if self._reversal_direction == "up":
            # ロング条件
            trigger = self._last_z2_high_after(self._last_reversal_bar_idx)
            if trigger is None:
                return
            if price <= trigger.price:
                return
            # 壁前禁止 (上に上位足ラインが ATR×wall_atr_k 以内なら避ける)
            if self._wall_above(price, atr_val):
                ctx.log(
                    f"[entry-skip] LONG blocked by wall_above bar={self._bar_idx} "
                    f"price={price:.5f}"
                )
                return
            # SL / TP
            if self._reversal_z1_pivot_price is None:
                return
            sl = self._reversal_z1_pivot_price - p.sl_buffer_k * atr_val
            sl_dist = price - sl
            if sl_dist <= 0:
                return
            line_tp = self._next_line_above(price)
            if line_tp is not None and (line_tp - price) >= p.min_rr * sl_dist:
                tp = line_tp
            else:
                tp = price + p.tp_rr * sl_dist
            vol = self._risk_lot(ctx, sl_dist)
            if vol <= 0:
                return
            # ★ v5: 時間帯フィルタ (流動性薄ゾーンはエントリ拒否)
            if p.block_low_liquidity and _is_low_liquidity_time(cur.time):
                ctx.log(f"[entry-block] LONG blocked by low-liquidity time bar={self._bar_idx}")
                self._reversal_direction = None
                return
            # ★ AI フィルタを通す
            if self.entry_filter is not None:
                try:
                    from server.ai.features import build_zigzag_features
                    features = build_zigzag_features(
                        self, atr_val, cur, "up",
                        recent_closes=closes, atr_line=atr_line,
                        recent_bars=bars,
                    )
                    if not self.entry_filter("up", features):
                        ctx.log(f"[ai-filter] LONG skipped by filter at bar={self._bar_idx}")
                        self._reversal_direction = None
                        return
                except Exception as e:  # noqa: BLE001
                    ctx.log(f"[ai-filter-error] {e}")
                    # 安全側: フィルタ失敗時は skip
                    self._reversal_direction = None
                    return
            # ★ v5: AI confidence 連動のサイズブースト
            ai_conf = 0.0
            last_ai = getattr(self, "_last_ai_decision", None)
            if last_ai and last_ai.get("decision") is not None:
                ai_conf = float(getattr(last_ai["decision"], "confidence", 0.0))
            size_mult = 1.0
            if ai_conf >= p.ai_conf_size_high:
                size_mult = p.ai_conf_size_mult
            vol_scaled = vol * size_mult
            ctx.buy(vol_scaled, sl=sl, tp=tp)
            # ★ v5: trailing close 用メタ情報を EA 向け command に添付
            if p.trail_enabled and hasattr(ctx, "pending_commands") and ctx.pending_commands:
                cmd = ctx.pending_commands[-1]
                cmd["entry_price"] = float(price)
                cmd["sl_dist"] = float(sl_dist)
                cmd["trail_activate_R"] = float(p.trail_activate_R)
                cmd["trail_stop_R"] = float(p.trail_stop_R)
            ctx.log(
                f"[entry] LONG price={price:.5f} sl={sl:.5f} tp={tp:.5f} "
                f"vol={vol_scaled:.3f} (x{size_mult}, conf={ai_conf:.2f}) bal={ctx.account_balance():.2f}"
            )
            # トリガ消費 (二重発火防止)
            self._reversal_direction = None

        elif self._reversal_direction == "down":
            trigger = self._last_z2_low_after(self._last_reversal_bar_idx)
            if trigger is None:
                return
            if price >= trigger.price:
                return
            if self._wall_below(price, atr_val):
                ctx.log(
                    f"[entry-skip] SHORT blocked by wall_below bar={self._bar_idx} "
                    f"price={price:.5f}"
                )
                return
            if self._reversal_z1_pivot_price is None:
                return
            sl = self._reversal_z1_pivot_price + p.sl_buffer_k * atr_val
            sl_dist = sl - price
            if sl_dist <= 0:
                return
            line_tp = self._next_line_below(price)
            if line_tp is not None and (price - line_tp) >= p.min_rr * sl_dist:
                tp = line_tp
            else:
                tp = price - p.tp_rr * sl_dist
            vol = self._risk_lot(ctx, sl_dist)
            if vol <= 0:
                return
            # ★ v5: 時間帯フィルタ
            if p.block_low_liquidity and _is_low_liquidity_time(cur.time):
                ctx.log(f"[entry-block] SHORT blocked by low-liquidity time bar={self._bar_idx}")
                self._reversal_direction = None
                return
            # ★ AI フィルタを通す
            if self.entry_filter is not None:
                try:
                    from server.ai.features import build_zigzag_features
                    features = build_zigzag_features(
                        self, atr_val, cur, "down",
                        recent_closes=closes, atr_line=atr_line,
                        recent_bars=bars,
                    )
                    if not self.entry_filter("down", features):
                        ctx.log(f"[ai-filter] SHORT skipped by filter at bar={self._bar_idx}")
                        self._reversal_direction = None
                        return
                except Exception as e:  # noqa: BLE001
                    ctx.log(f"[ai-filter-error] {e}")
                    self._reversal_direction = None
                    return
            # ★ v5: AI conf 連動サイズブースト
            ai_conf = 0.0
            last_ai = getattr(self, "_last_ai_decision", None)
            if last_ai and last_ai.get("decision") is not None:
                ai_conf = float(getattr(last_ai["decision"], "confidence", 0.0))
            size_mult = 1.0
            if ai_conf >= p.ai_conf_size_high:
                size_mult = p.ai_conf_size_mult
            vol_scaled = vol * size_mult
            ctx.sell(vol_scaled, sl=sl, tp=tp)
            # ★ v5: trailing close 用メタ情報
            if p.trail_enabled and hasattr(ctx, "pending_commands") and ctx.pending_commands:
                cmd = ctx.pending_commands[-1]
                cmd["entry_price"] = float(price)
                cmd["sl_dist"] = float(sl_dist)
                cmd["trail_activate_R"] = float(p.trail_activate_R)
                cmd["trail_stop_R"] = float(p.trail_stop_R)
            ctx.log(
                f"[entry] SHORT price={price:.5f} sl={sl:.5f} tp={tp:.5f} "
                f"vol={vol_scaled:.3f} (x{size_mult}, conf={ai_conf:.2f}) bal={ctx.account_balance():.2f}"
            )
            self._reversal_direction = None

        # 定期ステータスログ (500 本ごと)
        if self._bar_idx > 0 and self._bar_idx % 500 == 0:
            ctx.log(
                f"[stats] bar={self._bar_idx} "
                f"z1={len(self.z1.pivots)} z2={len(self.z2.pivots)} "
                f"z1h1={len(self.z1_h1.pivots)} z1h4={len(self.z1_h4.pivots)} "
                f"trend={self._dow_trend} armed={self._reversal_direction} "
                f"bal={ctx.account_balance():.0f}"
            )

    def _risk_lot(self, ctx: Context, sl_dist_price: float) -> float:
        """口座残高 × risk_pct を SL までの pip 距離で割ってロット算出。"""
        p = self.p
        bal = ctx.account_balance()
        if bal == float("inf") or bal <= 0:
            return p.volume  # フォールバック
        sl_pips = sl_dist_price / p.pip_size
        if sl_pips <= 0:
            return 0.0
        risk_money = bal * p.risk_pct
        # pnl = sl_pips * pip_value * volume が SL ヒット時の損失。
        # この損失を risk_money に合わせる volume を解く。
        vol = risk_money / (sl_pips * p.pip_value)
        # 最低 0.01 ロット / 最大 100 ロットで丸め
        vol = max(0.01, min(100.0, round(vol, 2)))
        return vol
