package com.sony.headmouse.motion

import kotlin.math.abs
import kotlin.math.hypot
import kotlin.math.pow

/**
 * 移植自 OpenTrack 的 Accela 动力学自适应滤波器 (Kotlin 版)
 * - 死区 (Deadzone)：彻底过滤掉微小的呼吸、心跳等生理震颤
 * - 动力学曲线 (Nonlinear Response)：慢速转头像素级精准点击，快速转头快速跟手
 * - 低通平滑 (Smoothing)：一阶 IIR 滤波消除阶跃抖动
 */
class AccelaFilter(
    private var deadzone: Float = 1.0f,
    private var sensitivity: Float = 1.0f,
    private var smoothing: Float = 0.72f
) {
    private var lastOutputX: Float = 0f
    private var lastOutputY: Float = 0f

    fun filter(inputX: Float, inputY: Float): Pair<Float, Float> {
        val magnitude = hypot(inputX.toDouble(), inputY.toDouble()).toFloat()

        if (magnitude <= deadzone) {
            // 在死区范围内直接归零，彻底保持光标绝对静止 (为悬停点击提供极佳的稳定性)
            lastOutputX = 0f
            lastOutputY = 0f
            return Pair(0f, 0f)
        }

        // 减去死区后的有效动力学速度
        val effectiveMag = magnitude - deadzone

        // Accela 非线性响应曲线 (慢速平滑，快速灵敏)
        val gain = (effectiveMag / 10f).toDouble().pow(1.35).toFloat() * sensitivity + 0.15f
        val targetX = (inputX / magnitude) * effectiveMag * gain
        val targetY = (inputY / magnitude) * effectiveMag * gain

        // 一阶 IIR 低通平滑
        val outX = lastOutputX * smoothing + targetX * (1f - smoothing)
        val outY = lastOutputY * smoothing + targetY * (1f - smoothing)

        lastOutputX = outX
        lastOutputY = outY

        return Pair(outX, outY)
    }

    fun reset() {
        lastOutputX = 0f
        lastOutputY = 0f
    }
}
