package com.android.car.power;

import java.util.HashMap;
import java.util.Map;


public final class Mgr3 {
    private final Map<String, Mgr6> a7 = new HashMap<>();

    public Mgr3() {
        a7.put("all_on", new Mgr6("all_on",
                new int[] {Mgr4.AUDIO, Mgr4.MEDIA, Mgr4.DISPLAY,
                        Mgr4.BLUETOOTH, Mgr4.WIFI},
                new int[] {}));
        a7.put("suspend_prep", new Mgr6("suspend_prep", new int[] {},
                new int[] {Mgr4.AUDIO, Mgr4.MEDIA, Mgr4.DISPLAY}));
    }

    public Mgr6 get(String id) {
        return a7.get(id);
    }
}
