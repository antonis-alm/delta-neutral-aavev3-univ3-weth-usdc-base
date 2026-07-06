import logging
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from typing import TYPE_CHECKING, Any

from almanak.framework.data import BalanceUnavailableError, MarketSnapshotError, PriceUnavailableError
from almanak.framework.intents import Intent
from almanak.framework.market import HealthUnavailableError, MarketSnapshot
from almanak.framework.market.errors import IndicatorUnavailableError
from almanak.framework.strategies import IntentStrategy, almanak_strategy

if TYPE_CHECKING:
    from almanak.framework.teardown import TeardownMode, TeardownPositionSummary

logger = logging.getLogger(__name__)

_DATA_UNAVAILABLE_ERRORS = (
    PriceUnavailableError,
    BalanceUnavailableError,
    IndicatorUnavailableError,
    MarketSnapshotError,
    ValueError,
    KeyError,
)


@almanak_strategy(
    name="Delta-Neutral-AaveV3-UniV3-WETH-USDC-Base",
    description="Delta-neutral WETH/USDC strategy combining Aave V3 lending with Uniswap V3 LP on Base",
    version="1.0.0",
    author="Almanak",
    tags=["delta-neutral", "aave-v3", "uniswap-v3", "base"],
    supported_chains=["base"],
    supported_protocols=["aave_v3", "uniswap_v3"],
    intent_types=["SUPPLY", "BORROW", "REPAY", "WITHDRAW", "LP_OPEN", "LP_CLOSE", "COLLECT_FEES", "SWAP", "HOLD"],
    default_chain="base",
    quote_asset="USD",
)
class DeltaNeutralAaveV3UniV3WETHUSDCBaseStrategy(IntentStrategy):
    def supports_teardown(self) -> bool:
        return True

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)

        self.pool = str(self.get_config("pool", "WETH/USDC/500"))
        self.lp_protocol = str(self.get_config("lp_protocol", "uniswap_v3"))
        self.lending_protocol = str(self.get_config("lending_protocol", "aave_v3"))
        self.collateral_token = str(self.get_config("collateral_token", "WETH"))
        self.borrow_token = str(self.get_config("borrow_token", "USDC"))

        self.lending_leg_pct = Decimal(str(self.get_config("lending_leg_pct", "0.50")))
        self.lp_leg_pct = Decimal(str(self.get_config("lp_leg_pct", "0.50")))
        self.supply_collateral_pct_of_lending_leg = Decimal(
            str(self.get_config("supply_collateral_pct_of_lending_leg", "1.00"))
        )
        self.borrow_target_pct_of_max_ltv = Decimal(str(self.get_config("borrow_target_pct_of_max_ltv", "0.60")))
        self.borrow_hard_cap_pct_of_max_ltv = Decimal(
            str(self.get_config("borrow_hard_cap_pct_of_max_ltv", "0.72"))
        )
        self.recursive_loops = bool(self.get_config("recursive_loops", False))

        self.min_health_factor_for_borrow = Decimal(str(self.get_config("min_health_factor_for_borrow", "1.50")))
        self.health_factor_target = Decimal(str(self.get_config("health_factor_target", "1.60")))
        self.health_factor_floor = Decimal(str(self.get_config("health_factor_floor", "1.50")))
        self.emergency_threshold = Decimal(str(self.get_config("emergency_threshold", "1.30")))
        self.halt_new_actions_until_hf_above = Decimal(
            str(self.get_config("halt_new_actions_until_hf_above", "1.50"))
        )

        self.lp_base_width_pct = Decimal(str(self.get_config("lp_base_width_pct", "0.10")))
        self.vol_low_threshold_pct = Decimal(str(self.get_config("vol_low_threshold_pct", "0.03")))
        self.width_pct_below = Decimal(str(self.get_config("width_pct_below", "0.07")))
        self.vol_high_threshold_pct = Decimal(str(self.get_config("vol_high_threshold_pct", "0.06")))
        self.width_pct_above = Decimal(str(self.get_config("width_pct_above", "0.15")))
        self.atr_period = int(self.get_config("atr_period", 7))
        self.atr_timeframe = str(self.get_config("atr_timeframe", "1d"))

        self.min_rebalance_cooldown_minutes = int(self.get_config("min_rebalance_cooldown_minutes", 60))
        self.fee_collect_cooldown_minutes = int(self.get_config("fee_collect_cooldown_minutes", 240))
        self.min_fee_collect_usd = Decimal(str(self.get_config("min_fee_collect_usd", "0.10")))

        self.min_lp_position_usd = Decimal(str(self.get_config("min_lp_position_usd", "250")))
        self.min_supply_step_pct_of_wallet_weth = Decimal(
            str(self.get_config("min_supply_step_pct_of_wallet_weth", "0.05"))
        )
        self.min_borrow_step_pct_of_debt_target = Decimal(
            str(self.get_config("min_borrow_step_pct_of_debt_target", "0.05"))
        )

        self.force_action = str(self.get_config("force_action", "") or "").strip().lower()
        self.force_position_id = self.get_config("force_position_id", None)

        self.idle_rsi_enabled = bool(self.get_config("idle_rsi_enabled", True))
        self.rsi_period = int(self.get_config("rsi_period", 14))
        self.rsi_timeframe = str(self.get_config("rsi_timeframe", "5m"))
        self.rsi_buy_cross_below = Decimal(str(self.get_config("rsi_buy_cross_below", "45")))
        self.rsi_sell_cross_above = Decimal(str(self.get_config("rsi_sell_cross_above", "55")))
        self.idle_trade_fraction = Decimal(str(self.get_config("idle_trade_fraction", "1.0")))
        self.idle_max_slippage = Decimal(str(self.get_config("idle_max_slippage", "0.005")))
        self.min_idle_trade_usd = Decimal(str(self.get_config("min_idle_trade_usd", "5")))
        self.base_token_decimals = int(self.get_config("base_token_decimals", 18))
        self.quote_token_decimals = int(self.get_config("quote_token_decimals", 6))

        self._weth_supplied = Decimal("0")
        self._usdc_borrowed = Decimal("0")
        self._last_hf: Decimal | None = None
        self._lp_position_id: str | None = None
        self._lp_range_lower: Decimal | None = None
        self._lp_range_upper: Decimal | None = None
        self._lp_weth_deployed_est = Decimal("0")
        self._lp_usdc_deployed_est = Decimal("0")
        self._pending_reopen = False
        self._halt_new_actions = False
        self._last_rebalance_at: datetime | None = None
        self._last_fee_collect_at: datetime | None = None
        self._rebalance_count = 0
        self._fees_collected_count = 0
        self._prev_rsi: Decimal | None = None
        self._last_rsi_signal: str | None = None

    def decide(self, market: MarketSnapshot) -> Intent | None:
        if self.force_action:
            return self._forced_intent(market)

        inputs = self._read_inputs(market)
        if not isinstance(inputs, dict):
            return inputs

        weth_price = inputs["weth_price"]
        usdc_price = inputs["usdc_price"]
        weth_balance = inputs["weth_balance"]
        usdc_balance = inputs["usdc_balance"]
        health = inputs["health"]
        hf = inputs["hf"]
        width_pct = inputs["width_pct"]
        current_rsi = inputs["rsi_value"]
        prev_rsi = self._prev_rsi
        if current_rsi is not None:
            self._prev_rsi = current_rsi

        if hf is not None:
            self._last_hf = hf

        if hf is not None and hf <= self.emergency_threshold:
            self._halt_new_actions = True
            if self._lp_position_id is not None:
                return self._lp_close_intent(self._lp_position_id)
            repay_amount = min(self._usdc_borrowed, Decimal(str(usdc_balance.balance)))
            if repay_amount > 0:
                return self._repay_intent(repay_amount)
            return Intent.hold(reason=f"emergency mode active hf={hf}")

        if self._halt_new_actions:
            if hf is None or hf < self.halt_new_actions_until_hf_above:
                return Intent.hold(reason="halted until health-factor recovers")
            self._halt_new_actions = False

        if hf is not None and hf < self.health_factor_floor and self._usdc_borrowed > 0:
            if self._lp_position_id is not None:
                return self._lp_close_intent(self._lp_position_id)
            repay_amount = min(self._usdc_borrowed, Decimal(str(usdc_balance.balance)))
            if repay_amount > 0:
                return self._repay_intent(repay_amount)
            return Intent.hold(reason="health-factor floor breached; waiting for repay liquidity")

        if self._weth_supplied <= 0:
            wallet_weth = Decimal(str(weth_balance.balance))
            supply_amount = wallet_weth * self.lending_leg_pct * self.supply_collateral_pct_of_lending_leg
            min_supply_step = wallet_weth * self.min_supply_step_pct_of_wallet_weth
            if supply_amount <= 0 or wallet_weth <= 0:
                return Intent.hold(reason="no WETH available to supply")
            if supply_amount < min_supply_step:
                return Intent.hold(reason="supply amount below minimum step")
            return self._supply_intent(supply_amount)

        if health is not None and hf is not None and hf >= self.min_health_factor_for_borrow:
            max_borrow_usd = Decimal(str(getattr(health, "max_borrow_usd", "0")))
            if max_borrow_usd > 0:
                target_usd = max_borrow_usd * self.borrow_target_pct_of_max_ltv
                hard_cap_usd = max_borrow_usd * self.borrow_hard_cap_pct_of_max_ltv
                target_amount = (target_usd / usdc_price) if usdc_price > 0 else Decimal("0")
                hard_cap_amount = (hard_cap_usd / usdc_price) if usdc_price > 0 else Decimal("0")
                target_amount = min(target_amount, hard_cap_amount)

                if self.recursive_loops:
                    borrow_delta = max(Decimal("0"), target_amount - self._usdc_borrowed)
                elif self._usdc_borrowed <= 0:
                    borrow_delta = target_amount
                else:
                    borrow_delta = Decimal("0")

                min_step = target_amount * self.min_borrow_step_pct_of_debt_target
                if borrow_delta > 0 and borrow_delta >= min_step:
                    return self._borrow_intent(borrow_delta)

        if self._lp_position_id is None:
            wallet_weth = Decimal(str(weth_balance.balance))
            wallet_usdc = Decimal(str(usdc_balance.balance))
            open_weth = wallet_weth * self.lp_leg_pct
            open_usdc = wallet_usdc * self.lp_leg_pct
            open_usd = (open_weth * weth_price) + (open_usdc * usdc_price)
            if open_weth <= 0 or open_usdc <= 0:
                return Intent.hold(reason="insufficient inventory for LP open")
            if open_usd < self.min_lp_position_usd:
                return Intent.hold(reason="LP position value below minimum")
            lower, upper = self._price_range(weth_price, width_pct)
            return self._lp_open_intent(open_weth, open_usdc, lower, upper)

        if self._lp_position_id is not None and self._lp_range_lower is not None and self._lp_range_upper is not None:
            out_of_range = weth_price < self._lp_range_lower or weth_price > self._lp_range_upper
            if out_of_range:
                if self._rebalance_cooldown_passed():
                    self._pending_reopen = True
                    return self._lp_close_intent(self._lp_position_id)
                return Intent.hold(reason="rebalance cooldown active")

        if self._lp_position_id is not None and self._fee_collect_cooldown_passed():
            estimated_fees_usd = self._estimate_claimable_fees_usd(market, weth_price=weth_price, usdc_price=usdc_price)
            if estimated_fees_usd is not None and estimated_fees_usd >= self.min_fee_collect_usd:
                return self._collect_fees_intent(self._lp_position_id)
            return Intent.hold(reason="LP rewards below claim threshold")

        idle_swap = self._idle_rsi_swap_intent(
            prev_rsi=prev_rsi,
            current_rsi=current_rsi,
            weth_balance=Decimal(str(weth_balance.balance)),
            usdc_balance=Decimal(str(usdc_balance.balance)),
            weth_price=weth_price,
            usdc_price=usdc_price,
        )
        if idle_swap is not None:
            return idle_swap

        return Intent.hold(reason="no action")

    def _read_inputs(self, market: MarketSnapshot) -> dict[str, Any] | Intent:
        try:
            weth_price = Decimal(str(market.price(self.collateral_token)))
            usdc_price = Decimal(str(market.price(self.borrow_token)))
        except _DATA_UNAVAILABLE_ERRORS as e:
            return Intent.hold(reason=f"price unavailable: {e}")

        try:
            weth_balance = market.balance(self.collateral_token)
            usdc_balance = market.balance(self.borrow_token)
        except _DATA_UNAVAILABLE_ERRORS as e:
            return Intent.hold(reason=f"balance unavailable: {e}")

        width_pct = self.lp_base_width_pct
        try:
            atr_data = market.atr(self.collateral_token, period=self.atr_period, timeframe=self.atr_timeframe)
            atr_value = Decimal(str(atr_data.value))
            if weth_price > 0:
                vol_pct = atr_value / weth_price
                if vol_pct <= self.vol_low_threshold_pct:
                    width_pct = self.width_pct_below
                elif vol_pct >= self.vol_high_threshold_pct:
                    width_pct = self.width_pct_above
        except _DATA_UNAVAILABLE_ERRORS:
            width_pct = self.lp_base_width_pct

        rsi_value: Decimal | None = None
        if self.idle_rsi_enabled:
            try:
                rsi = market.rsi(self.collateral_token, period=self.rsi_period, timeframe=self.rsi_timeframe)
                rsi_value = Decimal(str(rsi.value))
            except _DATA_UNAVAILABLE_ERRORS:
                rsi_value = None

        health = None
        hf = None
        needs_health = self._weth_supplied > 0 or self._usdc_borrowed > 0 or self._halt_new_actions
        if needs_health:
            try:
                health = market.position_health(protocol=self.lending_protocol, market_id=self.chain)
                hf = Decimal(str(health.health_factor))
            except HealthUnavailableError:
                hf = market.aave_health_factor(chain=self.chain)
                if hf is None:
                    return Intent.hold(reason="health data unavailable")
                hf = Decimal(str(hf))
            except _DATA_UNAVAILABLE_ERRORS:
                return Intent.hold(reason="health data unavailable")

        return {
            "weth_price": weth_price,
            "usdc_price": usdc_price,
            "weth_balance": weth_balance,
            "usdc_balance": usdc_balance,
            "health": health,
            "hf": hf,
            "width_pct": width_pct,
            "rsi_value": rsi_value,
        }

    def _idle_rsi_swap_intent(
        self,
        *,
        prev_rsi: Decimal | None,
        current_rsi: Decimal | None,
        weth_balance: Decimal,
        usdc_balance: Decimal,
        weth_price: Decimal,
        usdc_price: Decimal,
    ) -> Intent | None:
        if not self.idle_rsi_enabled or prev_rsi is None or current_rsi is None:
            return None

        buy_cross = prev_rsi >= self.rsi_buy_cross_below and current_rsi < self.rsi_buy_cross_below
        sell_cross = prev_rsi <= self.rsi_sell_cross_above and current_rsi > self.rsi_sell_cross_above

        if buy_cross:
            usdc_to_trade = (usdc_balance * self.idle_trade_fraction).quantize(
                Decimal(f"1e-{self.quote_token_decimals}"), rounding=ROUND_DOWN
            )
            if usdc_to_trade <= 0:
                return None

            trade_usd = usdc_to_trade * usdc_price
            if trade_usd < self.min_idle_trade_usd:
                return None

            self._last_rsi_signal = "buy"
            return Intent.swap(
                from_token=self.borrow_token,
                to_token=self.collateral_token,
                amount=usdc_to_trade,
                max_slippage=self.idle_max_slippage,
                protocol=self.lp_protocol,
                chain=self.chain,
            )

        if sell_cross:
            weth_to_trade = (weth_balance * self.idle_trade_fraction).quantize(
                Decimal(f"1e-{self.base_token_decimals}"), rounding=ROUND_DOWN
            )
            if weth_to_trade <= 0:
                return None

            trade_usd = weth_to_trade * weth_price
            if trade_usd < self.min_idle_trade_usd:
                return None

            self._last_rsi_signal = "sell"
            return Intent.swap(
                from_token=self.collateral_token,
                to_token=self.borrow_token,
                amount=weth_to_trade,
                max_slippage=self.idle_max_slippage,
                protocol=self.lp_protocol,
                chain=self.chain,
            )

        return None


    def _price_range(self, center_price: Decimal, width_pct: Decimal) -> tuple[Decimal, Decimal]:
        half = width_pct / Decimal("2")
        lower = center_price * (Decimal("1") - half)
        upper = center_price * (Decimal("1") + half)
        return lower, upper

    def _rebalance_cooldown_passed(self) -> bool:
        if self._last_rebalance_at is None:
            return True
        return datetime.now(UTC) - self._last_rebalance_at >= timedelta(minutes=self.min_rebalance_cooldown_minutes)

    def _fee_collect_cooldown_passed(self) -> bool:
        if self._last_fee_collect_at is None:
            return True
        return datetime.now(UTC) - self._last_fee_collect_at >= timedelta(minutes=self.fee_collect_cooldown_minutes)

    def _supply_intent(self, amount: Decimal) -> Intent:
        return Intent.supply(
            protocol=self.lending_protocol,
            token=self.collateral_token,
            amount=amount.quantize(Decimal("0.000000000000000001"), rounding=ROUND_DOWN),
            use_as_collateral=True,
            chain=self.chain,
        )

    def _borrow_intent(self, amount: Decimal) -> Intent:
        return Intent.borrow(
            protocol=self.lending_protocol,
            collateral_token=self.collateral_token,
            collateral_amount=Decimal("0"),
            borrow_token=self.borrow_token,
            borrow_amount=amount.quantize(Decimal("0.000001"), rounding=ROUND_DOWN),
            interest_rate_mode="variable",
            chain=self.chain,
        )

    def _repay_intent(self, amount: Decimal) -> Intent:
        repay_full = amount >= self._usdc_borrowed and self._usdc_borrowed > 0
        return Intent.repay(
            protocol=self.lending_protocol,
            token=self.borrow_token,
            amount=amount.quantize(Decimal("0.000001"), rounding=ROUND_DOWN),
            repay_full=repay_full,
            interest_rate_mode="variable",
            chain=self.chain,
        )

    def _withdraw_intent(self, amount: Decimal, withdraw_all: bool = False) -> Intent:
        return Intent.withdraw(
            protocol=self.lending_protocol,
            token=self.collateral_token,
            amount=amount.quantize(Decimal("0.000000000000000001"), rounding=ROUND_DOWN),
            withdraw_all=withdraw_all,
            chain=self.chain,
        )

    def _lp_open_intent(self, amount0: Decimal, amount1: Decimal, lower: Decimal, upper: Decimal) -> Intent:
        return Intent.lp_open(
            pool=self.pool,
            amount0=amount0.quantize(Decimal("0.000000000000000001"), rounding=ROUND_DOWN),
            amount1=amount1.quantize(Decimal("0.000001"), rounding=ROUND_DOWN),
            range_lower=lower,
            range_upper=upper,
            protocol=self.lp_protocol,
            chain=self.chain,
        )

    def _lp_close_intent(self, position_id: str) -> Intent:
        return Intent.lp_close(
            position_id=str(position_id),
            pool=self.pool,
            collect_fees=True,
            protocol=self.lp_protocol,
            chain=self.chain,
        )

    def _estimate_claimable_fees_usd(
        self,
        market: MarketSnapshot,
        *,
        weth_price: Decimal,
        usdc_price: Decimal,
    ) -> Decimal | None:
        if self._last_fee_collect_at is None:
            return Decimal("0")

        elapsed_seconds = Decimal(str((datetime.now(UTC) - self._last_fee_collect_at).total_seconds()))
        if elapsed_seconds <= 0:
            return Decimal("0")

        lp_notional_usd = (self._lp_weth_deployed_est * weth_price) + (self._lp_usdc_deployed_est * usdc_price)
        if lp_notional_usd <= 0:
            return Decimal("0")

        token0, token1, *_ = (self.pool.split("/") + ["", ""])
        try:
            analytics = market.best_pool(
                token0,
                token1,
                chain=self.chain,
                metric="fee_apr",
                protocols=[self.lp_protocol],
            )
            data = getattr(analytics, "data", analytics)
            fee_apr = Decimal(str(getattr(data, "fee_apr", "0")))
        except _DATA_UNAVAILABLE_ERRORS:
            return None

        if fee_apr <= 0:
            return Decimal("0")
        if fee_apr > 1:
            fee_apr = fee_apr / Decimal("100")

        yearly_seconds = Decimal("31536000")
        estimated_fees_usd = lp_notional_usd * fee_apr * (elapsed_seconds / yearly_seconds)
        return max(Decimal("0"), estimated_fees_usd)

    def _collect_fees_intent(self, position_id: str) -> Intent:
        return Intent.collect_fees(
            pool=self.pool,
            protocol=self.lp_protocol,
            chain=self.chain,
            protocol_params={"position_id": str(position_id)},
        )

    def _forced_intent(self, market: MarketSnapshot) -> Intent:
        action = self.force_action
        inputs = self._read_inputs(market)
        if isinstance(inputs, Intent):
            raise ValueError(f"force_action={action} requires market data")

        weth_price = inputs["weth_price"]
        weth_balance = Decimal(str(inputs["weth_balance"].balance))
        usdc_balance = Decimal(str(inputs["usdc_balance"].balance))
        width_pct = inputs["width_pct"]

        if action == "supply":
            amount = weth_balance * self.lending_leg_pct * self.supply_collateral_pct_of_lending_leg
            return self._supply_intent(max(amount, Decimal("0.0001")))
        if action == "borrow":
            health = inputs["health"]
            if health is None:
                raise ValueError("force_action=borrow requires health data")
            max_borrow_usd = Decimal(str(getattr(health, "max_borrow_usd", "0")))
            borrow_amount = (max_borrow_usd * self.borrow_target_pct_of_max_ltv) / inputs["usdc_price"]
            return self._borrow_intent(max(borrow_amount, Decimal("1")))
        if action == "lp_open":
            open_weth = max(weth_balance * self.lp_leg_pct, Decimal("0.0001"))
            open_usdc = max(usdc_balance * self.lp_leg_pct, Decimal("1"))
            lower, upper = self._price_range(weth_price, width_pct)
            return self._lp_open_intent(open_weth, open_usdc, lower, upper)
        if action == "collect_fees":
            position_id = self.force_position_id or self._lp_position_id
            if not position_id:
                raise ValueError("force_action=collect_fees requires force_position_id or open position")
            return self._collect_fees_intent(str(position_id))
        if action == "lp_close":
            position_id = self.force_position_id or self._lp_position_id
            if not position_id:
                raise ValueError("force_action=lp_close requires force_position_id or open position")
            return self._lp_close_intent(str(position_id))
        if action == "repay":
            amount = self._usdc_borrowed if self._usdc_borrowed > 0 else max(usdc_balance * Decimal("0.5"), Decimal("1"))
            return self._repay_intent(amount)
        if action == "withdraw":
            amount = self._weth_supplied if self._weth_supplied > 0 else max(weth_balance * Decimal("0.5"), Decimal("0.0001"))
            return self._withdraw_intent(amount, withdraw_all=self._weth_supplied > 0)

        raise ValueError(f"Unknown force_action: {action!r}")

    def on_intent_executed(self, intent: Any, success: bool, result: Any) -> None:
        if not success:
            return

        intent_type = getattr(intent, "intent_type", None)
        if intent_type is None:
            return
        intent_value = intent_type.value if hasattr(intent_type, "value") else str(intent_type)

        if intent_value == "SUPPLY":
            self._weth_supplied += Decimal(str(getattr(intent, "amount", "0")))
        elif intent_value == "BORROW":
            self._usdc_borrowed += Decimal(str(getattr(intent, "borrow_amount", "0")))
        elif intent_value == "REPAY":
            if getattr(intent, "repay_full", False):
                self._usdc_borrowed = Decimal("0")
            else:
                self._usdc_borrowed = max(
                    Decimal("0"), self._usdc_borrowed - Decimal(str(getattr(intent, "amount", "0")))
                )
        elif intent_value == "WITHDRAW":
            if getattr(intent, "withdraw_all", False):
                self._weth_supplied = Decimal("0")
            else:
                self._weth_supplied = max(Decimal("0"), self._weth_supplied - Decimal(str(getattr(intent, "amount", "0"))))
        elif intent_value == "LP_OPEN":
            pid = getattr(result, "position_id", None)
            self._lp_position_id = str(pid) if pid is not None else (self.force_position_id or self._lp_position_id)
            self._lp_range_lower = Decimal(str(getattr(intent, "range_lower", "0")))
            self._lp_range_upper = Decimal(str(getattr(intent, "range_upper", "0")))
            self._lp_weth_deployed_est = Decimal(str(getattr(intent, "amount0", "0")))
            self._lp_usdc_deployed_est = Decimal(str(getattr(intent, "amount1", "0")))
            self._pending_reopen = False
            self._last_fee_collect_at = datetime.now(UTC)
        elif intent_value == "LP_CLOSE":
            self._lp_position_id = None
            self._lp_range_lower = None
            self._lp_range_upper = None
            self._lp_weth_deployed_est = Decimal("0")
            self._lp_usdc_deployed_est = Decimal("0")
            self._last_rebalance_at = datetime.now(UTC)
            self._rebalance_count += 1
        elif intent_value == "COLLECT_FEES":
            self._last_fee_collect_at = datetime.now(UTC)
            self._fees_collected_count += 1

    def get_status(self) -> dict[str, Any]:
        return {
            "strategy": self.STRATEGY_NAME,
            "chain": self.chain,
            "weth_supplied": str(self._weth_supplied),
            "usdc_borrowed": str(self._usdc_borrowed),
            "health_factor": str(self._last_hf) if self._last_hf is not None else None,
            "lp_position_id": self._lp_position_id,
            "lp_range": [str(self._lp_range_lower), str(self._lp_range_upper)]
            if self._lp_range_lower is not None and self._lp_range_upper is not None
            else None,
            "rebalance_count": self._rebalance_count,
            "fees_collected_count": self._fees_collected_count,
            "halt_new_actions": self._halt_new_actions,
            "prev_rsi": str(self._prev_rsi) if self._prev_rsi is not None else None,
            "last_rsi_signal": self._last_rsi_signal,
        }

    def get_persistent_state(self) -> dict[str, Any]:
        return {
            "weth_supplied": str(self._weth_supplied),
            "usdc_borrowed": str(self._usdc_borrowed),
            "last_hf": str(self._last_hf) if self._last_hf is not None else None,
            "lp_position_id": self._lp_position_id,
            "lp_range_lower": str(self._lp_range_lower) if self._lp_range_lower is not None else None,
            "lp_range_upper": str(self._lp_range_upper) if self._lp_range_upper is not None else None,
            "lp_weth_deployed_est": str(self._lp_weth_deployed_est),
            "lp_usdc_deployed_est": str(self._lp_usdc_deployed_est),
            "pending_reopen": self._pending_reopen,
            "halt_new_actions": self._halt_new_actions,
            "last_rebalance_at": self._last_rebalance_at.isoformat() if self._last_rebalance_at else None,
            "last_fee_collect_at": self._last_fee_collect_at.isoformat() if self._last_fee_collect_at else None,
            "rebalance_count": self._rebalance_count,
            "fees_collected_count": self._fees_collected_count,
            "prev_rsi": str(self._prev_rsi) if self._prev_rsi is not None else None,
            "last_rsi_signal": self._last_rsi_signal,
        }

    def load_persistent_state(self, state: dict[str, Any]) -> None:
        self._weth_supplied = Decimal(str(state.get("weth_supplied", "0")))
        self._usdc_borrowed = Decimal(str(state.get("usdc_borrowed", "0")))

        last_hf = state.get("last_hf")
        self._last_hf = Decimal(str(last_hf)) if last_hf is not None else None

        self._lp_position_id = state.get("lp_position_id")

        low = state.get("lp_range_lower")
        high = state.get("lp_range_upper")
        self._lp_range_lower = Decimal(str(low)) if low is not None else None
        self._lp_range_upper = Decimal(str(high)) if high is not None else None

        self._lp_weth_deployed_est = Decimal(str(state.get("lp_weth_deployed_est", "0")))
        self._lp_usdc_deployed_est = Decimal(str(state.get("lp_usdc_deployed_est", "0")))
        self._pending_reopen = bool(state.get("pending_reopen", False))
        self._halt_new_actions = bool(state.get("halt_new_actions", False))

        rebalance_at = state.get("last_rebalance_at")
        fee_collect_at = state.get("last_fee_collect_at")
        self._last_rebalance_at = datetime.fromisoformat(rebalance_at) if rebalance_at else None
        self._last_fee_collect_at = datetime.fromisoformat(fee_collect_at) if fee_collect_at else None

        self._rebalance_count = int(state.get("rebalance_count", 0))
        self._fees_collected_count = int(state.get("fees_collected_count", 0))

        prev_rsi = state.get("prev_rsi")
        self._prev_rsi = Decimal(str(prev_rsi)) if prev_rsi is not None else None
        self._last_rsi_signal = state.get("last_rsi_signal")

    def get_open_positions(self) -> "TeardownPositionSummary":
        from almanak.framework.teardown import PositionInfo, PositionType, TeardownPositionSummary

        positions: list[PositionInfo] = []

        if self._lp_position_id is not None:
            positions.append(
                PositionInfo(
                    position_type=PositionType.LP,
                    position_id=self._lp_position_id,
                    chain=self.chain,
                    protocol=self.lp_protocol,
                    value_usd=Decimal("0"),
                    details={"pool": self.pool},
                )
            )

        if self._usdc_borrowed > 0:
            positions.append(
                PositionInfo(
                    position_type=PositionType.BORROW,
                    position_id=f"{self.lending_protocol}-borrow-{self.borrow_token}",
                    chain=self.chain,
                    protocol=self.lending_protocol,
                    value_usd=Decimal("0"),
                    details={"token": self.borrow_token, "amount": str(self._usdc_borrowed)},
                )
            )

        if self._weth_supplied > 0:
            positions.append(
                PositionInfo(
                    position_type=PositionType.SUPPLY,
                    position_id=f"{self.lending_protocol}-supply-{self.collateral_token}",
                    chain=self.chain,
                    protocol=self.lending_protocol,
                    value_usd=Decimal("0"),
                    details={"token": self.collateral_token, "amount": str(self._weth_supplied)},
                )
            )

        return TeardownPositionSummary(
            deployment_id=getattr(self, "deployment_id", self.STRATEGY_NAME),
            timestamp=datetime.now(UTC),
            positions=positions,
        )

    def generate_teardown_intents(self, mode: "TeardownMode", market: MarketSnapshot | None = None) -> list[Intent]:
        intents: list[Intent] = []

        if self._lp_position_id is not None:
            intents.append(self._lp_close_intent(self._lp_position_id))

        if self._usdc_borrowed > 0:
            intents.append(
                Intent.repay(
                    protocol=self.lending_protocol,
                    token=self.borrow_token,
                    repay_full=True,
                    interest_rate_mode="variable",
                    chain=self.chain,
                )
            )

        if self._weth_supplied > 0:
            intents.append(self._withdraw_intent(self._weth_supplied, withdraw_all=True))

        return intents
