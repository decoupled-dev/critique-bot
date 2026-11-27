package android.hardware.automotive.vehicle;

import android.hardware.automotive.vehicle.VehiclePropValue;

@VintfStability
interface IVehicle {
    VehiclePropValue get(in int propId);
    oneway void set(in VehiclePropValue value);
}
