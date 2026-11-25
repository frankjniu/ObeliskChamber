// SPDX-License-Identifier: GPL-3.0-or-later
// [diag] fault_watch: where the process' page faults land and which code takes them (declared in nxrt/gauge.h).
// A big area load takes about a million page faults (the `load` line's "page faults +N"); this splits them so it is clear whether
// touching memory ahead of time could shorten loads. Windows' working-set watch (InitializeProcessForWsWatch / GetWsChangesEx) records
// the faulting address and the faulting instruction of each fault that adds a page to the working set (soft and hard faults); a thread
// drains it every few milliseconds into hitch counters, so the `load` line and the hitch lines show the split:
//   by address: pf_heap (the guest heap's primary view), pf_pool (alias views: NVN memory pools, shared memory), pf_stack (guest
//     stacks, TLS and kernel-owned memory), pf_image (the game's and the SDK's module images), pf_host (everything else: our own
//     allocations, staging buffers, driver memory, the C runtime heap)
//   by code: pf_by_exe (ObeliskChamber.exe: the lifted game and our runtime), pf_by_driver (the GPU driver and the Vulkan loader),
//     pf_by_system (ntdll, kernel32/kernelbase, the C runtime), pf_by_kernel (kernel mode: file reads into user buffers, the driver's
//     kernel part), pf_by_other (any other module)
//   pf_seen (every fault recorded) and pf_lost (faults the kernel's buffer dropped before a drain): pf_seen against the `load` line's
//     page faults is the coverage.
// Every 60 s a log line lists the instructions with the most faults (module + offset: look them up in the .map file).
// Diagnostics only: the watch makes every page fault a little slower. Off by default. Read once.
#include "nxrt/gauge.h"

#include <windows.h>
#include <psapi.h>

#include <algorithm>
#include <atomic>
#include <cctype>
#include <cstdio>
#include <cstring>
#include <map>
#include <string>
#include <thread>
#include <unordered_map>
#include <vector>

#include "nxrt/config.h"
#include "nxrt/log.h"
#include "nxrt/memory.h"

namespace nxrt {
namespace {

enum Va { kVaHeap, kVaPool, kVaStack, kVaImage, kVaHost, kVaCount };
enum Pc { kPcExe, kPcDriver, kPcSystem, kPcKernel, kPcOther, kPcCount };

std::atomic<uint64_t> g_va[kVaCount];
std::atomic<uint64_t> g_pc[kPcCount];
std::atomic<uint64_t> g_seen{0}, g_lost{0};

int va_class(uint64_t va) {
    using namespace nxrt::mem;
    if (va >= kHeapBase && va < kHeapBase + kHeapMax) return kVaHeap;
    if (va >= kAliasBase && va < kAliasBase + kAliasSize) return kVaPool;
    if ((va >= kStackBase && va < kStackBase + kStackSize) || (va >= kKernelBase && va < kKernelBase + kKernelSize)) return kVaStack;
    if ((va >= kMainBase && va < kRemoteBase) || (va >= kSdkBase && va < kHeapBase)) return kVaImage;
    return kVaHost;
}

struct Module {
    uint64_t base = 0, end = 0;
    int cls = kPcOther;
    std::string name;
};

int name_class(std::string n) {
    for (char& c : n) c = static_cast<char>(tolower(static_cast<unsigned char>(c)));
    if (n.size() > 4 && n.compare(n.size() - 4, 4, ".exe") == 0) return kPcExe;
    static const char* sys[] = {"ntdll", "kernel32", "kernelbase", "ucrtbase", "vcruntime", "msvcp", "win32u", "user32", "gdi32",
                                "combase", "rpcrt4", "sechost", "advapi32", "bcrypt", "ws2_32"};
    for (const char* s : sys)
        if (n.rfind(s, 0) == 0) return kPcSystem;
    static const char* drv[] = {"nv", "amd", "atio", "ig", "intel", "vulkan", "dxgi", "d3d", "libvk"};
    for (const char* s : drv)
        if (n.rfind(s, 0) == 0) return kPcDriver;
    return kPcOther;
}

// The module holding `pc`, from a cache of module ranges (the drain thread only).
const Module* module_of(std::vector<Module>& mods, uint64_t pc) {
    for (const Module& m : mods)
        if (pc >= m.base && pc < m.end) return &m;
    HMODULE h = nullptr;
    if (!GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
                            reinterpret_cast<LPCWSTR>(pc), &h) || !h)
        return nullptr;
    MODULEINFO mi{};
    if (!GetModuleInformation(GetCurrentProcess(), h, &mi, sizeof mi)) return nullptr;
    char name[MAX_PATH] = {};
    GetModuleBaseNameA(GetCurrentProcess(), h, name, sizeof name);
    Module m;
    m.base = reinterpret_cast<uint64_t>(mi.lpBaseOfDll);
    m.end = m.base + mi.SizeOfImage;
    m.name = name;
    m.cls = name_class(m.name);
    mods.push_back(m);
    return &mods.back();
}

void drain_loop() {
    std::vector<PSAPI_WS_WATCH_INFORMATION_EX> buf(1 << 16);
    std::vector<Module> mods;
    mods.reserve(256);
    std::unordered_map<uint64_t, uint64_t> by_pc;     // faulting instruction -> faults, since the last summary
    uint64_t last_summary = GetTickCount64();
    for (;;) {
        DWORD cb = static_cast<DWORD>(buf.size() * sizeof buf[0]);
        if (!GetWsChangesEx(GetCurrentProcess(), buf.data(), &cb)) {
            const DWORD e = GetLastError();
            if (e == ERROR_INSUFFICIENT_BUFFER) {
                buf.resize(buf.size() * 2);
                continue;
            }
            // ERROR_NO_MORE_ITEMS: nothing since the last call
        } else {
            const size_t n = cb / sizeof buf[0];
            uint64_t seen = 0;
            for (size_t i = 0; i < n; i++) {
                const auto& w = buf[i];
                const uint64_t pc = reinterpret_cast<uint64_t>(w.BasicInfo.FaultingPc);
                const uint64_t va = reinterpret_cast<uint64_t>(w.BasicInfo.FaultingVa);
                if (!pc) {                       // the end of the list; a non-zero address there is the number of faults dropped
                    if (va && va < (1ull << 32)) g_lost.fetch_add(va, std::memory_order_relaxed);
                    break;
                }
                seen++;
                g_va[va_class(va)].fetch_add(1, std::memory_order_relaxed);
                int pcls = kPcKernel;
                if (pc < 0x0000800000000000ull) {
                    const Module* m = module_of(mods, pc);
                    pcls = m ? m->cls : kPcOther;
                }
                g_pc[pcls].fetch_add(1, std::memory_order_relaxed);
                if (by_pc.size() < 200000) by_pc[pc]++;
            }
            g_seen.fetch_add(seen, std::memory_order_relaxed);
        }
        const uint64_t now = GetTickCount64();
        if (now - last_summary >= 60000 && !by_pc.empty()) {
            last_summary = now;
            std::vector<std::pair<uint64_t, uint64_t>> v(by_pc.begin(), by_pc.end());
            const size_t top = std::min<size_t>(10, v.size());
            std::partial_sort(v.begin(), v.begin() + top, v.end(), [](const auto& a, const auto& b) { return a.second > b.second; });
            uint64_t total = 0;
            for (const auto& [pc, c] : v) total += c;
            std::string line;
            char t[160];
            for (size_t i = 0; i < top; i++) {
                const uint64_t pc = v[i].first;
                const Module* m = pc < 0x0000800000000000ull ? module_of(mods, pc) : nullptr;
                if (m) snprintf(t, sizeof t, "%s%s+0x%llx %llu", i ? ", " : "", m->name.c_str(), static_cast<unsigned long long>(pc - m->base),
                                static_cast<unsigned long long>(v[i].second));
                else snprintf(t, sizeof t, "%s0x%llx %llu", i ? ", " : "", static_cast<unsigned long long>(pc), static_cast<unsigned long long>(v[i].second));
                line += t;
            }
            NXLOG_I("faults", "page faults in the last 60 s: %llu recorded at %zu instructions; most: %s",
                    static_cast<unsigned long long>(total), v.size(), line.c_str());
            by_pc.clear();
        }
        Sleep(2);
    }
}

uint64_t rd(std::atomic<uint64_t>& a) { return a.load(std::memory_order_relaxed); }

}  // namespace

void fault_watch_start() {
    static bool started = false;
    if (started || !config_get_bool("diag", "fault_watch", false)) return;
    started = true;
    if (!InitializeProcessForWsWatch(GetCurrentProcess())) {
        NXLOG_W("faults", "[diag] fault_watch: InitializeProcessForWsWatch failed (%lu); no fault split", GetLastError());
        return;
    }
    hitch_add_counter("pf_seen", [] { return rd(g_seen); });
    hitch_add_counter("pf_lost", [] { return rd(g_lost); });
    hitch_add_counter("pf_heap", [] { return rd(g_va[kVaHeap]); });
    hitch_add_counter("pf_pool", [] { return rd(g_va[kVaPool]); });
    hitch_add_counter("pf_stack", [] { return rd(g_va[kVaStack]); });
    hitch_add_counter("pf_image", [] { return rd(g_va[kVaImage]); });
    hitch_add_counter("pf_host", [] { return rd(g_va[kVaHost]); });
    hitch_add_counter("pf_by_exe", [] { return rd(g_pc[kPcExe]); });
    hitch_add_counter("pf_by_driver", [] { return rd(g_pc[kPcDriver]); });
    hitch_add_counter("pf_by_system", [] { return rd(g_pc[kPcSystem]); });
    hitch_add_counter("pf_by_kernel", [] { return rd(g_pc[kPcKernel]); });
    hitch_add_counter("pf_by_other", [] { return rd(g_pc[kPcOther]); });
    std::thread th([] {
        SetThreadPriority(GetCurrentThread(), THREAD_PRIORITY_ABOVE_NORMAL);
        SetThreadDescription(GetCurrentThread(), L"fault-watch");
        drain_loop();
    });
    th.detach();
    NXLOG_I("faults", "[diag] fault_watch on: page faults split by address and by code (pf_* hitch counters, a summary every 60 s)");
}

}  // namespace nxrt
