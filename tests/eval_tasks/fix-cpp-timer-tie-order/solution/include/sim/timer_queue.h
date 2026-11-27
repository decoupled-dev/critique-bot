#pragma once

#include <cstdint>
#include <queue>
#include <vector>

#include "sim/timer.h"

namespace sim {

// Min-queue of timers ordered by due tick; timers due on the same tick come out
// in the order they were pushed.
class TimerQueue {
public:
    void push(Timer t);
    const Timer& top() const;
    Timer pop();
    bool empty() const { return heap_.empty(); }
    std::size_t size() const { return heap_.size(); }

private:
    struct Entry {
        Timer timer;
        std::uint64_t seq;
    };
    struct DueLater {
        bool operator()(const Entry& a, const Entry& b) const {
            if (a.timer.due != b.timer.due) return a.timer.due > b.timer.due;
            return a.seq > b.seq;
        }
    };
    std::priority_queue<Entry, std::vector<Entry>, DueLater> heap_;
    std::uint64_t next_seq_ = 0;
};

}  // namespace sim
