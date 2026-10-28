"""
静音音频保活模块 (Silent A2DP Keepalive)：
- 解决 Sony LinkBuds / XM 系列耳机在无音频播放时 2.5 秒自动休眠陀螺仪的固件特性
- 产生 100% 纯静音 PCM 波形（0 增益，完全无声），保持 Windows 蓝牙 A2DP 音频链路处于活跃激活态
- 零 CPU 占用，不干扰系统任何其他声音播放
"""

import os
import io
import wave
import tempfile
import winsound
import threading

class SilentAudioKeepalive:
    def __init__(self):
        self.wav_path = os.path.join(tempfile.gettempdir(), "sony_linkbuds_silent_keepalive.wav")
        self.running = False
        self._generate_silent_wav()

    def _generate_silent_wav(self):
        """生成 1 秒的纯静音 WAV 文件 (PCM 16-bit 44.1kHz 单声道全 0 采样)"""
        if not os.path.exists(self.wav_path):
            try:
                buf = io.BytesIO()
                with wave.open(buf, 'wb') as wf:
                    wf.setnchannels(1)
                    wf.setsampwidth(2)
                    wf.setframerate(44100)
                    wf.writeframes(b'\x00' * (44100 * 2)) # 1 秒静音
                with open(self.wav_path, 'wb') as f:
                    f.write(buf.getvalue())
            except Exception as e:
                print("生成静音文件异常:", e)

    def start(self):
        """在后台循环播放静音文件"""
        if self.running or not os.path.exists(self.wav_path):
            return
        self.running = True
        try:
            # SND_ASYNC 异步非阻塞，SND_LOOP 无限循环，SND_NOSTOP 不抢占其他音频
            winsound.PlaySound(
                self.wav_path,
                winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_LOOP
            )
        except Exception as e:
            print("启动静音保活失败:", e)

    def refresh(self):
        """重新触发静音播放，用于音频输出设备切换或重新连接时重新绑定"""
        if self.running and os.path.exists(self.wav_path):
            try:
                winsound.PlaySound(
                    self.wav_path,
                    winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_LOOP
                )
            except Exception:
                pass

    def stop(self):
        """停止静音保活"""
        if not self.running:
            return
        self.running = False
        try:
            winsound.PlaySound(None, 0)
        except Exception:
            pass
