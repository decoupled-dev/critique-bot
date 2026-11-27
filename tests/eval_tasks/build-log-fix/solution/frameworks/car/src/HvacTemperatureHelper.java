package com.android.car.hvac;

import android.car.Car;
import android.car.VehiclePropertyIds;
import android.car.hardware.CarPropertyValue;
import android.car.hardware.property.CarPropertyManager;
import android.util.Log;

/** Reads and sets the cabin temperature through the Car property service. */
public final class HvacTemperatureHelper {
    private static final String TAG = "HvacTemperatureHelper";
    private static final int DRIVER_AREA = 0x01;

    private final CarPropertyManager mCarPropertyManager;

    public HvacTemperatureHelper(Car car) {
        mCarPropertyManager = (CarPropertyManager) car.getCarManager(Car.PROPERTY_SERVICE);
    }

    /** Current set point of the driver zone, or NaN when it is not available. */
    public float getDriverSetpoint() {
        CarPropertyValue<Float> value = mCarPropertyManager.getProperty(
                Float.class, VehiclePropertyIds.HVAC_TEMPERATURE_SET, DRIVER_AREA);
        if (value == null || value.getStatus() != CarPropertyValue.STATUS_AVAILABLE) {
            Log.w(TAG, "set point not available");
            return Float.NaN;
        }
        return mCarPropertyManager.getProperty(
                Float.class, VehiclePropertyIds.HVAC_TEMPERATURE_SET, DRIVER_AREA).getValue();
    }

    public void setDriverSetpoint(float celsius) {
        mCarPropertyManager.setFloatProperty(VehiclePropertyIds.HVAC_TEMPERATURE_SET, DRIVER_AREA, celsius);
    }
}
