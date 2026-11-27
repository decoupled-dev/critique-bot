#include "sim/event_log.h"

namespace sim {

void EventLog::record(Tick tick, const std::string& what) {
    lines_.push_back("t=" + std::to_string(tick) + " " + what);
}

std::string EventLog::text() const {
    std::string out;
    for (const auto& l : lines_) {
        out += l;
        out += '\n';
    }
    return out;
}

}  // namespace sim
