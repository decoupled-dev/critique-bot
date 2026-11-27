// SPDX-License-Identifier: Apache-2.0
#include "PolicyManager.h"

namespace android::frameworks::automotive::powerpolicy {
CarPowerPolicyMeta PolicyManager::getPowerPolicy(const std::string& policyId) const {
    auto it = mRegisteredPowerPolicies.find(policyId);
    return it == mRegisteredPowerPolicies.end() ? CarPowerPolicyMeta{} : it->second;
}
}  // namespace android::frameworks::automotive::powerpolicy
