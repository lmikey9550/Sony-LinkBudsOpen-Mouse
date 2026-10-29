package com.sony.headmouse.service

import android.content.Context
import android.hardware.Sensor
import android.hardware.SensorEvent
import android.hardware.SensorEventListener
import android.hardware.SensorManager
import kotlin.math.PI

/**
 * 索尼 LinkBuds 姿态传感器管理器：
 * - 优先监听 Android 13+ 原生空间音频耳机传感器: Sensor.TYPE_HEAD_TRACKER (37)
 * - 若未佩戴耳机或系统不支持，自动无缝降级到设备自身陀螺仪，保障调试与兼容性
 */
class HeadTrackerManager(
    private val context: Context,
    private val listener: MotionListener
) : SensorEventListener {

    interface MotionListener {
        fun onHeadMotion(
            yawRate: Float,
            pitchRate: Float,
            rollRate: Float,
            yawDeg: Float,
            pitchDeg: Float,
            rollDeg: Float
        )
    }

    private val sensorManager = context.getSystemService(Context.SENSOR_SERVICE) as SensorManager
    private var activeSensor: Sensor? = null
    private var isTracking = false

    private var lastTimestamp: Long = 0
    private var lastYaw: Float = 0f
    private var lastPitch: Float = 0f
    private var lastRoll: Float = 0f

    companion object {
        const val SENSOR_TYPE_HEAD_TRACKER = 37 // Sensor.TYPE_HEAD_TRACKER in API 33+
    }

    fun start() {
        if (isTracking) return

        // 1. 优先尝试获取外接耳机头部追踪传感器 (Sony LinkBuds Head Tracker)
        activeSensor = sensorManager.getDefaultSensor(SENSOR_TYPE_HEAD_TRACKER)

        // 2. 若无耳机传感器，回退至系统旋转矢量陀螺仪作为备用
        if (activeSensor == null) {
            activeSensor = sensorManager.getDefaultSensor(Sensor.TYPE_GAME_ROTATION_VECTOR)
                ?: sensorManager.getDefaultSensor(Sensor.TYPE_ROTATION_VECTOR)
        }

        activeSensor?.let {
            sensorManager.registerListener(this, it, SensorManager.SENSOR_DELAY_GAME)
            isTracking = true
        }
    }

    fun stop() {
        if (!isTracking) return
        sensorManager.unregisterListener(this)
        isTracking = false
        lastTimestamp = 0
    }

    override fun onSensorChanged(event: SensorEvent?) {
        event ?: return

        val now = event.timestamp
        val dt = if (lastTimestamp > 0) (now - lastTimestamp) * 1e-9f else 0.02f
        lastTimestamp = now

        if (event.sensor.type == SENSOR_TYPE_HEAD_TRACKER) {
            // Android TYPE_HEAD_TRACKER 规范：
            // values[0..2]: Euler angles (radians): X (pitch), Y (roll), Z (yaw)
            // values[3..5]: Angular velocity (rad/s): X (pitch_rate), Y (roll_rate), Z (yaw_rate)
            val radToDeg = (180.0 / PI).toFloat()
            val pitchDeg = event.values[0] * radToDeg
            val rollDeg = event.values[1] * radToDeg
            val yawDeg = event.values[2] * radToDeg

            val pitchRate = if (event.values.size > 3) event.values[3] * radToDeg else (pitchDeg - lastPitch) / dt
            val rollRate = if (event.values.size > 4) event.values[4] * radToDeg else (rollDeg - lastRoll) / dt
            val yawRate = if (event.values.size > 5) event.values[5] * radToDeg else (yawDeg - lastYaw) / dt

            lastYaw = yawDeg
            lastPitch = pitchDeg
            lastRoll = rollDeg

            listener.onHeadMotion(yawRate, pitchRate, rollRate, yawDeg, pitchDeg, rollDeg)
        } else {
            // 备用：标准旋转矢量转换为姿态欧拉角
            val rotMatrix = FloatArray(9)
            SensorManager.getRotationMatrixFromVector(rotMatrix, event.values)
            val orientation = FloatArray(3)
            SensorManager.getOrientation(rotMatrix, orientation)

            val radToDeg = (180.0 / PI).toFloat()
            val yawDeg = orientation[0] * radToDeg
            val pitchDeg = orientation[1] * radToDeg
            val rollDeg = orientation[2] * radToDeg

            val validDt = if (dt in 0.005f..0.1f) dt else 0.02f
            val yawRate = (yawDeg - lastYaw) / validDt
            val pitchRate = (pitchDeg - lastPitch) / validDt
            val rollRate = (rollDeg - lastRoll) / validDt

            lastYaw = yawDeg
            lastPitch = pitchDeg
            lastRoll = rollDeg

            listener.onHeadMotion(yawRate, pitchRate, rollRate, yawDeg, pitchDeg, rollDeg)
        }
    }

    override fun onAccuracyChanged(sensor: Sensor?, accuracy: Int) {}
}
