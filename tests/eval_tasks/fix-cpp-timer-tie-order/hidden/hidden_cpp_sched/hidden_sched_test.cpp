#include <iostream>
#include <string>
#include <vector>

#include "sim/pump_controller.h"
#include "sim/scheduler.h"

static int failures = 0;
#define CHECK(c) do { if (!(c)) { std::cerr << "FAIL " << __LINE__ << ": " #c "\n"; ++failures; } } while (0)

using namespace sim;

int main() {
    {  // many timers on one tick run in scheduling order
        Scheduler s;
        std::vector<int> order;
        for (int i = 0; i < 25; ++i) s.at(7, "t" + std::to_string(i), [&order, i] { order.push_back(i); });
        s.run_until(7);
        std::vector<int> want;
        for (int i = 0; i < 25; ++i) want.push_back(i);
        CHECK(order == want);
    }
    {  // mixed ticks, scheduled out of order
        Scheduler s;
        std::vector<std::string> order;
        const char* names[] = {"b1", "a1", "b2", "c1", "a2", "b3", "a3", "c2"};
        const Tick ticks[] = {2, 1, 2, 3, 1, 2, 1, 3};
        for (int i = 0; i < 8; ++i) {
            std::string n = names[i];
            s.at(ticks[i], n, [&order, n] { order.push_back(n); });
        }
        s.run_until(5);
        CHECK((order == std::vector<std::string>{"a1", "a2", "a3", "b1", "b2", "b3", "c1", "c2"}));
    }
    {  // a periodic timer re-armed at tick 2 goes after timers already scheduled for tick 4
        Scheduler s;
        std::vector<std::string> order;
        s.every(2, "p", [&] { order.push_back("p@" + std::to_string(s.now())); });
        s.at(4, "x", [&] { order.push_back("x@4"); });
        s.at(4, "y", [&] { order.push_back("y@4"); });
        s.run_until(6);
        CHECK((order == std::vector<std::string>{"p@2", "x@4", "y@4", "p@4", "p@6"}));
    }
    {  // timers added from inside an action for the current tick run after the existing ones
        Scheduler s;
        std::vector<std::string> order;
        s.at(3, "first", [&] {
            order.push_back("first");
            s.at(3, "late", [&] { order.push_back("late"); });
        });
        for (int i = 0; i < 6; ++i) s.at(3, "n", [&order, i] { order.push_back("n" + std::to_string(i)); });
        s.run_until(3);
        CHECK((order == std::vector<std::string>{"first", "n0", "n1", "n2", "n3", "n4", "n5", "late"}));
    }
    {  // the station starts and stops without faults
        Scheduler s;
        EventLog log;
        PumpController ctl(s, log, 6);
        ctl.schedule_startup(5);
        ctl.schedule_monitoring(10);
        ctl.schedule_shutdown(40);
        s.run_until(30);
        CHECK(ctl.running() == 6);
        s.run_until(60);
        CHECK(ctl.faults().empty());
        CHECK(ctl.running() == 0);
        CHECK(!ctl.valve_open());
        CHECK(log.lines().size() >= 3 && log.lines()[0] == "t=5 prime" && log.lines()[1] == "t=5 open valve" &&
              log.lines()[2] == "t=5 start pump-0");
        if (!ctl.faults().empty()) std::cerr << log.text();
    }
    {  // cancellation still works
        Scheduler s;
        int n = 0;
        TimerId a = s.at(2, "a", [&] { ++n; });
        s.at(2, "b", [&] { n += 10; });
        CHECK(s.cancel(a));
        s.run_until(2);
        CHECK(n == 10);
        CHECK(s.pending() == 0);
    }
    if (failures) { std::cerr << failures << " failure(s)\n"; return 1; }
    std::cout << "hidden scheduler tests passed\n";
    return 0;
}
