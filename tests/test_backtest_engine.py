# =============================================================================
# tests/test_backtest_engine.py — Unit test untuk mesin backtest & tuning
# =============================================================================
# Mencakup: engine/backtest_core.py, engine/tuning_core.py, user/backtest.py,
# user/hypertune.py.
#
# ⚠️ ISOLASI: backtest_core memasang mock position_manager/execution/bot_monitor
# ke sys.modules SAAT DI-IMPORT. Supaya tidak merusak test lain (mis.
# test_position_manager yang import di dalam fixture), kita:
#   1. JANGAN import modul ber-mock di top-level (biar tidak kena saat collection).
#   2. Import di dalam fixture module-scoped, lalu RESTORE sys.modules saat teardown.
# =============================================================================
from __future__ import annotations

import sys
import types

import pytest

# Modul under test — di-set oleh fixture _isolate (jangan import di top-level).
core = None        # backtest_core
tuning = None      # tuning_core
btcli = None       # user/backtest.py
hypertune = None   # user/hypertune.py


@pytest.fixture(scope="module", autouse=True)
def _isolate():
    """Snapshot sys.modules, import modul ber-mock, restore saat selesai."""
    keys = ("execution", "position_manager", "bot_monitor")
    _ABSENT = object()
    saved = {k: sys.modules.get(k, _ABSENT) for k in keys}

    # try/finally: backtest_core memasang mock ke sys.modules saat import (sebelum
    # baris yang bisa gagal). Kalau import apa pun raise, RESTORE wajib tetap jalan
    # supaya mock tidak bocor & merusak test lain (mis. test_position_manager).
    try:
        global core, tuning, btcli, hypertune
        import backtest_core
        import tuning_core
        import backtest as _btcli_mod        # user/backtest.py
        import hypertune as _hyper_mod        # user/hypertune.py
        core, tuning, btcli, hypertune = backtest_core, tuning_core, _btcli_mod, _hyper_mod
        yield
    finally:
        # RESTORE — kembalikan modul asli supaya test lain dapat yang benar.
        for k, v in saved.items():
            if v is _ABSENT:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


# =============================================================================
# HELPERS
# =============================================================================
def make_candles(closes, highs=None, lows=None, interval=7200, base_ts=1700000000.0):
    out = []
    for i, c in enumerate(closes):
        out.append({
            "timeframe": "2h",
            "open_time": base_ts + i * interval,
            "open": c, "close": c,
            "high": highs[i] if highs else c + 0.5,
            "low":  lows[i] if lows else c - 0.5,
            "volume": 1000.0, "buy_volume": 550.0, "sell_volume": 450.0, "tick_count": 10,
        })
    return out


def make_strategy(actions=None, fn=None, **attrs):
    """Strategy-like object: punya TF, MIN_CANDLES, generate_signal."""
    sn = types.SimpleNamespace()
    sn.TF = attrs.pop("TF", "2h")
    sn.MIN_CANDLES = attrs.pop("MIN_CANDLES", 0)
    for k, v in attrs.items():
        setattr(sn, k, v)
    if fn is not None:
        sn.generate_signal = fn
    else:
        seq = list(actions or [])
        st = {"i": 0}

        def gen(data):
            i = st["i"]; st["i"] += 1
            a = seq[i] if i < len(seq) else "hold"
            return {"action": a, "reason": "t"}
        sn.generate_signal = gen
    return sn


def make_subbars(rows, open_time, interval=60):
    """Bangun sub-bar 1m (OHLC) untuk SATU candle htf. rows = [(o,h,l,c), ...]."""
    return [{"timeframe": "1m", "open_time": open_time + i * interval,
             "open": o, "high": h, "low": l, "close": c,
             "volume": 1.0, "buy_volume": 0.5, "sell_volume": 0.5, "tick_count": 1}
            for i, (o, h, l, c) in enumerate(rows)]


# =============================================================================
# backtest_core — helpers murni
# =============================================================================
class TestCoreHelpers:
    def test_build_data_dict(self):
        candles = make_candles([10, 11, 12])
        d = core.build_data_dict(candles[:-1], candles[-1], "2h")
        assert d["candles"]["2h"] == candles[:-1]
        assert d["current"]["2h"] == candles[-1]
        # Acuan bid/ask = close bar TERTUTUP terakhir (candles[-2]=11), bukan
        # current (12) → cegah look-ahead.
        assert d["best_bid"]["price"] == pytest.approx(11 * 0.9999)
        assert d["is_warmup"] is False

    def test_patch_params(self):
        sn = types.SimpleNamespace()
        core.patch_params(sn, ["A", "B"], (1, 2.5))
        assert sn.A == 1 and sn.B == 2.5

    def test_reset_bot_state(self):
        sn = types.SimpleNamespace(bot_state={"in_position": True, "side": "long",
                                              "entry_price": 9.0, "entry_time": 5.0})
        core.reset_bot_state(sn)
        assert sn.bot_state["in_position"] is False
        assert sn.bot_state["side"] == "none"

    def test_reset_bot_state_no_attr(self):
        sn = types.SimpleNamespace()        # tanpa bot_state → tidak error
        core.reset_bot_state(sn)

    def test_sync_strategy_state_long(self):
        sn = types.SimpleNamespace(bot_state={})
        core._MOCK_STATE = {"side": "long", "entry_price": 5.0, "qty": 1.0,
                            "entry_time": 111.0, "open_time": 111.0}
        core.sync_strategy_state(sn)
        assert sn.bot_state["in_position"] is True
        assert sn.bot_state["side"] == "long"
        assert sn.bot_state["entry_price"] == 5.0

    def test_sync_strategy_state_flat(self):
        sn = types.SimpleNamespace(bot_state={"in_position": True})
        core._MOCK_STATE = {"side": "none", "entry_price": 0.0, "qty": 0.0,
                            "entry_time": 0.0, "open_time": None}
        core.sync_strategy_state(sn)
        assert sn.bot_state["in_position"] is False
        assert sn.bot_state["side"] == "none"


# =============================================================================
# backtest_core — _extract_action / _warn_once
# =============================================================================
class TestExtractAction:
    def test_valid_action(self):
        assert core._extract_action({"action": "buy"}) == "buy"

    def test_unknown_action_to_hold(self):
        assert core._extract_action({"action": "teleport"}) == "hold"

    def test_non_dict_to_hold(self):
        assert core._extract_action("buy") == "hold"
        assert core._extract_action(["buy"]) == "hold"

    def test_missing_action_defaults_hold(self):
        assert core._extract_action({"reason": "x"}) == "hold"


# =============================================================================
# backtest_core — load_strategy
# =============================================================================
class TestLoadStrategy:
    def _write(self, tmp_path, name, body):
        f = tmp_path / f"{name}.py"
        f.write_text(body, encoding="utf-8")
        sys.path.insert(0, str(tmp_path))
        return name

    def _cleanup(self, tmp_path, name):
        sys.modules.pop(name, None)
        if str(tmp_path) in sys.path:
            sys.path.remove(str(tmp_path))

    def test_success_and_reload_and_tf(self, tmp_path):
        name = self._write(tmp_path, "tmp_strat_ok",
                           "TF='2h'\ndef generate_signal(data):\n    return {'action':'hold'}\n")
        try:
            mod = core.load_strategy(name, "1h")
            assert hasattr(mod, "generate_signal")
            assert mod.TF == "1h"                      # TF di-set ke timeframe
            mod2 = core.load_strategy(name, "5m")      # path reload
            assert mod2.TF == "5m"
        finally:
            self._cleanup(tmp_path, name)

    def test_missing_module_exits(self):
        with pytest.raises(SystemExit):
            core.load_strategy("zzz_definitely_missing_strat")

    def test_missing_generate_signal_exits(self, tmp_path):
        name = self._write(tmp_path, "tmp_strat_nogen", "X = 1\n")
        try:
            with pytest.raises(SystemExit):
                core.load_strategy(name)
        finally:
            self._cleanup(tmp_path, name)


# =============================================================================
# backtest_core — simulate_single
# =============================================================================
class TestSimulateSingle:
    @pytest.fixture(autouse=True)
    def _zero_costs(self, monkeypatch):
        """Nol-kan spread/slippage/funding → fokus LOGIKA harga (entry/exit/pnl
        bersih). Realisme biaya diuji terpisah di TestBacktestRealism."""
        monkeypatch.setattr(core, "SLIPPAGE", 0.0)
        monkeypatch.setattr(core, "HALF_SPREAD", 0.0)
        monkeypatch.setattr(core, "FUNDING_RATE", 0.0)

    def test_long_trade_pnl(self):
        candles = make_candles([10, 11, 12, 13, 14])
        strat = make_strategy(actions=["buy", "hold", "hold", "close", "hold"], MIN_CANDLES=0)
        res = core.simulate_single(strat, candles, "2h")

        assert len(res["trades"]) == 1
        tr = res["trades"][0]
        assert tr["side"] == "long"
        assert tr["entry"] == 10 and tr["exit"] == 13
        # notional=2*3=6, qty=0.6, gross=(13-10)*0.6=1.8, fee=6*0.0002*2=0.0024
        assert tr["pnl"] == pytest.approx(1.8 - 0.0024)
        assert res["final_balance"] == pytest.approx(1000 + 1.7976)
        assert len(res["equity_curve"]) == 5

    def test_short_trade_pnl(self):
        candles = make_candles([10, 9, 8])
        strat = make_strategy(actions=["sell", "hold", "close"], MIN_CANDLES=0)
        res = core.simulate_single(strat, candles, "2h")
        assert len(res["trades"]) == 1
        tr = res["trades"][0]
        assert tr["side"] == "short"
        # gross=(10-8)*0.6=1.2, fee 0.0024
        assert tr["pnl"] == pytest.approx(1.2 - 0.0024)

    def test_no_trades_all_hold(self):
        candles = make_candles([10, 10, 10])
        strat = make_strategy(actions=["hold", "hold", "hold"], MIN_CANDLES=0)
        res = core.simulate_single(strat, candles, "2h")
        assert res["trades"] == []
        assert res["final_balance"] == 1000

    def test_strategy_exception_treated_hold(self):
        def boom(data):
            raise RuntimeError("strategy bug")
        candles = make_candles([10, 11, 12])
        strat = make_strategy(fn=boom, MIN_CANDLES=0)
        res = core.simulate_single(strat, candles, "2h")   # tidak crash
        assert res["trades"] == []
        # Bukti loop benar-benar iterate tiap candle (jadi cabang except SUNGGUH diuji,
        # bukan lolos karena generate_signal tak pernah dipanggil).
        assert len(res["equity_curve"]) == len(candles)


# =============================================================================
# backtest_core — _get_risk_params + simulate_multi
# =============================================================================
class TestRiskParams:
    def test_reads_params(self):
        sn = types.SimpleNamespace(STOP_LOSS_PCT=0.02, TAKE_PROFIT_PCT=0.04, TIME_STOP_SEC=3600)
        sl, tp, ts = core._get_risk_params(sn)
        assert (sl, tp, ts) == (0.02, 0.04, 3600)

    def test_alternate_names(self):
        sn = types.SimpleNamespace(TARGET_SL_PCT=0.01, TARGET_TP_PCT=0.03)
        sl, tp, ts = core._get_risk_params(sn)
        assert sl == 0.01 and tp == 0.03 and ts == 0

    def test_missing_raises(self):
        sn = types.SimpleNamespace()
        with pytest.raises(RuntimeError):
            core._get_risk_params(sn)


class TestSimulateMulti:
    @pytest.fixture(autouse=True)
    def _zero_costs(self, monkeypatch):
        """Nol-kan biaya realisme → fokus LOGIKA SL/TP/TimeStop (harga bersih).
        Gap + slippage + funding diuji terpisah di TestBacktestRealism."""
        monkeypatch.setattr(core, "SLIPPAGE", 0.0)
        monkeypatch.setattr(core, "HALF_SPREAD", 0.0)
        monkeypatch.setattr(core, "FUNDING_RATE", 0.0)

    def test_long_take_profit(self):
        # entry @100, TP=+10% → 110. i1 high=111 → TP hit.
        candles = make_candles([100, 105, 105], highs=[100, 111, 105], lows=[99, 104, 104])
        strat = make_strategy(actions=["buy", "hold", "hold"], MIN_CANDLES=0,
                              STOP_LOSS_PCT=0.05, TAKE_PROFIT_PCT=0.10)
        res = core.simulate_multi(strat, candles, "2h")
        assert len(res["trades"]) == 1
        assert res["trades"][0]["reason"] == "TP"
        assert res["trades"][0]["exit"] == pytest.approx(110)

    def test_long_stop_loss(self):
        candles = make_candles([100, 100, 100], highs=[100, 101, 100], lows=[99, 94, 99])
        strat = make_strategy(actions=["buy", "hold", "hold"], MIN_CANDLES=0,
                              STOP_LOSS_PCT=0.05, TAKE_PROFIT_PCT=0.10)
        res = core.simulate_multi(strat, candles, "2h")
        assert len(res["trades"]) == 1
        assert res["trades"][0]["reason"] == "SL"

    def test_short_take_profit(self):
        # sell @100, TP=-10% → 90. i1 low=89 → TP.
        candles = make_candles([100, 95, 95], highs=[100, 101, 96], lows=[99, 89, 94])
        strat = make_strategy(actions=["sell", "hold", "hold"], MIN_CANDLES=0,
                              STOP_LOSS_PCT=0.05, TAKE_PROFIT_PCT=0.10)
        res = core.simulate_multi(strat, candles, "2h")
        assert len(res["trades"]) == 1
        assert res["trades"][0]["reason"] == "TP"
        assert res["trades"][0]["side"] == "short"

    def test_short_stop_loss(self):
        # sell @100, SL=+5% → 105. i1 high=106 → SL.
        candles = make_candles([100, 100, 100], highs=[100, 106, 100], lows=[99, 100, 99])
        strat = make_strategy(actions=["sell", "hold", "hold"], MIN_CANDLES=0,
                              STOP_LOSS_PCT=0.05, TAKE_PROFIT_PCT=0.10)
        res = core.simulate_multi(strat, candles, "2h")
        assert res["trades"][0]["reason"] == "SL"

    def test_time_stop(self):
        # SL/TP lebar (tak kena), TIME_STOP=7200 → exit di i1 (elapsed 7200).
        candles = make_candles([100, 101], highs=[100, 101.5], lows=[99.5, 100.5])
        strat = make_strategy(actions=["buy", "hold"], MIN_CANDLES=0,
                              STOP_LOSS_PCT=0.5, TAKE_PROFIT_PCT=0.5, TIME_STOP_SEC=7200)
        res = core.simulate_multi(strat, candles, "2h")
        assert len(res["trades"]) == 1
        assert res["trades"][0]["reason"] == "TimeStop"


# =============================================================================
# backtest_core — REALISME fill (spread+slippage, fill di open, stop gap, funding)
# =============================================================================
# Assertion sengaja DIREKSIONAL (>, <, ada/tidak), bukan angka persis — supaya
# tahan kalau konstanta slippage/funding diubah. Di kode LAMA semua MERAH:
#   - fill di close tanpa biaya → entry/exit tepat di harga acuan
#   - tak ada funding → tak ada key "funding"
#   - stop di-fill persis di sl_price (tak ada gap)
class TestBacktestRealism:
    def test_entry_and_exit_pay_spread_slippage(self):
        # Harga FLAT (open==close==100). Dengan spread+slippage: beli >100, jual <100.
        candles = make_candles([100, 100, 100, 100])
        strat = make_strategy(actions=["buy", "hold", "close", "hold"], MIN_CANDLES=0)
        tr = core.simulate_single(strat, candles, "2h")["trades"][0]
        assert tr["entry"] > 100, "beli harus bayar di atas acuan (ask+slippage)"
        assert tr["exit"] < 100, "jual harus terima di bawah acuan (bid−slippage)"

    def test_fill_uses_open_not_close_of_signal_bar(self):
        def mk(o, c, t):
            return {"timeframe": "2h", "open_time": t, "open": o, "close": c,
                    "high": max(o, c) + 1, "low": min(o, c) - 1, "volume": 1.0}
        # bar1 open=20, close=30 → buy harus fill ~OPEN(20), BUKAN close(30).
        candles = [mk(10, 10, 1700000000), mk(20, 30, 1700007200), mk(40, 40, 1700014400)]
        strat = make_strategy(actions=["hold", "buy", "close"], MIN_CANDLES=0)
        tr = core.simulate_single(strat, candles, "2h")["trades"][0]
        assert tr["entry"] < 25, f"fill harus dekat OPEN(20), bukan close(30); dapat {tr['entry']}"

    def test_funding_charged_for_held_position(self):
        candles = make_candles([100, 100, 100, 100, 100])   # flat, ditahan 3 bar
        strat = make_strategy(actions=["buy", "hold", "hold", "close", "hold"], MIN_CANDLES=0)
        tr = core.simulate_single(strat, candles, "2h")["trades"][0]
        assert "funding" in tr and tr["funding"] > 0, "posisi yang ditahan kena funding"

    def test_stop_fill_gaps_worse_than_sl_price(self):
        def mk(o, c, h, l, t):
            return {"timeframe": "2h", "open_time": t, "open": o, "close": c,
                    "high": h, "low": l, "volume": 1.0}
        # entry @100, SL=95. Bar berikut GAP turun (open=90) → fill harus < 95.
        candles = [mk(100, 100, 100.5, 99.5, 1700000000), mk(90, 92, 93, 88, 1700007200)]
        strat = make_strategy(actions=["buy", "hold"], MIN_CANDLES=0,
                              STOP_LOSS_PCT=0.05, TAKE_PROFIT_PCT=0.10)
        tr = core.simulate_multi(strat, candles, "2h")["trades"][0]
        assert tr["reason"] == "SL"
        assert tr["exit"] < 95, f"gap turun → fill di bawah SL 95, dapat {tr['exit']}"


# =============================================================================
# backtest_core — REALISME 1-MENIT (BACKTEST_REALISM): eksekusi intrabar
# =============================================================================
# Inti: di level candle tinggi, kalau range candle menyentuh SL DAN TP, mesin
# lama TIDAK tahu mana duluan → asumsi pesimis SL. Dengan sub-bar 1m, urutan
# NYATA terbaca. Test membuktikan: data 1m yang sama-candle bisa membalik hasil
# (SL↔TP) sesuai urutan sentuhan sebenarnya. Assertion direksional/perilaku.
class TestRealism1mDetail:
    @pytest.fixture(autouse=True)
    def _zero_costs(self, monkeypatch):
        monkeypatch.setattr(core, "SLIPPAGE", 0.0)
        monkeypatch.setattr(core, "HALF_SPREAD", 0.0)
        monkeypatch.setattr(core, "FUNDING_RATE", 0.0)

    def test_group_subbars_buckets_by_window(self):
        # 2 candle htf (window [0,7200) & [7200,14400)); 4 sub-bar dibagi 2-2.
        htf = make_candles([10, 10], base_ts=0.0, interval=7200)
        detail = (make_subbars([(10, 10, 10, 10)], 0.0)
                  + make_subbars([(10, 10, 10, 10)], 60.0)
                  + make_subbars([(10, 10, 10, 10)], 7200.0)
                  + make_subbars([(10, 10, 10, 10)], 7260.0))
        buckets = core._group_subbars(htf, detail, 7200)
        assert len(buckets) == 2
        assert len(buckets[0]) == 2 and len(buckets[1]) == 2
        assert buckets[0][0]["open_time"] == 0.0
        assert buckets[1][0]["open_time"] == 7200.0

    def test_multi_realism_tp_before_sl_changes_outcome(self):
        # candle1 range menyentuh SL(95) DAN TP(110). Mesin level-candle pesimis → SL.
        # Data 1m: harga NAIK ke TP dulu, baru turun ke SL → TP yang menang.
        candles = make_candles([100, 100], highs=[100, 112], lows=[100, 93])

        def strat():
            return make_strategy(actions=["buy", "hold"], MIN_CANDLES=0,
                                 STOP_LOSS_PCT=0.05, TAKE_PROFIT_PCT=0.10)

        res_norm = core.simulate_multi(strat(), candles, "2h")
        assert res_norm["trades"][0]["reason"] == "SL", "level-candle: asumsi pesimis SL"

        subs1 = make_subbars([(100, 112, 100, 111), (111, 111, 93, 95)],
                             open_time=candles[1]["open_time"])
        res_real = core.simulate_multi(strat(), candles, "2h", subbars=[[], subs1])
        assert res_real["trades"][0]["reason"] == "TP", "1m: TP tersentuh lebih dulu → TP"

    def test_multi_realism_sl_before_tp(self):
        candles = make_candles([100, 100], highs=[100, 112], lows=[100, 93])
        strat = make_strategy(actions=["buy", "hold"], MIN_CANDLES=0,
                              STOP_LOSS_PCT=0.05, TAKE_PROFIT_PCT=0.10)
        # 1m: turun ke SL dulu (sub-bar awal), baru naik ke TP → SL menang.
        subs1 = make_subbars([(100, 100, 93, 96), (96, 112, 96, 111)],
                             open_time=candles[1]["open_time"])
        res = core.simulate_multi(strat, candles, "2h", subbars=[[], subs1])
        assert res["trades"][0]["reason"] == "SL"

    def test_realism_empty_subbars_matches_level_candle(self):
        # Sub-bar kosong (data 1m bolong) → fallback ke logika level-candle (identik).
        candles = make_candles([100, 100], highs=[100, 112], lows=[100, 93])
        a = core.simulate_multi(make_strategy(actions=["buy", "hold"], MIN_CANDLES=0,
                                STOP_LOSS_PCT=0.05, TAKE_PROFIT_PCT=0.10), candles, "2h")
        b = core.simulate_multi(make_strategy(actions=["buy", "hold"], MIN_CANDLES=0,
                                STOP_LOSS_PCT=0.05, TAKE_PROFIT_PCT=0.10),
                                candles, "2h", subbars=[[], []])
        assert b["trades"][0]["reason"] == a["trades"][0]["reason"]
        assert b["trades"][0]["exit"] == pytest.approx(a["trades"][0]["exit"])

    def test_subbars_none_equals_default_multi(self):
        candles = make_candles([100, 100, 100], highs=[100, 111, 100], lows=[99, 104, 99])
        a = core.simulate_multi(make_strategy(actions=["buy", "hold", "hold"], MIN_CANDLES=0,
                                STOP_LOSS_PCT=0.05, TAKE_PROFIT_PCT=0.10), candles, "2h")
        b = core.simulate_multi(make_strategy(actions=["buy", "hold", "hold"], MIN_CANDLES=0,
                                STOP_LOSS_PCT=0.05, TAKE_PROFIT_PCT=0.10),
                                candles, "2h", subbars=None)
        assert a["trades"] == b["trades"]
        assert a["final_balance"] == b["final_balance"]

    def test_single_realism_closes_intrabar(self):
        # Strategi baca FORMING current candle: masuk long sekali, CLOSE saat close >= 105.
        candles = make_candles([100, 100], highs=[106, 100], lows=[100, 100])
        candles[0]["open"] = 100; candles[0]["close"] = 106
        candles[1]["open"] = 100; candles[1]["close"] = 100

        def make_fn():
            st = {"entered": False}

            def fn(data):
                cur = data["current"]["2h"]
                if not st["entered"]:
                    st["entered"] = True
                    return {"action": "buy", "reason": "enter"}
                if cur["close"] >= 105:
                    return {"action": "close", "reason": "target"}
                return {"action": "hold", "reason": "wait"}
            return fn

        # Level-candle: c1 close=100 (<105) → tak pernah close → 0 trade.
        res_norm = core.simulate_single(make_strategy(fn=make_fn(), MIN_CANDLES=0), candles, "2h")
        assert len(res_norm["trades"]) == 0

        # 1m: forming close c0 merangkak ke 106 → close DI DALAM c0 (intrabar).
        subs0 = make_subbars([(100, 100, 100, 100), (100, 103, 100, 103), (104, 106, 104, 106)],
                             open_time=candles[0]["open_time"])
        res_real = core.simulate_single(make_strategy(fn=make_fn(), MIN_CANDLES=0),
                                        candles, "2h", subbars=[subs0, []])
        assert len(res_real["trades"]) == 1
        assert res_real["trades"][0]["pnl"] > 0


# =============================================================================
# tuning_core — fungsi murni
# =============================================================================
class TestExpandParam:
    def test_float_range(self):
        assert tuning.expand_param("x", [0.0, 1.0, 3]) == [0.0, 0.5, 1.0]

    def test_int_range(self):
        assert tuning.expand_param("x", [10, 20, 3]) == [10, 15, 20]

    def test_int_range_dedup(self):
        # linspace(0,2,5)=[0,.5,1,1.5,2] → int round → [0,0,1,2,2] → dedup [0,1,2]
        assert tuning.expand_param("x", [0, 2, 5]) == [0, 1, 2]

    def test_n_equals_one(self):
        assert tuning.expand_param("x", [5, 10, 1]) == [5]

    def test_discrete_list(self):
        assert tuning.expand_param("x", [True, False]) == [True, False]
        assert tuning.expand_param("x", ["a", "b", "c"]) == ["a", "b", "c"]

    def test_len_not_three_is_discrete(self):
        assert tuning.expand_param("x", [1, 2]) == [1, 2]

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            tuning.expand_param("x", [])

    def test_non_list_raises(self):
        with pytest.raises(ValueError):
            tuning.expand_param("x", 5)


def test_expand_grid():
    names, combos = tuning.expand_grid({"A": [1, 2], "B": [3, 4]})
    assert names == ["A", "B"]
    assert len(combos) == 4
    assert (1, 3) in combos and (2, 4) in combos


class TestScoreMetrics:
    def _m(self, **kw):
        base = {"num_trades": 5, "pnl": 12.0, "max_dd_pct": 3.0, "sharpe": 1.5}
        base.update(kw)
        return base

    def test_too_few_trades(self):
        assert tuning.score_metrics(self._m(num_trades=2), "profit") == float("-inf")

    def test_profit_mode(self):
        assert tuning.score_metrics(self._m(), "profit") == 12.0

    def test_drawdown_mode(self):
        assert tuning.score_metrics(self._m(), "drawdown") == -3.0

    def test_balanced_mode(self):
        assert tuning.score_metrics(self._m(), "balanced") == 1.5

    def test_unknown_mode(self):
        assert tuning.score_metrics(self._m(), "weird") == 0.0


def test_progress_bar():
    s = tuning._progress_bar(5, 10)
    assert "5/10" in s and "[" in s and "50.0%" in s


def test_progress_bar_zero_total():
    s = tuning._progress_bar(0, 0)
    assert "0/0" in s


class TestFmt:
    def test_fmt_value_bool(self):
        assert tuning._fmt_value(True) == "True"

    def test_fmt_value_float_trim(self):
        assert tuning._fmt_value(0.500000) == "0.5"

    def test_fmt_value_other(self):
        assert tuning._fmt_value(7) == "7"

    def test_fmt_params(self):
        assert tuning._fmt_params({"A": 1, "B": 0.5}) == "A=1, B=0.5"


# =============================================================================
# tuning_core — validate_config
# =============================================================================
class TestValidateConfig:
    def _set_valid(self, monkeypatch):
        tc = tuning.tc
        monkeypatch.setattr(tc, "STRATEGY_FILE", "strategy", raising=False)
        monkeypatch.setattr(tc, "TIMEFRAME", "15m", raising=False)
        monkeypatch.setattr(tc, "DAYS_BACK", 5, raising=False)
        monkeypatch.setattr(tc, "OPTIM_MODE", "profit", raising=False)
        monkeypatch.setattr(tc, "POSITION_MODEL", "single", raising=False)
        monkeypatch.setattr(tc, "PARAMS", {"X": [1, 2]}, raising=False)

    def test_valid_passes(self, monkeypatch):
        self._set_valid(monkeypatch)
        tuning.validate_config()        # tidak raise

    def test_missing_field(self, monkeypatch):
        self._set_valid(monkeypatch)
        monkeypatch.delattr(tuning.tc, "PARAMS", raising=False)
        with pytest.raises(SystemExit):
            tuning.validate_config()

    def test_bad_timeframe(self, monkeypatch):
        self._set_valid(monkeypatch)
        monkeypatch.setattr(tuning.tc, "TIMEFRAME", "99z")
        with pytest.raises(SystemExit):
            tuning.validate_config()

    def test_bad_optim_mode(self, monkeypatch):
        self._set_valid(monkeypatch)
        monkeypatch.setattr(tuning.tc, "OPTIM_MODE", "nonsense")
        with pytest.raises(SystemExit):
            tuning.validate_config()

    def test_bad_position_model(self, monkeypatch):
        self._set_valid(monkeypatch)
        monkeypatch.setattr(tuning.tc, "POSITION_MODEL", "triple")
        with pytest.raises(SystemExit):
            tuning.validate_config()

    def test_bad_params(self, monkeypatch):
        self._set_valid(monkeypatch)
        monkeypatch.setattr(tuning.tc, "PARAMS", {})
        with pytest.raises(SystemExit):
            tuning.validate_config()

    def test_bad_days_back(self, monkeypatch):
        self._set_valid(monkeypatch)
        monkeypatch.setattr(tuning.tc, "DAYS_BACK", 0)
        with pytest.raises(SystemExit):
            tuning.validate_config()


# =============================================================================
# tuning_core — run_grid_search + print_top_results
# =============================================================================
def _alternating_strategy():
    """Buy saat flat, close saat in_position → banyak trade deterministik."""
    sn = types.SimpleNamespace(TF="2h", MIN_CANDLES=0, bot_state={})

    def gen(data):
        return {"action": "close" if sn.bot_state.get("in_position") else "buy", "reason": "x"}
    sn.generate_signal = gen
    return sn


def test_run_grid_search(monkeypatch):
    monkeypatch.setattr(tuning.tc, "POSITION_MODEL", "single", raising=False)
    monkeypatch.setattr(tuning.tc, "OPTIM_MODE", "profit", raising=False)
    monkeypatch.setattr(tuning.tc, "TIMEFRAME", "2h", raising=False)

    train = make_candles([10 + i for i in range(12)])
    test = make_candles([20 + i for i in range(12)])
    strat = _alternating_strategy()
    top = tuning.run_grid_search(strat, train, test, ["DUMMY"], [(1,), (2,)])
    # Harus benar-benar MENEMUKAN kandidat valid (bukan sekadar "list apa pun").
    assert 1 <= len(top) <= tuning.TOP_N_DISPLAY
    best = top[0]
    assert set(best) >= {"params", "in", "out", "score_in", "score_out"}
    # Kandidat valid wajib >=3 trade (syarat score_metrics) — bukti simulator jalan.
    assert best["in"]["num_trades"] >= 3
    assert "num_trades" in best["out"]          # out-of-sample benar-benar divalidasi


def test_print_top_results_empty(capsys):
    tuning.print_top_results([])
    assert "Tidak ada kombinasi" in capsys.readouterr().out


def test_print_top_results_nonempty(monkeypatch, capsys):
    monkeypatch.setattr(tuning.tc, "OPTIM_MODE", "profit", raising=False)
    monkeypatch.setattr(tuning.tc, "STRATEGY_FILE", "strategy", raising=False)
    m = tuning.compute_metrics(
        {"trades": [{"pnl": 5.0, "fee": 0.1}], "equity_curve": [1000, 1005],
         "final_balance": 1005.0}, 1000.0)
    top = [{"params": {"A": 1}, "in": m, "out": m, "score_in": 5.0, "score_out": 4.0}]
    tuning.print_top_results(top)
    out = capsys.readouterr().out
    assert "TOP 1 PARAMETERS" in out
    assert "A=1" in out


# =============================================================================
# tuning_core — run_tuning (orchestrator; deps berat di-monkeypatch)
# =============================================================================
class TestRunTuning:
    def _set_valid(self, monkeypatch):
        tc = tuning.tc
        monkeypatch.setattr(tc, "STRATEGY_FILE", "strategy", raising=False)
        monkeypatch.setattr(tc, "TIMEFRAME", "2h", raising=False)
        monkeypatch.setattr(tc, "DAYS_BACK", 5, raising=False)
        monkeypatch.setattr(tc, "OPTIM_MODE", "profit", raising=False)
        monkeypatch.setattr(tc, "POSITION_MODEL", "single", raising=False)
        monkeypatch.setattr(tc, "PARAMS", {"DUMMY": [1, 2]}, raising=False)

    def _recording_grid(self, captured):
        """Pengganti run_grid_search yang merekam ukuran split yang diterima."""
        def fake(strat, train, test, names, combos):
            captured["train"] = len(train)
            captured["test"] = len(test)
            captured["combos"] = len(combos)
            return []
        return fake

    def test_run_tuning_cache_hit(self, monkeypatch, capsys):
        self._set_valid(monkeypatch)
        candles = make_candles([10 + (i % 5) for i in range(120)])
        captured = {}
        monkeypatch.setattr(tuning, "_try_load_cache", lambda *a, **k: (candles, "hit: dummy"))
        monkeypatch.setattr(tuning, "load_strategy", lambda *a, **k: _alternating_strategy())
        monkeypatch.setattr(tuning, "run_grid_search", self._recording_grid(captured))
        tuning.run_tuning()
        out = capsys.readouterr().out
        assert "HYPERTUNING" in out
        # Bukti orchestration tuntas, bukan cuma banner:
        assert captured == {"train": 96, "test": 24, "combos": 2}   # split 80/20 dari 120
        assert "Tidak ada kombinasi" in out                          # print_top_results([]) jalan di akhir

    def test_run_tuning_cache_miss_download(self, monkeypatch, capsys):
        self._set_valid(monkeypatch)
        candles = make_candles([10 + (i % 5) for i in range(120)])
        captured = {}
        monkeypatch.setattr(tuning, "_try_load_cache", lambda *a, **k: (None, "miss"))
        monkeypatch.setattr(tuning, "download_candles", lambda *a, **k: candles)
        saved = {"n": 0}
        monkeypatch.setattr(tuning, "save_candles_csv",
                            lambda *a, **k: (saved.__setitem__("n", saved["n"] + 1), "data/fake.csv")[1])
        monkeypatch.setattr(tuning, "load_strategy", lambda *a, **k: _alternating_strategy())
        monkeypatch.setattr(tuning, "run_grid_search", self._recording_grid(captured))
        tuning.run_tuning()
        out = capsys.readouterr().out
        assert "Downloading" in out
        assert saved["n"] == 1                                       # data hasil download disimpan
        assert captured == {"train": 96, "test": 24, "combos": 2}    # split benar
        assert "Tidak ada kombinasi" in out

    def test_run_tuning_too_few_candles(self, monkeypatch):
        self._set_valid(monkeypatch)
        candles = make_candles([10, 11, 12])      # jauh di bawah minimum split
        monkeypatch.setattr(tuning, "_try_load_cache", lambda *a, **k: (candles, "hit"))
        monkeypatch.setattr(tuning, "load_strategy", lambda *a, **k: _alternating_strategy())
        with pytest.raises(SystemExit):
            tuning.run_tuning()


# =============================================================================
# user/backtest.py — main()
# =============================================================================
class TestBacktestCli:
    def _set_valid(self, monkeypatch):
        bc = btcli.bc
        monkeypatch.setattr(bc, "TIMEFRAME", "2h", raising=False)
        monkeypatch.setattr(bc, "POSITION_MODEL", "single", raising=False)
        monkeypatch.setattr(bc, "START", "2026-04-22", raising=False)
        monkeypatch.setattr(bc, "END", "end", raising=False)
        monkeypatch.setattr(bc, "STRATEGY_FILE", "strategy", raising=False)

    def test_main_runs(self, monkeypatch, capsys):
        self._set_valid(monkeypatch)
        candles = make_candles([10, 11, 12, 13, 14])
        monkeypatch.setattr(btcli, "download_candles_range_cached",
                            lambda *a, **k: (candles, "src: fake"))
        monkeypatch.setattr(btcli, "load_strategy",
                            lambda *a, **k: make_strategy(actions=["buy", "hold", "close"], MIN_CANDLES=0))
        btcli.main()
        out = capsys.readouterr().out
        assert "BACKTEST" in out and "Net PnL" in out

    def test_invalid_timeframe(self, monkeypatch):
        self._set_valid(monkeypatch)
        monkeypatch.setattr(btcli.bc, "TIMEFRAME", "99z")
        with pytest.raises(SystemExit):
            btcli.main()

    def test_invalid_position_model(self, monkeypatch):
        self._set_valid(monkeypatch)
        monkeypatch.setattr(btcli.bc, "POSITION_MODEL", "quad")
        with pytest.raises(SystemExit):
            btcli.main()

    def test_start_after_end(self, monkeypatch):
        self._set_valid(monkeypatch)
        monkeypatch.setattr(btcli.bc, "START", "2026-05-01")
        monkeypatch.setattr(btcli.bc, "END", "2026-04-01")
        with pytest.raises(SystemExit):
            btcli.main()

    def test_invalid_date(self, monkeypatch):
        self._set_valid(monkeypatch)
        monkeypatch.setattr(btcli.bc, "START", "not-a-date")
        with pytest.raises(SystemExit):
            btcli.main()

    def test_empty_data_exits(self, monkeypatch):
        self._set_valid(monkeypatch)
        monkeypatch.setattr(btcli, "download_candles_range_cached", lambda *a, **k: ([], "empty"))
        with pytest.raises(SystemExit):
            btcli.main()


# =============================================================================
# user/hypertune.py — main()
# =============================================================================
def test_hypertune_main(monkeypatch):
    called = {"n": 0}
    monkeypatch.setattr(hypertune, "run_tuning", lambda: called.__setitem__("n", 1))
    hypertune.main()
    assert called["n"] == 1
