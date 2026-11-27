// SPDX-License-Identifier: GPL-3.0-or-later
// [gfx] present_log (D1): see present_log.h.
#include "present_log.h"

#include <algorithm>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <deque>
#include <mutex>
#include <thread>
#include <vector>

#include "nxrt/config.h"
#include "nxrt/gfx.h"
#include "nxrt/log.h"
#include "nxrt/platform.h"
#include "extras/framegen.h"

namespace nxrt::gfx {
namespace {
constexpr const char* kTag = "gfx.present";
constexpr uint64_t kWaitNs = 10000000ull;          // one vkWaitForPresentKHR call at most: swapchain_going waits for it, on the render thread
constexpr uint64_t kGiveUpNs = 2000000000ull;      // a present not shown after this long is dropped from the log
constexpr int64_t kLogEveryNs = 60000000000ll;

int64_t now_ns() {
    return std::chrono::duration_cast<std::chrono::nanoseconds>(std::chrono::steady_clock::now().time_since_epoch()).count();
}
}  // namespace

struct PresentLog::Impl {
    VkDevice dev = VK_NULL_HANDLE;
    std::thread th;
    std::mutex qmu;                   // the queue
    std::condition_variable qcv;
    struct Item { VkSwapchainKHR sc; uint64_t id; int64_t queued_ns; uint64_t gen; };
    std::deque<Item> q;
    bool stop = false;
    std::mutex wait_mu;               // held by the waiter around each vkWaitForPresentKHR
    // under qmu: goes up each time a swapchain goes (swapchain_going). An item queued before that is never waited on: a single "gone"
    // handle could be overwritten by a second swapchain going while the waiter still held an item of the first, or match a new
    // swapchain that got the same handle value.
    uint64_t gen = 0;
    // ids (render thread, under the queue lock of the device)
    uint64_t last_id = 0;
    bool broken = false;              // an id would have gone backwards (low latency took ids of its own): the log stopped
    bool fg_off_logged = false;
    bool track = false;               // the id id_for_present gave last is one to wait for
    // statistics (waiter thread)
    int64_t last_shown = 0;
    VkSwapchainKHR last_sc = VK_NULL_HANDLE;
    uint64_t n = 0, b1 = 0, b2 = 0, b3 = 0, together = 0, not_shown = 0;
    double sum_ms = 0, max_ms = 0, lat_sum_ms = 0, lat_max_ms = 0;
    uint64_t lat_n = 0;
    int64_t window_start = 0;

    void record(const Item& it, int64_t shown) {
        if (!window_start) window_start = shown;
        const double lat = static_cast<double>(shown - it.queued_ns) * 1e-6;
        lat_sum_ms += lat;
        lat_max_ms = std::max(lat_max_ms, lat);
        ++lat_n;
        if (last_shown && last_sc == it.sc) {
            const double d = static_cast<double>(shown - last_shown) * 1e-6;
            float hz = platform::display_refresh_hz();
            if (hz < 20.0f || hz > 1000.0f) hz = 60.0f;
            const double t = 1000.0 / hz;
            ++n;
            sum_ms += d;
            max_ms = std::max(max_ms, d);
            if (d < 0.5 * t) ++together;
            else if (d < 1.5 * t) ++b1;
            else if (d < 2.5 * t) ++b2;
            else ++b3;
        }
        last_shown = shown;
        last_sc = it.sc;
        if (shown - window_start >= kLogEveryNs) {
            float hz = platform::display_refresh_hz();
            const double pct = n ? 100.0 / static_cast<double>(n) : 0.0;
            const uint64_t top = std::max({b1, b2, b3});
            NXLOG_I(kTag, "present wait (%.0f s): %llu frames shown; intervals in refreshes of the %.1f Hz display: 1: %.1f%%, 2: %.1f%%, 3 or more: %.1f%%, "
                          "under half a refresh (replaced, or shown together): %.1f%%; evenly paced (the most common interval) %.1f%%; mean interval %.2f ms, "
                          "longest %.1f ms; present call to screen %.1f ms on average, %.1f at most; %llu not seen shown ([gfx] present_log)",
                    static_cast<double>(shown - window_start) * 1e-9, static_cast<unsigned long long>(n + 1), hz, b1 * pct, b2 * pct, b3 * pct, together * pct,
                    top * pct, n ? sum_ms / static_cast<double>(n) : 0.0, max_ms, lat_n ? lat_sum_ms / static_cast<double>(lat_n) : 0.0, lat_max_ms,
                    static_cast<unsigned long long>(not_shown));
            n = b1 = b2 = b3 = together = not_shown = lat_n = 0;
            sum_ms = max_ms = lat_sum_ms = lat_max_ms = 0;
            window_start = shown;
        }
    }

    void run() {
        for (;;) {
            Item it;
            {
                std::unique_lock<std::mutex> lk(qmu);
                qcv.wait(lk, [&] { return stop || !q.empty(); });
                if (stop) return;
                it = q.front();
            }
            bool done = false;
            for (;;) {
                {
                    std::lock_guard<std::mutex> lk(qmu);
                    if (stop) return;
                    if (it.gen != gen) break;
                }
                VkResult r;
                {
                    std::lock_guard<std::mutex> wl(wait_mu);
                    {
                        std::lock_guard<std::mutex> lk(qmu);
                        if (it.gen != gen) break;
                    }
                    r = vkWaitForPresentKHR(dev, it.sc, it.id, kWaitNs);
                }
                if (r == VK_SUCCESS || r == VK_SUBOPTIMAL_KHR) {
                    record(it, now_ns());
                    done = true;
                    break;
                }
                if (r != VK_TIMEOUT || static_cast<uint64_t>(now_ns() - it.queued_ns) > kGiveUpNs) break;
            }
            if (!done) ++not_shown;
            std::lock_guard<std::mutex> lk(qmu);
            if (!q.empty() && q.front().id == it.id && q.front().sc == it.sc) q.pop_front();
        }
    }
};

std::unique_ptr<PresentLog> PresentLog::create(Device& dev) {
    if (!dev.features().present_wait || !config_get_bool("gfx", "present_log", true)) return nullptr;
    return std::unique_ptr<PresentLog>(new PresentLog(dev));
}

PresentLog::PresentLog(Device& dev) : impl_(new Impl) {
    impl_->dev = dev.device();
    impl_->th = std::thread([this] {
        log_set_thread_name("present-wait");
        impl_->run();
    });
    NXLOG_I(kTag, "present wait log on: when each frame reached the screen, a summary every 60 s ([gfx] present_log)");
}

PresentLog::~PresentLog() {
    {
        std::lock_guard<std::mutex> lk(impl_->qmu);
        impl_->stop = true;
    }
    impl_->qcv.notify_all();
    if (impl_->th.joinable()) impl_->th.join();
}

uint64_t PresentLog::id_for_present(uint64_t reflex_id) {
    Impl& s = *impl_;
    s.track = false;
    if (s.broken) return reflex_id;
    if (extras::framegen_requested()) {   // a frame generation wrapper may own the swapchain: no waits on it
        if (!s.fg_off_logged) {
            s.fg_off_logged = true;
            NXLOG_I(kTag, "present wait log: off while frame generation is on");
        }
        return reflex_id;
    }
    const uint64_t id = reflex_id ? reflex_id : s.last_id + 1;
    if (id <= s.last_id) {                // low latency's own ids are behind ours: they are sent as they are, and the log stops
        s.broken = true;
        NXLOG_I(kTag, "present wait log: stopped (present ids taken by low latency went backwards)");
        return reflex_id;
    }
    s.last_id = id;
    s.track = true;
    return id;
}

void PresentLog::presented(VkSwapchainKHR sc, uint64_t id) {
    Impl& s = *impl_;
    if (!id || !sc || !s.track) return;
    {
        std::lock_guard<std::mutex> lk(s.qmu);
        if (s.q.size() >= 64) s.q.pop_front();         // the waiter is far behind (a hung display): keep the newest
        s.q.push_back({sc, id, now_ns(), s.gen});
    }
    s.qcv.notify_one();
}

void PresentLog::swapchain_going(VkSwapchainKHR sc) {
    if (!sc) return;
    Impl& s = *impl_;
    {
        std::lock_guard<std::mutex> lk(s.qmu);
        ++s.gen;
        s.q.erase(std::remove_if(s.q.begin(), s.q.end(), [&](const Impl::Item& i) { return i.sc == sc; }), s.q.end());
    }
    std::lock_guard<std::mutex> wl(s.wait_mu);   // the waiter is out of any wait on it
    s.last_id = 0;                               // a new swapchain starts its ids again
    s.last_sc = VK_NULL_HANDLE;
}

}  // namespace nxrt::gfx
