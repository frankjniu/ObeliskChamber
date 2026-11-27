// Reads (and writes) of guest addresses nothing is mapped at: [memory] unmapped_access = zero (default) answers them the way the yuzu
// family of emulators (yuzu, Eden, Citron, Suyu) does, instead of crashing.
//
// Those emulators run the game through dynarmic, whose memory callbacks go to Core::Memory::Read / Write (src/core/memory.cpp): an
// address on an unmapped page is logged ("Unmapped Read32 @ 0x...") and the read returns 0, the write is dropped, and the game runs
// on. With fastmem the fault in the host arena falls back to the same callbacks. On the console (and here, where guest addresses are
// host addresses and the lifted code loads them directly) such an access faults. Mods built and tested on those emulators can rely on
// that without knowing it: a table whose references were never registered for relocation keeps raw file offsets (a few MB), the game
// reads a string through one, gets 0 on the emulator (an empty string) and goes on; here it crashed (a Skill.bin with 571 such
// references, the first a burst-skill field read at 0x2f74f9).
//
// How: a vectored exception handler, registered last (the write tracker and the heap's lazy commit answer their own faults first).
// For an access violation on a guest thread, at an address below 4 GiB whose 64 KiB granule is free both in the process (VirtualQuery:
// MEM_FREE) and in the guest's memory map (svcQueryMemory: free), it maps that granule as zeroed memory and lets the instruction run
// again: reads see 0. A write gets a writable granule (the console would fault, the emulators drop it; here later reads of the same
// address see what was written: a difference only for code that reads back what it wrote to nowhere). Nothing the game or the runtime
// maps lives below 4 GiB (code at 0x7100000000, the heap and stacks higher), and the granules are given back if the guest maps there.
// Each new granule is logged once (from a logging thread, not the handler), with the guest function that read it; at most 4096
// granules (256 MiB of address space), then faults crash as before. [memory] unmapped_access = crash: the old behaviour.
//
// Two cases cannot be answered by mapping zeros, because Windows will not map there: the first 64 KiB (the null page area: a mod's
// empty reference read as 0 is then used as a pointer, and the emulators answer that read with 0 too), and pages Windows reserved for
// one of our own allocations (host memory is kept low on purpose, so a raw offset of a few MB can land in, say, the reserved tail of a
// heap segment). For a READ by code in the exe (the lifted game) there, the handler points the instruction's base register at a read-only zero
// block for one instruction: it decodes only the instruction's memory operand (prefixes, REX / VEX / EVEX, ModRM, SIB), moves the base
// register by (zero block - fault address), sets the trap flag, and on the single-step that follows puts the register back unless the
// instruction wrote it. The load reads 0, every other register and the game's memory are untouched, and nothing of the host's is
// committed or changed. Writes there, faults outside the lifted code, RIP-relative and segment-prefixed operands, and operands whose
// base register can't be moved crash as before. Each guest instruction answered this way is logged once.
#include "nxrt/memory.h"

#define WIN32_LEAN_AND_MEAN
#include <windows.h>

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <thread>

#include "nxrt/config.h"
#include "nxrt/dispatch.h"
#include "nxrt/kernel.h"
#include "nxrt/log.h"

namespace nxrt::mem {
namespace {

constexpr const char* kTag = "mem";
constexpr uint64_t kGranule = 0x10000, kLow = 0x10000, kHigh = 0x100000000ull;
constexpr int kMaxGranules = 4096;

struct Entry {
    std::atomic<uint64_t> granule{0};   // 0: empty
    uint64_t addr = 0, rip = 0;
    bool write = false;
    bool skip = false;       // slot taken by a mapping that failed: nothing to log
    bool redirected = false; // answered by pointing one instruction at the zero block (granule stays 0: nothing mapped)
    std::atomic<bool> ready{false};
};
Entry g_entries[kMaxGranules];
std::atomic<int> g_used{0};
std::atomic<uint64_t> g_faults{0};
std::atomic<bool> g_on{false};
void* g_veh = nullptr;

// ---- one-instruction base-register redirect (null page area and host reservations) ----

// Bit set: one-byte opcodes and 0F opcodes that have a ModRM byte.
bool modrm_1byte(uint8_t op) {
    const uint8_t lo = op & 0x0f;
    if (op < 0x40) return (lo <= 3) || (lo >= 8 && lo <= 0xb);
    if (op == 0x62 || op == 0x63 || op == 0x69 || op == 0x6b) return true;
    if (op >= 0x80 && op <= 0x8f) return true;
    if (op == 0xc0 || op == 0xc1 || op == 0xc6 || op == 0xc7) return true;
    if (op >= 0xd0 && op <= 0xd3) return true;
    if (op >= 0xd8 && op <= 0xdf) return true;
    return op == 0xf6 || op == 0xf7 || op == 0xfe || op == 0xff;
}
bool modrm_0f(uint8_t op) {
    switch (op) {
    case 0x05: case 0x06: case 0x07: case 0x08: case 0x09: case 0x0b: case 0x0e: case 0x77: case 0xa0: case 0xa1: case 0xa2:
    case 0xa8: case 0xa9: case 0xaa: return false;
    default: break;
    }
    if (op >= 0x30 && op <= 0x37) return false;
    if (op >= 0x80 && op <= 0x8f) return false;   // jcc rel32
    if (op >= 0xc8 && op <= 0xcf) return false;   // bswap
    return true;
}

// The register that forms the memory operand's base (0..15 in encoding order), or -1 if it can't be redirected.
int memory_base_register(const uint8_t* ip) {
    int i = 0;
    for (; i < 15; ++i) {   // legacy prefixes
        const uint8_t b = ip[i];
        if (b == 0x66 || b == 0xf2 || b == 0xf3 || b == 0xf0) continue;
        if (b == 0x26 || b == 0x2e || b == 0x36 || b == 0x3e || b == 0x64 || b == 0x65 || b == 0x67) return -1;   // segment / addr32
        break;
    }
    int rex_b = 0, rex_x = 0;
    uint8_t op = ip[i];
    bool has_modrm = false;
    if (op >= 0x40 && op <= 0x4f) {
        rex_b = op & 1;
        rex_x = (op >> 1) & 1;
        op = ip[++i];
    }
    if (op == 0xc5) {   // two-byte VEX: map 0F
        const uint8_t opc = ip[i + 2];
        if (opc == 0x77) return -1;   // vzeroupper / vzeroall
        i += 3;
        has_modrm = true;
    } else if (op == 0xc4) {   // three-byte VEX
        rex_b = !((ip[i + 1] >> 5) & 1);
        rex_x = !((ip[i + 1] >> 6) & 1);
        const int map = ip[i + 1] & 0x1f;
        if (map < 1 || map > 3) return -1;
        if (map == 1 && ip[i + 3] == 0x77) return -1;
        i += 4;
        has_modrm = true;
    } else if (op == 0x62) {   // EVEX
        rex_b = !((ip[i + 1] >> 5) & 1);
        rex_x = !((ip[i + 1] >> 6) & 1);
        i += 5;
        has_modrm = true;
    } else if (op == 0x0f) {
        const uint8_t op2 = ip[i + 1];
        if (op2 == 0x38 || op2 == 0x3a) {
            i += 3;
            has_modrm = true;
        } else {
            has_modrm = modrm_0f(op2);
            i += 2;
        }
    } else {
        has_modrm = modrm_1byte(op);
        i += 1;
    }
    if (!has_modrm) return -1;
    const uint8_t modrm = ip[i];
    const int mod = modrm >> 6, rm = modrm & 7;
    if (mod == 3) return -1;                 // a register operand: not this fault
    if (rm == 5 && mod == 0) return -1;      // RIP-relative
    if (rm != 4) return rm | (rex_b << 3);   // [base + disp]
    const uint8_t sib = ip[i + 1];
    const int base = sib & 7, index = ((sib >> 3) & 7) | (rex_x << 3);
    if (base == 5 && mod == 0) return -1;    // no base register (disp32 [+ index]): leave it
    const int b = base | (rex_b << 3);
    if (index != 4 && index == b) return -1; // [r + r*s]: moving it moves both
    return b;
}

DWORD64* context_register(CONTEXT* c, int r) {
    switch (r) {
    case 0: return &c->Rax; case 1: return &c->Rcx; case 2: return &c->Rdx; case 3: return &c->Rbx;
    case 4: return nullptr;   // rsp: never
    case 5: return &c->Rbp; case 6: return &c->Rsi; case 7: return &c->Rdi;
    case 8: return &c->R8; case 9: return &c->R9; case 10: return &c->R10; case 11: return &c->R11;
    case 12: return &c->R12; case 13: return &c->R13; case 14: return &c->R14; case 15: return &c->R15;
    default: return nullptr;
    }
}

uint8_t* g_zero = nullptr;   // 128 KiB read-only zeros; loads are pointed at its middle
uint64_t g_exe_lo = 0, g_exe_hi = 0;   // the exe's image: the lifted game (and its native HLE helpers) live there, the runtime does not
constexpr uint64_t kZeroHalf = 0x10000;

struct Redirect {
    bool active = false;
    int reg = -1;
    DWORD64 orig = 0, moved = 0;
    uint64_t rip = 0;
};
thread_local Redirect t_redirect;

// One logged line per guest instruction answered by a redirect (they can repeat every frame).
constexpr int kRipSet = 8192;
std::atomic<uint64_t> g_rips[kRipSet];
bool first_time_rip(uint64_t rip) {
    uint64_t h = (rip * 0x9e3779b97f4a7c15ull) >> 51;
    for (int n = 0; n < 64; ++n, h = (h + 1) & (kRipSet - 1)) {
        uint64_t cur = g_rips[h].load(std::memory_order_relaxed);
        if (cur == rip) return false;
        if (cur == 0 && g_rips[h].compare_exchange_strong(cur, rip)) return true;
        if (cur == rip) return false;
    }
    return false;   // full: stop logging new ones
}

LONG redirect_read(EXCEPTION_POINTERS* ep, uint64_t addr) {
    CONTEXT* c = ep->ContextRecord;
    const uint64_t rip = static_cast<uint64_t>(c->Rip);
    if (!g_zero || t_redirect.active || rip < g_exe_lo || rip >= g_exe_hi) return EXCEPTION_CONTINUE_SEARCH;   // lifted code only
    if (addr >= kLow && vm_query(addr & ~uint64_t(0xfff)).state != kMemFree) return EXCEPTION_CONTINUE_SEARCH;   // the guest's own
    const int reg = memory_base_register(reinterpret_cast<const uint8_t*>(rip));
    DWORD64* r = reg >= 0 ? context_register(c, reg) : nullptr;
    if (!r) return EXCEPTION_CONTINUE_SEARCH;
    const uint64_t target = reinterpret_cast<uint64_t>(g_zero) + kZeroHalf;
    t_redirect.active = true;
    t_redirect.reg = reg;
    t_redirect.orig = *r;
    t_redirect.moved = *r + (target - addr);   // the operand's address becomes the zero block's middle (± the access width)
    t_redirect.rip = rip;
    *r = t_redirect.moved;
    c->EFlags |= 0x100;   // trap flag: the single-step after this one instruction puts the register back
    if (first_time_rip(rip)) {
        const int slot = g_used.fetch_add(1, std::memory_order_relaxed);
        if (slot < kMaxGranules) {
            Entry& e = g_entries[slot];
            e.addr = addr;
            e.rip = rip;
            e.redirected = true;
            e.ready.store(true, std::memory_order_release);
        }
    }
    g_faults.fetch_add(1, std::memory_order_relaxed);
    return EXCEPTION_CONTINUE_EXECUTION;
}

LONG single_step(EXCEPTION_POINTERS* ep) {
    Redirect& t = t_redirect;
    if (!t.active) return EXCEPTION_CONTINUE_SEARCH;
    CONTEXT* c = ep->ContextRecord;
    DWORD64* r = context_register(c, t.reg);
    if (r && *r == t.moved) *r = t.orig;   // unless the instruction wrote the register (then it holds what was loaded)
    c->EFlags &= ~DWORD(0x100);
    t.active = false;
    return EXCEPTION_CONTINUE_EXECUTION;
}

LONG CALLBACK on_fault(EXCEPTION_POINTERS* ep) {
    const EXCEPTION_RECORD* er = ep->ExceptionRecord;
    if (!g_on.load(std::memory_order_relaxed)) return EXCEPTION_CONTINUE_SEARCH;
    if (er->ExceptionCode == EXCEPTION_SINGLE_STEP) return single_step(ep);
    if (er->ExceptionCode != EXCEPTION_ACCESS_VIOLATION || er->NumberParameters < 2) return EXCEPTION_CONTINUE_SEARCH;
    const ULONG_PTR kind = er->ExceptionInformation[0];   // 0 read, 1 write, 8 execute
    const uint64_t addr = er->ExceptionInformation[1];
    if ((kind != 0 && kind != 1) || addr >= kHigh || !kernel::current_ctx()) return EXCEPTION_CONTINUE_SEARCH;
    if (addr < kLow) return kind == 0 ? redirect_read(ep, addr) : EXCEPTION_CONTINUE_SEARCH;   // the null page area can't be mapped
    const uint64_t granule = addr & ~(kGranule - 1);
    MEMORY_BASIC_INFORMATION mbi{};
    if (!VirtualQuery(reinterpret_cast<void*>(granule), &mbi, sizeof mbi)) return EXCEPTION_CONTINUE_SEARCH;
    if (mbi.State != MEM_FREE || reinterpret_cast<uint64_t>(mbi.BaseAddress) + mbi.RegionSize < granule + kGranule)
        return kind == 0 ? redirect_read(ep, addr) : EXCEPTION_CONTINUE_SEARCH;   // not wholly free (a host reservation): redirect a read
    if (vm_query(granule).state != kMemFree) return EXCEPTION_CONTINUE_SEARCH;
    const int slot = g_used.fetch_add(1, std::memory_order_relaxed);
    if (slot >= kMaxGranules) return EXCEPTION_CONTINUE_SEARCH;
    if (!VirtualAlloc(reinterpret_cast<void*>(granule), kGranule, MEM_RESERVE | MEM_COMMIT, PAGE_READWRITE)) {
        g_entries[slot].skip = true;   // the slot is taken: mark it so the logger does not wait on it for ever
        g_entries[slot].ready.store(true, std::memory_order_release);
        // another thread mapped it first (both faulted on the same granule): the access can run again if it is ours now
        if (VirtualQuery(reinterpret_cast<void*>(granule), &mbi, sizeof mbi) && mbi.State == MEM_COMMIT) return EXCEPTION_CONTINUE_EXECUTION;
        return EXCEPTION_CONTINUE_SEARCH;
    }
    Entry& e = g_entries[slot];
    e.addr = addr;
    e.rip = static_cast<uint64_t>(ep->ContextRecord->Rip);
    e.write = kind == 1;
    e.granule.store(granule, std::memory_order_relaxed);
    e.ready.store(true, std::memory_order_release);
    g_faults.fetch_add(1, std::memory_order_relaxed);
    return EXCEPTION_CONTINUE_EXECUTION;
}

void log_thread() {
    int logged = 0;
    for (;;) {
        std::this_thread::sleep_for(std::chrono::milliseconds(500));
        const int n = std::min(g_used.load(std::memory_order_relaxed), kMaxGranules);
        while (logged < n && g_entries[logged].ready.load(std::memory_order_acquire)) {
            const Entry& e = g_entries[logged++];
            if (e.skip) continue;
            const uint64_t fn = guest_function_of(e.rip);
            NXLOG_W(kTag, "unmapped %s at 0x%llx (guest function f_%llx): answered as the emulators do, %s ([memory] unmapped_access = zero; "
                    "a broken mod table is the usual cause)", e.write ? "write" : "read", static_cast<unsigned long long>(e.addr),
                    static_cast<unsigned long long>(fn),
                    e.write        ? "the write kept in zeroed memory mapped there"
                    : e.redirected ? "it reads 0 (that instruction is pointed at zeros each time; logged once)"
                                   : "it reads 0");
        }
        if (logged >= kMaxGranules) {
            NXLOG_W(kTag, "unmapped accesses: %d granules mapped, the most there may be: further ones crash", kMaxGranules);
            return;
        }
    }
}

}  // namespace

int unmapped_base_register_for_test(const uint8_t* code) { return memory_base_register(code); }

void unmapped_install() {
    static std::atomic<bool> done{false};
    if (done.exchange(true)) return;
    const std::string m = config_get_str("memory", "unmapped_access", "zero");
    if (m != "zero") {
        if (m != "crash") NXLOG_W(kTag, "[memory] unmapped_access = %s: not zero or crash, crash is used", m.c_str());
        NXLOG_I(kTag, "unmapped guest accesses crash ([memory] unmapped_access = crash)");
        return;
    }
    if (const auto* dos = reinterpret_cast<const IMAGE_DOS_HEADER*>(GetModuleHandleW(nullptr))) {
        const auto* nt = reinterpret_cast<const IMAGE_NT_HEADERS*>(reinterpret_cast<const uint8_t*>(dos) + dos->e_lfanew);
        g_exe_lo = reinterpret_cast<uint64_t>(dos);
        g_exe_hi = g_exe_lo + nt->OptionalHeader.SizeOfImage;
    }
    g_zero = static_cast<uint8_t*>(VirtualAlloc(nullptr, 2 * kZeroHalf, MEM_RESERVE | MEM_COMMIT, PAGE_READONLY));
    if (!g_zero) NXLOG_W(kTag, "unmapped guest accesses: no zero block: reads of the null page area and host reservations crash");
    g_veh = AddVectoredExceptionHandler(0, on_fault);
    if (!g_veh) {
        NXLOG_W(kTag, "unmapped guest accesses: no exception handler: they crash");
        return;
    }
    g_on.store(true, std::memory_order_relaxed);
    std::thread(log_thread).detach();
    NXLOG_I(kTag, "unmapped guest accesses below 4 GiB read 0, as on the yuzu-family emulators ([memory] unmapped_access = zero)");
}

// A fixed reservation (reserve_fixed) that overlaps granules mapped here takes them back first.
void unmapped_release(uint64_t addr, uint64_t size) {
    if (addr >= kHigh || g_used.load(std::memory_order_relaxed) == 0) return;
    const int n = std::min(g_used.load(std::memory_order_relaxed), kMaxGranules);
    for (int i = 0; i < n; ++i) {
        uint64_t g = g_entries[i].granule.load(std::memory_order_relaxed);
        const uint64_t was = g;
        if (g && g < addr + size && g + kGranule > addr && g_entries[i].granule.compare_exchange_strong(g, 0)) {
            g = was;
            VirtualFree(reinterpret_cast<void*>(g), 0, MEM_RELEASE);
            NXLOG_I(kTag, "unmapped accesses: the zeroed granule at 0x%llx given back (the guest maps there)", static_cast<unsigned long long>(g));
        }
    }
}

}  // namespace nxrt::mem
