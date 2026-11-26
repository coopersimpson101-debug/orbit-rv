"""tools/spike_compare.py: CSR masking and spurious-SC resynchronisation (hand-made logs)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from spike_compare import EXPECTED_FAIL, MUST_MATCH, compare  # noqa: E402

from orbit_rv.commitlog import Commit as C  # noqa: E402

LR, SC, NOP = 0x1005272F, 0x18E5272F, 0x00000013
GOOD = [C(0, 0x100, LR, {14: 5}), C(0, 0x104, SC, {14: 0}), C(0, 0x108, NOP, {})]
# the same loop where the first SC failed and was retried
RETRY = [C(0, 0x100, LR, {14: 5}), C(0, 0x104, SC, {14: 1}), C(0, 0x0F8, NOP, {}),
         C(0, 0x100, LR, {14: 5}), C(0, 0x104, SC, {14: 0}), C(0, 0x108, NOP, {})]


def test_spurious_sc_failure_is_resynchronised_both_ways():
    for ref, dut in ((RETRY, GOOD), (GOOD, RETRY)):
        ok, n, msg = compare(ref, dut)
        assert ok and n == 3 and "1 spurious SC" in msg


def test_real_difference_after_resync_is_reported():
    bad = GOOD[:2] + [C(0, 0x108, NOP, {1: 1})]
    ok, n, _ = compare(RETRY, bad)
    assert not ok and n == 2


def test_impl_defined_csr_values_are_masked():
    csrr_misa = 0x301022F3  # csrr x5, misa
    ok, _, _ = compare([C(3, 0, csrr_misa, {5: 1})], [C(3, 0, csrr_misa, {5: 2})])
    assert ok


def test_must_match_and_expected_failures():
    assert MUST_MATCH.match("rv64ui-v-add") and MUST_MATCH.match("bench-dhrystone")
    assert not MUST_MATCH.match("rv64mi-p-breakpoint") and not MUST_MATCH.match("bench-irqtest")
    assert "rv64ui-v-ma_data" in EXPECTED_FAIL


def test_commits_before_the_entry_are_counted():
    from spike_compare import boot_rom_commits
    from orbit_rv.commitlog import format_commit
    log = [format_commit(3, 0x1000 + 4 * k, 0x13, 0, 0) for k in range(5)]
    log += [format_commit(3, 0x8000_0000, 0x13, 0, 0)]
    assert boot_rom_commits(log, 0x8000_0000) == 5
    assert boot_rom_commits(log[5:], 0x8000_0000) == 0
