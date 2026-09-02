package com.example.chatty;

import android.util.Log;
import java.util.List;

public class LoopLogs {
    private static final String TAG = "LoopLogs";

    void scan(List<String> items) {
        Log.i(TAG, "once");
        for (String item : items) {
            Log.d(TAG, item);
        }
        while (items.isEmpty()) {
            Log.v(TAG, "spin");
        }
        items.forEach(x -> Log.w(TAG, x));
    }
}
