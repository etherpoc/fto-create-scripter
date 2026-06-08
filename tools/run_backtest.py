"""
run_backtest.py — strategies/<name>/strategy.py をローカル検証する。

Usage:
    python tools/run_backtest.py <strategy_name> [csv_path]

- CSV が無ければ tools/make_sample_data.py で自動生成する。
- strategies/<name>/strategy.py を動的 import し、
  Params (StrategyParams サブクラス) と Strategy (Strategy サブクラス) を
  自動で取り出してエンジンで回す。

既知の罠への対応:
- 動的 import 時、@dataclass の型解決のため
  module_from_spec で作ったモジュールを **exec_module する前に
  sys.modules[spec.name] = mod へ登録する**。
- Strategy サブクラスの抽出は `on_bar` を持ち、かつ `__module__` が当該モジュール
  のものに限定する（import 済みの基底 Strategy / StrategyParams を拾わない）。
"""

from __future__ import annotations

import importlib.util
import inspect
import sys
from pathlib import Path

# リポジトリ root を sys.path に通す
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.backtest.engine import Engine, EngineConfig, load_csv, print_stats  # noqa: E402
from src.core.strategy_base import Strategy, StrategyParams  # noqa: E402


def _load_strategy_module(name: str):
    strategy_path = ROOT / "strategies" / name / "strategy.py"
    if not strategy_path.exists():
        raise FileNotFoundError(f"strategy not found: {strategy_path}")

    mod_name = f"strategies.{name}.strategy"
    spec = importlib.util.spec_from_file_location(mod_name, str(strategy_path))
    if spec is None or spec.loader is None:
        raise ImportError(f"could not load spec for {strategy_path}")
    mod = importlib.util.module_from_spec(spec)
    # ★ dataclass の型解決のため exec_module の前に sys.modules へ登録する。
    sys.modules[spec.name] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception:
        # 失敗したら sys.modules から外す
        sys.modules.pop(spec.name, None)
        raise
    return mod


def _pick_params_and_strategy(mod) -> tuple[type, type]:
    params_cls = None
    strategy_cls = None
    for _, obj in inspect.getmembers(mod, inspect.isclass):
        # 当該モジュール定義のクラスのみ
        if getattr(obj, "__module__", None) != mod.__name__:
            continue
        if issubclass(obj, StrategyParams) and obj is not StrategyParams:
            params_cls = obj
        # Strategy サブクラスは on_bar を独自に持っているかで判定
        if (
            issubclass(obj, Strategy)
            and obj is not Strategy
            and "on_bar" in obj.__dict__
        ):
            strategy_cls = obj
    if params_cls is None:
        raise RuntimeError(
            "could not find a StrategyParams subclass in strategy module"
        )
    if strategy_cls is None:
        raise RuntimeError(
            "could not find a Strategy subclass (with on_bar) in strategy module"
        )
    return params_cls, strategy_cls


def _ensure_csv(csv_path: Path, period_seconds: int = 3600) -> None:
    if csv_path.exists():
        return
    print(f"[info] csv not found: {csv_path} - generating sample data...")
    sys.path.insert(0, str(ROOT / "tools"))
    import make_sample_data  # type: ignore

    make_sample_data.generate(str(csv_path), period_seconds=period_seconds)


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: python tools/run_backtest.py <strategy_name> [csv_path]")
        return 2
    name = argv[1]

    mod = _load_strategy_module(name)
    Params, StrategyCls = _pick_params_and_strategy(mod)

    # 戦略モジュールが要求するサンプルデータ周期 / CSV 名
    sample_period = int(getattr(mod, "SAMPLE_PERIOD_SECONDS", 3600))
    default_csv_name = getattr(mod, "DEFAULT_CSV", "sample_H1.csv")
    mtf_periods = list(getattr(mod, "MTF_PERIODS", []))
    initial_balance = float(getattr(mod, "INITIAL_BALANCE", 10_000.0))

    csv_path = (
        Path(argv[2]) if len(argv) >= 3 else ROOT / "data" / default_csv_name
    )
    _ensure_csv(csv_path, period_seconds=sample_period)

    bars = load_csv(str(csv_path))
    print(f"[info] loaded {len(bars)} bars from {csv_path}")
    if mtf_periods:
        print(f"[info] MTF periods (seconds): {mtf_periods}")

    config = EngineConfig(initial_balance=initial_balance)
    engine = Engine(bars, config=config, mtf_periods=mtf_periods)
    strategy = StrategyCls(Params())
    engine.run(strategy)

    print_stats(engine.stats())
    print(f"  final_balance: {engine.ctx.account_balance():.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
