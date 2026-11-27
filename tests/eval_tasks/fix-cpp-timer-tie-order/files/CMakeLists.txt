cmake_minimum_required(VERSION 3.16)
project(pumpsim CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

add_library(sim
    src/event_log.cpp
    src/timer_queue.cpp
    src/scheduler.cpp
    src/pump_controller.cpp
)
target_include_directories(sim PUBLIC include)
target_compile_options(sim PRIVATE -Wall -Wextra)

add_executable(pumpsim src/main.cpp)
target_link_libraries(pumpsim PRIVATE sim)

add_executable(sim_tests tests/test_scheduler.cpp)
target_link_libraries(sim_tests PRIVATE sim)
