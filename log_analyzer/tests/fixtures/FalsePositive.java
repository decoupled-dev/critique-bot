package com.example.chatty;

import android.util.Log;
import android.view.View;

public class FalsePositive {
    View view;
    CustomDrawer drawer;

    void bind() {
        view.d("not a log");
        drawer.debug("also not a log");
        String sample = "Log.d(\"nope\", \"string literal\")";
        // Log.e("nope", "commented out");
        LogUtils.e("real wrapper");
        logger.debug("real logger");
    }
}

class LogUtils {
    static void e(String msg) {}
}

class CustomDrawer {
    void debug(String msg) {}
}
