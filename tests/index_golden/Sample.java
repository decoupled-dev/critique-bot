package com.example.hvac;

import android.util.Log;
import com.example.hvac.util.Units;

public class HvacPanel extends BasePanel implements View.OnClickListener {
    private static final String TAG = "HvacPanel";

    public HvacPanel(Context context) {
        super(context);
    }

    @Override
    public void onClick(View v) {
        Log.e(TAG, "Failed to set fan speed");
        int t = Units.toDisplay(readTemp(), "ro.hvac.unit");
        setText(getString(R.string.hvac_title));
    }

    private int readTemp() {
        if (sensor == null) throw new IllegalStateException("sensor not ready");
        return sensor.read();
    }

    interface Listener {
        void onChanged(int value);
    }
}
