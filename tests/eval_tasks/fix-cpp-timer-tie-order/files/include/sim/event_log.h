#pragma once

#include <string>
#include <vector>

#include "sim/timer.h"

namespace sim {

class EventLog {
public:
    void record(Tick tick, const std::string& what);
    const std::vector<std::string>& lines() const { return lines_; }
    std::string text() const;

private:
    std::vector<std::string> lines_;
};

}  // namespace sim
