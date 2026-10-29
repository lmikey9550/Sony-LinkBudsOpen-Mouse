package com.sony.headmouse.util

import android.content.Context
import android.content.SharedPreferences

class PreferenceHelper(context: Context) {
    private val prefs: SharedPreferences =
        context.getSharedPreferences(Constants.PREFS_NAME, Context.MODE_PRIVATE)

    var dragMode: Int
        get() = prefs.getInt(Constants.KEY_DRAG_MODE, Constants.DRAG_MODE_STICKY)
        set(value) = prefs.edit().putInt(Constants.KEY_DRAG_MODE, value).apply()

    var rightAction: Int
        get() = prefs.getInt(Constants.KEY_RIGHT_ACTION, Constants.RIGHT_ACTION_BACK)
        set(value) = prefs.edit().putInt(Constants.KEY_RIGHT_ACTION, value).apply()

    var dwellTime: Float
        get() = prefs.getFloat(Constants.KEY_DWELL_TIME, Constants.DEFAULT_DWELL_TIME)
        set(value) = prefs.edit().putFloat(Constants.KEY_DWELL_TIME, value).apply()

    var tiltThreshold: Float
        get() = prefs.getFloat(Constants.KEY_TILT_THRESHOLD, Constants.DEFAULT_TILT_THRESHOLD)
        set(value) = prefs.edit().putFloat(Constants.KEY_TILT_THRESHOLD, value).apply()

    var invertX: Boolean
        get() = prefs.getBoolean(Constants.KEY_INVERT_X, true)
        set(value) = prefs.edit().putBoolean(Constants.KEY_INVERT_X, value).apply()

    var invertY: Boolean
        get() = prefs.getBoolean(Constants.KEY_INVERT_Y, false)
        set(value) = prefs.edit().putBoolean(Constants.KEY_INVERT_Y, value).apply()

    var allowPhoneMountFallback: Boolean
        get() = prefs.getBoolean("allow_phone_mount_fallback", false)
        set(value) = prefs.edit().putBoolean("allow_phone_mount_fallback", value).apply()
}
