# Unit conversions for the trip computer (moved here from src/units/).
add_library(vehicle_units STATIC src/convert.cpp)
target_include_directories(vehicle_units PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/../../src/units)
