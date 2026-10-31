import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "research" / "audit_2026-10" / "scripts"))
from champion_pit_test import simulate_exit  # noqa: E402

A = np.array


def run(o, h, l, c, start=1, sl=90.0, tp=125.0, **kw):
    return simulate_exit(A(o, float), A(h, float), A(l, float), A(c, float), start, sl, tp, **kw)


def test_stop_hit_intrabar_exits_at_stop():
    assert run([100, 99], [100, 100], [100, 89], [100, 95]) == (1, 90.0, "SL")


def test_gap_down_through_stop_exits_at_open():
    assert run([100, 85], [100, 86], [100, 84], [100, 85]) == (1, 85.0, "SL_GAP")


def test_target_hit_exits_at_target_and_gap_up_at_open():
    assert run([100, 110], [100, 126], [100, 109], [100, 120]) == (1, 125.0, "TP")
    assert run([100, 130], [100, 131], [100, 128], [100, 129]) == (1, 130.0, "TP")


def test_stop_and_target_same_bar_stop_wins():
    assert run([100, 100], [100, 130], [100, 85], [100, 100]) == (1, 90.0, "SL")


def test_no_trigger_marks_to_last_close():
    assert run([100, 101, 102], [100, 103, 104], [100, 99, 100], [100, 102, 103]) == (2, 103.0, "OPEN_AT_END")


def test_locked_lower_circuit_defers_to_next_open():
    # day 1 opens=high=low=close=85 (locked -15%): cannot sell; day 2 opens 83 and trades -> exit at 83
    o = [100, 85, 83]
    h = [100, 85, 84]
    l = [100, 85, 80]
    c = [100, 85, 82]
    assert run(o, h, l, c, locked_check=True) == (2, 83.0, "SL_GAP_DEFERRED")
