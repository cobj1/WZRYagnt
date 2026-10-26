# -*- coding: utf-8 -*-
"""实时内存读取器（Route C）：把本地 ELF 偏移换算成运行时地址并直接读取。

原理
----
MuMu 里 guest 的 arm64 库被原样映射进游戏进程，且保留 ELF 段文件偏移：

    400036a00000-400038b85000 r--p 00000000  .../lib/arm64/libGameCore.so   (段1: 文件偏移 0)
    400038b85000-40003b543000 r--p 02184000  .../lib/arm64/libGameCore.so   (段2: 文件偏移 0x2184000)
    40003b543000-40003b9f8000 r--p 04b41000  .../lib/arm64/libGameCore.so

因此：runtime = 段起始地址 + (目标文件偏移 - 该段文件偏移)。
再配合本地 ELF 的段表把 vaddr 换算成文件偏移，就能直接读运行时数据。

读取方式：必须用 `adb exec-out`（不经 pty），`adb shell` 会对二进制做 CR/LF 转换。

自检（已验证）：
    .rodata 文件偏移 0x1165dd8 -> 运行时 "[SelectTactics] model result size: %d"

用法：
    python livemem.py --demo              # 自检 + 读规则表
    python livemem.py --selftest          # 只做地址换算与字符串自检
"""
import argparse
import struct
import subprocess
import sys
from pathlib import Path

DEFAULT_ADB = r"D:\app\MuMuPlayer\nx_main\adb.exe"
DEFAULT_SERIAL = "127.0.0.1:16384"
DEFAULT_PKG = "com.tencent.tmgp.sgame"
LOCAL_SO = Path(__file__).resolve().parent / "out" / "il2cpp" / "lib" / "arm64-v8a"

# 只读安全：这些映射区不读（GPU/ION/DMA/内核辅助页），
# 参考 TrueVision 的 mass-scan 重启事故；调用方仍应只做定向读。
DENY_READ_MARKERS = ("/dev/", "dmabuf", "ion", "gpu", "[vvar]", "[vsyscall]", "[vectors]")
DEFAULT_MAX_READ = 4 * 1024 * 1024


class ElfLayout:
    """本地 ELF：vaddr -> 文件偏移（另有 PT_LOAD 表，可覆盖 .bss）。"""

    def __init__(self, path: Path):
        self.data = path.read_bytes()
        self.path = path
        d = self.data
        e_shoff, = struct.unpack_from("<Q", d, 0x28)
        es, = struct.unpack_from("<H", d, 0x3A)
        num, = struct.unpack_from("<H", d, 0x3C)
        self.secs = []
        for i in range(num):
            v = struct.unpack_from("<IIQQQQIIQQ", d, e_shoff + i * es)
            self.secs.append(dict(type=v[1], addr=v[3], off=v[4], size=v[5]))
        # PT_LOAD（p_vaddr/p_offset/p_filesz/p_memsz），用于解析 .bss：
        # .bss 在段表里没有文件偏移，但加载器按页映射，尾部是零填充页。
        e_phoff, = struct.unpack_from("<Q", d, 0x20)
        e_phentsize, = struct.unpack_from("<H", d, 0x36)
        e_phnum, = struct.unpack_from("<H", d, 0x38)
        self.loads = []
        for i in range(e_phnum):
            o = e_phoff + i * e_phentsize
            p_type, = struct.unpack_from("<I", d, o)
            p_offset, p_vaddr, _p_paddr, p_filesz, p_memsz, _align = \
                struct.unpack_from("<QQQQQQ", d, o + 8)
            if p_type == 1 and p_memsz:
                self.loads.append(dict(off=p_offset, vaddr=p_vaddr,
                                       filesz=p_filesz, memsz=p_memsz))

    def load_of(self, vaddr: int):
        for ld in self.loads:
            if ld["vaddr"] <= vaddr < ld["vaddr"] + ld["memsz"]:
                return ld
        return None

    def vaddr_to_off(self, vaddr: int):
        for s in self.secs:
            if s["size"] and s["addr"] <= vaddr < s["addr"] + s["size"]:
                return s["off"] + (vaddr - s["addr"])
        return None

    def off_to_vaddr(self, off: int):
        for s in self.secs:
            if s["size"] and s["off"] <= off < s["off"] + s["size"]:
                return s["addr"] + (off - s["off"])
        return None


class LiveMem:
    """游戏进程内存读写。"""

    def __init__(self, adb=DEFAULT_ADB, serial=DEFAULT_SERIAL, pkg=DEFAULT_PKG):
        self.adb, self.serial, self.pkg = adb, serial, pkg
        self.pid = None
        self.maps = []          # [(start, end, file_off, path)]
        self.map_perms = {}     # {start: "rwxp"}; 用于识别 exec 段/重复映射
        self.read_stats = {"ok": 0, "denied": 0, "short": 0, "error": 0, "bytes": 0}
        self._elf = {}

    # ---------- 基础 ----------
    def _run(self, args, binary=False):
        cmd = [self.adb, "-s", self.serial] + args
        r = subprocess.run(cmd, capture_output=True)
        if r.returncode != 0 and not binary:
            raise RuntimeError(r.stderr.decode("utf-8", "replace").strip())
        return r.stdout

    def find_pid(self):
        out = self._run(["exec-out", f"pidof {self.pkg}"]).decode().split()
        self.pid = out[0] if out else None
        return self.pid

    def refresh_maps(self):
        raw = self._run(["exec-out", f"cat /proc/{self.pid}/maps"]).decode("utf-8", "replace")
        self.maps = []
        self.map_perms = {}
        for line in raw.splitlines():
            # 注意：匿名段常只有 5 个字段（无路径名）。旧版要求 len>=6 会把
            # guest 堆这类无名段整段丢掉，导致"指针落点"统计出现大量假未映射。
            parts = line.split(None, 5)
            if len(parts) < 5:
                continue
            rng, perms, off, _dev, _ino = parts[:5]
            path = parts[5] if len(parts) > 5 else ""
            try:
                start, end = (int(x, 16) for x in rng.split("-"))
                self.maps.append((start, end, int(off, 16), path))
                self.map_perms[start] = perms
            except ValueError:
                continue
        return len(self.maps)

    def elf(self, so_name: str) -> ElfLayout:
        if so_name not in self._elf:
            self._elf[so_name] = ElfLayout(LOCAL_SO / so_name)
        return self._elf[so_name]

    def segments(self, so_name: str):
        return [m for m in self.maps if m[3].endswith("/" + so_name)]

    def resolve(self, so_name: str, vaddr: int):
        """本地 ELF vaddr -> 运行时地址（PT_LOAD 页映射，支持 .bss）。

        旧实现按 section 的 vaddr->file_off 换算，.bss 没有文件偏移会直接失败；
        且文件映射尾部的零填充页会被算偏 0x3000。这里改用 PT_LOAD：
            runtime = seg_start + (vaddr - (p_vaddr & ~0xfff))
        seg_start 由运行时 maps 中同样 p_offset 页的文件偏移匹配。
        """
        elf = self.elf(so_name)
        ld = elf.load_of(vaddr)
        if ld is None:
            return None
        page_off = ld["off"] & ~0xFFF
        page_vaddr = ld["vaddr"] & ~0xFFF
        cands = [m for m in self.segments(so_name) if m[2] == page_off]
        if not cands:
            return None
        # 跨局可能残留只读副本（无 x 段）；优先 exec 段，避免选错 base。
        cands.sort(key=lambda m: 0 if "x" in self.map_perms.get(m[0], "") else 1)
        start = cands[0][0]
        return start + (vaddr - page_vaddr)

    # ---------- 读 ----------
    def read(self, addr: int, size: int) -> bytes:
        """按页读取并切片（/proc/pid/mem 支持 lseek，dd skip 是 O(1)）。"""
        first = addr - (addr % 4096)
        last = addr + size
        pages = (last - first + 4095) // 4096
        out = self._run(["exec-out",
                         f"dd if=/proc/{self.pid}/mem bs=4096 skip={first // 4096} "
                         f"count={pages} 2>/dev/null"], binary=True)
        s = addr - first
        return out[s:s + size]

    def read_pages(self, pages, page_size=4096):
        """一次 adb exec-out 读多个 4KB 页（返回 {page: bytes}）。

        用 base64 + 唯一标记分隔每页，避免某一页读失败导致整体错位；
        缺失/短页再逐页回退。用途：把一帧几十页合并成一次 adb 往返。
        """
        import base64
        pages = list(dict.fromkeys(int(p) for p in pages))
        if not pages:
            return {}
        cmd = ";".join(
            f"printf '@@P{p:x}@@\\n'; "
            f"dd if=/proc/{self.pid}/mem bs={page_size} "
            f"skip={p // page_size} count=1 2>/dev/null | base64 | tr -d '\\n'; "
            f"printf '\\n'" for p in pages)
        raw = self._run(["exec-out", cmd], binary=True)
        out = raw.decode("ascii", "replace")
        res, cur, buf = {}, None, []
        for line in out.splitlines():
            if line.startswith("@@P") and line.endswith("@@"):
                if cur is not None:
                    s = "".join(buf)
                    res[cur] = base64.b64decode(s) if s else b""
                try:
                    cur = int(line[3:-2], 16)
                except ValueError:
                    cur = None
                buf = []
            elif cur is not None and line:
                buf.append(line)
        if cur is not None:
            s = "".join(buf)
            res[cur] = base64.b64decode(s) if s else b""
        for p in pages:
            if p not in res or len(res[p]) < page_size:
                res[p] = self.read(p, page_size)
        return res

    def read_spans_checked(self, spans, max_total: int = DEFAULT_MAX_READ):
        """校验每个 span 在允许映射内、总量在预算内，再调用 read_spans。"""
        spans = [(int(s), int(n)) for s, n in spans if n > 0]
        if not spans:
            return []
        total = sum(n for _, n in spans)
        if total <= 0 or total > max_total:
            self.read_stats["denied"] += 1
            return []
        for start, size in spans:
            if not self.is_mapped(start, size):
                self.read_stats["denied"] += 1
                return []
        return self.read_spans(spans)

    def read_spans(self, spans):
        """一次 adb exec-out 读多个**已映射连续区间** [(start,size), ...]。

        每个区间必须落在单个 /proc/pid/maps 段内。用 "@@S<start>:<size>@@\\n"
        文本头 + 定长二进制数据，顺序解析；比逐页 dd 少几十倍进程开销。
        返回 [(start, bytes), ...]；解析失败返回 []（调用方回退）。
        """
        spans = [(int(s), int(n)) for s, n in spans if n > 0]
        if not spans:
            return []
        parts = []
        for start, size in spans:
            pages = (size + 0xFFF) // 0x1000
            parts.append(
                f"printf '@@S{start:x}:{pages * 0x1000:x}@@\\n'; "
                f"dd if=/proc/{self.pid}/mem bs=4096 skip={start // 0x1000} "
                f"count={pages} 2>/dev/null")
        out = self._run(["exec-out", ";".join(parts)], binary=True)
        res, i = [], 0
        while i < len(out):
            if not out.startswith(b"@@S", i):
                nl = out.find(b"\n", i)
                if nl < 0:
                    break
                i = nl + 1
                continue
            j = out.find(b"@@\n", i)
            if j < 0:
                break
            try:
                start_s, size_s = out[i + 3:j].decode().split(":")
                start, size = int(start_s, 16), int(size_s, 16)
            except ValueError:
                break
            data = out[j + 3:j + 3 + size]
            if len(data) < size:
                return []
            res.append((start, data))
            i = j + 3 + size
        return res if len(res) == len(spans) else []

    def read_so(self, so_name: str, vaddr: int, size: int) -> bytes:
        rt = self.resolve(so_name, vaddr)
        return None if rt is None else self.read(rt, size)

    # ---------- 安全读（只加约束，不改原 read 语义） ----------
    def segment_info(self, so_name: str):
        """[(start, end, file_off, path, perms), ...]，用于诊断重复映射。"""
        return [(s, e, o, p, self.map_perms.get(s, ""))
                for s, e, o, p in self.segments(so_name)]

    def active_exec_start(self, so_name: str):
        """含 exec 的映射中最低的起始地址；没有 exec 时回退到最低映射。"""
        segs = self.segments(so_name)
        if not segs:
            return None
        execs = [m for m in segs if "x" in self.map_perms.get(m[0], "")]
        return min(m[0] for m in (execs or segs))

    def is_mapped(self, addr: int, size: int,
                  deny_markers=DENY_READ_MARKERS) -> bool:
        """检查 [addr, addr+size) 是否全在允许的映射区；允许跨连续段。"""
        if addr <= 0 or size <= 0:
            return False
        end = addr + size
        pos = addr
        while pos < end:
            hit = None
            for st, en, _off, path in self.maps:
                if st <= pos < en:
                    hit = (en, path)
                    break
            if hit is None:
                return False
            en, path = hit
            low = (path or "").lower()
            if any(m in low for m in deny_markers):
                return False
            pos = min(end, en)
        return True

    def read_checked(self, addr: int, size: int,
                     max_bytes: int = DEFAULT_MAX_READ):
        """带边界/预算的读；失败返回 None，不抛异常。"""
        if size <= 0 or size > max_bytes or not self.is_mapped(addr, size):
            self.read_stats["denied"] += 1
            return None
        try:
            data = self.read(addr, size)
        except Exception:
            self.read_stats["error"] += 1
            return None
        if len(data) != size:
            self.read_stats["short"] += 1
            return None
        self.read_stats["ok"] += 1
        self.read_stats["bytes"] += size
        return data

    def read_ptr_checked(self, addr: int):
        """读 8 字节指针；0/失败返回 None，并按 ARM64 PAC 规则去 tag。"""
        b = self.read_checked(addr, 8)
        if b is None:
            return None
        p = struct.unpack_from("<Q", b)[0]
        return (p & 0xFFFFFFFFFFFF) if p else None

    def read_i32_checked(self, addr: int):
        b = self.read_checked(addr, 4)
        return struct.unpack_from("<i", b)[0] if b is not None else None

    def read_xyz_checked(self, addr: int):
        b = self.read_checked(addr, 12)
        return struct.unpack_from("<3i", b) if b is not None else None

    def cstr(self, so_name: str, vaddr: int, limit=128):
        b = self.read_so(so_name, vaddr, limit)
        if b is None:
            return None
        z = b.find(b"\0")
        return b[:z if z >= 0 else len(b)].decode("ascii", "replace")

    def u64s(self, addr: int, n: int):
        b = self.read(addr, 8 * n)
        return list(struct.unpack_from(f"<{n}Q", b, 0)) if len(b) == 8 * n else []

    def u32s(self, addr: int, n: int):
        b = self.read(addr, 4 * n)
        return list(struct.unpack_from(f"<{n}I", b, 0)) if len(b) == 4 * n else []


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--adb", default=DEFAULT_ADB)
    ap.add_argument("--serial", default=DEFAULT_SERIAL)
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()

    m = LiveMem(a.adb, a.serial)
    pid = m.find_pid()
    if not pid:
        print("未找到游戏进程，请先启动游戏")
        return 1
    n = m.refresh_maps()
    print(f"pid={pid}  maps={n}")
    for seg in m.segments("libGameCore.so"):
        print(f"  seg base=0x{seg[0]:x} file_off=0x{seg[2]:x}")

    print("--- selftest: .rodata 已知串 ---")
    for vaddr, expect in ((0x1165DD8, "[SelectTactics]"),
                          (0x114C10A, "tactics_config_file"),
                          (0x1159E44, "RuleTactics->Process")):
        rt = m.resolve("libGameCore.so", vaddr)
        got = m.cstr("libGameCore.so", vaddr)
        ok = got is not None and got.startswith(expect)
        print(f"  0x{vaddr:x} -> 0x{rt:x}  {'OK ' if ok else 'FAIL'} got={got!r}")

    if a.demo:
        print("--- 读运行时规则函数指针表 (本地 vaddr 0x509c000) ---")
        rt = m.resolve("libGameCore.so", 0x509C000)
        print(f"  table runtime = 0x{rt:x}")
        pt = m.u64s(rt, 24)
        for i, v in enumerate(pt):
            print(f"  [{i:02d}] slot=0x{0x509c000 + i * 8:x} -> 0x{v:08x}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
