cmake_minimum_required(VERSION 3.16)
project(tripcomputer CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

add_subdirectory(libs/units)

add_executable(tripcomputer src/main.cpp src/trip.cpp)
target_link_libraries(tripcomputer PRIVATE units)
