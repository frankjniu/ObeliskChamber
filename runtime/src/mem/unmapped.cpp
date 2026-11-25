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
    std::atomic<bool> ready{false};
};
Entry g_entries[kMaxGranules];
std::atomic<int> g_used{0};
std::atomic<uint64_t> g_faults{0};
std::atomic<bool> g_on{false};
void* g_veh = nullptr;

LONG CALLBACK on_fault(EXCEPTION_POINTERS* ep) {
    const EXCEPTION_RECORD* er = ep->ExceptionRecord;
    if (!g_on.load(std::memory_order_relaxed) || er->ExceptionCode != EXCEPTION_ACCESS_VIOLATION || er->NumberParameters < 2)
        return EXCEPTION_CONTINUE_SEARCH;
    const ULONG_PTR kind = er->ExceptionInformation[0];   // 0 read, 1 write, 8 execute
    const uint64_t addr = er->ExceptionInformation[1];
    if ((kind != 0 && kind != 1) || addr < kLow || addr >= kHigh || !kernel::current_ctx()) return EXCEPTION_CONTINUE_SEARCH;
    const uint64_t granule = addr & ~(kGranule - 1);
    MEMORY_BASIC_INFORMATION mbi{};
    if (!VirtualQuery(reinterpret_cast<void*>(granule), &mbi, sizeof mbi) || mbi.State != MEM_FREE ||
        reinterpret_cast<uint64_t>(mbi.BaseAddress) + mbi.RegionSize < granule + kGranule)
        return EXCEPTION_CONTINUE_SEARCH;   // not wholly free: someone's (or one of ours mapped by another thread just now: run again)
    if (vm_query(granule).state != kMemFree) return EXCEPTION_CONTINUE_SEARCH;
    const int slot = g_used.fetch_add(1, std::memory_order_relaxed);
    if (slot >= kMaxGranules) return EXCEPTION_CONTINUE_SEARCH;
    if (!VirtualAlloc(reinterpret_cast<void*>(granule), kGranule, MEM_RESERVE | MEM_COMMIT, PAGE_READWRITE)) {
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
            const uint64_t fn = guest_function_of(e.rip);
            NXLOG_W(kTag, "unmapped %s at 0x%llx (guest function f_%llx): answered as the emulators do, %s ([memory] unmapped_access = zero; "
                    "a broken mod table is the usual cause)", e.write ? "write" : "read", static_cast<unsigned long long>(e.addr),
                    static_cast<unsigned long long>(fn), e.write ? "the write kept in zeroed memory mapped there" : "it reads 0");
        }
        if (logged >= kMaxGranules) {
            NXLOG_W(kTag, "unmapped accesses: %d granules mapped, the most there may be: further ones crash", kMaxGranules);
            return;
        }
    }
}

}  // namespace

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
