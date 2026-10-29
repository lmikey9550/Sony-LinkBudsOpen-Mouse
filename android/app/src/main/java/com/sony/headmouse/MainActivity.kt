package com.sony.headmouse

import android.app.Activity
import android.content.Intent
import android.graphics.Color
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.provider.Settings
import android.widget.*
import com.sony.headmouse.service.HeadMouseAccessibilityService
import com.sony.headmouse.util.Constants
import com.sony.headmouse.util.PreferenceHelper

class MainActivity : Activity(), HeadMouseAccessibilityService.DiagnosticListener {

    private lateinit var prefs: PreferenceHelper

    private lateinit var tvStatus: TextView
    private lateinit var btnAccessibility: Button
    private lateinit var btnOverlay: Button

    private lateinit var tvSensorSource: TextView
    private lateinit var tvSensorAngles: TextView
    private lateinit var btnRedetectSensor: Button

    private lateinit var rgDragMode: RadioGroup
    private lateinit var rbSticky: RadioButton
    private lateinit var rbFlick: RadioButton
    private lateinit var rbDock: RadioButton

    private lateinit var rgRightAction: RadioGroup
    private lateinit var rbBack: RadioButton
    private lateinit var rbHome: RadioButton
    private lateinit var rbRecents: RadioButton

    private lateinit var tvDwellVal: TextView
    private lateinit var sbDwell: SeekBar

    private lateinit var tvTiltVal: TextView
    private lateinit var sbTilt: SeekBar

    private lateinit var btnTest: Button
    private var testCount = 0

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        prefs = PreferenceHelper(this)
        initViews()
        loadPreferences()
        setupListeners()
    }

    override fun onResume() {
        super.onResume()
        HeadMouseAccessibilityService.diagnosticListener = this
        updateServiceStatus()
        updateSensorDiagnostic()
    }

    override fun onPause() {
        super.onPause()
        if (HeadMouseAccessibilityService.diagnosticListener === this) {
            HeadMouseAccessibilityService.diagnosticListener = null
        }
    }

    private fun initViews() {
        tvStatus = findViewById(R.id.tv_service_status)
        btnAccessibility = findViewById(R.id.btn_open_accessibility)
        btnOverlay = findViewById(R.id.btn_open_overlay)

        tvSensorSource = findViewById(R.id.tv_sensor_source)
        tvSensorAngles = findViewById(R.id.tv_sensor_angles)
        btnRedetectSensor = findViewById(R.id.btn_redetect_sensor)

        rgDragMode = findViewById(R.id.rg_drag_mode)
        rbSticky = findViewById(R.id.rb_mode_sticky)
        rbFlick = findViewById(R.id.rb_mode_flick)
        rbDock = findViewById(R.id.rb_mode_dock)

        rgRightAction = findViewById(R.id.rg_right_action)
        rbBack = findViewById(R.id.rb_right_back)
        rbHome = findViewById(R.id.rb_right_home)
        rbRecents = findViewById(R.id.rb_right_recents)

        tvDwellVal = findViewById(R.id.tv_dwell_val)
        sbDwell = findViewById(R.id.sb_dwell)

        tvTiltVal = findViewById(R.id.tv_tilt_val)
        sbTilt = findViewById(R.id.sb_tilt)

        btnTest = findViewById(R.id.btn_test_click)
    }

    private fun loadPreferences() {
        // 拖动方案
        when (prefs.dragMode) {
            Constants.DRAG_MODE_STICKY -> rbSticky.isChecked = true
            Constants.DRAG_MODE_FLICK -> rbFlick.isChecked = true
            Constants.DRAG_MODE_DOCK -> rbDock.isChecked = true
        }

        // 右侧按键映射
        when (prefs.rightAction) {
            Constants.RIGHT_ACTION_BACK -> rbBack.isChecked = true
            Constants.RIGHT_ACTION_HOME -> rbHome.isChecked = true
            Constants.RIGHT_ACTION_RECENTS -> rbRecents.isChecked = true
        }

        // 悬停时间 (0.3s ~ 2.5s, step 0.1s)
        val dwellSec = prefs.dwellTime
        val dwellProgress = ((dwellSec - 0.3f) * 10).toInt()
        sbDwell.progress = dwellProgress
        tvDwellVal.text = String.format("%.1f 秒", dwellSec)

        // 歪头角度 (6° ~ 20°, step 1°)
        val tiltDeg = prefs.tiltThreshold.toInt()
        sbTilt.progress = tiltDeg - 6
        tvTiltVal.text = "${tiltDeg}°"
    }

    private fun setupListeners() {
        btnAccessibility.setOnClickListener {
            val intent = Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS)
            startActivity(intent)
        }

        btnOverlay.setOnClickListener {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
                if (!Settings.canDrawOverlays(this)) {
                    val intent = Intent(
                        Settings.ACTION_MANAGE_OVERLAY_PERMISSION,
                        Uri.parse("package:$packageName")
                    )
                    startActivity(intent)
                } else {
                    Toast.makeText(this, "悬浮窗权限已获取！", Toast.LENGTH_SHORT).show()
                }
            }
        }

        rgDragMode.setOnCheckedChangeListener { _, checkedId ->
            val mode = when (checkedId) {
                R.id.rb_mode_sticky -> Constants.DRAG_MODE_STICKY
                R.id.rb_mode_flick -> Constants.DRAG_MODE_FLICK
                R.id.rb_mode_dock -> Constants.DRAG_MODE_DOCK
                else -> Constants.DRAG_MODE_STICKY
            }
            prefs.dragMode = mode
        }

        rgRightAction.setOnCheckedChangeListener { _, checkedId ->
            val action = when (checkedId) {
                R.id.rb_right_back -> Constants.RIGHT_ACTION_BACK
                R.id.rb_right_home -> Constants.RIGHT_ACTION_HOME
                R.id.rb_right_recents -> Constants.RIGHT_ACTION_RECENTS
                else -> Constants.RIGHT_ACTION_BACK
            }
            prefs.rightAction = action
        }

        sbDwell.setOnSeekBarChangeListener(object : SeekBar.OnSeekBarChangeListener {
            override fun onProgressChanged(seekBar: SeekBar?, progress: Int, fromUser: Boolean) {
                val sec = 0.3f + progress * 0.1f
                tvDwellVal.text = String.format("%.1f 秒", sec)
                prefs.dwellTime = sec
            }
            override fun onStartTrackingTouch(seekBar: SeekBar?) {}
            override fun onStopTrackingTouch(seekBar: SeekBar?) {}
        })

        sbTilt.setOnSeekBarChangeListener(object : SeekBar.OnSeekBarChangeListener {
            override fun onProgressChanged(seekBar: SeekBar?, progress: Int, fromUser: Boolean) {
                val deg = 6 + progress
                tvTiltVal.text = "${deg}°"
                prefs.tiltThreshold = deg.toFloat()
            }
            override fun onStartTrackingTouch(seekBar: SeekBar?) {}
            override fun onStopTrackingTouch(seekBar: SeekBar?) {}
        })

        btnRedetectSensor.setOnClickListener {
            val service = HeadMouseAccessibilityService.instance
            if (service != null) {
                Toast.makeText(this, "正在重新探测 Sony LinkBuds 耳机...", Toast.LENGTH_SHORT).show()
                service.reDetectHeadset()
                updateSensorDiagnostic()
            } else {
                Toast.makeText(this, "请先点击上方开启【无障碍服务】", Toast.LENGTH_LONG).show()
            }
        }

        btnTest.setOnClickListener {
            testCount++
            btnTest.text = "🎯 成功触发触屏点击！累计点击: $testCount 次"
            btnTest.setBackgroundColor(Color.parseColor("#D1E7DD"))
            btnTest.postDelayed({
                btnTest.setBackgroundColor(Color.parseColor("#EAECEF"))
            }, 300)
        }
    }

    private fun updateServiceStatus() {
        val isRunning = HeadMouseAccessibilityService.isServiceRunning
        if (isRunning) {
            tvStatus.text = getString(R.string.status_service_running)
            tvStatus.setTextColor(Color.parseColor("#2ECC71"))
        } else {
            tvStatus.text = getString(R.string.status_service_stopped)
            tvStatus.setTextColor(Color.parseColor("#E74C3C"))
        }
    }

    private fun updateSensorDiagnostic() {
        val service = HeadMouseAccessibilityService.instance
        if (service != null) {
            val desc = service.getSensorDescription()
            val isConnected = service.isHeadsetAttached()
            onSensorStatus(desc, isConnected)
        } else {
            tvSensorSource.text = "⚠️ 无障碍服务未开启 (请点击上方按钮开启服务)"
            tvSensorSource.setTextColor(Color.parseColor("#E67E22"))
            tvSensorAngles.text = "头动角度：等待无障碍服务与耳机连接..."
        }
    }

    override fun onSensorStatus(statusText: String, isConnected: Boolean) {
        runOnUiThread {
            tvSensorSource.text = statusText
            if (isConnected) {
                tvSensorSource.setTextColor(Color.parseColor("#27AE60"))
            } else {
                tvSensorSource.setTextColor(Color.parseColor("#E74C3C"))
            }
        }
    }

    override fun onMotionData(yawDeg: Float, pitchDeg: Float, rollDeg: Float) {
        runOnUiThread {
            tvSensorAngles.text = String.format(
                "实时姿态：Yaw(偏航): %+.1f°  Pitch(俯仰): %+.1f°  Roll(翻滚): %+.1f°",
                yawDeg, pitchDeg, rollDeg
            )
        }
    }
}
