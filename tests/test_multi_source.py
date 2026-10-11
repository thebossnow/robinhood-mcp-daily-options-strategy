"""Tests for MultiSourceHistory's dispatch-by-symbol routing. All sources mocked."""

from unittest import mock

import pandas as pd

from options_trader.data.multi_source import MultiSourceHistory


def test_day_chains_routes_to_correct_source():
    dolthub, databento = mock.Mock(), mock.Mock()
    dolthub.day_chains.return_value = pd.DataFrame({"a": [1]})
    databento.day_chains.return_value = pd.DataFrame({"a": [2]})
    hist = MultiSourceHistory({"F": dolthub, "SOFI": databento})

    hist.day_chains("F", "2023-01-01", max_dte=30)
    hist.day_chains("SOFI", "2023-01-01", max_dte=30)

    dolthub.day_chains.assert_called_once_with("F", "2023-01-01", 30)
    databento.day_chains.assert_called_once_with("SOFI", "2023-01-01", 30)


def test_unrouted_symbol_without_default_raises():
    hist = MultiSourceHistory({"F": mock.Mock()})
    try:
        hist.day_chains("UNKNOWN", "2023-01-01")
        assert False, "expected KeyError"
    except KeyError:
        pass


def test_unrouted_symbol_uses_default():
    default = mock.Mock()
    default.day_chains.return_value = pd.DataFrame()
    hist = MultiSourceHistory({}, default=default)
    hist.day_chains("ANY", "2023-01-01", 30)
    default.day_chains.assert_called_once_with("ANY", "2023-01-01", 30)


def test_prefetch_groups_symbols_by_source():
    dolthub, databento = mock.Mock(), mock.Mock()
    hist = MultiSourceHistory({"F": dolthub, "AAL": dolthub, "SOFI": databento})

    hist.prefetch(["F", "AAL", "SOFI"], ["2023-01-01", "2023-01-02"], workers=2)

    dolthub.prefetch.assert_called_once_with(
        ["F", "AAL"], ["2023-01-01", "2023-01-02"], max_dte=50, workers=2,
        progress_every=25,
    )
    databento.prefetch.assert_called_once_with(
        ["SOFI"], ["2023-01-01", "2023-01-02"], max_dte=50, workers=2,
        progress_every=25,
    )
