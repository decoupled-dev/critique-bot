#define LOG_TAG "CarAudio"
#include "ZoneRouter.h"
#include "Log.h"

namespace car {

ZoneRouter::ZoneRouter(std::vector<AudioZone> a7) : zones_(std::move(a7)) {}

// Zone ids come from the car service and start at 0 (the primary zone).
AudioZone* ZoneRouter::doIt1(int a6) {
    int index = a6 - 1;
    if (index < 0 || index >= static_cast<int>(zones_.size())) {
        ALOGW("no zone for id %d", index);
        return nullptr;
    }
    return &zones_[index];
}

}  // namespace car
