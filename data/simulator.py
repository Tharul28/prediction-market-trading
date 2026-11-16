"""A simple but realistic simulated multi-outcome prediction market.

Use this when you cannot reach a real exchange. It produces data in exactly
the same format as the Kalshi recorder (see ``data/schema.py``), so the rest
of the pipeline does not know or care where the data came from.

How the simulation works
------------------------
1. **True probabilities.** Each of ``n`` mutually exclusive outcomes has a
   latent score that follows a random walk (Gaussian diffusion plus
   occasional "news" jumps). A softmax turns the scores into probabilities
   that always sum to 1.
2. **Other market makers' quotes.** The visible book of each outcome is
   centred on the true probability plus a persistent quoting error (an AR(1)
   process). This error is what creates occasional mispricings, including
   cases where the outcomes' prices do not add up to 100.
3. **Book shape.** A spread of 1 to 4 cents that changes every 20 seconds
   or so, five levels per side, persistent random sizes that grow with
   depth, and a tilt so that more size sits on
   the side the true price is moving towards (this makes imbalance mildly
   informative, as it is in real markets).
4. **Trades.** Trades arrive as a Poisson process. A fraction of them are
   *informed*: they know the true probability a few seconds ahead and only
   trade when the book is wrong. The rest are noise traders who buy or sell
   at random. Informed flow is what makes market making hard (adverse
   selection).

Known simplifications (also listed in the README):

* Trades do not deplete the book: the next snapshot is generated
  independently of the trades that happened in between.
* Trades only happen at the best bid or best ask, never deeper.
* Our own orders do not change anyone else's behaviour.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from data.schema import BOOK_COLUMNS, TRADE_COLUMNS, MarketData


@dataclass(frozen=True)
class SimulationConfig:
    """Parameters of the simulated market. Defaults describe a two-hour
    session in a liquid-ish three-outcome market."""

    seed: int = 0
    outcome_names: tuple[str, ...] = ("SIM-A", "SIM-B", "SIM-C")
    initial_probabilities: tuple[float, ...] = (0.5, 0.3, 0.2)
    duration_s: float = 2 * 3600.0
    snapshot_interval_s: float = 1.0

    # True probability dynamics (in latent "logit" units).
    diffusion_vol: float = 0.004  # per sqrt(second)
    jump_rate_per_s: float = 1.0 / 900.0  # about four news jumps per outcome per hour
    jump_std: float = 0.3

    # Other market makers' quoting error, in cents.
    quote_noise_std_cents: float = 1.0
    quote_noise_persistence: float = 0.95  # AR(1) coefficient per snapshot

    # Book shape.
    spread_choices_cents: tuple[int, ...] = (1, 2, 3, 4)
    spread_probabilities: tuple[float, ...] = (0.35, 0.35, 0.2, 0.1)
    spread_change_rate_per_s: float = 0.05  # spreads persist for about 20 s on average
    n_levels: int = 5
    level_size_mean: float = 40.0
    level_size_growth: float = 0.5  # level i has mean size * (1 + growth * i)
    size_noise_sigma: float = 0.5  # lognormal noise on sizes
    size_noise_persistence: float = 0.98  # AR(1) coefficient of log-size noise per snapshot
    imbalance_sensitivity: float = 0.25  # size tilt per cent of quoting error

    # Trade flow.
    trade_rate_per_s: float = 0.12  # per outcome
    informed_fraction: float = 0.3
    informed_horizon_s: float = 20.0
    mean_trade_size: float = 6.0


def _simulate_true_probabilities(config: SimulationConfig, rng: np.random.Generator,
                                 n_steps: int) -> np.ndarray:
    """Random walk in latent space, mapped to probabilities with a softmax.

    Returns:
        Array of shape (n_steps, n_outcomes) whose rows sum to 1.
    """
    n_outcomes = len(config.outcome_names)
    dt = config.snapshot_interval_s
    latent = np.empty((n_steps, n_outcomes))
    latent[0] = np.log(np.asarray(config.initial_probabilities, dtype=float))
    diffusion = rng.normal(0.0, config.diffusion_vol * np.sqrt(dt), (n_steps - 1, n_outcomes))
    jump_counts = rng.poisson(config.jump_rate_per_s * dt, (n_steps - 1, n_outcomes))
    jumps = jump_counts * rng.normal(0.0, config.jump_std, (n_steps - 1, n_outcomes))
    latent[1:] = latent[0] + np.cumsum(diffusion + jumps, axis=0)
    shifted = np.exp(latent - latent.max(axis=1, keepdims=True))
    return shifted / shifted.sum(axis=1, keepdims=True)


def _ar1(rng: np.random.Generator, shape: tuple[int, ...], phi: float, sd: float) -> np.ndarray:
    """AR(1) noise along the first axis with stationary standard deviation ``sd``.

    ``x[t] = phi * x[t-1] + sqrt(1 - phi^2) * sd * eps[t]``, so values are
    persistent over time but do not drift away.
    """
    innovations = rng.normal(0.0, sd * np.sqrt(1.0 - phi**2), shape)
    noise = np.empty(shape)
    noise[0] = rng.normal(0.0, sd, shape[1:])
    for step in range(1, shape[0]):
        noise[step] = phi * noise[step - 1] + innovations[step]
    return noise


def _simulate_quote_noise(config: SimulationConfig, rng: np.random.Generator,
                          n_steps: int) -> np.ndarray:
    """AR(1) quoting error in cents with the configured stationary std."""
    n_outcomes = len(config.outcome_names)
    return _ar1(rng, (n_steps, n_outcomes), config.quote_noise_persistence, config.quote_noise_std_cents)


def _simulate_spreads(config: SimulationConfig, rng: np.random.Generator, n_steps: int) -> np.ndarray:
    """Spread in cents per snapshot and outcome, changing only occasionally."""
    n_outcomes = len(config.outcome_names)
    choices = np.asarray(config.spread_choices_cents)
    probabilities = np.asarray(config.spread_probabilities)
    draws = rng.choice(choices, size=(n_steps, n_outcomes), p=probabilities)
    changes = rng.random((n_steps, n_outcomes)) < config.spread_change_rate_per_s * config.snapshot_interval_s
    spread = np.empty((n_steps, n_outcomes), dtype=int)
    spread[0] = draws[0]
    for step in range(1, n_steps):
        spread[step] = np.where(changes[step], draws[step], spread[step - 1])
    return spread


def _build_book_table(config: SimulationConfig, rng: np.random.Generator, times: np.ndarray,
                      true_prob: np.ndarray, center: np.ndarray,
                      best_bid: np.ndarray, best_ask: np.ndarray) -> pd.DataFrame:
    """Create the long-format books table from top-of-book arrays."""
    n_steps, n_outcomes = best_bid.shape
    levels = np.arange(config.n_levels)
    bid_prices = best_bid[:, :, None] - levels[None, None, :]
    ask_prices = best_ask[:, :, None] + levels[None, None, :]

    base_size = config.level_size_mean * (1.0 + config.level_size_growth * levels)
    tilt = np.clip(config.imbalance_sensitivity * (100.0 * true_prob - center), -2.0, 2.0)
    noise_shape = (n_steps, n_outcomes, config.n_levels)
    bid_noise = _ar1(rng, noise_shape, config.size_noise_persistence, config.size_noise_sigma)
    ask_noise = _ar1(rng, noise_shape, config.size_noise_persistence, config.size_noise_sigma)
    bid_sizes = base_size * np.exp(bid_noise) * np.exp(tilt)[:, :, None]
    ask_sizes = base_size * np.exp(ask_noise) * np.exp(-tilt)[:, :, None]
    bid_sizes = np.maximum(np.round(bid_sizes), 1.0)
    ask_sizes = np.maximum(np.round(ask_sizes), 1.0)

    names = np.asarray(config.outcome_names)
    shape = noise_shape
    timestamp_grid = np.broadcast_to(times[:, None, None], shape)
    market_grid = np.broadcast_to(names[None, :, None], shape)
    level_grid = np.broadcast_to(levels[None, None, :], shape)

    tables = []
    for side, prices, sizes in (("bid", bid_prices, bid_sizes), ("ask", ask_prices, ask_sizes)):
        valid = (prices >= 1) & (prices <= 99)
        tables.append(pd.DataFrame({
            "timestamp": timestamp_grid[valid],
            "market_id": market_grid[valid],
            "side": side,
            "level": level_grid[valid],
            "price": prices[valid].astype(int),
            "size": sizes[valid],
        }))
    books = pd.concat(tables, ignore_index=True)
    return books.sort_values(["timestamp", "market_id", "side", "level"]).reset_index(drop=True)[BOOK_COLUMNS]


def _simulate_trades(config: SimulationConfig, rng: np.random.Generator, times: np.ndarray,
                     true_prob: np.ndarray, best_bid: np.ndarray, best_ask: np.ndarray,
                     best_bid_size: np.ndarray, best_ask_size: np.ndarray) -> pd.DataFrame:
    """Poisson trade arrivals between snapshots, a mix of informed and noise flow.

    A trade between snapshot ``k`` and ``k + 1`` executes against the book
    shown in snapshot ``k`` (the book everyone could see at the time).
    """
    dt = config.snapshot_interval_s
    n_steps, n_outcomes = best_bid.shape
    horizon_steps = int(round(config.informed_horizon_s / dt))
    frames = []
    for outcome in range(n_outcomes):
        counts = rng.poisson(config.trade_rate_per_s * dt, n_steps - 1)
        step = np.repeat(np.arange(n_steps - 1), counts)
        if len(step) == 0:
            continue
        # Strictly between snapshots so a trade never shares a timestamp with a book.
        trade_time = times[step] + rng.uniform(0.001, 0.999, len(step)) * dt
        informed = rng.random(len(step)) < config.informed_fraction

        future_value = 100.0 * true_prob[np.minimum(step + horizon_steps, n_steps - 1), outcome]
        bid = best_bid[step, outcome]
        ask = best_ask[step, outcome]

        side = np.where(rng.random(len(step)) < 0.5, "buy", "sell").astype(object)
        side[informed & (future_value > ask)] = "buy"
        side[informed & (future_value < bid)] = "sell"
        # Informed traders who see no edge do not trade.
        no_edge = informed & (future_value <= ask) & (future_value >= bid)
        keep = ~no_edge

        is_buy = side == "buy"
        price = np.where(is_buy, ask, bid)
        available = np.where(is_buy, best_ask_size[step, outcome], best_bid_size[step, outcome])
        size = np.minimum(rng.geometric(1.0 / config.mean_trade_size, len(step)), available)

        frames.append(pd.DataFrame({
            "timestamp": trade_time[keep],
            "market_id": config.outcome_names[outcome],
            "price": price[keep].astype(int),
            "size": size[keep].astype(float),
            "aggressor": side[keep],
        }))
    if not frames:
        return pd.DataFrame(columns=TRADE_COLUMNS)
    trades = pd.concat(frames, ignore_index=True)
    return trades.sort_values("timestamp").reset_index(drop=True)[TRADE_COLUMNS]


def simulate_market(config: SimulationConfig | None = None) -> MarketData:
    """Generate one simulated trading session.

    Args:
        config: Simulation parameters. Defaults to :class:`SimulationConfig()`.

    Returns:
        A :class:`MarketData` with books, trades, the true probability path
        (``truth``, for analysis only) and a settlement drawn from the final
        true probabilities.
    """
    config = config or SimulationConfig()
    if len(config.outcome_names) != len(config.initial_probabilities):
        raise ValueError("outcome_names and initial_probabilities must have the same length")
    if not np.isclose(sum(config.initial_probabilities), 1.0):
        raise ValueError("initial_probabilities must sum to 1")

    rng = np.random.default_rng(config.seed)
    dt = config.snapshot_interval_s
    n_steps = int(round(config.duration_s / dt)) + 1
    times = np.arange(n_steps) * dt

    true_prob = _simulate_true_probabilities(config, rng, n_steps)
    quote_noise = _simulate_quote_noise(config, rng, n_steps)
    center = np.clip(100.0 * true_prob + quote_noise, 1.5, 98.5)

    spread = _simulate_spreads(config, rng, n_steps)
    best_bid = np.clip(np.floor(center - spread / 2.0 + 0.5), 1, 98).astype(int)
    best_ask = np.minimum(best_bid + spread, 99).astype(int)

    books = _build_book_table(config, rng, times, true_prob, center, best_bid, best_ask)

    top = books[books["level"] == 0]
    best_bid_size = top[top["side"] == "bid"]["size"].to_numpy().reshape(n_steps, -1)
    best_ask_size = top[top["side"] == "ask"]["size"].to_numpy().reshape(n_steps, -1)
    trades = _simulate_trades(config, rng, times, true_prob, best_bid, best_ask,
                              best_bid_size, best_ask_size)

    winner = int(rng.choice(len(config.outcome_names), p=true_prob[-1]))
    settlement = {name: (100 if i == winner else 0) for i, name in enumerate(config.outcome_names)}

    truth = pd.DataFrame({
        "timestamp": np.repeat(times, len(config.outcome_names)),
        "market_id": np.tile(np.asarray(config.outcome_names), n_steps),
        "true_prob": true_prob.ravel(),
    })

    return MarketData(
        books=books,
        trades=trades,
        markets=list(config.outcome_names),
        mutually_exclusive=True,
        exhaustive=True,
        settlement=settlement,
        truth=truth,
        description=f"Simulated {len(config.outcome_names)}-outcome market, seed {config.seed}, "
                    f"{config.duration_s / 3600:.1f} hours",
        extra={"config": config},
    )
