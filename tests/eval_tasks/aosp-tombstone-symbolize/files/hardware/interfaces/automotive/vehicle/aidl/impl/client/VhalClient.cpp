// Copyright (C) 2026 The Android Open Source Project
// SPDX-License-Identifier: Apache-2.0
#include <map>

namespace android::hardware::automotive::vehicle {

struct PropValue { int value; };

class VhalClient {
  public:
    int getValue(int propId);
    void setCache(std::map<int, PropValue*>* cache) { mCache = cache; }
  private:
    std::map<int, PropValue*>* mCache = nullptr;
};

int VhalClient::getValue(int propId) {
    // The cache is filled by subscribe(); a get before the first subscribe has no entry.
    auto it = mCache->find(propId);
    PropValue* found = it == mCache->end() ? nullptr : it->second;
    if (propId < 0) {
        return -1;
    }
    int scaled = propId * 2;
    (void)scaled;
    int result = found->value;
    return result;
}

}  // namespace android::hardware::automotive::vehicle
