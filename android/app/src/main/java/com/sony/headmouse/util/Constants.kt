package com.sony.headmouse.util

object Constants {
    const val PREFS_NAME = "sony_linkbuds_head_mouse_prefs"

    // 拖动方案常量
    const val DRAG_MODE_STICKY = 0   // 粘滞拖拽锁定 (敲击左侧按住，再敲一下释放)
    const val DRAG_MODE_FLICK = 1    // 轻挑快速翻页 (微挑头部向上/下平滑滑屏)
    const val DRAG_MODE_DOCK = 2     // 侧边辅助小岛

    // 右侧按键映射常量
    const val RIGHT_ACTION_BACK = 0
    const val RIGHT_ACTION_HOME = 1
    const val RIGHT_ACTION_RECENTS = 2

    // 键值
    const val KEY_DRAG_MODE = "drag_mode"
    const val KEY_RIGHT_ACTION = "right_action"
    const val KEY_DWELL_TIME = "dwell_time"
    const val KEY_TILT_THRESHOLD = "tilt_threshold"
    const val KEY_INVERT_X = "invert_x"
    const val KEY_INVERT_Y = "invert_y"

    // 默认值
    const val DEFAULT_DWELL_TIME = 0.9f
    const val DEFAULT_TILT_THRESHOLD = 10.0f
}
