## locate without a model (localization tasks)

| variant / retrievers | tasks | file acc@1 | file acc@5 | function acc@5 | p95 s |
| --- | --- | --- | --- | --- | --- |
| original/fused | 10 | 0.4 | 1.0 | 0.4 | 0.011 |
| original/names_only | 10 | 0.3 | 0.7 | 0.3 | 0.006 |
| obf/fused | 10 | 0.4 | 1.0 | 0.3 | 0.011 |
| obf/names_only | 10 | 0.1 | 0.5 | 0.1 | 0.007 |
| obf-nc/fused | 10 | 0.5 | 0.8 | 0.3 | 0.01 |
| obf-nc/names_only | 10 | 0.1 | 0.5 | 0.1 | 0.007 |

Log lines quoted in the tasks: 18; the emitting line is find_strings' top hit (its literal prints the message) for 100%.

- `I ClusterUnits: display units from property: metric` -> packages/apps/Car/Cluster/src/com/android/car/cluster/DisplayUnitsReader.java:13 "display units from property: " (0.911)
- `I ClusterUnits: display units from property: metric` -> packages/apps/Car/Cluster/src/com/android/car/cluster/Mgr2.java:13 "display units from property: " (0.911)
- `I ClusterUnits: display units from property: metric` -> packages/apps/Car/Cluster/src/com/android/car/cluster/Mgr2.java:13 "display units from property: " (0.911)
- `I thermal: cabin at 47 C: full fan` -> fan/thermal.c:11 "cabin at %d C: full fan" (1.0)
- `E fanctl: fan level too high: 9` -> fan/fan.c:8 "fan level too high: %d" (1.0)
- `I thermal: cabin at 47 C: full fan` -> fan/thermal.c:11 "cabin at %d C: full fan" (1.0)
- `E fanctl: fan level too high: 9` -> fan/fan.c:8 "fan level too high: %d" (1.0)
- `I thermal: cabin at 47 C: full fan` -> fan/thermal.c:11 "cabin at %d C: full fan" (1.0)
- `E fanctl: fan level too high: 9` -> fan/fan.c:8 "fan level too high: %d" (1.0)
- `W CarAudio: no zone for id -1` -> audio/ZoneRouter.cpp:13 "no zone for id %d" (0.98)
- `W CarAudio: no zone for id -1` -> audio/ZoneRouter.cpp:13 "no zone for id %d" (0.98)
- `W CarAudio: no zone for id -1` -> audio/ZoneRouter.cpp:13 "no zone for id %d" (0.98)
- `I SeatHeatVM: seat heat request level=3 zone=DRIVER` -> app/src/main/java/com/example/seat/SeatHeatViewModel.kt:11 "seat heat request level=$level zone=$zone" (0.98)
- `D SeatProp: write SEAT_HEAT level=2 zone=1` -> app/src/main/java/com/example/seat/VehiclePropertyBridge.kt:10 "write SEAT_HEAT level=$level zone=${zone.areaId}" (1.0)
- `I SeatHeatVM: seat heat request level=3 zone=DRIVER` -> app/src/main/java/com/example/seat/SeatHeatViewModel.kt:11 "seat heat request level=$a4 zone=$a8" (0.98)
- `D SeatProp: write SEAT_HEAT level=2 zone=1` -> app/src/main/java/com/example/seat/VehiclePropertyBridge.kt:10 "write SEAT_HEAT level=$a4 zone=${a8.areaId}" (1.0)
- `I SeatHeatVM: seat heat request level=3 zone=DRIVER` -> app/src/main/java/com/example/seat/SeatHeatViewModel.kt:11 "seat heat request level=$a4 zone=$a8" (0.98)
- `D SeatProp: write SEAT_HEAT level=2 zone=1` -> app/src/main/java/com/example/seat/VehiclePropertyBridge.kt:10 "write SEAT_HEAT level=$a4 zone=${a8.areaId}" (1.0)

