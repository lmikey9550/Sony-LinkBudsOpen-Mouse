package com.sony.headmouse.service

import android.content.Context
import android.hardware.Sensor
import android.hardware.SensorEvent
import android.hardware.SensorEventListener
import android.hardware.SensorManager
import android.hardware.input.InputManager
import android.media.AudioManager
import android.media.Spatializer
import android.os.Build
import android.view.InputDevice
import java.lang.reflect.InvocationHandler
import java.lang.reflect.Method
import java.lang.reflect.Proxy
import java.util.concurrent.Executors
import kotlin.math.PI
import kotlin.math.atan2
import kotlin.math.asin

/**
 * 专为 Sony LinkBuds / XM 系列打造的真实耳机外设姿态管理器：
 * 【核心修复】：彻底剔除手机内置陀螺仪降级逻辑，100% 仅读取耳机端姿态数据流！
 * 
 * 采用 4 重深度探测管道抓取耳机数据：
 * 管道 1：Android 12+ (API 31+) InputDevice.getSensorManager() 蓝牙 HID 外设硬件传感器
 * 管道 2：Android 13+ (API 33+) SensorManager 动态外接耳机传感器 (DynamicSensor: TYPE_HEAD_TRACKER)
 * 管道 3：Android 13+ Spatializer 空间音频耳机姿态数据流 (Pose Stream)
 * 管道 4：系统外设传感器过滤器 (严格匹配 Sony/LinkBuds/Head Tracker 外部硬件)
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
        fun onSensorStatusChanged(statusText: String, isConnected: Boolean)
    }

    private val sensorManager = context.getSystemService(Context.SENSOR_SERVICE) as SensorManager
    private val inputManager = context.getSystemService(Context.INPUT_SERVICE) as InputManager

    private var activeSensor: Sensor? = null
    private var activeDeviceSensorManager: SensorManager? = null
    private var isTracking = false

    private var dynamicSensorCallback: Any? = null
    private var spatializerListenerProxy: Any? = null

    private var lastTimestamp: Long = 0
    private var lastYaw: Float = 0f
    private var lastPitch: Float = 0f
    private var lastRoll: Float = 0f

    var activeSourceDescription: String = "未连接耳机传感器 (等待 Sony LinkBuds)"
        private set
    var isHeadsetDetected: Boolean = false
        private set

    companion object {
        const val SENSOR_TYPE_HEAD_TRACKER = 37 // Sensor.TYPE_HEAD_TRACKER in API 33+
    }

    fun start() {
        if (isTracking) return
        isTracking = true
        lastTimestamp = 0
        detectAndBindHeadsetSensor()
    }

    fun stop() {
        if (!isTracking) return
        isTracking = false

        // 解除动态传感器监听
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.N && dynamicSensorCallback != null) {
            try {
                sensorManager.unregisterDynamicSensorCallback(
                    dynamicSensorCallback as SensorManager.DynamicSensorCallback
                )
            } catch (e: Exception) {
                e.printStackTrace()
            }
            dynamicSensorCallback = null
        }

        // 解除外设 SensorManager 监听
        activeDeviceSensorManager?.unregisterListener(this)
        activeDeviceSensorManager = null

        // 解除主系统传感器监听
        sensorManager.unregisterListener(this)
        activeSensor = null

        // 解除 Spatializer 监听
        removeSpatializerPoseListener()

        isHeadsetDetected = false
        activeSourceDescription = "已停止头控追踪"
        listener.onSensorStatusChanged(activeSourceDescription, false)
    }

    /**
     * 核心检测逻辑：只绑定外部耳机，坚决不使用手机自身陀螺仪！
     */
    fun detectAndBindHeadsetSensor() {
        // --- 方案 1：Android 12+ (API 31+) InputDevice 外设蓝牙 HID 硬件传感器 ---
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            val deviceIds = inputManager.inputDeviceIds
            for (id in deviceIds) {
                val dev = inputManager.getInputDevice(id) ?: continue
                val devName = dev.name ?: ""

                // 检查是否为 Sony 耳机或具备传感器的蓝牙外设
                val isSonyHeadset = devName.contains("LinkBuds", ignoreCase = true) ||
                        devName.contains("Sony", ignoreCase = true) ||
                        devName.contains("WF-", ignoreCase = true) ||
                        devName.contains("WH-", ignoreCase = true) ||
                        devName.contains("Headset", ignoreCase = true)

                if (dev.sources and InputDevice.SOURCE_SENSOR != 0 || isSonyHeadset) {
                    try {
                        val dsm = dev.sensorManager
                        val sensors = dsm.getSensorList(Sensor.TYPE_ALL)
                        for (s in sensors) {
                            if (isTargetHeadphoneSensor(s)) {
                                bindExternalSensor(dsm, s, "蓝牙外设 HID 传感器 (${dev.name})")
                                return
                            }
                        }
                    } catch (e: Exception) {
                        e.printStackTrace()
                    }
                }
            }
        }

        // --- 方案 2：Android 13+ (API 33+) 动态耳机传感器 (DynamicSensor) ---
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.N) {
            try {
                val dynamicList = sensorManager.getDynamicSensorList(Sensor.TYPE_ALL)
                for (s in dynamicList) {
                    if (isTargetHeadphoneSensor(s)) {
                        bindExternalSensor(sensorManager, s, "动态耳机传感器 (TYPE_HEAD_TRACKER)")
                        return
                    }
                }

                // 注册动态连接监听，以便耳机刚开机连接时秒级接管
                val callback = object : SensorManager.DynamicSensorCallback() {
                    override fun onDynamicSensorConnected(sensor: Sensor?) {
                        sensor ?: return
                        if (isTargetHeadphoneSensor(sensor)) {
                            bindExternalSensor(sensorManager, sensor, "动态耳机传感器接入 (${sensor.name})")
                        }
                    }

                    override fun onDynamicSensorDisconnected(sensor: Sensor?) {
                        if (sensor == activeSensor) {
                            unbindExternalSensor("耳机传感器已断开")
                        }
                    }
                }
                sensorManager.registerDynamicSensorCallback(callback)
                dynamicSensorCallback = callback
            } catch (e: Exception) {
                e.printStackTrace()
            }
        }

        // --- 方案 3：Android 13+ Spatializer 空间音频耳机姿态数据流 ---
        if (tryAttachSpatializerPoseListener()) {
            return
        }

        // --- 方案 4：在系统全量传感器中严格过滤外接耳机设备 (绝不匹配手机自身陀螺仪) ---
        val allSensors = sensorManager.getSensorList(Sensor.TYPE_ALL)
        for (s in allSensors) {
            val sName = s.name.lowercase()
            val sVendor = s.vendor.lowercase()
            val sType = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.KITKAT_WATCH) s.stringType.lowercase() else ""

            val matchesHeadphone = s.type == SENSOR_TYPE_HEAD_TRACKER ||
                    sName.contains("linkbuds") || sName.contains("sony") || sName.contains("head tracker") ||
                    sVendor.contains("sony") || sType.contains("head_tracker")

            // 严防手机自带传感器 (剔除常见手机内置陀螺仪芯片品牌)
            val isPhoneInternalChip = sVendor.contains("qualcomm") || sVendor.contains("bosch") ||
                    sVendor.contains("stmicroelectronics") || sVendor.contains("invensense") ||
                    sVendor.contains("akm") || sVendor.contains("google")

            if (matchesHeadphone && !isPhoneInternalChip) {
                bindExternalSensor(sensorManager, s, "系统外置传感器 (${s.name})")
                return
            }
        }

        // 若未找到任何耳机传感器，明确通知状态，绝对不偷梁换柱使用手机陀螺仪！
        isHeadsetDetected = false
        activeSourceDescription = "❌ 未检测到耳机传感器 (请确认已佩戴 Sony LinkBuds 并开启蓝牙)"
        listener.onSensorStatusChanged(activeSourceDescription, false)
    }

    private fun isTargetHeadphoneSensor(s: Sensor): Boolean {
        if (s.type == SENSOR_TYPE_HEAD_TRACKER) return true
        val name = s.name.lowercase()
        val type = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.KITKAT_WATCH) s.stringType.lowercase() else ""
        return name.contains("head") || name.contains("sony") || name.contains("linkbuds") ||
                type.contains("head_tracker") || s.type == Sensor.TYPE_ROTATION_VECTOR ||
                s.type == Sensor.TYPE_GAME_ROTATION_VECTOR || s.type == Sensor.TYPE_GYROSCOPE
    }

    private fun bindExternalSensor(sm: SensorManager, sensor: Sensor, desc: String) {
        activeSensor?.let { sm.unregisterListener(this) }
        activeSensor = sensor
        activeDeviceSensorManager = sm

        sm.registerListener(this, sensor, SensorManager.SENSOR_DELAY_GAME)
        isHeadsetDetected = true
        activeSourceDescription = "✅ $desc"
        listener.onSensorStatusChanged(activeSourceDescription, true)
    }

    private fun unbindExternalSensor(reason: String) {
        activeSensor?.let {
            activeDeviceSensorManager?.unregisterListener(this) ?: sensorManager.unregisterListener(this)
        }
        activeSensor = null
        activeDeviceSensorManager = null
        isHeadsetDetected = false
        activeSourceDescription = "⚠️ $reason"
        listener.onSensorStatusChanged(activeSourceDescription, false)
    }

    /**
     * 尝试通过反射接入 Android 13+ 原生 Spatializer 空间音频耳机姿态流
     */
    private fun tryAttachSpatializerPoseListener(): Boolean {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU) return false

        try {
            val audioManager = context.getSystemService(Context.AUDIO_SERVICE) as AudioManager
            val spatializer = audioManager.spatializer

            // 检查系统当前耳机是否支持头部追踪
            val isHeadTrackerAvailableMethod = spatializer.javaClass.getMethod("isHeadTrackerAvailable")
            val isAvailable = isHeadTrackerAvailableMethod.invoke(spatializer) as? Boolean ?: false
            if (!isAvailable) {
                return false
            }

            val listenerClass = Class.forName("android.media.Spatializer\$OnHeadToSoundstagePoseUpdatedListener")
            val proxy = Proxy.newProxyInstance(
                listenerClass.classLoader,
                arrayOf(listenerClass),
                object : InvocationHandler {
                    override fun invoke(proxy: Any?, method: Method, args: Array<out Any>?): Any? {
                        if (method.name == "onHeadToSoundstagePoseUpdated") {
                            val pose = args?.getOrNull(1) as? FloatArray
                            if (pose != null && pose.size >= 6) {
                                handleSpatializerPose(pose)
                            }
                        }
                        return null
                    }
                }
            )

            val setListenerMethod = spatializer.javaClass.getMethod(
                "setOnHeadToSoundstagePoseUpdatedListener",
                java.util.concurrent.Executor::class.java,
                listenerClass
            )
            val executor = Executors.newSingleThreadExecutor()
            setListenerMethod.invoke(spatializer, executor, proxy)
            spatializerListenerProxy = proxy

            isHeadsetDetected = true
            activeSourceDescription = "✅ Android 空间音频耳机姿态流 (Spatializer)"
            listener.onSensorStatusChanged(activeSourceDescription, true)
            return true
        } catch (e: Exception) {
            // 若系统抛出 SecurityException 或无权限，则继续降级使用 HID 管道
            return false
        }
    }

    private fun removeSpatializerPoseListener() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU && spatializerListenerProxy != null) {
            try {
                val audioManager = context.getSystemService(Context.AUDIO_SERVICE) as AudioManager
                val spatializer = audioManager.spatializer
                val listenerClass = Class.forName("android.media.Spatializer\$OnHeadToSoundstagePoseUpdatedListener")
                val clearMethod = spatializer.javaClass.getMethod(
                    "clearOnHeadToSoundstagePoseUpdatedListener"
                )
                clearMethod.invoke(spatializer)
            } catch (e: Exception) {
                // Ignore
            }
            spatializerListenerProxy = null
        }
    }

    private fun handleSpatializerPose(pose: FloatArray) {
        val now = System.currentTimeMillis()
        val dt = if (lastTimestamp > 0) (now - lastTimestamp) * 0.001f else 0.02f
        lastTimestamp = now

        // Android Spatializer 姿态数组格式：
        // 0..2: 平移 (x, y, z)
        // 3..5: 旋转向量/欧拉角 (rx, ry, rz in radians)
        val radToDeg = (180.0 / PI).toFloat()
        val pitchDeg = pose[3] * radToDeg
        val rollDeg = pose[4] * radToDeg
        val yawDeg = pose[5] * radToDeg

        val validDt = if (dt in 0.005f..0.1f) dt else 0.02f
        val yawRate = (yawDeg - lastYaw) / validDt
        val pitchRate = (pitchDeg - lastPitch) / validDt
        val rollRate = (rollDeg - lastRoll) / validDt

        lastYaw = yawDeg
        lastPitch = pitchDeg
        lastRoll = rollDeg

        listener.onHeadMotion(yawRate, pitchRate, rollRate, yawDeg, pitchDeg, rollDeg)
    }

    override fun onSensorChanged(event: SensorEvent?) {
        event ?: return
        if (!isHeadsetDetected) return

        val now = event.timestamp
        val dt = if (lastTimestamp > 0) (now - lastTimestamp) * 1e-9f else 0.02f
        lastTimestamp = now

        val radToDeg = (180.0 / PI).toFloat()

        if (event.sensor.type == SENSOR_TYPE_HEAD_TRACKER) {
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
        } else if (event.sensor.type == Sensor.TYPE_ROTATION_VECTOR || event.sensor.type == Sensor.TYPE_GAME_ROTATION_VECTOR) {
            val rotMatrix = FloatArray(9)
            SensorManager.getRotationMatrixFromVector(rotMatrix, event.values)
            val orientation = FloatArray(3)
            SensorManager.getOrientation(rotMatrix, orientation)

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
        } else if (event.sensor.type == Sensor.TYPE_GYROSCOPE) {
            val pitchRate = event.values[0] * radToDeg
            val rollRate = event.values[1] * radToDeg
            val yawRate = event.values[2] * radToDeg

            lastPitch += pitchRate * dt
            lastRoll += rollRate * dt
            lastYaw += yawRate * dt

            listener.onHeadMotion(yawRate, pitchRate, rollRate, lastYaw, lastPitch, lastRoll)
        }
    }

    override fun onAccuracyChanged(sensor: Sensor?, accuracy: Int) {}
}
