#include <iostream>

#include "sim/pump_controller.h"

int main() {
    sim::Scheduler sched;
    sim::EventLog log;
    sim::PumpController ctl(sched, log, 4);
    ctl.schedule_startup(5);
    ctl.schedule_monitoring(10);
    ctl.schedule_shutdown(40);
    sched.run_until(60);
    std::cout << log.text();
    for (const auto& f : ctl.faults()) std::cout << "fault: " << f << "\n";
    return ctl.faults().empty() ? 0 : 1;
}
