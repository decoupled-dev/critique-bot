#include <iostream>
#include <string>
#include <vector>

#include "sim/scheduler.h"

static int failures = 0;
#define CHECK(c) do { if (!(c)) { std::cerr << "FAIL " << __LINE__ << ": " #c "\n"; ++failures; } } while (0)

int main() {
    using namespace sim;
    {
        Scheduler s;
        std::vector<std::string> order;
        s.at(3, "c", [&] { order.push_back("c"); });
        s.at(1, "a", [&] { order.push_back("a"); });
        s.at(2, "b", [&] { order.push_back("b"); });
        CHECK(s.run_until(10) == 3);
        CHECK((order == std::vector<std::string>{"a", "b", "c"}));
        CHECK(s.now() == 10);
    }
    {
        Scheduler s;
        int n = 0;
        TimerId id = s.every(2, "tick", [&] { ++n; });
        s.run_until(7);
        CHECK(n == 3);
        CHECK(s.cancel(id));
        s.run_until(20);
        CHECK(n == 3);
        CHECK(s.pending() == 0);
    }
    {
        Scheduler s;
        int n = 0;
        TimerId id = s.at(5, "x", [&] { ++n; });
        CHECK(s.cancel(id));
        CHECK(!s.cancel(id));
        s.run_until(10);
        CHECK(n == 0);
    }
    if (failures) { std::cerr << failures << " failure(s)\n"; return 1; }
    std::cout << "scheduler tests passed\n";
    return 0;
}
