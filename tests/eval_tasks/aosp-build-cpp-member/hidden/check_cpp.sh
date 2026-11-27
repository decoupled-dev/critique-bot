set -e
g++ -std=c++17 -fsyntax-only -Ihardware/google/car/fan hardware/google/car/fan/fan_hal.cpp
cat > /tmp/crit_fan_expected_h.$$ <<'EOF'
// SPDX-License-Identifier: Apache-2.0
#pragma once

namespace android::car {

class FanControl {
  public:
    // Speed levels 0..7; replaces setLevel(), which took percent.
    void setSpeedLevel(int level) { mLevel = level < 0 ? 0 : (level > 7 ? 7 : level); }
    int speedLevel() const { return mLevel; }

  private:
    int mLevel = 0;
};

}  // namespace android::car
EOF
cmp -s /tmp/crit_fan_expected_h.$$ hardware/google/car/fan/FanControl.h || { rm -f /tmp/crit_fan_expected_h.$$; echo "FanControl.h was changed"; exit 1; }
rm -f /tmp/crit_fan_expected_h.$$
cat > evalcheck_main.cpp <<'EOF'
#include "fan_hal.cpp"
int main() { android::car::FanControl fan; return android::car::applyVoiceCommand(fan, 5) == 5 ? 0 : 1; }
EOF
g++ -std=c++17 -Ihardware/google/car/fan -o evalcheck_main evalcheck_main.cpp
./evalcheck_main
echo ok
