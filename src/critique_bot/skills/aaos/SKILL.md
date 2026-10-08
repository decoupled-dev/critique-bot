---
name: aaos
description: Android Automotive OS - CarService, Car APIs, Vehicle HAL properties, driver distraction, occupant zones, car audio, power, automotive tests.
keywords: android automotive, aaos, carservice, car_service, android.car, carpropertymanager, caroccupantzonemanager, caraudiomanager, caruxrestrictionsmanager, carpowermanager, vehicle hal, vhal, ivehicle, vehicleproperty, vehicleareaseat, driver distraction, distraction optimized, car app library, carsystemui, garage mode, headless system user, occupant zone, car_audio_configuration, evs, android.car.permission, sdk_car, cf_x86_64_auto
files: car_audio_configuration.xml, packages/services/Car/**, hardware/interfaces/automotive/vehicle/**, DefaultProperties.json, car_ux_restrictions_map.xml
---
# Android Automotive OS

## How to reason
- Identify the stack layer: car app (Car App Library / distraction-optimized activity) -> Car API (android.car, packages/services/Car/car-lib) -> CarService (packages/services/Car/service) -> Vehicle HAL (hardware/interfaces/automotive/vehicle, AIDL IVehicle) -> vehicle bus. Fix where the contract breaks.
- For a property issue, read its definition: ID, type, area type (GLOBAL, SEAT, WINDOW, DOOR, MIRROR, WHEEL), access (READ, WRITE, READ_WRITE), change mode (STATIC, ON_CHANGE, CONTINUOUS with sample rates), and the required permission.
- Ask which user: AAOS often runs a headless system user (user 0) plus a foreground user (10+). Services in user 0 vs apps in the current user behave differently; check singleUser, INTERACT_ACROSS_USERS, and Context.createContextAsUser.
- Ask which display/occupant zone: driver, front passenger, rear seats; audio zones and input map to occupant zones.
- For any UI shown while driving: what does CarUxRestrictions say, and is the activity marked distractionOptimized?
- Distinguish AOSP-generic behavior from OEM customization (vendor VHAL, RROs on CarSystemUI or Car UI library).
- Check car_service dumpsys for the live state before changing code.

## Do
- Obtain managers via Car.createCar with a lifecycle listener, then getCarManager:
```kotlin
val car = Car.createCar(context, null, Car.CAR_WAIT_TIMEOUT_WAIT_FOREVER) { c, ready ->
  if (ready) {
    val pm = c.getCarManager(Car.PROPERTY_SERVICE) as CarPropertyManager
    pm.registerCallback(cb, VehiclePropertyIds.PERF_VEHICLE_SPEED, CarPropertyManager.SENSOR_RATE_NORMAL)
  }
}
// unregisterCallback and car.disconnect() on teardown
```
- Check CarPropertyConfig (getCarPropertyConfig) for supported area IDs and min/max before get/set; handle PropertyNotAvailableException and status UNAVAILABLE/ERROR values.
- Declare the exact android.car.permission.* needed (e.g. CAR_SPEED, CAR_ENERGY, CONTROL_CAR_CLIMATE, CAR_POWERTRAIN). Many are signature|privileged - the app must be platform-signed or privileged with an allowlist.
- VHAL: add properties to the reference implementation config (DefaultProperties.json in the default VHAL) for emulator/cuttlefish; vendor-specific properties use the VENDOR group bit and need permission mapping in CarService config.
- Driver distraction: mark activities with `<meta-data android:name="distractionOptimized" android:value="true"/>` only if they comply; listen to CarUxRestrictionsManager and hide/limit content when isRequiresDistractionOptimization().
- Third-party car apps: use Car App Library templates (ListTemplate, PaneTemplate, NavigationTemplate, PlaceListMapTemplate) and declared categories (navigation, POI, IoT, etc.); media apps use MediaBrowserService/MediaLibraryService, not custom UI.
- Audio: configure zones/volume groups in car_audio_configuration.xml (usages map to buses/devices); use CarAudioManager for group volume and zone queries; dynamic routing requires the config flag enabled.
- Power: listen via CarPowerManager state listener; do deferrable maintenance in garage mode (shutdown-prepare) rather than at boot; handle suspend-to-RAM resume (re-register, re-query state).
- Multi-display: use CarOccupantZoneManager to map zone -> display -> user; launch on the right display with ActivityOptions.setLaunchDisplayId.
- Customize system UI via CarSystemUI config and RROs targeting Car UI library resources, not by forking.

## Avoid
- Treating a phone app as automotive-ready -> needs android.hardware.type.automotive feature declaration and distraction compliance.
- Polling properties in a loop -> subscribe with registerCallback and an appropriate rate.
- Assuming user 0 is the driver user -> query current user and occupant zone.
- Hardcoding areaId values -> read them from CarPropertyConfig.
- Using phone AudioManager stream volumes -> use CarAudioManager volume groups per zone.
- Faking distractionOptimized to pass review.
- Long work in power-state callbacks -> they are time-limited; complete asynchronously and report completion.
- Editing generated VehicleProperty Java/C++ files -> edit the AIDL source and regenerate.

## Commands
```bash
source build/envsetup.sh
lunch sdk_car_x86_64-trunk_staging-userdebug       # AAOS emulator; release name varies by branch
lunch aosp_cf_x86_64_auto-trunk_staging-userdebug  # Cuttlefish auto
m && emulator                                      # or launch_cvd for Cuttlefish
adb shell dumpsys car_service                      # full CarService state
adb shell dumpsys car_service --help
adb shell cmd car_service get-do-activities <pkg>
adb shell dumpsys car_service --services CarPropertyService
adb shell dumpsys android.hardware.automotive.vehicle.IVehicle/default   # VHAL debug dump
adb shell cmd car_service inject-vhal-event <propId> <value>
adb shell cmd user list; adb shell am get-current-user
atest CarServiceUnitTest; atest CtsCarTestCases
```
Use `adb shell cmd car_service help` to confirm sub-command names on the target build; they vary between releases.

## Verify before COMPLETED
- Builds for the auto lunch target; emulator or Cuttlefish boots and CarService connects (no Car not-ready in logcat).
- Property read/write/subscribe works for each supported area; permission denials absent in logcat.
- UX restriction transitions (parked vs moving, inject speed/gear) behave correctly.
- Behavior correct for the foreground user and after user switch; multi-display/zone paths tested if touched.
- Relevant CarService unit tests and CtsCarTestCases modules pass; CTS-Verifier automotive cases checked for UX-facing changes.
