"""Commit-log formatting, parsing (incl. Spike's layout) and comparison."""
from orbit_rv.commitlog import Commit, compare, format_commit, parse_lines

# Hand-written lines in the layout Spike's --log-commits uses (not captured Spike output).
SPIKE_STYLE = """\
core   0: 3 0x0000000000001000 (0x00000297) x5  0x0000000000001000
core   0: 3 0x0000000080000000 (0x0540006f)
core   0: 3 0x0000000080000054 (0x00000093) x1  0x0000000000000000
core   0: 3 0x0000000080000058 (0x0182b283) x5  0x0000000080000018 mem 0x0000000080000018
core   0: 3 0x000000008000005c (0x00b53023) mem 0x0000000080001000 0x0000000000000001
core   0: 3 0x0000000080000060 (0x30001073) c768_mstatus 0x0000000a00000000
core   0: exception trap_illegal_instruction, epc 0x0000000080000064
"""


def test_parse_spike_style_skips_boot_rom_and_non_gpr_fields():
    cs = parse_lines(SPIKE_STYLE.splitlines(), start_pc=0x8000_0000)
    assert [c.pc for c in cs] == [0x8000_0000, 0x8000_0054, 0x8000_0058, 0x8000_005C, 0x8000_0060]
    assert cs[0].writes == {} and cs[1].writes == {1: 0}
    assert cs[2].writes == {5: 0x8000_0018}
    assert cs[3].writes == {} and cs[4].writes == {}


def test_roundtrip_and_compare():
    lines = [format_commit(3, 0x8000_0000, 0x13, 0, 0), format_commit(1, 0x8000_0004, 0x00100093, 1, 1)]
    cs = parse_lines(lines)
    assert cs[1] == Commit(1, 0x8000_0004, 0x00100093, {1: 1})
    assert compare(cs, cs).ok
    bad = [cs[0], Commit(1, 0x8000_0004, 0x00100093, {1: 2})]
    r = compare(cs, bad)
    assert not r.ok and r.compared == 1
