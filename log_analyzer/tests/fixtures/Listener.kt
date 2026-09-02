package com.example.chatty

import android.util.Log
import android.view.View
import android.widget.Button
import android.widget.EditText

class ListenerLogs {
    fun wire(button: Button, field: EditText) {
        button.setOnClickListener {
            Log.d("Listener", "click")
        }
        field.addTextChangedListener {
            Log.i("Listener", "text")
        }
        button.setOnTouchListener { _, _ ->
            Log.v("Listener", "touch")
            false
        }
    }

    fun onClick(v: View) {
        println("clicked")
    }
}
