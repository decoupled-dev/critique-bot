package com.example.chatty

import android.util.Log

class NotInLoop {
    fun afterLoop(items: List<String>) {
        Log.i("T", "before")
        for (item in items) {
            Log.d("T", "inside for")
        }
        Log.d("T", "after for")
        items.forEach { consume(it) }
        Log.i("T", "after forEach")
        val names = items.map { it.length }
        Log.w("T", "after map $names")
        repeat(3) { consume("x") }
        Log.e("T", "after repeat")
    }

    fun consume(s: String) {}

    fun usesMap(items: List<String>) {
        Log.d("T", items.map { it }.toString())
    }
}
