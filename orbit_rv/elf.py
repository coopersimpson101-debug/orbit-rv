"""Minimal ELF64 loader (PT_LOAD segments + symbol lookup), no dependencies."""
from __future__ import annotations

import struct
from dataclasses import dataclass, field


@dataclass
class Image:
    entry: int
    segments: list[tuple[int, bytes]] = field(default_factory=list)  # (paddr, data incl. bss)
    symbols: dict[str, int] = field(default_factory=dict)

    @property
    def tohost(self) -> int | None:
        return self.symbols.get("tohost")

    def span(self) -> tuple[int, int]:
        lo = min(a for a, _ in self.segments)
        hi = max(a + len(d) for a, d in self.segments)
        return lo, hi

    def flat(self, base: int, size: int | None = None) -> bytes:
        """A flat little-endian memory image starting at ``base``."""
        lo, hi = self.span()
        end = base + size if size else hi
        buf = bytearray(end - base)
        for a, d in self.segments:
            if a < base or a + len(d) > end:
                raise ValueError(f"segment {a:#x}+{len(d):#x} outside {base:#x}..{end:#x}")
            buf[a - base:a - base + len(d)] = d
        return bytes(buf)


def load_elf(path) -> Image:
    data = open(path, "rb").read()
    if data[:4] != b"\x7fELF" or data[4] != 2 or data[5] != 1:
        raise ValueError(f"{path}: not a little-endian ELF64 file")
    (e_type, e_machine, _v, e_entry, e_phoff, e_shoff, _fl, _eh, e_phentsize, e_phnum,
     e_shentsize, e_shnum, e_shstrndx) = struct.unpack_from("<HHIQQQIHHHHHH", data, 16)
    if e_machine != 243:
        raise ValueError(f"{path}: not a RISC-V ELF (machine {e_machine})")
    img = Image(entry=e_entry)
    for i in range(e_phnum):
        p_type, _flags, p_off, _vaddr, p_paddr, p_filesz, p_memsz, _al = struct.unpack_from(
            "<IIQQQQQQ", data, e_phoff + i * e_phentsize)
        if p_type == 1 and p_memsz:
            seg = data[p_off:p_off + p_filesz] + bytes(p_memsz - p_filesz)
            img.segments.append((p_paddr, seg))
    sections = [struct.unpack_from("<IIQQQQIIQQ", data, e_shoff + i * e_shentsize)
                for i in range(e_shnum)]
    for sh in sections:
        if sh[1] == 2:  # SHT_SYMTAB
            strtab = sections[sh[6]]
            s_off = strtab[4]
            for j in range(sh[5] // 24):
                st_name, _info, _other, _shndx, st_value, _size = struct.unpack_from(
                    "<IBBHQQ", data, sh[4] + j * 24)
                if st_name:
                    end = data.index(b"\0", s_off + st_name)
                    img.symbols[data[s_off + st_name:end].decode()] = st_value
    return img
