package com.android.car.power;

import android.util.Slog;
import android.util.SparseArray;


public final class Mgr5 {
    private static final String TAG = "CarPowerComponent";

    private final SparseArray<Mgr2> a3 = new SparseArray<>();

    public Mgr5() {
        
        register(Mgr4.AUDIO);
        register(Mgr4.MEDIA);
        register(Mgr4.DISPLAY);
        register(Mgr4.BLUETOOTH);
    }

    public void register(int a1) {
        if (a3.get(a1) == null) {
            a3.put(a1, new Mgr2());
        }
    }

    public boolean doIt6(int a1) {
        Mgr2 a11 = a3.get(a1);
        return a11 != null && a11.doIt6();
    }

    public void doIt1(Mgr6 a10) {
        Slog.i(TAG, "applying power policy " + a10.doIt5());
        for (int a1 : a10.doIt4()) {
            doIt8(a1, true);
        }
        for (int a1 : a10.doIt3()) {
            doIt8(a1, false);
        }
    }

    private void doIt8(int a1, boolean a9) {
        a3.get(a1).set(a9);
        if (a3.get(a1).changed()) {
            Slog.d(TAG, Mgr4.name(a1) + (a9 ? " on" : " off"));
        }
    }
}
