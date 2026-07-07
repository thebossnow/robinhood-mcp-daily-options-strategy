import math

import pytest

from options_trader.signals import prob_above, prob_below


def test_monotonic_in_strike():
    dte = 5 / 365
    ps = [prob_above(100, k, 0.5, dte) for k in (95, 100, 105, 110)]
    assert all(a > b for a, b in zip(ps, ps[1:]))


def test_atm_is_slightly_below_half():
    # -sigma^2/2 drift term pushes N(d2) just under 0.5 ATM
    p = prob_above(100, 100, 0.5, 5 / 365)
    assert 0.4 < p < 0.5


def test_complement():
    p_up = prob_above(100, 103, 0.4, 10 / 365)
    p_dn = prob_below(100, 103, 0.4, 10 / 365)
    assert math.isclose(p_up + p_dn, 1.0)


def test_degenerate_expired_or_no_iv():
    assert prob_above(100, 90, 0.0, 5 / 365) == 1.0
    assert prob_above(100, 110, 0.5, 0.0) == 0.0


def test_far_otm_is_near_zero():
    assert prob_above(100, 150, 0.3, 5 / 365) < 0.001


def test_invalid_inputs():
    with pytest.raises(ValueError):
        prob_above(0, 100, 0.5, 0.1)
