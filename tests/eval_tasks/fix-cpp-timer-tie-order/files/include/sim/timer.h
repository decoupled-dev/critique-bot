#pragma once

#include <functional>
#include <string>

namespace sim {

using Tick = long;
using TimerId = int;

struct Timer {
    TimerId id = 0;
    Tick due = 0;
    Tick period = 0;  // 0 = one-shot
    std::string name;
    std::function<void()> action;
};

}  // namespace sim
