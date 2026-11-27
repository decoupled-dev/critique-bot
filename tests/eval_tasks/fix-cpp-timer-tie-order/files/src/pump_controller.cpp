#include "sim/pump_controller.h"

namespace sim {

PumpController::PumpController(Scheduler& sched, EventLog& log, int pumps)
    : sched_(sched), log_(log), pump_on_(static_cast<std::size_t>(pumps), false) {}

int PumpController::running() const {
    int n = 0;
    for (bool on : pump_on_) n += on ? 1 : 0;
    return n;
}

void PumpController::start_pump(int i) {
    if (!valve_open_) {
        faults_.push_back("DRY_RUN pump-" + std::to_string(i));
        log_.record(sched_.now(), "FAULT dry run pump-" + std::to_string(i));
        return;
    }
    pump_on_[static_cast<std::size_t>(i)] = true;
    log_.record(sched_.now(), "start pump-" + std::to_string(i));
}

void PumpController::stop_pump(int i) {
    pump_on_[static_cast<std::size_t>(i)] = false;
    log_.record(sched_.now(), "stop pump-" + std::to_string(i));
}

void PumpController::schedule_startup(Tick at) {
    sched_.at(at, "prime", [this] {
        primed_ = true;
        log_.record(sched_.now(), "prime");
    });
    sched_.at(at, "open-valve", [this] {
        if (!primed_) faults_.push_back("VALVE_UNPRIMED");
        valve_open_ = true;
        log_.record(sched_.now(), "open valve");
    });
    for (int i = 0; i < static_cast<int>(pump_on_.size()); ++i) {
        sched_.at(at, "start-pump-" + std::to_string(i), [this, i] { start_pump(i); });
    }
}

void PumpController::schedule_monitoring(Tick period) {
    monitor_ = sched_.every(period, "pressure-check", [this] {
        log_.record(sched_.now(), "pressure ok, " + std::to_string(running()) + " running");
    });
}

void PumpController::schedule_shutdown(Tick at) {
    for (int i = 0; i < static_cast<int>(pump_on_.size()); ++i) {
        sched_.at(at, "stop-pump-" + std::to_string(i), [this, i] { stop_pump(i); });
    }
    sched_.at(at, "close-valve", [this] {
        if (running() > 0) faults_.push_back("VALVE_CLOSED_UNDER_LOAD");
        valve_open_ = false;
        log_.record(sched_.now(), "close valve");
    });
    sched_.at(at, "stop-monitoring", [this] { sched_.cancel(monitor_); });
}

}  // namespace sim
