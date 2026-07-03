import importlib
import sys
import types
from unittest.mock import MagicMock, patch


if "streamlit" not in sys.modules:
    sys.modules["streamlit"] = types.SimpleNamespace(title=lambda *args, **kwargs: None)

templates_module = types.ModuleType("almanak.framework.dashboard.templates")
templates_module.get_uniswap_v3_config = lambda **kwargs: types.SimpleNamespace(
    protocol="uniswap_v3",
    token0=kwargs["token0"],
    token1=kwargs["token1"],
    fee_tier=kwargs["fee_tier"],
    chain=kwargs["chain"],
)
templates_module.prepare_lp_session_state = lambda *args, **kwargs: {}
templates_module.render_lp_dashboard = lambda *args, **kwargs: None
sys.modules["almanak.framework.dashboard.templates"] = templates_module

ui = importlib.import_module("dashboard.ui")


def test_build_lp_config_from_pool_and_chain():
    strategy_config = {
        "pool": "WETH/USDC/500",
        "chain": "base",
    }

    config = ui._build_lp_config(strategy_config)

    assert config.protocol == "uniswap_v3"
    assert config.token0 == "WETH"
    assert config.token1 == "USDC"
    assert config.fee_tier == "0.05%"
    assert config.chain == "base"


def test_render_custom_dashboard_uses_lp_template():
    strategy_config = {"pool": "WETH/USDC/500", "chain": "base"}
    api_client = MagicMock()
    session_state = {"lp_position_id": "123"}

    with (
        patch("dashboard.ui.st.title") as mock_title,
        patch("dashboard.ui.prepare_lp_session_state", return_value={"prepared": True}) as mock_prepare,
        patch("dashboard.ui.render_lp_dashboard") as mock_render,
    ):
        ui.render_custom_dashboard("dep-1", strategy_config, api_client, session_state)

    mock_title.assert_called_once_with("Delta-Neutral-AaveV3-UniV3-WETH-USDC-Base")
    mock_prepare.assert_called_once()
    args, kwargs = mock_prepare.call_args
    assert args[0] is api_client
    assert kwargs["session_state"] == session_state
    assert kwargs["deployment_id"] == "dep-1"

    mock_render.assert_called_once()
    render_args, render_kwargs = mock_render.call_args
    assert render_args[0] == "dep-1"
    assert render_args[1] == strategy_config
    assert render_args[2] == {"prepared": True}
    assert render_kwargs["api_client"] is api_client
