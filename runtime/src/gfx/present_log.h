// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
// [gfx] present_log (D1): when each presented image actually reached the screen, from VK_KHR_present_wait, logged as how evenly frames
// were shown. A thread of its own waits for the present ids in turn (vkWaitForPresentKHR) and notes when each one was shown; every 60 s a
// line gives the intervals between shown frames in refreshes of the display (1, 2, 3 or more), frames shown together with the next one
// (replaced before a refresh), and the time from the present call to the screen. Off while frame generation is asked for: the wrappers own
// the swapchain then (NVIDIA crashed in vkWaitForPresentKHR under one, per Super Odyssey PC Recomp's notes).
#include <cstdint>
#include <memory>

#include <volk.h>

namespace nxrt::gfx {

class Device;

class PresentLog {
public:
    // Null when off: the device has no present wait, or [gfx] present_log = false.
    static std::unique_ptr<PresentLog> create(Device& dev);
    ~PresentLog();
    PresentLog(const PresentLog&) = delete;
    PresentLog& operator=(const PresentLog&) = delete;

    // The id to attach to this present (VkPresentIdKHR), 0 for none. reflex_id: the id low latency already gives the present (0: none),
    // which is then used too. Call before vkQueuePresentKHR, under the queue lock.
    uint64_t id_for_present(uint64_t reflex_id);
    // The present with that id was queued (vkQueuePresentKHR returned success or suboptimal).
    void presented(VkSwapchainKHR sc, uint64_t id);
    // Before the swapchain is destroyed: the waiter lets go of it (waits for at most one 50 ms wait).
    void swapchain_going(VkSwapchainKHR sc);

private:
    struct Impl;
    explicit PresentLog(Device& dev);
    std::unique_ptr<Impl> impl_;
};

}  // namespace nxrt::gfx
