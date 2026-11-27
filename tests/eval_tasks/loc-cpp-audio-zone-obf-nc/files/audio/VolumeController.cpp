#define LOG_TAG "CarVolume"
#include "VolumeController.h"
#include "Log.h"

namespace car {

bool VolumeController::doIt3(int a6, int a4) {
    AudioZone* a5 = router_.doIt1(a6);
    if (a5 == nullptr) {
        return false;
    }
    float a1 = mixer_.gainForStep(a4);
    a5->doIt2(a1);
    ALOGI("zone %s volume step %d", a5->name().c_str(), a4);
    return true;
}

}  
