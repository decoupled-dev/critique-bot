#pragma once

#include <string>
#include <vector>

#include "sim/event_log.h"
#include "sim/scheduler.h"

namespace sim {

// Starts and stops a pump line. The suction valve must be open before the pump
// runs; a pump started against a closed valve raises DRY_RUN.
class PumpController {
public:
    PumpController(Scheduler& sched, EventLog& log, int pumps);

    // Schedule the startup sequence for tick `at`: prime, open valve, start pumps.
    void schedule_startup(Tick at);
    // Schedule a pressure check every `period` ticks.
    void schedule_monitoring(Tick period);
    // Schedule the shutdown sequence for tick `at`: stop pumps, close valve.
    void schedule_shutdown(Tick at);

    bool valve_open() const { return valve_open_; }
    int running() const;
    const std::vector<std::string>& faults() const { return faults_; }

private:
    void start_pump(int i);
    void stop_pump(int i);

    Scheduler& sched_;
    EventLog& log_;
    std::vector<bool> pump_on_;
    bool primed_ = false;
    bool valve_open_ = false;
    TimerId monitor_ = 0;
    std::vector<std::string> faults_;
};

}  // namespace sim
