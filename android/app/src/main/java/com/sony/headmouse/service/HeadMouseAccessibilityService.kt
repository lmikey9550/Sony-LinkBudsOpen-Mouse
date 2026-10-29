package com.sony.headmouse.service

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.GestureDescription
import android.graphics.Path
import android.os.SystemClock
import android.view.KeyEvent
import com.sony.headmouse.motion.AccelaFilter
import com.sony.headmouse.util.Constants
import com.sony.headmouse.util.PreferenceHelper
import kotlin.math.abs
import kotlin.math.hypot

/**
 * 核心无障碍服务 (AccessibilityService)：
 * 1. 拦截 LinkBuds 耳机多媒体/耳周广域按键，屏蔽系统切歌，直接转化为触屏手势
 * 2. 注入 Android 系统级点击、粘滞拖拽 (Touch Hold & Drag)、快速平滑划屏 (Flick Swipe)
 * 3. 注入系统级导航 (返回 Back、桌面 Home、多任务 Recents)
 */
class HeadMouseAccessibilityService : AccessibilityService(), HeadTrackerManager.MotionListener {

    interface DiagnosticListener {
        fun onSensorStatus(statusText: String, isConnected: Boolean)
        fun onMotionData(yawDeg: Float, pitchDeg: Float, rollDeg: Float)
    }

    companion object {
        var isServiceRunning = false
            private set
        var instance: HeadMouseAccessibilityService? = null
            private set
        var diagnosticListener: DiagnosticListener? = null
    }

    private lateinit var prefs: PreferenceHelper
    private lateinit var accelaFilter: AccelaFilter
    private lateinit var trackerManager: HeadTrackerManager
    private lateinit var overlayCursor: OverlayCursorManager

    private var isHeadsetConnected = false

    // 拖拽与手势状态机
    private var isDragging = false
    private var dragStartPath: Path? = null
    private var lastDragX = 0f
    private var lastDragY = 0f

    // 悬停点击状态机
    private var dwellAnchorX = 0f
    private var dwellAnchorY = 0f
    private var dwellStartTime: Long = 0
    private var dwellTriggered = false

    // 左右歪头状态机
    private var baselineRoll = 0f
    private var tiltTriggered = false
    private var tiltFreezeUntil: Long = 0
    private var lastTiltTime: Long = 0

    // 甩头翻页状态机 (Flick to Scroll)
    private var lastPitchRate = 0f
    private var lastFlickTime: Long = 0

    // 按键防抖
    private var lastKeyActionTime: Long = 0

    override fun onServiceConnected() {
        super.onServiceConnected()
        isServiceRunning = true
        instance = this

        prefs = PreferenceHelper(this)
        accelaFilter = AccelaFilter(deadzone = 1.0f, sensitivity = 1.0f, smoothing = 0.7f)
        overlayCursor = OverlayCursorManager(this)
        trackerManager = HeadTrackerManager(this, this)

        overlayCursor.show()
        trackerManager.start()
    }

    override fun onDestroy() {
        super.onDestroy()
        isServiceRunning = false
        instance = null
        diagnosticListener = null
        trackerManager.stop()
        overlayCursor.hide()
    }

    override fun onSensorStatusChanged(statusText: String, isConnected: Boolean) {
        isHeadsetConnected = isConnected
        overlayCursor.setHeadsetStatus(isConnected, statusText)
        diagnosticListener?.onSensorStatus(statusText, isConnected)
    }

    fun reDetectHeadset() {
        trackerManager.stop()
        trackerManager.start()
    }

    fun getSensorDescription(): String {
        return trackerManager.activeSourceDescription
    }

    fun isHeadsetAttached(): Boolean {
        return trackerManager.isHeadsetDetected
    }

    override fun onAccessibilityEvent(event: android.view.accessibility.AccessibilityEvent?) {}
    override fun onInterrupt() {}

    /**
     * 关键特性：耳周广域点击 (Wide Area Tap) 硬件按键系统级拦截
     * 自动拦截索尼耳机发送的 Play/Pause, Next, Previous 等按键并静默吞掉，不干扰音乐播放
     */
    override fun onKeyEvent(event: KeyEvent?): Boolean {
        event ?: return false
        val now = SystemClock.uptimeMillis()

        if (event.action == KeyEvent.ACTION_DOWN) {
            when (event.keyCode) {
                // 左侧耳周敲击 (播放/暂停、上一曲、音量减) -> 左侧主要动作
                KeyEvent.KEYCODE_MEDIA_PLAY_PAUSE,
                KeyEvent.KEYCODE_MEDIA_PLAY,
                KeyEvent.KEYCODE_MEDIA_PAUSE,
                KeyEvent.KEYCODE_MEDIA_PREVIOUS,
                KeyEvent.KEYCODE_VOLUME_DOWN -> {
                    if (now - lastKeyActionTime > 250) {
                        lastKeyActionTime = now
                        handleLeftAction()
                    }
                    return true // 拦截并消费事件，阻止系统暂停音乐
                }

                // 右侧耳周敲击 (下一曲、音量加) -> 右侧辅助动作 (默认全局返回 Back)
                KeyEvent.KEYCODE_MEDIA_NEXT,
                KeyEvent.KEYCODE_VOLUME_UP -> {
                    if (now - lastKeyActionTime > 250) {
                        lastKeyActionTime = now
                        handleRightAction()
                    }
                    return true // 拦截并消费事件，阻止系统切歌
                }
            }
        }
        return super.onKeyEvent(event)
    }

    /**
     * 左侧核心动作逻辑：支持单击、粘滞拖拽锁定 (Sticky Drag)
     */
    private fun handleLeftAction() {
        when (prefs.dragMode) {
            Constants.DRAG_MODE_STICKY -> {
                // 方案一：粘滞拖拽锁定 (点一下按住不放，再点一下松开)
                if (!isDragging) {
                    startStickyDrag()
                } else {
                    stopStickyDrag()
                }
            }
            else -> {
                // 普通单击模式
                dispatchTap(overlayCursor.cursorX, overlayCursor.cursorY)
                overlayCursor.flashFeedback("单击")
            }
        }
    }

    /**
     * 右侧核心动作逻辑：针对 Android 深度定制的全局按键 (返回、主页、多任务)
     */
    private fun handleRightAction() {
        if (isDragging) {
            // 若当前处于拖动中，右侧敲击作为安全取消/释放键
            stopStickyDrag()
            return
        }

        when (prefs.rightAction) {
            Constants.RIGHT_ACTION_BACK -> {
                performGlobalAction(GLOBAL_ACTION_BACK)
                overlayCursor.flashFeedback("返回")
            }
            Constants.RIGHT_ACTION_HOME -> {
                performGlobalAction(GLOBAL_ACTION_HOME)
                overlayCursor.flashFeedback("桌面")
            }
            Constants.RIGHT_ACTION_RECENTS -> {
                performGlobalAction(GLOBAL_ACTION_RECENTS)
                overlayCursor.flashFeedback("多任务")
            }
        }
    }

    private fun startStickyDrag() {
        isDragging = true
        lastDragX = overlayCursor.cursorX
        lastDragY = overlayCursor.cursorY
        overlayCursor.setDragMode(true)
        overlayCursor.flashFeedback("开始拖拽")
    }

    private fun stopStickyDrag() {
        if (!isDragging) return
        isDragging = false
        overlayCursor.setDragMode(false)
        overlayCursor.flashFeedback("释放拖拽")

        // 注入一次完整的拖拽轨迹
        val curX = overlayCursor.cursorX
        val curY = overlayCursor.cursorY
        val path = Path().apply {
            moveTo(lastDragX, lastDragY)
            lineTo(curX, curY)
        }
        val gesture = GestureDescription.Builder()
            .addStroke(GestureDescription.StrokeDescription(path, 0, 300))
            .build()
        dispatchGesture(gesture, null, null)
    }

    /**
     * 头部姿态连续数据帧处理
     */
    override fun onHeadMotion(
        yawRate: Float,
        pitchRate: Float,
        rollRate: Float,
        yawDeg: Float,
        pitchDeg: Float,
        rollDeg: Float
    ) {
        diagnosticListener?.onMotionData(yawDeg, pitchDeg, rollDeg)
        if (!isHeadsetConnected) return

        val now = SystemClock.uptimeMillis()

        // 1. 自适应零点平滑更新
        if (abs(yawRate) < 4f && abs(pitchRate) < 4f && abs(rollDeg - baselineRoll) < 6f) {
            baselineRoll = baselineRoll * 0.96f + rollDeg * 0.04f
        }
        val deltaRoll = rollDeg - baselineRoll

        // 2. 歪头手势检测
        val isFrozen = now < tiltFreezeUntil
        val tiltThresh = prefs.tiltThreshold

        if (!isFrozen) {
            if (deltaRoll <= -tiltThresh) {
                // 向左微偏头部 -> 触发左侧动作 (单击 / 切换拖动)
                if (!tiltTriggered && (now - lastTiltTime > 450)) {
                    tiltTriggered = true
                    lastTiltTime = now
                    tiltFreezeUntil = now + 280
                    handleLeftAction()
                }
            } else if (deltaRoll >= tiltThresh) {
                // 向右微偏头部 -> 触发右侧动作 (返回 Back)
                if (!tiltTriggered && (now - lastTiltTime > 450)) {
                    tiltTriggered = true
                    lastTiltTime = now
                    tiltFreezeUntil = now + 280
                    handleRightAction()
                }
            } else if (abs(deltaRoll) < tiltThresh * 0.4f) {
                tiltTriggered = false
            }
        }

        // 3. 甩头快速翻页检测 (Flick to Scroll)
        if (prefs.dragMode == Constants.DRAG_MODE_FLICK && !isFrozen && !isDragging) {
            if (now - lastFlickTime > 600) {
                // 向上快速轻挑抬头 (pitchRate 突发变正) -> 屏幕内容向下滑动 (向下翻)
                if (pitchRate > 28f && lastPitchRate > 20f) {
                    lastFlickTime = now
                    dispatchFlickSwipe(upward = false)
                }
                // 向下快速轻叩点头 (pitchRate 突发变负) -> 屏幕内容向上滑动 (向上翻)
                else if (pitchRate < -28f && lastPitchRate < -20f) {
                    lastFlickTime = now
                    dispatchFlickSwipe(upward = true)
                }
            }
            lastPitchRate = pitchRate
        }

        // 4. 光标位移平滑驱动
        if (!isFrozen) {
            val signX = if (prefs.invertX) -1f else 1f
            val signY = if (prefs.invertY) 1f else -1f

            val (fx, fy) = accelaFilter.filter(yawRate, pitchRate)
            val moveScale = 0.55f
            val dx = signX * fx * moveScale
            val dy = signY * fy * moveScale

            overlayCursor.moveBy(dx, dy)
        }

        // 5. 悬停点击 (Dwell Click) 判定
        if (!isDragging && !isFrozen) {
            val curX = overlayCursor.cursorX
            val curY = overlayCursor.cursorY
            val dist = hypot((curX - dwellAnchorX).toDouble(), (curY - dwellAnchorY).toDouble()).toFloat()

            if (dist > 18f || abs(yawRate) > 1.2f || abs(pitchRate) > 1.2f) {
                dwellAnchorX = curX
                dwellAnchorY = curY
                dwellStartTime = now
                dwellTriggered = false
                overlayCursor.setDwellProgress(0f)
            } else {
                if (dwellStartTime > 0 && !dwellTriggered) {
                    val elapsed = (now - dwellStartTime) / 1000f
                    val targetDwell = prefs.dwellTime
                    val progress = (elapsed / targetDwell).coerceIn(0f, 1f)
                    overlayCursor.setDwellProgress(progress)

                    if (elapsed >= targetDwell) {
                        dispatchTap(curX, curY)
                        overlayCursor.flashFeedback("悬停点击")
                        dwellTriggered = true
                        overlayCursor.setDwellProgress(0f)
                    }
                }
            }
        } else {
            overlayCursor.setDwellProgress(0f)
        }
    }

    /**
     * 注入触屏单次点击
     */
    private fun dispatchTap(x: Float, y: Float) {
        val path = Path().apply {
            moveTo(x, y)
        }
        val gesture = GestureDescription.Builder()
            .addStroke(GestureDescription.StrokeDescription(path, 0, 50))
            .build()
        dispatchGesture(gesture, null, null)
    }

    /**
     * 快速平滑划屏翻页 (模拟手指自然向上或向下滑动)
     */
    private fun dispatchFlickSwipe(upward: Boolean) {
        val cx = overlayCursor.cursorX
        val cy = overlayCursor.cursorY
        val distance = 550f

        val startY = if (upward) cy + distance / 2 else cy - distance / 2
        val endY = if (upward) cy - distance / 2 else cy + distance / 2

        val path = Path().apply {
            moveTo(cx, startY.coerceIn(100f, overlayCursor.screenHeight - 100f))
            lineTo(cx, endY.coerceIn(100f, overlayCursor.screenHeight - 100f))
        }

        val gesture = GestureDescription.Builder()
            .addStroke(GestureDescription.StrokeDescription(path, 0, 240))
            .build()
        dispatchGesture(gesture, null, null)
        overlayCursor.flashFeedback(if (upward) "向上翻页" else "向下翻页")
    }
}
