package com.example.chatty;

import android.util.Log;
import java.util.List;
import java.util.Optional;

public class NotInLoop {
    void afterLoop(List<String> items) {
        Log.i("T", "before");
        for (String item : items) {
            Log.d("T", "inside for");
            consume(item);
        }
        Log.d("T", "after for");
        items.forEach(this::consume);
        Log.i("T", "after forEach");
        items.stream().map(String::trim).forEach(this::consume);
        Log.w("T", "after stream");
        Optional.ofNullable("x").map(v -> v + "y");
        Log.e("T", "after optional");
    }

    void consume(String s) {}
}
