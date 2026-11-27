#define LOG_TAG "CarAudio"
#include "audio/Zone.h"

namespace car {

class AudioZone : public ZoneBase, private Observer {
public:
    void setVolume(int v);
};

void AudioZone::setVolume(int v) {
    if (v < 0) {
        LOG(ERROR) << "negative volume";
    }
    applyGain(v);
    mixer.flush();
}

}  // namespace car
