"""Signal features: no lookahead, correct targets and flows, time-ordered split."""

import numpy as np
import pandas as pd
import pytest

from data.simulator import SimulationConfig, simulate_market
from research.features import FEATURES, build_features, time_split
from tests.helpers import book, make_data

SIM = simulate_market(SimulationConfig(duration_s=1200, seed=3))


def test_features_do_not_change_when_the_future_changes():
    """Cut the recording at T: every feature of every row up to T must be identical."""
    cut = 600.0
    full = build_features(SIM, expiry={m: 5000.0 for m in SIM.markets})
    past = SIM.__class__(**{**SIM.__dict__, "books": SIM.books[SIM.books["timestamp"] <= cut],
                            "trades": SIM.trades[SIM.trades["timestamp"] <= cut]})
    truncated = build_features(past, expiry={m: 5000.0 for m in SIM.markets})
    merged = full[full["timestamp"] <= cut].merge(truncated, on=["timestamp", "market_id"], suffixes=("", "_cut"))
    assert len(merged) == len(truncated) > 100
    for feature in FEATURES:
        np.testing.assert_allclose(merged[feature], merged[f"{feature}_cut"], equal_nan=True, err_msg=feature)
    # The labels do look ahead, so near the cut they are unknown in the truncated version.
    assert merged["target_60s_cut"].isna().sum() > merged["target_60s"].isna().sum()


def test_targets_flows_and_imbalance_by_hand():
    snaps = [book(t, [(50, 30), (49, 10)], [(52, 10), (53, 10)]) for t in (0, 5, 10)]
    snaps += [book(t, [(54, 10)], [(56, 10)]) for t in (15, 20, 25)]
    trades = [{"timestamp": 5.0, "market_id": "M", "price": 52, "size": 4, "aggressor": "buy"},   # at t: known
              {"timestamp": 5.1, "market_id": "M", "price": 50, "size": 1, "aggressor": "sell"},  # after t
              {"timestamp": 12.0, "market_id": "M", "price": 56, "size": 7, "aggressor": "buy"}]
    f = build_features(make_data(snaps, trades)).set_index("timestamp")
    assert f.loc[5.0, "flow_10s"] == 4.0
    assert f.loc[10.0, "flow_10s"] == 3.0           # +4 - 1, both within (0, 10]
    assert f.loc[20.0, "flow_10s"] == 7.0           # the t=5 trades are now older than 10 s
    assert f.loc[0.0, "imb_1"] == pytest.approx((30 - 10) / 40)
    assert f.loc[0.0, "imb_3"] == pytest.approx((40 - 20) / 60)
    assert f.loc[5.0, "target_10s"] == 4.0          # mid 51 -> 55 at t=15
    assert f.loc[10.0, "ret_10s"] == 0.0 and f.loc[15.0, "ret_10s"] == 4.0
    assert np.isnan(f.loc[20.0, "target_10s"])      # t + 10 is past the end of the recording


def test_time_split_never_mixes_past_and_future():
    day = 86400.0
    frame = pd.DataFrame({"timestamp": np.concatenate([np.arange(0, 3600, 5) + d * day for d in range(4)])})
    train, test, how = time_split(frame, test_share=0.3, embargo_s=60)
    assert how.startswith("by day")
    assert train["timestamp"].max() < test["timestamp"].min() - 60
    assert test["timestamp"].min() >= 3 * day      # last of 4 days held out
    train, test, how = time_split(frame[frame["timestamp"] < day], test_share=0.3, embargo_s=60)
    assert how.startswith("single day") and train["timestamp"].max() < test["timestamp"].min() - 60
