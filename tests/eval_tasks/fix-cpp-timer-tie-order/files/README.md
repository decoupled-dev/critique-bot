# pumpsim

Discrete-time simulator for the booster pump station controller.

* `sim::Scheduler` runs one-shot and periodic timers on an integer tick clock.
* `sim::PumpController` sequences valves and pumps on top of the scheduler.
* `sim::EventLog` records what happened, one line per action.

Build:

    cmake -S . -B build && cmake --build build
    ./build/pumpsim
    ./build/sim_tests
