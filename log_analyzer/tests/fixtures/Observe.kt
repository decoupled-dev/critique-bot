package com.example.chatty

import android.util.Log
import androidx.lifecycle.LiveData
import kotlinx.coroutines.flow.Flow
import timber.log.Timber

class ObserveLogs {
    fun bind(liveData: LiveData<String>, flow: Flow<String>) {
        Timber.i("setup")
        liveData.observeForever { value ->
            Timber.d("livedata %s", value)
        }
        liveData.observe(this) { Log.d("ObserveLogs", it) }
        flow.collect { Timber.v("flow %s", it) }
    }
}
