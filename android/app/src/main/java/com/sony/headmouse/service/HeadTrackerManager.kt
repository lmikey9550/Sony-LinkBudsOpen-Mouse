package com.sony.headmouse.service

import android.Manifest
import android.bluetooth.*
import android.content.Context
import android.content.pm.PackageManager
import android.hardware.Sensor
import android.hardware.SensorEvent
import android.hardware.SensorEventListener
import android.hardware.SensorManager
import android.hardware.input.InputManager
import android.media.AudioManager
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.view.InputDevice
import androidx.core.content.ContextCompat
import com.sony.headmouse.util.PreferenceHelper
import java.lang.reflect.InvocationHandler
import java.lang.reflect.Method
import java.lang.reflect.Proxy
import java.text.SimpleDateFormat
import java.util.*
import java.util.concurrent.Executors
import kotlin.math.PI

/**
 * 专为 Sony LinkBuds / XM 系列打造的全通道外设姿态与连接管理器：
 * 
 * 核心设计：
 * 1. 深度检测：
 *    - 管道 1：蓝牙外设直接发现 (Bonded Bluetooth Devices) 与 BLE GATT 直连
 *    - 管道 2：Android 12+ (API 31+) InputDevice 外设 HID 硬件传感器
 *    - 管道 3：Android 13+ (API 33+) DynamicSensor (动态耳机传感器 TYPE_HEAD_TRACKER)
 *    - 管道 4：Android 13+ Spatializer 空间音频耳机姿态流
 *    - 管道 5：系统外设传感器过滤器
 * 2. 硬件诊断日志控制台：
 *    - 实时记录每一步探测结果，精准告知用户手机与耳机之间的握手状态
 * 3. 严格保护策略：
 *    - 默认严禁手机自带陀螺仪冒充耳机！
 *    - 仅当用户显式开启“手机固定支架备用模式”时才允许兜底。
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
        fun onDiagnosticLog(logText: String)
    }

    private val sensorManager = context.getSystemService(Context.SENSOR_SERVICE) as SensorManager
    private val inputManager = context.getSystemService(Context.INPUT_SERVICE) as InputManager
    private val prefs = PreferenceHelper(context)
    private val mainHandler = Handler(Looper.getMainLooper())

    private var activeSensor: Sensor? = null
    private var activeDeviceSensorManager: SensorManager? = null
    private var bluetoothGatt: BluetoothGatt? = null
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
        val diagnosticLogList = Collections.synchronizedList(ArrayList<String>())

        fun addDiagnosticLog(msg: String) {
            val time = SimpleDateFormat("HH:mm:ss.SSS", Locale.getDefault()).format(Date())
            diagnosticLogList.add("[$time] $msg")
            if (diagnosticLogList.size > 200) {
                diagnosticLogList.removeAt(0)
            }
        }

        fun getFullDiagnosticReport(): String {
            return synchronized(diagnosticLogList) {
                diagnosticLogList.joinToString("\n")
            }
        }
    }

    private fun log(msg: String) {
        addDiagnosticLog(msg)
        mainHandler.post {
            listener.onDiagnosticLog(msg)
        }
    }

    fun start() {
        if (isTracking) return
        isTracking = true
        lastTimestamp = 0
        log("🚀 启动头控姿态探测引擎")
        detectAndBindHeadsetSensor()
    }

    fun stop() {
        if (!isTracking) return
        isTracking = false

        log("⏹ 停止头控姿态追踪")

        // 断开 GATT 连接
        try {
            bluetoothGatt?.disconnect()
            bluetoothGatt?.close()
        } catch (e: Exception) {
            e.printStackTrace()
        }
        bluetoothGatt = null

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
     * 核心检测逻辑
     */
    fun detectAndBindHeadsetSensor() {
        log("--- 开始执行 5 重外设探测 ---")
        log("设备型号: ${Build.MANUFACTURER} ${Build.MODEL}, Android ${Build.VERSION.RELEASE} (SDK ${Build.VERSION.SDK_INT})")

        // --- 方案 1：检测已配对蓝牙设备 (Bonded Bluetooth Devices) 与尝试直连 ---
        if (checkBluetoothAndProbeGatt()) {
            return
        }

        // --- 方案 2：Android 12+ (API 31+) InputDevice 外设蓝牙 HID 硬件传感器 ---
        if (checkInputDeviceSensors()) {
            return
        }

        // --- 方案 3：Android 13+ (API 33+) 动态耳机传感器 (DynamicSensor) ---
        if (checkDynamicSensors()) {
            return
        }

        // --- 方案 4：Android 13+ Spatializer 空间音频姿态流 ---
        if (tryAttachSpatializerPoseListener()) {
            return
        }

        // --- 方案 5：在系统全量传感器中严格匹配外置耳机设备 ---
        if (checkSystemSensors()) {
            return
        }

        // --- 检查是否用户主动开启了“支架备用模式” ---
        if (prefs.allowPhoneMountFallback) {
            val phoneSensor = sensorManager.getDefaultSensor(Sensor.TYPE_GAME_ROTATION_VECTOR)
                ?: sensorManager.getDefaultSensor(Sensor.TYPE_ROTATION_VECTOR)
            if (phoneSensor != null) {
                activeSensor = phoneSensor
                sensorManager.registerListener(this, phoneSensor, SensorManager.SENSOR_DELAY_GAME)
                isHeadsetDetected = true
                activeSourceDescription = "⚠️ 备用模式已生效 (手机固定支架姿态源)"
                log("⚠️ 用户已开启手机支架备用模式，绑定主板传感器: ${phoneSensor.name}")
                listener.onSensorStatusChanged(activeSourceDescription, true)
                return
            }
        }

        // 若均未检测到，安全闭锁
        isHeadsetDetected = false
        activeSourceDescription = "❌ 未检测到耳机传感器 (请确认已佩戴 Sony LinkBuds 并开启蓝牙)"
        log("❌ 5 重探测均未找到耳机传感器数据流")
        listener.onSensorStatusChanged(activeSourceDescription, false)
    }

    /**
     * 方案 1：检查蓝牙已配对设备与尝试建立 GATT/HID 监听
     */
    private fun checkBluetoothAndProbeGatt(): Boolean {
        val hasBtConnect = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            ContextCompat.checkSelfPermission(context, Manifest.permission.BLUETOOTH_CONNECT) == PackageManager.PERMISSION_GRANTED
        } else {
            true
        }

        if (!hasBtConnect) {
            log("⚠️ 未授予 BLUETOOTH_CONNECT 权限，无法直接探测蓝牙外设")
            return false
        }

        val btManager = context.getSystemService(Context.BLUETOOTH_SERVICE) as? BluetoothManager
        val btAdapter = btManager?.adapter
        if (btAdapter == null || !btAdapter.isEnabled) {
            log("⚠️ 手机蓝牙未开启")
            return false
        }

        val bonded = btAdapter.bondedDevices
        log("已配对蓝牙设备数量: ${bonded.size}")

        var matchedDevice: BluetoothDevice? = null
        for (dev in bonded) {
            val name = dev.name ?: "未知设备"
            val addr = dev.address ?: ""
            val isSony = isSonyName(name)
            log("  - 发现配对设备: \"$name\" ($addr)${if (isSony) " [匹配 Sony 外设]" else ""}")
            if (isSony && matchedDevice == null) {
                matchedDevice = dev
            }
        }

        if (matchedDevice != null) {
            log("🎯 正在与 Sony 设备发起连接: ${matchedDevice.name}")
            // 尝试通过 BLE GATT 直连耳机
            try {
                bluetoothGatt?.close()
                bluetoothGatt = matchedDevice.connectGatt(context, false, gattCallback)
            } catch (e: Exception) {
                log("GATT 直连异常: ${e.message}")
            }
        }

        return false
    }

    private val gattCallback = object : BluetoothGattCallback() {
        override fun onConnectionStateChange(gatt: BluetoothGatt?, status: Int, newState: Int) {
            if (newState == BluetoothProfile.STATE_CONNECTED) {
                log("✅ 耳机 GATT 协议通道已连接，正在发现服务...")
                gatt?.discoverServices()
            } else if (newState == BluetoothProfile.STATE_DISCONNECTED) {
                log("⚠️ 耳机 GATT 连接已断开")
            }
        }

        override fun onServicesDiscovered(gatt: BluetoothGatt?, status: Int) {
            if (status == BluetoothGatt.GATT_SUCCESS && gatt != null) {
                log("GATT 服务发现成功，共发现 ${gatt.services.size} 个服务:")
                for (service in gatt.services) {
                    val sUuid = service.uuid.toString()
                    log("  - Service: $sUuid")
                    for (ch in service.characteristics) {
                        val cUuid = ch.uuid.toString()
                        val props = ch.properties
                        val isNotify = (props and BluetoothGattCharacteristic.PROPERTY_NOTIFY) != 0
                        val isIndicate = (props and BluetoothGattCharacteristic.PROPERTY_INDICATE) != 0

                        if (isNotify || isIndicate) {
                            log("    * 具备通知特性的特征值: $cUuid")
                            // 尝试开启通知
                            try {
                                gatt.setCharacteristicNotification(ch, true)
                                val desc = ch.getDescriptor(UUID.fromString("00002902-0000-1000-8000-00805f9b34fb"))
                                if (desc != null) {
                                    desc.value = BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE
                                    gatt.writeDescriptor(desc)
                                    log("    -> 已订阅特征值通知: $cUuid")
                                }
                            } catch (e: Exception) {
                                e.printStackTrace()
                            }
                        }
                    }
                }
            }
        }

        override fun onCharacteristicChanged(gatt: BluetoothGatt?, characteristic: BluetoothGattCharacteristic?) {
            val data = characteristic?.value ?: return
            handleRawGattPayload(data)
        }
    }

    private fun handleRawGattPayload(bytes: ByteArray) {
        if (bytes.size < 7) return

        // 识别 14 字节 HID 报文格式：[ReportID, PitchL, PitchH, RollL, RollH, YawL, YawH]
        val v1 = ((bytes[2].toInt() shl 8) or (bytes[1].toInt() and 0xFF)).toShort()
        val v2 = ((bytes[4].toInt() shl 8) or (bytes[3].toInt() and 0xFF)).toShort()
        val v3 = ((bytes[6].toInt() shl 8) or (bytes[5].toInt() and 0xFF)).toShort()

        val pitchDeg = (v1 * 180.0f / 32768.0f)
        val rollDeg = (v2 * 180.0f / 32768.0f)
        val yawDeg = (v3 * 180.0f / 32768.0f)

        val now = System.currentTimeMillis()
        val dt = if (lastTimestamp > 0) (now - lastTimestamp) * 0.001f else 0.02f
        lastTimestamp = now

        val validDt = if (dt in 0.005f..0.1f) dt else 0.02f
        val yawRate = (yawDeg - lastYaw) / validDt
        val pitchRate = (pitchDeg - lastPitch) / validDt
        val rollRate = (rollDeg - lastRoll) / validDt

        lastYaw = yawDeg
        lastPitch = pitchDeg
        lastRoll = rollDeg

        if (!isHeadsetDetected) {
            isHeadsetDetected = true
            activeSourceDescription = "✅ 蓝牙直接姿态通道 (GATT)"
            log("🎉 成功从耳机 GATT 捕获实时姿态数据流！")
            listener.onSensorStatusChanged(activeSourceDescription, true)
        }

        listener.onHeadMotion(yawRate, pitchRate, rollRate, yawDeg, pitchDeg, rollDeg)
    }

    /**
     * 方案 2：检测 InputDevice 传感器
     */
    private fun checkInputDeviceSensors(): Boolean {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            val deviceIds = inputManager.inputDeviceIds
            log("扫描系统输入外设 (InputDevice, 共 ${deviceIds.size} 个):")
            for (id in deviceIds) {
                val dev = inputManager.getInputDevice(id) ?: continue
                val devName = dev.name ?: ""
                val isSony = isSonyName(devName)
                val hasSensorSource = (dev.sources and InputDevice.SOURCE_SENSOR) != 0

                val dsm = dev.sensorManager
                val sensors = try { dsm.getSensorList(Sensor.TYPE_ALL) } catch (e: Exception) { emptyList() }
                log("  - \"$devName\" (ID $id), sources=${Integer.toHexString(dev.sources)}, 外设传感器数=${sensors.size}")

                if (sensors.isNotEmpty() && (isSony || hasSensorSource || dev.isExternal)) {
                    for (s in sensors) {
                        bindExternalSensor(dsm, s, "蓝牙外设 HID 硬件传感器 (${dev.name})")
                        return true
                    }
                }
            }
        }
        return false
    }

    /**
     * 方案 3：检测 DynamicSensor
     */
    private fun checkDynamicSensors(): Boolean {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.N) {
            try {
                val dynamicList = sensorManager.getDynamicSensorList(Sensor.TYPE_ALL)
                log("扫描系统动态传感器 (DynamicSensor, 共 ${dynamicList.size} 个):")
                for (s in dynamicList) {
                    log("  - DynamicSensor: ${s.name}, Type=${s.type}, Vendor=${s.vendor}")
                    if (isTargetHeadphoneSensor(s)) {
                        bindExternalSensor(sensorManager, s, "动态外接耳机传感器 (${s.name})")
                        return true
                    }
                }

                // 注册动态连接回调
                val callback = object : SensorManager.DynamicSensorCallback() {
                    override fun onDynamicSensorConnected(sensor: Sensor?) {
                        sensor ?: return
                        log("🔔 监听到动态传感器接入: ${sensor.name}")
                        if (isTargetHeadphoneSensor(sensor)) {
                            bindExternalSensor(sensorManager, sensor, "动态耳机传感器接入 (${sensor.name})")
                        }
                    }

                    override fun onDynamicSensorDisconnected(sensor: Sensor?) {
                        log("⚠️ 动态传感器已断开: ${sensor?.name}")
                        if (sensor == activeSensor) {
                            unbindExternalSensor("耳机传感器已断开")
                        }
                    }
                }
                sensorManager.registerDynamicSensorCallback(callback)
                dynamicSensorCallback = callback
            } catch (e: Exception) {
                log("DynamicSensor 检测异常: ${e.message}")
            }
        }
        return false
    }

    /**
     * 方案 4：Spatializer
     */
    private fun tryAttachSpatializerPoseListener(): Boolean {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU) return false

        try {
            val audioManager = context.getSystemService(Context.AUDIO_SERVICE) as AudioManager
            val spatializer = audioManager.spatializer

            val isHeadTrackerAvailableMethod = spatializer.javaClass.getMethod("isHeadTrackerAvailable")
            val isAvailable = isHeadTrackerAvailableMethod.invoke(spatializer) as? Boolean ?: false
            log("Spatializer.isHeadTrackerAvailable() = $isAvailable")
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
            log("✅ 成功接入 Android Spatializer 空间音频耳机姿态流")
            listener.onSensorStatusChanged(activeSourceDescription, true)
            return true
        } catch (e: Exception) {
            log("Spatializer 接入受限: ${e.message}")
            return false
        }
    }

    private fun removeSpatializerPoseListener() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU && spatializerListenerProxy != null) {
            try {
                val audioManager = context.getSystemService(Context.AUDIO_SERVICE) as AudioManager
                val spatializer = audioManager.spatializer
                val clearMethod = spatializer.javaClass.getMethod("clearOnHeadToSoundstagePoseUpdatedListener")
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

    /**
     * 方案 5：在系统全量传感器中严格匹配外置耳机设备
     */
    private fun checkSystemSensors(): Boolean {
        val allSensors = sensorManager.getSensorList(Sensor.TYPE_ALL)
        for (s in allSensors) {
            val sName = s.name.lowercase()
            val sVendor = s.vendor.lowercase()
            val sType = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.KITKAT_WATCH) s.stringType.lowercase() else ""

            val matchesHeadphone = s.type == SENSOR_TYPE_HEAD_TRACKER ||
                    sName.contains("linkbuds") || sName.contains("sony") || sName.contains("head tracker") ||
                    sVendor.contains("sony") || sType.contains("head_tracker")

            val isPhoneInternalChip = sVendor.contains("qualcomm") || sVendor.contains("bosch") ||
                    sVendor.contains("stmicroelectronics") || sVendor.contains("invensense") ||
                    sVendor.contains("akm") || sVendor.contains("google")

            if (matchesHeadphone && !isPhoneInternalChip) {
                bindExternalSensor(sensorManager, s, "系统外置传感器 (${s.name})")
                return true
            }
        }
        return false
    }

    private fun isSonyName(name: String): Boolean {
        val n = name.lowercase()
        return n.contains("linkbuds") || n.contains("sony") || n.contains("wf-") ||
                n.contains("wh-") || n.contains("headphone") || n.contains("headset")
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
        log("🎉 成功绑定姿态传感器: $desc")
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
        log("⚠️ 已解绑传感器: $reason")
        listener.onSensorStatusChanged(activeSourceDescription, false)
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
