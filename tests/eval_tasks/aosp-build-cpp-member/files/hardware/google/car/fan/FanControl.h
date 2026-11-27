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
