import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from strategy import DeltaNeutralAaveV3UniV3WETHUSDCBaseStrategy


def _load_config() -> dict:
    with open(Path(__file__).parent.parent / "config.json") as f:
        return json.load(f)


@pytest.fixture
def base_config() -> dict:
    return _load_config()


@pytest.fixture
def strategy(base_config: dict) -> DeltaNeutralAaveV3UniV3WETHUSDCBaseStrategy:
    return DeltaNeutralAaveV3UniV3WETHUSDCBaseStrategy(
        config=base_config,
        chain="base",
        wallet_address="0x" + "1" * 40,
    )


def _market(
    *,
    weth_price: Decimal = Decimal("3000"),
    usdc_price: Decimal = Decimal("1"),
    weth_balance: Decimal = Decimal("2"),
    usdc_balance: Decimal = Decimal("6000"),
    health_factor: Decimal | None = None,
    max_borrow_usd: Decimal = Decimal("4000"),
    raise_health: bool = False,
    atr_value: Decimal = Decimal("90"),
    fee_apr: Decimal = Decimal("0.20"),
):
    market = MagicMock()

    price_map = {"WETH": weth_price, "USDC": usdc_price}

    def price(token: str):
        return price_map[token]

    def balance(token: str):
        if token == "WETH":
            return SimpleNamespace(symbol="WETH", balance=weth_balance, balance_usd=weth_balance * weth_price)
        return SimpleNamespace(symbol="USDC", balance=usdc_balance, balance_usd=usdc_balance * usdc_price)

    market.price.side_effect = price
    market.balance.side_effect = balance
    market.atr.return_value = SimpleNamespace(value=atr_value)

    if raise_health:
        market.position_health.side_effect = ValueError("health unavailable")
    elif health_factor is None:
        market.position_health.return_value = SimpleNamespace(
            health_factor=Decimal("1.8"), max_borrow_usd=max_borrow_usd
        )
    else:
        market.position_health.return_value = SimpleNamespace(
            health_factor=health_factor, max_borrow_usd=max_borrow_usd
        )

    market.aave_health_factor.return_value = health_factor
    market.best_pool.return_value = SimpleNamespace(data=SimpleNamespace(fee_apr=fee_apr))
    return market


def _intent_type(intent) -> str:
    return intent.intent_type.value if hasattr(intent.intent_type, "value") else str(intent.intent_type)


def test_bootstrap_supply_branch(strategy: DeltaNeutralAaveV3UniV3WETHUSDCBaseStrategy):
    market = _market()
    intent = strategy.decide(market)
    assert _intent_type(intent) == "SUPPLY"


def test_borrow_branch_after_supply(strategy: DeltaNeutralAaveV3UniV3WETHUSDCBaseStrategy):
    strategy._weth_supplied = Decimal("1")
    strategy._usdc_borrowed = Decimal("0")
    market = _market(health_factor=Decimal("1.7"), max_borrow_usd=Decimal("6000"))
    intent = strategy.decide(market)
    assert _intent_type(intent) == "BORROW"


def test_borrow_blocked_when_hf_below_min(strategy: DeltaNeutralAaveV3UniV3WETHUSDCBaseStrategy):
    strategy._weth_supplied = Decimal("1")
    market = _market(health_factor=Decimal("1.2"), max_borrow_usd=Decimal("6000"))
    intent = strategy.decide(market)
    assert _intent_type(intent) == "HOLD"


def test_lp_open_when_lending_leg_ready(strategy: DeltaNeutralAaveV3UniV3WETHUSDCBaseStrategy):
    strategy._weth_supplied = Decimal("1")
    strategy._usdc_borrowed = Decimal("1200")
    market = _market(health_factor=Decimal("1.8"), weth_balance=Decimal("0.8"), usdc_balance=Decimal("3000"))
    intent = strategy.decide(market)
    assert _intent_type(intent) == "LP_OPEN"


def test_emergency_closes_lp_first(strategy: DeltaNeutralAaveV3UniV3WETHUSDCBaseStrategy):
    strategy._weth_supplied = Decimal("1")
    strategy._usdc_borrowed = Decimal("1500")
    strategy._lp_position_id = "42"
    market = _market(health_factor=Decimal("1.2"))
    intent = strategy.decide(market)
    assert _intent_type(intent) == "LP_CLOSE"


def test_emergency_repays_after_lp_closed(strategy: DeltaNeutralAaveV3UniV3WETHUSDCBaseStrategy):
    strategy._weth_supplied = Decimal("1")
    strategy._usdc_borrowed = Decimal("1500")
    market = _market(health_factor=Decimal("1.2"), usdc_balance=Decimal("2000"))
    intent = strategy.decide(market)
    assert _intent_type(intent) == "REPAY"


def test_halt_new_actions_enforced(strategy: DeltaNeutralAaveV3UniV3WETHUSDCBaseStrategy):
    strategy._weth_supplied = Decimal("1")
    strategy._usdc_borrowed = Decimal("1200")
    strategy._halt_new_actions = True
    market = _market(health_factor=Decimal("1.4"))
    intent = strategy.decide(market)
    assert _intent_type(intent) == "HOLD"


def test_halt_recovery_clears_gate(strategy: DeltaNeutralAaveV3UniV3WETHUSDCBaseStrategy):
    strategy._weth_supplied = Decimal("1")
    strategy._usdc_borrowed = Decimal("1200")
    strategy._halt_new_actions = True
    market = _market(health_factor=Decimal("1.6"), weth_balance=Decimal("0.7"), usdc_balance=Decimal("2500"))
    intent = strategy.decide(market)
    assert strategy._halt_new_actions is False
    assert _intent_type(intent) in {"LP_OPEN", "HOLD", "COLLECT_FEES"}


def test_rebalance_trigger_closes_lp(strategy: DeltaNeutralAaveV3UniV3WETHUSDCBaseStrategy):
    strategy._weth_supplied = Decimal("2")
    strategy._usdc_borrowed = Decimal("1000")
    strategy._lp_position_id = "777"
    strategy._lp_weth_deployed_est = Decimal("1")
    strategy._last_rebalance_at = datetime.now(UTC) - timedelta(minutes=120)
    strategy._last_fee_collect_at = datetime.now(UTC)
    market = _market(health_factor=Decimal("1.8"))
    intent = strategy.decide(market)
    assert _intent_type(intent) == "LP_CLOSE"


def test_rebalance_cooldown_holds(strategy: DeltaNeutralAaveV3UniV3WETHUSDCBaseStrategy):
    strategy._weth_supplied = Decimal("2")
    strategy._usdc_borrowed = Decimal("1000")
    strategy._lp_position_id = "777"
    strategy._lp_weth_deployed_est = Decimal("1")
    strategy._last_rebalance_at = datetime.now(UTC)
    strategy._last_fee_collect_at = datetime.now(UTC)
    market = _market(health_factor=Decimal("1.8"))
    intent = strategy.decide(market)
    assert _intent_type(intent) == "HOLD"


def test_fee_collection_branch(strategy: DeltaNeutralAaveV3UniV3WETHUSDCBaseStrategy):
    strategy._weth_supplied = Decimal("1")
    strategy._usdc_borrowed = Decimal("1000")
    strategy._lp_position_id = "888"
    strategy._lp_weth_deployed_est = Decimal("0.98")
    strategy._last_fee_collect_at = datetime.now(UTC) - timedelta(minutes=600)
    strategy._last_rebalance_at = datetime.now(UTC)
    market = _market(health_factor=Decimal("1.9"), fee_apr=Decimal("0.20"))
    intent = strategy.decide(market)
    assert _intent_type(intent) == "LP_COLLECT_FEES"


def test_fee_collection_blocked_below_reward_threshold(strategy: DeltaNeutralAaveV3UniV3WETHUSDCBaseStrategy):
    strategy._weth_supplied = Decimal("1")
    strategy._usdc_borrowed = Decimal("1000")
    strategy._lp_position_id = "888"
    strategy._lp_weth_deployed_est = Decimal("0.98")
    strategy._last_fee_collect_at = datetime.now(UTC) - timedelta(minutes=600)
    strategy._last_rebalance_at = datetime.now(UTC)
    market = _market(health_factor=Decimal("1.9"), fee_apr=Decimal("0.001"))
    intent = strategy.decide(market)
    assert _intent_type(intent) == "HOLD"


def test_health_unavailable_fail_closed_with_open_debt(strategy: DeltaNeutralAaveV3UniV3WETHUSDCBaseStrategy):
    strategy._weth_supplied = Decimal("1")
    strategy._usdc_borrowed = Decimal("1000")
    market = _market(raise_health=True, health_factor=None)
    market.aave_health_factor.return_value = None
    intent = strategy.decide(market)
    assert _intent_type(intent) == "HOLD"


@pytest.mark.parametrize(
    "action,expected",
    [
        ("supply", "SUPPLY"),
        ("borrow", "BORROW"),
        ("lp_open", "LP_OPEN"),
        ("repay", "REPAY"),
        ("withdraw", "WITHDRAW"),
        ("collect_fees", "LP_COLLECT_FEES"),
        ("lp_close", "LP_CLOSE"),
    ],
)
def test_force_actions_emit_non_hold(
    base_config: dict,
    action: str,
    expected: str,
):
    cfg = dict(base_config)
    cfg["force_action"] = action
    cfg["force_position_id"] = "123"
    strat = DeltaNeutralAaveV3UniV3WETHUSDCBaseStrategy(
        config=cfg,
        chain="base",
        wallet_address="0x" + "1" * 40,
    )
    strat._usdc_borrowed = Decimal("1000")
    market = _market(health_factor=Decimal("1.7"))
    intent = strat.decide(market)
    assert _intent_type(intent) == expected


def test_teardown_order_lp_repay_withdraw(strategy: DeltaNeutralAaveV3UniV3WETHUSDCBaseStrategy):
    strategy._lp_position_id = "55"
    strategy._usdc_borrowed = Decimal("900")
    strategy._weth_supplied = Decimal("1.2")
    intents = strategy.generate_teardown_intents(mode=None)
    assert [_intent_type(i) for i in intents] == ["LP_CLOSE", "REPAY", "WITHDRAW"]


def test_persistent_state_round_trip(base_config: dict):
    strat = DeltaNeutralAaveV3UniV3WETHUSDCBaseStrategy(
        config=base_config,
        chain="base",
        wallet_address="0x" + "1" * 40,
    )
    strat._weth_supplied = Decimal("1.5")
    strat._usdc_borrowed = Decimal("700")
    strat._lp_position_id = "99"
    strat._lp_range_lower = Decimal("2800")
    strat._lp_range_upper = Decimal("3200")
    strat._pending_reopen = True
    state = strat.get_persistent_state()

    fresh = DeltaNeutralAaveV3UniV3WETHUSDCBaseStrategy(
        config=base_config,
        chain="base",
        wallet_address="0x" + "2" * 40,
    )
    fresh.load_persistent_state(state)

    assert fresh._weth_supplied == Decimal("1.5")
    assert fresh._usdc_borrowed == Decimal("700")
    assert fresh._lp_position_id == "99"
    assert fresh._lp_range_lower == Decimal("2800")
    assert fresh._lp_range_upper == Decimal("3200")
    assert fresh._pending_reopen is True
