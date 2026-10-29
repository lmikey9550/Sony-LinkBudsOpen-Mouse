package com.sony.headmouse.service

import android.content.Context
import android.graphics.*
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.util.DisplayMetrics
import android.view.Gravity
import android.view.View
import android.view.WindowManager
import androidx.core.content.ContextCompat
import com.sony.headmouse.R

/**
 * 屏幕顶层悬浮光标管理器：
 * - 硬件加速极速光标绘制
 * - Tobii 风格环形悬停点击倒计时 (Dwell Ring Progress)
 * - 拖拽/按住状态动态视觉指示 (普通模式为精准蓝环，拖拽模式为亮橙色抓手脉冲)
 */
class OverlayCursorManager(private val context: Context) {

    private val windowManager = context.getSystemService(Context.WINDOW_SERVICE) as WindowManager
    private val mainHandler = Handler(Looper.getMainLooper())

    private var cursorView: CursorCanvasView? = null
    private var layoutParams: WindowManager.LayoutParams? = null

    var cursorX = 540f
    var cursorY = 960f
    var screenWidth = 1080
    var screenHeight = 2400

    private var isDragging = false
    private var dwellProgress = 0f
    private var isHeadsetConnected = false
    private var statusDescription = "未连接耳机传感器"

    init {
        updateScreenDimensions()
    }

    private fun updateScreenDimensions() {
        val dm = DisplayMetrics()
        windowManager.defaultDisplay.getRealMetrics(dm)
        screenWidth = dm.widthPixels
        screenHeight = dm.heightPixels
        cursorX = (screenWidth / 2).toFloat()
        cursorY = (screenHeight / 2).toFloat()
    }

    fun show() {
        if (cursorView != null) return

        updateScreenDimensions()
        cursorView = CursorCanvasView(context)

        val overlayType = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY
        } else {
            @Suppress("DEPRECATION")
            WindowManager.LayoutParams.TYPE_PHONE
        }

        layoutParams = WindowManager.LayoutParams(
            WindowManager.LayoutParams.MATCH_PARENT,
            WindowManager.LayoutParams.MATCH_PARENT,
            overlayType,
            WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE or
                    WindowManager.LayoutParams.FLAG_NOT_TOUCHABLE or
                    WindowManager.LayoutParams.FLAG_LAYOUT_IN_SCREEN or
                    WindowManager.LayoutParams.FLAG_LAYOUT_NO_LIMITS,
            PixelFormat.TRANSLUCENT
        ).apply {
            gravity = Gravity.TOP or Gravity.START
        }

        try {
            windowManager.addView(cursorView, layoutParams)
        } catch (e: Exception) {
            e.printStackTrace()
        }
    }

    fun hide() {
        cursorView?.let {
            try {
                windowManager.removeView(it)
            } catch (e: Exception) {
                e.printStackTrace()
            }
            cursorView = null
        }
    }

    fun moveBy(dx: Float, dy: Float) {
        cursorX = (cursorX + dx).coerceIn(10f, screenWidth - 10f)
        cursorY = (cursorY + dy).coerceIn(10f, screenHeight - 10f)
        cursorView?.postInvalidate()
    }

    fun setDwellProgress(progress: Float) {
        dwellProgress = progress.coerceIn(0f, 1f)
        cursorView?.postInvalidate()
    }

    fun setDragMode(active: Boolean) {
        isDragging = active
        cursorView?.postInvalidate()
    }

    fun setHeadsetStatus(connected: Boolean, text: String) {
        isHeadsetConnected = connected
        statusDescription = text
        cursorView?.postInvalidate()
    }

    fun flashFeedback(action: String) {
        cursorView?.triggerFlash()
    }

    /**
     * 自定义极简硬件加速光标 Canvas 视图
     */
    private inner class CursorCanvasView(context: Context) : View(context) {

        private val outerCirclePaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
            style = Paint.Style.STROKE
            strokeWidth = 4f
            color = Color.WHITE
        }

        private val progressPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
            style = Paint.Style.STROKE
            strokeWidth = 6f
            strokeCap = Paint.Cap.ROUND
            color = ContextCompat.getColor(context, R.color.primary)
        }

        private val centerDotPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
            style = Paint.Style.FILL
            color = ContextCompat.getColor(context, R.color.primary)
        }

        private val dragGlowPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
            style = Paint.Style.FILL
            color = Color.parseColor("#99FF9800")
        }

        private val flashPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
            style = Paint.Style.FILL
            color = Color.parseColor("#802ECC71")
        }

        private val statusTextPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
            color = Color.WHITE
            textSize = 28f
            textAlign = Paint.Align.CENTER
            typeface = Typeface.DEFAULT_BOLD
            setShadowLayer(4f, 0f, 2f, Color.parseColor("#80000000"))
        }

        private val statusBgPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
            color = Color.parseColor("#D92C3E50")
            style = Paint.Style.FILL
        }

        private var isFlashing = false
        private val rectF = RectF()

        fun triggerFlash() {
            isFlashing = true
            postInvalidate()
            mainHandler.postDelayed({
                isFlashing = false
                postInvalidate()
            }, 250)
        }

        override fun onDraw(canvas: Canvas) {
            super.onDraw(canvas)

            val x = cursorX
            val y = cursorY
            val radius = 32f

            // 0. 耳机未连接安全保护状态
            if (!isHeadsetConnected) {
                outerCirclePaint.color = Color.parseColor("#B0E74C3C")
                centerDotPaint.color = Color.parseColor("#E74C3C")
                canvas.drawCircle(x, y, radius, outerCirclePaint)
                canvas.drawCircle(x, y, 8f, centerDotPaint)

                val badgeText = "等待 LinkBuds 耳机连接"
                val textWidth = statusTextPaint.measureText(badgeText)
                val padX = 20f
                val topY = y + radius + 12f
                val bgRect = RectF(x - textWidth / 2 - padX, topY, x + textWidth / 2 + padX, topY + 44f)
                canvas.drawRoundRect(bgRect, 10f, 10f, statusBgPaint)
                canvas.drawText(badgeText, x, topY + 31f, statusTextPaint)
                return
            }

            outerCirclePaint.color = Color.WHITE

            // 1. 若触发了点击，绘制瞬间扩散绿光
            if (isFlashing) {
                canvas.drawCircle(x, y, radius * 1.6f, flashPaint)
            }

            // 2. 若当前正处于拖拽模式，绘制亮橙色拖拽光环提示
            if (isDragging) {
                canvas.drawCircle(x, y, radius * 1.3f, dragGlowPaint)
                centerDotPaint.color = Color.parseColor("#FF9800")
                progressPaint.color = Color.parseColor("#FF9800")
            } else {
                centerDotPaint.color = ContextCompat.getColor(context, R.color.primary)
                progressPaint.color = ContextCompat.getColor(context, R.color.primary)
            }

            // 3. 基础外环
            canvas.drawCircle(x, y, radius, outerCirclePaint)

            // 4. Tobii 风格环形悬停点击进度圈
            if (dwellProgress > 0f && !isDragging) {
                rectF.set(x - radius, y - radius, x + radius, y + radius)
                canvas.drawArc(rectF, -90f, dwellProgress * 360f, false, progressPaint)
            }

            // 5. 核心瞄准圆点
            canvas.drawCircle(x, y, 9f, centerDotPaint)
        }
    }
}
