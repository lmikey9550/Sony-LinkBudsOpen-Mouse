"""
配置持久化管理器：
- 自动读取与保存右键托盘菜单的所有配置项到 config.json
- 每次用户修改设置时即时落盘，下次启动完全恢复用户偏好
"""

import os
import json

CONFIG_FILENAME = "config.json"

DEFAULT_CONFIG = {
    "tracking_enabled": True,
    "invert_x": True,
    "invert_y": False,
    "enable_dwell_click": True,
    "dwell_time": 0.9,
    "enable_nod_click": True,
    "nod_sensitivity": 6,
    "deadzone": 1.2,
    "sensitivity": 0.85,
    "smoothing": 0.75
}

class ConfigManager:
    def __init__(self, base_dir=None):
        if base_dir is None:
            base_dir = os.path.dirname(os.path.abspath(__file__))
        self.config_path = os.path.join(base_dir, CONFIG_FILENAME)
        self.data = DEFAULT_CONFIG.copy()
        self.load()

    def load(self):
        if os.path.exists(self.config_path):
            try:
                with open(self.config_path, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                    if isinstance(saved, dict):
                        self.data.update(saved)
            except Exception as e:
                print(f"读取配置文件失败，使用默认配置: {e}")
        else:
            self.save()

    def save(self):
        try:
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=4, ensure_ascii=False)
        except Exception as e:
            print(f"保存配置文件失败: {e}")

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value
        self.save()
