"""Delta-Neutral-AaveV3-UniV3-WETH-USDC-Base dashboard."""

from __future__ import annotations

from typing import Any

import streamlit as st

from almanak.framework.dashboard.templates import (
    get_uniswap_v3_config,
    prepare_lp_session_state,
    render_lp_dashboard,
)

_FEE_BPS_TO_PCT = {
    "100": "0.01%",
    "500": "0.05%",
    "3000": "0.30%",
    "10000": "1.00%",
}


def _format_fee_tier(raw_fee: str) -> str:
    fee = str(raw_fee).strip()
    if fee.endswith("%"):
        return fee
    return _FEE_BPS_TO_PCT.get(fee, "0.30%")


def _build_lp_config(strategy_config: dict[str, Any]):
    pool = str(strategy_config.get("pool", "WETH/USDC/3000"))
    parts = [part.strip() for part in pool.split("/") if part.strip()]

    token0 = parts[0] if len(parts) > 0 else "WETH"
    token1 = parts[1] if len(parts) > 1 else "USDC"
    fee_raw = parts[2] if len(parts) > 2 else strategy_config.get("fee_tier", "3000")

    return get_uniswap_v3_config(
        token0=token0,
        token1=token1,
        fee_tier=_format_fee_tier(str(fee_raw)),
        chain=str(strategy_config.get("chain", "base")),
    )


def render_custom_dashboard(
    deployment_id: str,
    strategy_config: dict[str, Any],
    api_client: Any,
    session_state: dict[str, Any],
) -> None:
    st.title("Delta-Neutral-AaveV3-UniV3-WETH-USDC-Base")

    config = _build_lp_config(strategy_config)
    lp_session_state = prepare_lp_session_state(
        api_client,
        session_state=session_state,
        config=config,
        deployment_id=deployment_id,
    )

    render_lp_dashboard(
        deployment_id,
        strategy_config,
        lp_session_state,
        config,
        api_client=api_client,
    )
