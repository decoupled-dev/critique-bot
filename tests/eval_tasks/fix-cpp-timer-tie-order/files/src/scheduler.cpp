#include "sim/scheduler.h"

#include <stdexcept>
#include <utility>

namespace sim {

TimerId Scheduler::arm(Timer t) {
    TimerId id = t.id;
    live_.insert(id);
    queue_.push(std::move(t));
    return id;
}

TimerId Scheduler::at(Tick due, std::string name, std::function<void()> action) {
    Timer t;
    t.id = next_id_++;
    t.due = due < now_ ? now_ : due;
    t.name = std::move(name);
    t.action = std::move(action);
    return arm(std::move(t));
}

TimerId Scheduler::every(Tick period, std::string name, std::function<void()> action) {
    if (period <= 0) throw std::invalid_argument("period must be positive");
    Timer t;
    t.id = next_id_++;
    t.due = now_ + period;
    t.period = period;
    t.name = std::move(name);
    t.action = std::move(action);
    return arm(std::move(t));
}

bool Scheduler::cancel(TimerId id) {
    return live_.erase(id) > 0;
}

int Scheduler::run_until(Tick until) {
    int ran = 0;
    while (!queue_.empty() && queue_.top().due <= until) {
        Timer t = queue_.pop();
        if (live_.count(t.id) == 0) continue;  // cancelled
        now_ = t.due;
        if (t.action) t.action();
        ++ran;
        if (t.period > 0 && live_.count(t.id)) {
            t.due += t.period;
            queue_.push(std::move(t));
        } else {
            live_.erase(t.id);
        }
    }
    if (until > now_) now_ = until;
    return ran;
}

}  // namespace sim
