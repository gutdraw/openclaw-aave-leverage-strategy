from bot.pnl import compute_unrealised, compute_realised, should_exit


def _long(entry=2000.0, current=2000.0, supply=1.0, leverage=3.0, tp=5.0, sl=3.0):
    return compute_unrealised(entry, current, supply, borrow=0.0, leverage=leverage,
                               take_profit_pct=tp, stop_loss_pct=sl, direction="long")


def _short(entry=2000.0, current=2000.0, borrow=2.0, leverage=3.0, tp=5.0, sl=3.0):
    return compute_unrealised(entry, current, supply=0.0, borrow=borrow, leverage=leverage,
                               take_profit_pct=tp, stop_loss_pct=sl, direction="short")


# ── Long P&L ──────────────────────────────────────────────────────────────────

def test_long_breakeven():
    p = _long(entry=2000.0, current=2000.0)
    assert p.unrealised_usd == 0.0
    assert p.unrealised_pct == 0.0
    assert not p.is_tp and not p.is_sl


def test_long_take_profit():
    p = _long(entry=2000.0, current=2100.0)   # +5%
    assert p.is_tp
    assert not p.is_sl
    assert should_exit(p) == "take_profit"


def test_long_stop_loss():
    p = _long(entry=2000.0, current=1940.0)   # -3%
    assert p.is_sl
    assert should_exit(p) == "stop_loss"


def test_long_leverage_amplifies():
    # 5% move on 1 ETH @ 3x leverage → 3 * (2100-2000) * 1 = 300 USD
    p = _long(entry=2000.0, current=2100.0, supply=1.0, leverage=3.0)
    assert abs(p.unrealised_usd - 300.0) < 1e-6


def test_long_no_exit_in_range():
    p = _long(entry=2000.0, current=2050.0)   # +2.5%
    assert should_exit(p) is None


# ── Short P&L ─────────────────────────────────────────────────────────────────

def test_short_breakeven():
    p = _short(entry=2000.0, current=2000.0)
    assert p.unrealised_usd == 0.0
    assert p.unrealised_pct == 0.0


def test_short_take_profit_when_price_falls():
    p = _short(entry=2000.0, current=1900.0)   # -5% → profitable for short
    assert p.unrealised_pct == 5.0
    assert p.is_tp
    assert should_exit(p) == "take_profit"


def test_short_stop_loss_when_price_rises():
    p = _short(entry=2000.0, current=2060.0)   # +3% → loss for short
    assert p.unrealised_pct < 0
    assert p.is_sl
    assert should_exit(p) == "stop_loss"


def test_short_usd_pnl():
    # 2 ETH shorted at 2000, price drops to 1900 → 2 * 100 = $200 profit
    p = _short(entry=2000.0, current=1900.0, borrow=2.0)
    assert abs(p.unrealised_usd - 200.0) < 1e-6


def test_short_no_exit_in_range():
    p = _short(entry=2000.0, current=1960.0)   # -2% → within range
    assert should_exit(p) is None


# ── Realised P&L ──────────────────────────────────────────────────────────────

def test_realised_long():
    entry = {"entry_price": 2000.0, "supply": 1.0, "borrow": 0.0, "leverage": 3.0, "direction": "long"}
    r = compute_realised(entry, 2100.0)
    assert abs(r - 300.0) < 1e-6


def test_realised_short():
    entry = {"entry_price": 2000.0, "supply": 2000.0, "borrow": 2.0, "leverage": 3.0, "direction": "short"}
    r = compute_realised(entry, 1900.0)
    assert abs(r - 200.0) < 1e-6   # 2 ETH * $100 drop = $200


def test_realised_short_loss():
    entry = {"entry_price": 2000.0, "supply": 2000.0, "borrow": 2.0, "leverage": 3.0, "direction": "short"}
    r = compute_realised(entry, 2100.0)
    assert r < 0   # price rose, short loses


# ── Edge cases ────────────────────────────────────────────────────────────────

def test_zero_entry_price():
    p = compute_unrealised(0.0, 2000.0, 1.0, 0.0, 3.0, 5.0, 3.0)
    assert p.unrealised_pct == 0.0
