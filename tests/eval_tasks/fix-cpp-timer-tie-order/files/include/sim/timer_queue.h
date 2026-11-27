#pragma once

#include <queue>
#include <vector>

#include "sim/timer.h"

namespace sim {

// Min-queue of timers ordered by due tick.
class TimerQueue {
public:
    void push(Timer t);
    const Timer& top() const;
    Timer pop();
    bool empty() const { return heap_.empty(); }
    std::size_t size() const { return heap_.size(); }

private:
    struct DueLater {
        bool operator()(const Timer& a, const Timer& b) const { return a.due > b.due; }
    };
    std::priority_queue<Timer, std::vector<Timer>, DueLater> heap_;
};

}  // namespace sim
