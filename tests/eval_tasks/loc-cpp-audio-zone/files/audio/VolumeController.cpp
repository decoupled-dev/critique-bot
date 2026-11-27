#define LOG_TAG "CarVolume"
#include "VolumeController.h"
#include "Log.h"

namespace car {

bool VolumeController::setVolume(int zoneId, int step) {
    AudioZone* zone = router_.routeFor(zoneId);
    if (zone == nullptr) {
        return false;
    }
    float gain = mixer_.gainForStep(step);
    zone->setGain(gain);
    ALOGI("zone %s volume step %d", zone->name().c_str(), step);
    return true;
}

}  // namespace car
