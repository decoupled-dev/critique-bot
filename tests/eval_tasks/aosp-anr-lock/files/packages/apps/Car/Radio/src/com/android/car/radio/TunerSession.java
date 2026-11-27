package com.android.car.radio;


public class TunerSession {

    private final Object mLock = new Object();
    private int mStation;










































    int getCurrentStation() {
        synchronized (mLock) {
            return mStation;
        }
    }



































    void scanBand(int[] band) throws InterruptedException {
        synchronized (mLock) {
            for (int frequency : band) {
                mStation = frequency;
                // Wait for the tuner to settle before reading the signal.
                Thread.sleep(250);
                readSignal(frequency);
            }
        }
    }

    private void readSignal(int frequency) {}




}
