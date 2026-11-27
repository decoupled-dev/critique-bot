package com.android.car.power;

import java.util.HashMap;
import java.util.Map;

/** Policies from the vendor power policy XML, already parsed. */
public final class PolicyReader {
    private final Map<String, PowerPolicy> mPolicies = new HashMap<>();

    public PolicyReader() {
        mPolicies.put("all_on", new PowerPolicy("all_on",
                new int[] {PowerComponent.AUDIO, PowerComponent.MEDIA, PowerComponent.DISPLAY,
                        PowerComponent.BLUETOOTH, PowerComponent.WIFI},
                new int[] {}));
        mPolicies.put("suspend_prep", new PowerPolicy("suspend_prep", new int[] {},
                new int[] {PowerComponent.AUDIO, PowerComponent.MEDIA, PowerComponent.DISPLAY}));
    }

    public PowerPolicy get(String id) {
        return mPolicies.get(id);
    }
}
