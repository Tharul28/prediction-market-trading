"""Single entry point for getting market data, real or simulated.

``scripts/run_all.py`` calls :func:`load_market_data` with whatever is in
its ``DATA_SOURCE`` line. Changing that one line from ``"simulated"`` to a
directory recorded by ``python -m data.kalshi record`` is all it takes to run
the whole pipeline on real data.
"""

from __future__ import annotations

from dataclasses import replace

from data.schema import MarketData, validate_market_data
from data.simulator import SimulationConfig, simulate_market
from data.storage import load_market_data_dir

SIMULATED = "simulated"


def load_market_data(source: str, seed: int = 0,
                     simulation_config: SimulationConfig | None = None) -> MarketData:
    """Load market data from a source.

    Args:
        source: ``"simulated"`` to generate a fresh simulated session, or the
            path of a directory saved by ``data.storage.save_market_data``
            (for example ``"data/raw/KXEXAMPLE-26DEC31"``).
        seed: Random seed, used only for simulated data.
        simulation_config: Optional simulator settings; its seed is replaced
            by ``seed``.

    Returns:
        A validated :class:`MarketData`.
    """
    if source == SIMULATED:
        config = replace(simulation_config or SimulationConfig(), seed=seed)
        data = simulate_market(config)
    else:
        data = load_market_data_dir(source)
    validate_market_data(data)
    return data
