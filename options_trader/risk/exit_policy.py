"""Loss stop for income-framework credit positions (operator decision 2026-10-10).

Two modes, chosen in configs/income_framework.json:

  account_equity     (alternate) close when the position's unrealized loss in
                     dollars reaches max_loss_pct_account x account equity
                     (0.05 x $75,000 = $3,750 on the paper account).
  position_max_loss  (default in income_framework.json) close when the unrealized loss reaches
                     max_loss_pct_position x the position's own max loss.

stop_loss_mode = None (StrategyConfig's default) disables the stop, so the
credit book (configs/credit_paper.json) is unaffected.

"Unrealized loss" = (cost to close incl. slippage - credit received) x 100 x
contracts. A profitable position has a negative loss and never stops.
"""

from __future__ import annotations

MODES = ("account_equity", "position_max_loss")


def stop_threshold(cfg, position_max_loss_dollars: float) -> float | None:
    """Dollar loss at which the stop fires, or None when no stop applies."""
    mode = getattr(cfg, "stop_loss_mode", None)
    if mode is None:
        return None
    if mode == "account_equity":
        return cfg.max_loss_pct_account * cfg.account_equity
    if mode == "position_max_loss":
        return cfg.max_loss_pct_position * position_max_loss_dollars
    raise ValueError(f"unknown stop_loss_mode {mode!r}; have {MODES}")


def stop_hit(cfg, loss_per_share: float, contracts: int,
             position_max_loss_dollars: float) -> tuple[bool, float | None, float]:
    """(fired, threshold_dollars, loss_dollars)."""
    loss = loss_per_share * 100.0 * contracts
    thr = stop_threshold(cfg, position_max_loss_dollars)
    return (thr is not None and loss >= thr), thr, loss
