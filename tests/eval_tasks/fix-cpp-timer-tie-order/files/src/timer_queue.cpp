#include "sim/timer_queue.h"

#include <stdexcept>
#include <utility>

namespace sim {

void TimerQueue::push(Timer t) {
    heap_.push(std::move(t));
}

const Timer& TimerQueue::top() const {
    if (heap_.empty()) throw std::logic_error("TimerQueue::top on empty queue");
    return heap_.top();
}

Timer TimerQueue::pop() {
    if (heap_.empty()) throw std::logic_error("TimerQueue::pop on empty queue");
    Timer t = heap_.top();
    heap_.pop();
    return t;
}

}  // namespace sim
