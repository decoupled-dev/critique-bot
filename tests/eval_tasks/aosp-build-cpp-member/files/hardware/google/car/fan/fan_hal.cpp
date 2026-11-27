// SPDX-License-Identifier: Apache-2.0
#include "FanControl.h"

namespace android::car {


































int applyVoiceCommand(FanControl& fan, int requested) {
    // requested is already a level (0..7) from the voice grammar
    int level = requested;
    fan.setLevel(level);
    return fan.speedLevel();
}

}  // namespace android::car
