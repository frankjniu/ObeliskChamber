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
// heap segment). For an access by the lifted game's code there (the function holding the instruction is a registered lifted
// function), the handler points the instruction's base register at a block of zeros of the faulting thread's own for one
// instruction: it decodes only the instruction's memory operand (prefixes, REX / VEX / EVEX, ModRM, SIB), moves the base register by
// (block - fault address), sets the trap flag, and on the single-step that follows puts the register back unless the instruction
// wrote it, and zeroes the block around where it pointed. A read reads 0, a write is dropped (as on the emulators: a store into a
// null object such as [x0 + 0x18]), every other register and the game's memory are untouched, and nothing of the host's is committed
// or changed. Faults outside the lifted code, RIP-relative and segment-prefixed operands, and operands whose base register can't be
// moved crash as before. Each guest instruction answered this way is logged once.
#include "nxrt/memory.h"

#define WIN32_LEAN_AND_MEAN
#include <windows.h>

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstring>
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

// Only instructions on an allowlist are redirected: plain loads into a general register, compares and tests against memory, x87
// loads and stores, SSE / AVX / AVX-512 instructions whose ModRM.reg names a vector register (loads and stores), and stores and
// read-modify-writes of memory (mov m, r / imm; op m, r / imm; inc, dec, not, neg, shifts; setcc; xchg, xadd, cmpxchg; movnti).
// What the instruction does with the general register named by ModRM.reg decides whether it may share the base register: a load's
// destination may (its new value is kept), a register only stored may (the write is dropped), a register read as a value may not (it
// would see the moved pointer, and so would the flags). An instruction that uses rax or rcx without naming it (cmpxchg, shifts by
// cl) is refused when that register is the base.
enum class Role { kNone, kDest, kDestPartial, kSrc, kSrcDest, kStore };   // kStore: ModRM.reg only stored to memory

struct Operand {
    int base = -1;      // the memory operand's base register (0..15), -1: can't be redirected
    int reg = -1;       // the general register ModRM.reg names (0..15), -1 if it names none
    Role role = Role::kNone;
    bool writes = false; // a general-purpose instruction that writes memory (a lock prefix is allowed only on those)
};

bool vector_op_map1(uint8_t op, bool p66, bool pf3) {   // 0F xx (legacy SSE, VEX map 1, EVEX map 1): ModRM.reg is a vector register
    if (op == 0x10 || op == 0x12 || op == 0x14 || op == 0x15 || op == 0x16 || op == 0x28 || op == 0x2a || op == 0x2e || op == 0x2f)
        return true;
    if (op >= 0x51 && op <= 0x6f) return true;
    if (op == 0x70 || op == 0x74 || op == 0x75 || op == 0x76 || op == 0xc2 || op == 0xc4 || op == 0xc6 || op == 0xe6) return true;
    if (op == 0x11 || op == 0x13 || op == 0x17 || op == 0x29 || op == 0x2b || op == 0x7e || op == 0x7f) return true;   // and stores
    if (op >= 0xd1 && op <= 0xfe) return op != 0xd7 && op != 0xf7;   // not pmovmskb (no memory), maskmov (writes at rdi)
    return false;
}

Operand decode_operand(const uint8_t* ip) {
    Operand o;
    int i = 0;
    bool p66 = false, pf2 = false, pf3 = false, lock = false;
    for (; i < 15; ++i) {   // legacy prefixes
        const uint8_t b = ip[i];
        if (b == 0x66) { p66 = true; continue; }
        if (b == 0xf2) { pf2 = true; continue; }
        if (b == 0xf3) { pf3 = true; continue; }
        if (b == 0xf0) { lock = true; continue; }
        if (b == 0x26 || b == 0x2e || b == 0x36 || b == 0x3e || b == 0x64 || b == 0x65 || b == 0x67) return o;   // segment / addr32
        break;
    }
    int rex_b = 0, rex_x = 0, rex_r = 0;
    bool rex = false;
    uint8_t op = ip[i];
    if (op >= 0x40 && op <= 0x4f) {
        rex = true;
        rex_b = op & 1;
        rex_x = (op >> 1) & 1;
        rex_r = (op >> 2) & 1;
        op = ip[++i];
    }
    int map = 0;          // 0: one-byte, 1: 0F, 2: 0F 38, 3: 0F 3A
    bool vex = false;     // VEX / EVEX: ModRM.reg of an allowed op is a vector register
    uint8_t opc = op;
    if (op == 0xc5) {
        vex = true;
        map = 1;
        rex_r = !((ip[i + 1] >> 7) & 1);
        const uint8_t pp = ip[i + 1] & 3;
        p66 = pp == 1; pf3 = pp == 2; pf2 = pp == 3;
        opc = ip[i + 2];
        i += 3;
    } else if (op == 0xc4 || op == 0x62) {
        vex = true;
        rex_r = !((ip[i + 1] >> 7) & 1);
        rex_b = !((ip[i + 1] >> 5) & 1);
        rex_x = !((ip[i + 1] >> 6) & 1);
        map = ip[i + 1] & (op == 0x62 ? 0x07 : 0x1f);
        const uint8_t pp = ip[i + 2] & 3;
        p66 = pp == 1; pf3 = pp == 2; pf2 = pp == 3;
        opc = ip[i + (op == 0x62 ? 4 : 3)];
        i += op == 0x62 ? 5 : 4;
    } else if (op == 0x0f) {
        const uint8_t op2 = ip[i + 1];
        if (op2 == 0x38 || op2 == 0x3a) {
            map = op2 == 0x38 ? 2 : 3;
            opc = ip[i + 2];
            i += 3;
        } else {
            map = 1;
            opc = op2;
            i += 2;
        }
    } else {
        i += 1;
    }
    const uint8_t modrm = ip[i];
    const int mod = modrm >> 6, rm = modrm & 7, regf = (modrm >> 3) & 7;
    const int reg = regf | (rex_r << 3);
    const bool p16 = p66 && !vex;   // operand size 16 for the general-register forms
    // Classify: is it allowed, and what is ModRM.reg to it?
    bool byte_op = false, uses_rax = false, uses_rcx = false;
    if (map == 0) {
        if (op == 0x8b) o.role = p16 ? Role::kDestPartial : Role::kDest;
        else if (op == 0x8a) { o.role = Role::kDestPartial; byte_op = true; }
        else if (op == 0x63 || op == 0x69 || op == 0x6b) o.role = p16 ? Role::kDestPartial : Role::kDest;
        else if (op < 0x40 && (op & 7) >= 2 && (op & 7) <= 3 && (op & 0x38) != 0x38) { o.role = Role::kSrcDest; byte_op = !(op & 1); }  // add/or/adc/sbb/and/sub/xor r, m
        else if (op == 0x3a || op == 0x3b || op == 0x38 || op == 0x39 || op == 0x84 || op == 0x85) { o.role = Role::kSrc; byte_op = !(op & 1); }  // cmp / test
        else if ((op == 0x80 || op == 0x81 || op == 0x83) && regf == 7) o.role = Role::kNone;   // cmp m, imm
        else if ((op == 0xf6 || op == 0xf7) && regf <= 1) o.role = Role::kNone;                 // test m, imm
        else if ((op == 0xd9 && (regf == 4 || regf == 5)) || (op == 0xdd && regf == 4)) return o;   // fldenv / fldcw / frstor: zeros would set the host's FPU state
        else if (op >= 0xd8 && op <= 0xdf) o.role = Role::kNone;                                // x87 loads and stores
        else if (op == 0x88 || op == 0x89) { o.role = Role::kStore; o.writes = true; byte_op = op == 0x88; }   // mov m, r
        else if ((op == 0xc6 || op == 0xc7) && regf == 0) o.writes = true;                      // mov m, imm
        else if (op < 0x40 && (op & 7) <= 1 && (op & 0x38) != 0x38) { o.role = Role::kSrc; o.writes = true; byte_op = !(op & 1); }  // add/or/adc/sbb/and/sub/xor m, r
        else if (op == 0x80 || op == 0x81 || op == 0x83) o.writes = true;                        // the same, m, imm
        else if ((op == 0xf6 || op == 0xf7) && (regf == 2 || regf == 3)) o.writes = true;       // not / neg m
        else if ((op == 0xfe || op == 0xff) && regf <= 1) o.writes = true;                      // inc / dec m
        else if (op == 0xc0 || op == 0xc1 || op == 0xd0 || op == 0xd1) { if (regf == 6) return o; o.writes = true; }   // shifts by imm / 1
        else if (op == 0xd2 || op == 0xd3) { if (regf == 6) return o; o.writes = true; uses_rcx = true; }            // shifts by cl
        else if (op == 0x86 || op == 0x87) { o.role = op == 0x86 || p16 ? Role::kDestPartial : Role::kDest; o.writes = true; byte_op = op == 0x86; }  // xchg: the register gets the 0
        else return o;
    } else if (!vex && map == 1) {
        if (opc == 0xb6 || opc == 0xb7 || opc == 0xbe || opc == 0xbf) o.role = p16 ? Role::kDestPartial : Role::kDest;
        else if ((opc >= 0x40 && opc <= 0x4f) || opc == 0xaf || opc == 0xbc || opc == 0xbd || (opc == 0xb8 && pf3)) o.role = Role::kSrcDest;
        else if ((opc == 0x2c || opc == 0x2d) && (pf2 || pf3)) o.role = Role::kDest;   // cvt(t)ss/sd2si r, m (no prefix: to MMX)
        else if (opc >= 0x90 && opc <= 0x9f) o.writes = true;                                   // setcc m8
        else if (opc == 0xc3) { o.role = Role::kStore; o.writes = true; }                       // movnti m, r
        else if (opc == 0xc0 || opc == 0xc1) { o.role = opc == 0xc0 || p16 ? Role::kDestPartial : Role::kDest; o.writes = true; byte_op = opc == 0xc0; }  // xadd: the register gets the old 0
        else if (opc == 0xb0 || opc == 0xb1) { o.role = Role::kStore; o.writes = true; uses_rax = true; byte_op = opc == 0xb0; }  // cmpxchg: rax gets the 0 when they differ
        else if (opc == 0xba && regf >= 4) o.writes = regf > 4;                                 // bt / bts / btr / btc m, imm
        else if (vector_op_map1(opc, p66, pf3)) o.role = Role::kNone;
        else return o;
    } else if (map == 1) {   // VEX / EVEX map 1
        if ((opc == 0x2c || opc == 0x2d) && (pf2 || pf3)) o.role = Role::kDest;
        else if (vector_op_map1(opc, p66, pf3)) o.role = Role::kNone;
        else return o;
    } else if (map == 2) {
        if (opc >= 0xf0) return o;   // movbe / crc32 / BMI: general registers
        if ((opc >= 0x90 && opc <= 0x93) || (opc >= 0xa0 && opc <= 0xa3)) return o;   // gathers / scatters: their lanes move with the base
        if (opc == 0x49 || opc == 0x4b) return o;   // AMX tile loads / stores: rows strided far from the base
        o.role = Role::kNone;
    } else if (map == 3) {
        if (opc == 0xf0 || (opc >= 0x60 && opc <= 0x63)) return o;   // rorx; pcmp[ei]str[im] use rax/rcx/rdx
        o.role = Role::kNone;
    } else {
        return o;
    }
    if (o.role != Role::kNone) {
        o.reg = reg;
        if (byte_op && !rex && regf >= 4) o.reg = regf - 4;   // ah / ch / dh / bh: a byte of rax..rbx
    }
    // The memory operand.
    if (mod == 3) return o;                 // a register operand: not this fault
    if (rm == 5 && mod == 0) return o;      // RIP-relative
    int b;
    if (rm != 4) {
        b = rm | (rex_b << 3);
    } else {
        const uint8_t sib = ip[i + 1];
        const int base = sib & 7, index = ((sib >> 3) & 7) | (rex_x << 3);
        if (base == 5 && mod == 0) return o;    // no base register
        b = base | (rex_b << 3);
        if (index != 4 && index == b) return o; // [r + r*s]: moving it moves both
    }
    if (b == 4) return o;                   // rsp: never
    if (o.reg == b && o.role != Role::kDest && o.role != Role::kStore) return o;   // the base read as a value, or partly overwritten
    if ((uses_rax && b == 0) || (uses_rcx && b == 1)) return o;   // the instruction uses the base register without naming it
    if (lock && !o.writes) return o;        // lock on anything else: not one of ours
    o.base = b;
    return o;
}

// The instruction replaces the base register with what it read (a load into it, xchg / xadd): keep that. A register only stored
// (kStore with reg == base) is the moved pointer's old value: it must be put back like any other.
bool keeps_base(const Operand& o) { return o.role == Role::kDest && o.reg == o.base; }

DWORD64* context_register(CONTEXT* c, int r) {
    switch (r) {
    case 0: return &c->Rax; case 1: return &c->Rcx; case 2: return &c->Rdx; case 3: return &c->Rbx;
    case 5: return &c->Rbp; case 6: return &c->Rsi; case 7: return &c->Rdi;
    case 8: return &c->R8; case 9: return &c->R9; case 10: return &c->R10; case 11: return &c->R11;
    case 12: return &c->R12; case 13: return &c->R13; case 14: return &c->R14; case 15: return &c->R15;
    default: return nullptr;   // rsp and anything else: never
    }
}

// Per guest thread, made at its first redirect (top down, away from the low addresses): 128 KiB of zeros, accesses are pointed at its middle. After each redirected
// instruction the bytes around the middle are zeroed again (a write is dropped), so every read through it sees 0; being the thread's
// own, no other thread's write can show through. Kept for the thread's life (a thread that exits leaves its 128 KiB behind).
thread_local uint8_t* t_sink = nullptr;
constexpr uint64_t kSinkHalf = 0x10000, kSinkWipe = 0x1000;   // wiped: kSinkWipe on each side of the middle

// Lifted code only: the function containing the faulting instruction (its unwind entry) must start at a registered lifted
// function (a leaf with no unwind entry: a lifted function must start within 16 KiB below it). The snapshot of their host addresses is rebuilt by the log thread whenever more are registered.
std::atomic<const std::vector<uint64_t>*> g_lifted{nullptr};

bool in_lifted_code(uint64_t rip) {
    const std::vector<uint64_t>* v = g_lifted.load(std::memory_order_acquire);
    if (!v || v->empty()) return false;
    DWORD64 image = 0;
    if (const RUNTIME_FUNCTION* f = RtlLookupFunctionEntry(rip, &image, nullptr))
        return std::binary_search(v->begin(), v->end(), image + f->BeginAddress);
    // No unwind entry: a leaf function (no frame, no saved registers). Accept it when the nearest lifted function at or below
    // the instruction starts close by AND no other function's unwind entry starts between that lifted start and the instruction
    // (otherwise the leaf may be someone else's, such as a CRT helper without unwind data placed after it).
    auto it = std::upper_bound(v->begin(), v->end(), rip);
    if (it == v->begin() || rip - *(it - 1) >= 0x4000) return false;
    const uint64_t lifted = *(it - 1);
    void* base = nullptr;
    if (!RtlPcToFileHeader(reinterpret_cast<void*>(rip), &base) || !base) return false;
    const auto* dos = static_cast<const IMAGE_DOS_HEADER*>(base);
    const auto* nt = reinterpret_cast<const IMAGE_NT_HEADERS*>(static_cast<const uint8_t*>(base) + dos->e_lfanew);
    const IMAGE_DATA_DIRECTORY& dir = nt->OptionalHeader.DataDirectory[IMAGE_DIRECTORY_ENTRY_EXCEPTION];
    const auto* fns = reinterpret_cast<const RUNTIME_FUNCTION*>(static_cast<const uint8_t*>(base) + dir.VirtualAddress);
    size_t lo = 0, hi = dir.Size / sizeof(RUNTIME_FUNCTION);   // sorted by BeginAddress: the last entry starting at or below rip
    const uint64_t rva = rip - reinterpret_cast<uint64_t>(base);
    while (lo < hi) {
        const size_t mid = (lo + hi) / 2;
        if (fns[mid].BeginAddress <= rva) lo = mid + 1; else hi = mid;
    }
    if (lo == 0) return true;
    const uint64_t last = reinterpret_cast<uint64_t>(base) + fns[lo - 1].BeginAddress;
    return last <= lifted;   // the last function with unwind data before the instruction is the lifted one or earlier
}

struct Redirect {
    bool active = false;
    int reg = -1;
    bool keep = false;   // the instruction loads into the base register: keep what it wrote
    DWORD64 orig = 0;
    uint64_t rip = 0;
};
thread_local Redirect t_redirect;

void wipe_sink() { std::memset(t_sink + kSinkHalf - kSinkWipe, 0, 2 * kSinkWipe); }

// One logged line per guest instruction answered by a redirect (they can repeat every frame), in their own table.
constexpr int kRipSet = 8192, kMaxRedirectLogs = 1024;
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
Entry g_redirect_logs[kMaxRedirectLogs];
std::atomic<int> g_redirect_used{0};

LONG redirect(EXCEPTION_POINTERS* ep, uint64_t addr, bool write) {
    CONTEXT* c = ep->ContextRecord;
    const uint64_t rip = static_cast<uint64_t>(c->Rip);
    if (t_redirect.active || !in_lifted_code(rip)) return EXCEPTION_CONTINUE_SEARCH;
    if (addr >= kLow && vm_query(addr & ~uint64_t(0xfff)).state != kMemFree) return EXCEPTION_CONTINUE_SEARCH;   // the guest's own
    const Operand o = decode_operand(reinterpret_cast<const uint8_t*>(rip));
    DWORD64* r = o.base >= 0 ? context_register(c, o.base) : nullptr;
    if (!r) return EXCEPTION_CONTINUE_SEARCH;
    if (!t_sink) {
        // top down: away from the low addresses broken mod offsets point at (and the guest might map)
        t_sink = static_cast<uint8_t*>(VirtualAlloc(nullptr, 2 * kSinkHalf, MEM_RESERVE | MEM_COMMIT | MEM_TOP_DOWN, PAGE_READWRITE));
        if (!t_sink) return EXCEPTION_CONTINUE_SEARCH;
    }
    const uint64_t target = reinterpret_cast<uint64_t>(t_sink) + kSinkHalf;
    t_redirect.active = true;
    t_redirect.reg = o.base;
    t_redirect.keep = keeps_base(o);
    t_redirect.orig = *r;
    t_redirect.rip = rip;
    *r = *r + (target - addr);   // the operand's address becomes the block's middle (± the access width)
    c->EFlags |= 0x100;          // trap flag: the single-step after this one instruction puts the register back
    if (first_time_rip(rip)) {
        const int slot = g_redirect_used.fetch_add(1, std::memory_order_relaxed);
        if (slot < kMaxRedirectLogs) {
            Entry& e = g_redirect_logs[slot];
            e.addr = addr;
            e.rip = rip;
            e.redirected = true;
            e.write = write;
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
    if (r && !t.keep) *r = t.orig;   // the instruction did not write the base register: give it back its value
    c->EFlags &= ~DWORD(0x100);
    t.active = false;
    wipe_sink();                     // what a write put there is dropped
    return EXCEPTION_CONTINUE_EXECUTION;
}

// Another exception while a redirect is pending (the instruction faulted otherwise, e.g. a divide by the 0 it read): put the
// register back in what the crash report and any handler see, and forget the redirect.
void abandon_redirect(EXCEPTION_POINTERS* ep) {
    Redirect& t = t_redirect;
    if (!t.active) return;
    CONTEXT* c = ep->ContextRecord;
    if (static_cast<uint64_t>(c->Rip) == t.rip) {
        if (DWORD64* r = context_register(c, t.reg)) *r = t.orig;
        c->EFlags &= ~DWORD(0x100);
    }
    t.active = false;
    wipe_sink();
}

LONG CALLBACK on_fault(EXCEPTION_POINTERS* ep) {
    const EXCEPTION_RECORD* er = ep->ExceptionRecord;
    if (!g_on.load(std::memory_order_relaxed)) return EXCEPTION_CONTINUE_SEARCH;
    if (er->ExceptionCode == EXCEPTION_SINGLE_STEP) return single_step(ep);
    if (t_redirect.active) abandon_redirect(ep);
    if (er->ExceptionCode != EXCEPTION_ACCESS_VIOLATION || er->NumberParameters < 2) return EXCEPTION_CONTINUE_SEARCH;
    const ULONG_PTR kind = er->ExceptionInformation[0];   // 0 read, 1 write, 8 execute
    const uint64_t addr = er->ExceptionInformation[1];
    if ((kind != 0 && kind != 1) || addr >= kHigh || !kernel::current_ctx()) return EXCEPTION_CONTINUE_SEARCH;
    if (addr < kLow) return redirect(ep, addr, kind == 1);   // the null page area can't be mapped
    const uint64_t granule = addr & ~(kGranule - 1);
    MEMORY_BASIC_INFORMATION mbi{};
    if (!VirtualQuery(reinterpret_cast<void*>(granule), &mbi, sizeof mbi)) return EXCEPTION_CONTINUE_SEARCH;
    if (mbi.State != MEM_FREE || reinterpret_cast<uint64_t>(mbi.BaseAddress) + mbi.RegionSize < granule + kGranule)
        return redirect(ep, addr, kind == 1);   // not wholly free (a host reservation): can't map either
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

void log_entry(const Entry& e) {
    const uint64_t fn = guest_function_of(e.rip);
    NXLOG_W(kTag, "unmapped %s at 0x%llx (guest function f_%llx): answered as the emulators do, %s ([memory] unmapped_access = zero; "
            "a broken mod table is the usual cause)", e.write ? "write" : "read", static_cast<unsigned long long>(e.addr),
            static_cast<unsigned long long>(fn),
            e.redirected ? (e.write ? "the write is dropped (that instruction is pointed at scratch zeros each time; logged once)"
                                    : "it reads 0 (that instruction is pointed at zeros each time; logged once)")
            : e.write    ? "the write kept in zeroed memory mapped there"
                         : "it reads 0");
}

void log_thread() {
    int logged = 0, logged_redirects = 0;
    uint64_t lifted_for = 0, last_count = ~uint64_t(0);
    const std::vector<uint64_t>* retired = nullptr;
    bool full_said = false;
    for (int tick = 0;; ++tick) {
        std::this_thread::sleep_for(std::chrono::milliseconds(100));
        // Lifted functions registered since the last snapshot (modules load at boot, mods can add replacements): publish a new
        // sorted list of their host addresses for in_lifted_code. Old lists are kept (a reader may hold one); they are few.
        // Rebuilt once the registrations have held still for a tick (modules register thousands at boot); the generation also
        // changes when a mod replaces a function at an existing address. The list from two rebuilds ago is freed: a reader holds
        // one only for the length of one fault, and rebuilds are at least 100 ms apart.
        const uint64_t gen = fn_generation();
        if (gen != lifted_for && gen == last_count) {
            const std::vector<uint64_t>* old = g_lifted.exchange(new std::vector<uint64_t>(host_function_starts()), std::memory_order_acq_rel);
            delete retired;
            retired = old;
            lifted_for = gen;
        }
        last_count = gen;
        if (tick % 5) continue;   // log every 500 ms
        const int n = std::min(g_used.load(std::memory_order_relaxed), kMaxGranules);
        while (logged < n && g_entries[logged].ready.load(std::memory_order_acquire)) {
            const Entry& e = g_entries[logged++];
            if (!e.skip) log_entry(e);
        }
        const int nr = std::min(g_redirect_used.load(std::memory_order_relaxed), kMaxRedirectLogs);
        while (logged_redirects < nr && g_redirect_logs[logged_redirects].ready.load(std::memory_order_acquire))
            log_entry(g_redirect_logs[logged_redirects++]);
        if (logged >= kMaxGranules && !full_said) {
            NXLOG_W(kTag, "unmapped accesses: %d granules mapped, the most there may be: further ones crash", kMaxGranules);
            full_said = true;
        }
    }
}

}  // namespace

int unmapped_base_register_for_test(const uint8_t* code) { return decode_operand(code).base; }
bool unmapped_keeps_base_for_test(const uint8_t* code) { const Operand o = decode_operand(code); return o.base >= 0 && keeps_base(o); }

void unmapped_install() {
    static std::atomic<bool> done{false};
    if (done.exchange(true)) return;
    const std::string m = config_get_str("memory", "unmapped_access", "zero");
    if (m != "zero") {
        if (m != "crash") NXLOG_W(kTag, "[memory] unmapped_access = %s: not zero or crash, crash is used", m.c_str());
        NXLOG_I(kTag, "unmapped guest accesses crash ([memory] unmapped_access = crash)");
        return;
    }
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
