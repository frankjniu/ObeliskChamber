// SPDX-License-Identifier: GPL-3.0-or-later
// `ObeliskChamber.exe --build-pipelines` (S1): the lines it prints and the status file it keeps, for the launcher (launcher/src-tauri/src/
// pipelines.rs reads the file) and for a person at a console. No Windows or Vulkan here, so a test can build it anywhere.
//
// stdout, one line each (the format of `mpr.exe --prepare-shaders`'s `prepare:` lines):
//   build-pipelines: start keys=<recorded> total=<to build> threads=<n>
//   build-pipelines: <done>/<total>                       about every 250 ms while it changes
//   build-pipelines: ready keys=<recorded>                nothing to do: every recorded key is in the cache for this GPU and driver
//   build-pipelines: done built=<n> failed=<n> total=<n> seconds=<s> median_ms=<ms> threads=<n>
//   build-pipelines: stopped built=<n> failed=<n> total=<n> seconds=<s> median_ms=<ms> threads=<n>
//   build-pipelines: error <why>
// The status file (`[boot] build_pipelines_status`, default pipeline_build.txt in the run folder) is rewritten whole with every progress
// line, as `key: value` lines: state (starting, running, ready, done, stopped, error), done, total, keys, built, failed, threads, seconds,
// median_ms, gpu, identity, message. A file named like it plus `.stop` asks the run to stop: it saves what it built and ends (state stopped).
#pragma once
#include <cstdint>
#include <cstdio>
#include <string>

namespace fefw_build {

struct Status {
    std::string state = "starting";
    uint64_t done = 0, total = 0, keys = 0, built = 0, failed = 0;
    int threads = 0;
    double seconds = 0, median_ms = 0;
    std::string gpu, identity, message;
};

// One line of a value: no line breaks (a driver name or an error text must not start a key of its own).
inline std::string one_line(const std::string& s) {
    std::string o;
    o.reserve(s.size());
    for (char c : s) o.push_back(c == '\n' || c == '\r' ? ' ' : c);
    return o;
}

inline std::string status_text(const Status& s) {
    char b[512];
    std::snprintf(b, sizeof b, "state: %s\ndone: %llu\ntotal: %llu\nkeys: %llu\nbuilt: %llu\nfailed: %llu\nthreads: %d\nseconds: %.1f\nmedian_ms: %.2f\n",
                  s.state.c_str(), static_cast<unsigned long long>(s.done), static_cast<unsigned long long>(s.total),
                  static_cast<unsigned long long>(s.keys), static_cast<unsigned long long>(s.built), static_cast<unsigned long long>(s.failed), s.threads,
                  s.seconds, s.median_ms);
    return std::string(b) + "gpu: " + one_line(s.gpu) + "\nidentity: " + one_line(s.identity) + "\nmessage: " + one_line(s.message) + "\n";
}

inline std::string progress_line(uint64_t done, uint64_t total) {
    char b[96];
    std::snprintf(b, sizeof b, "build-pipelines: %llu/%llu", static_cast<unsigned long long>(done), static_cast<unsigned long long>(total));
    return b;
}

inline std::string start_line(const Status& s) {
    char b[160];
    std::snprintf(b, sizeof b, "build-pipelines: start keys=%llu total=%llu threads=%d", static_cast<unsigned long long>(s.keys),
                  static_cast<unsigned long long>(s.total), s.threads);
    return b;
}

// The last line: ready, done, stopped or error.
inline std::string end_line(const Status& s) {
    if (s.state == "error") return "build-pipelines: error " + one_line(s.message);
    char b[256];
    if (s.state == "ready") {
        std::snprintf(b, sizeof b, "build-pipelines: ready keys=%llu", static_cast<unsigned long long>(s.keys));
        return b;
    }
    std::snprintf(b, sizeof b, "build-pipelines: %s built=%llu failed=%llu total=%llu seconds=%.1f median_ms=%.2f threads=%d", s.state.c_str(),
                  static_cast<unsigned long long>(s.built), static_cast<unsigned long long>(s.failed), static_cast<unsigned long long>(s.total),
                  s.seconds, s.median_ms, s.threads);
    return b;
}

}  // namespace fefw_build
