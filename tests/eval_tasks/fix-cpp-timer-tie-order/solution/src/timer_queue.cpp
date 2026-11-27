#include "sim/timer_queue.h"

#include <stdexcept>
#include <utility>

namespace sim {

void TimerQueue::push(Timer t) {
    heap_.push(Entry{std::move(t), next_seq_++});
}

const Timer& TimerQueue::top() const {
    if (heap_.empty()) throw std::logic_error("TimerQueue::top on empty queue");
    return heap_.top().timer;
}

Timer TimerQueue::pop() {
    if (heap_.empty()) throw std::logic_error("TimerQueue::pop on empty queue");
    Timer t = heap_.top().timer;
    heap_.pop();
    return t;
}

}  // namespace sim
