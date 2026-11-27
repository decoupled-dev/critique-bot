#pragma once

#include <functional>
#include <string>
#include <unordered_set>

#include "sim/timer.h"
#include "sim/timer_queue.h"

namespace sim {

class Scheduler {
public:
    Tick now() const { return now_; }

    // Run `action` once at tick `due` (clamped to now()).
    TimerId at(Tick due, std::string name, std::function<void()> action);
    // Run `action` every `period` ticks, first at now() + period.
    TimerId every(Tick period, std::string name, std::function<void()> action);
    // Cancel a pending timer; returns false if it is unknown or already done.
    bool cancel(TimerId id);

    // Run every timer due at or before `until`, then set now() to `until`.
    // Returns the number of actions run.
    int run_until(Tick until);

    std::size_t pending() const { return live_.size(); }

private:
    TimerId arm(Timer t);

    Tick now_ = 0;
    TimerId next_id_ = 1;
    TimerQueue queue_;
    std::unordered_set<TimerId> live_;
};

}  // namespace sim
