# -*- coding: utf-8 -*-
"""设置窗口 —— 让用户自己调人设和主动搭话策略，不用碰代码。

页签：人设（名字 / 小名 / 描述 / 场景 / 性格 / 称呼 / 备注，可多套随时切）、
主动搭话（开关、每日上限、最小间隔、免打扰时段）、
时间感知（让她知道现在几点 + 你离开多久算"刚回来"）、
桌面显示（置顶窗口 + 全屏 / 打游戏时自动让开）。

保存后调用 pet.apply_settings() 热更新，不用重启桌宠。
"""
import os
import json

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QTabWidget, QWidget,
    QLineEdit, QTextEdit, QCheckBox, QSpinBox, QComboBox, QPushButton,
    QLabel, QInputDialog, QMessageBox, QGroupBox, QRadioButton,
)
from PyQt6.QtCore import Qt

from persona_store import (
    list_personas, load as load_persona, save as save_persona, key_from_config,
)

DEFAULT_PROACTIVE = {
    "enabled": True, "max_per_day": 8, "interval_sec": 180,
    "quiet_start": "23:00", "quiet_end": "08:00",
    "on_activity_change": True, "on_game_start": True, "on_startup": True,
}

try:
    import paths
    PERSONA_DIR = paths.PERSONA_DIR
    CONFIG_PATH = paths.CONFIG_PATH
except Exception:
    _ROOT = os.path.dirname(os.path.abspath(__file__))
    PERSONA_DIR = os.path.join(_ROOT, "config", "personas")
    CONFIG_PATH = os.path.join(_ROOT, "config", "config.json")


class SettingsDialog(QDialog):
    def __init__(self, pet, api_config):
        super().__init__()
        self.pet = pet
        self.api = api_config
        self.persona_key = key_from_config(api_config)
        self.cur = load_persona(self.persona_key)

        self.setWindowTitle("角色 · 设置")
        self.setMinimumSize(440, 520)
        self.setStyleSheet("QDialog{background:#fff5f8;}")

        root = QVBoxLayout(self)
        tabs = QTabWidget()
        root.addWidget(tabs, 1)

        tabs.addTab(self._persona_tab(), "人设")
        tabs.addTab(self._proactive_tab(), "主动搭话")
        tabs.addTab(self._time_tab(), "时间感知")
        tabs.addTab(self._window_tab(), "桌面显示")

        # 按钮
        bar = QHBoxLayout()
        bar.addStretch()
        b_cancel = QPushButton("取消")
        b_cancel.clicked.connect(self.reject)
        b_save = QPushButton("保存")
        b_save.setStyleSheet(
            "QPushButton{background:#e06c9f; color:white; border:0;"
            " border-radius:6px; padding:7px 22px; font-weight:bold;}"
            " QPushButton:hover{background:#d6588e;}")
        b_save.clicked.connect(self._save)
        bar.addWidget(b_cancel)
        bar.addWidget(b_save)
        root.addLayout(bar)

        self._load_values()

    # --- 人设页 ---
    def _persona_tab(self):
        w = QWidget()
        v = QVBoxLayout(w)

        top = QHBoxLayout()
        top.addWidget(QLabel("当前人设："))
        self.cb_persona = QComboBox()
        for key, label in list_personas():
            self.cb_persona.addItem(f"{label}（{key}）", key)
        top.addWidget(self.cb_persona, 1)
        b_saveas = QPushButton("另存为…")
        b_saveas.setToolTip("把当前内容存成一套新的人设，方便做多套切换")
        b_saveas.clicked.connect(self._save_as)
        top.addWidget(b_saveas)
        v.addLayout(top)
        self.cb_persona.currentIndexChanged.connect(self._switch_persona)

        form = QFormLayout()
        self.ed_name = QLineEdit()
        self.ed_nick = QLineEdit()
        self.ed_nick.setPlaceholderText("多个小名用 / 分隔，如：角色/角色")
        self.ed_call = QLineEdit()
        self.ed_background = QTextEdit(); self.ed_background.setFixedHeight(70)
        self.ed_scene = QTextEdit(); self.ed_scene.setFixedHeight(52)
        self.ed_personality = QTextEdit(); self.ed_personality.setFixedHeight(64)
        self.ed_notes = QTextEdit(); self.ed_notes.setFixedHeight(52)
        self.ed_notes.setPlaceholderText("给 AI 的内部指令（会原样拼进提示）")

        form.addRow("名字", self.ed_name)
        form.addRow("小名", self.ed_nick)
        form.addRow("她怎么称呼你", self.ed_call)
        form.addRow("人设描述", self.ed_background)
        form.addRow("当前场景", self.ed_scene)
        form.addRow("性格特点", self.ed_personality)
        form.addRow("创作者备注", self.ed_notes)
        v.addLayout(form)
        v.addStretch()
        return w

    # --- 主动搭话页 ---
    def _proactive_tab(self):
        w = QWidget()
        v = QVBoxLayout(w)

        self.cb_pro_on = QCheckBox("允许她主动搭话")
        v.addWidget(self.cb_pro_on)

        form = QFormLayout()
        self.sp_max = QSpinBox(); self.sp_max.setRange(0, 99); self.sp_max.setSuffix(" 条/天")
        self.sp_max.setToolTip("到量之后当天就不再主动开口")
        self.sp_gap = QSpinBox(); self.sp_gap.setRange(1, 600); self.sp_gap.setSuffix(" 分钟")
        self.sp_gap.setToolTip("两次主动搭话之间至少隔多久")
        self.ed_qs = QLineEdit(); self.ed_qs.setPlaceholderText("23:00")
        self.ed_qe = QLineEdit(); self.ed_qe.setPlaceholderText("08:00")
        qrow = QHBoxLayout()
        qrow.addWidget(QLabel("从")); qrow.addWidget(self.ed_qs)
        qrow.addWidget(QLabel("到")); qrow.addWidget(self.ed_qe)
        qrow.addStretch()
        qw = QWidget(); qw.setLayout(qrow)

        form.addRow("每天最多", self.sp_max)
        form.addRow("最小间隔", self.sp_gap)
        form.addRow("免打扰时段", qw)
        v.addLayout(form)

        v.addWidget(QLabel("什么情况下主动开口："))
        self.cb_act = QCheckBox("你切换活动时（写代码→刷B站）")
        self.cb_game = QCheckBox("你开始打游戏时")
        self.cb_start = QCheckBox("桌宠启动时打个招呼")
        v.addWidget(self.cb_act)
        v.addWidget(self.cb_game)
        v.addWidget(self.cb_start)
        v.addStretch()
        return w

    # --- 时间感知页 ---
    def _time_tab(self):
        w = QWidget()
        v = QVBoxLayout(w)
        self.cb_time = QCheckBox("让她知道现在几点（聊起来更自然）")
        v.addWidget(self.cb_time)
        form = QFormLayout()
        self.sp_absence = QSpinBox(); self.sp_absence.setRange(1, 1440)
        self.sp_absence.setSuffix(" 分钟")
        self.sp_absence.setToolTip("隔这么久没聊，她会觉得你'刚回来'")
        form.addRow("多久算离开过", self.sp_absence)
        v.addLayout(form)
        v.addWidget(QLabel("说明：开启后，她的系统提示里会带上当前时间和\n你上次说话距今多久。纯本地拼装，不额外调用 API。"))
        v.addStretch()
        return w

    # --- 桌面显示页 ---
    def _window_tab(self):
        w = QWidget()
        v = QVBoxLayout(w)

        box = QGroupBox("窗口层级")
        bv = QVBoxLayout(box)
        self.cb_top = QCheckBox("置顶窗口（压在所有窗口上面）")
        self.cb_top.setToolTip("勾上：别的软件都盖不住她，包括全屏游戏；"
                               "取消：她沉在所有窗口下面，任何软件都能盖住她")
        bv.addWidget(self.cb_top)
        v.addWidget(box)

        self.cb_sink = QCheckBox("全屏 / 打游戏时自动躲到后面")
        self.cb_sink.setToolTip(
            "让她自己判断你是不是在打游戏/看全屏，是的话临时沉底，退出后回到你选的那层。"
            "默认关着 —— 你要的是手动控制，不要她自己判断。")
        v.addWidget(self.cb_sink)

        tip = QLabel(
            "默认不勾「自动躲开」：她不会自己判断，层级完全由上面这个勾决定。\n"
            "右键立绘也能随手切「置顶窗口」。\n"
            "这些存在 config.json 的 window 段里。")
        tip.setWordWrap(True)
        tip.setStyleSheet("color:#8a7f86; font-size:12px;")
        v.addWidget(tip)
        v.addStretch()
        return w

    # --- 读写 ---
    def _load_values(self):
        p = self.cur
        self.ed_name.setText(p.get("name", ""))
        self.ed_nick.setText("/".join(p.get("nicknames") or []))
        self.ed_call.setText(p.get("call_user", ""))
        self.ed_background.setPlainText(p.get("background", ""))
        self.ed_scene.setPlainText(p.get("scene", ""))
        self.ed_personality.setPlainText(p.get("personality", ""))
        self.ed_notes.setPlainText(p.get("notes", ""))
        # 同步下拉选择
        idx = self.cb_persona.findData(self.persona_key)
        if idx >= 0:
            self.cb_persona.blockSignals(True)
            self.cb_persona.setCurrentIndex(idx)
            self.cb_persona.blockSignals(False)

        pro = dict(DEFAULT_PROACTIVE)
        if isinstance(self.api.get("proactive"), dict):
            pro.update(self.api["proactive"])
        # 间隔从 watcher.title_proactive_interval 取（秒）
        try:
            pro["interval_sec"] = int(self.api.get("watcher", {})
                                      .get("title_proactive_interval", pro["interval_sec"]))
        except Exception:
            pass
        self.cb_pro_on.setChecked(bool(pro["enabled"]))
        self.sp_max.setValue(int(pro["max_per_day"]))
        self.sp_gap.setValue(max(1, int(pro["interval_sec"]) // 60))
        self.ed_qs.setText(pro.get("quiet_start", ""))
        self.ed_qe.setText(pro.get("quiet_end", ""))
        self.cb_act.setChecked(bool(pro.get("on_activity_change", True)))
        self.cb_game.setChecked(bool(pro.get("on_game_start", True)))
        self.cb_start.setChecked(bool(pro.get("on_startup", True)))

        ta = self.api.get("time_awareness")
        if not isinstance(ta, dict):
            ta = {"enabled": True, "absence_minutes": 30}
        self.cb_time.setChecked(bool(ta.get("enabled", True)))
        self.sp_absence.setValue(int(ta.get("absence_minutes", 30)))

        wc = self.api.get("window")
        if not isinstance(wc, dict):
            wc = {}
        # 窗口层级：置顶/沉底 两档。老配置是 always_on_top 布尔值 / 三档，这里迁移
        lv = str(wc.get("level") or "").strip().lower()
        if lv not in ("top", "bottom"):
            lv = "top" if wc.get("always_on_top", True) else "bottom"
        self.cb_top.setChecked(lv == "top")
        self.cb_sink.setChecked(bool(wc.get("sink_on_fullscreen", False)))

    def _collect_persona(self):
        return {
            "name": self.ed_name.text().strip(),
            "nicknames": [n.strip() for n in self.ed_nick.text().split("/") if n.strip()],
            "call_user": self.ed_call.text().strip(),
            "background": self.ed_background.toPlainText().strip(),
            "scene": self.ed_scene.toPlainText().strip(),
            "personality": self.ed_personality.toPlainText().strip(),
            "notes": self.ed_notes.toPlainText().strip(),
        }

    def _switch_persona(self, index):
        key = self.cb_persona.itemData(index)
        if not key or key == self.persona_key:
            return
        self.persona_key = key
        self.cur = load_persona(key)
        self._load_values()

    def _save_as(self):
        key, ok = QInputDialog.getText(self, "另存为", "新人设的标识（英文/数字，别带空格）：")
        key = (key or "").strip()
        if not ok or not key:
            return
        if os.path.exists(os.path.join(PERSONA_DIR, f"{key}.json")):
            QMessageBox.warning(self, "已存在", f"{key}.json 已经存在了，换个名字。")
            return
        save_persona(self._collect_persona(), key)
        self.cb_persona.addItem(f"{self.ed_name.text() or key}（{key}）", key)
        self.persona_key = key
        self.cb_persona.setCurrentIndex(self.cb_persona.count() - 1)
        QMessageBox.information(self, "已保存", f"新人设已保存为 {key}.json，别忘了点「保存」让它生效。")

    def _save(self):
        # 时间格式校验
        qs, qe = self.ed_qs.text().strip(), self.ed_qe.text().strip()
        for t in (qs, qe):
            if t and (len(t.split(":")) != 2 or
                      not all(x.isdigit() for x in t.split(":"))):
                QMessageBox.warning(self, "格式不对",
                                    "免打扰时间要用 24 小时制，像 23:00 这样。")
                return

        # 人设
        save_persona(self._collect_persona(), self.persona_key)

        # 主动搭话 + 时间感知 写回 config
        interval_sec = self.sp_gap.value() * 60
        self.api["persona_file"] = f"personas/{self.persona_key}.json"
        self.api["proactive"] = {
            "enabled": self.cb_pro_on.isChecked(),
            "max_per_day": self.sp_max.value(),
            "interval_sec": interval_sec,
            "quiet_start": qs,
            "quiet_end": qe,
            "on_activity_change": self.cb_act.isChecked(),
            "on_game_start": self.cb_game.isChecked(),
            "on_startup": self.cb_start.isChecked(),
        }
        self.api["time_awareness"] = {
            "enabled": self.cb_time.isChecked(),
            "absence_minutes": self.sp_absence.value(),
        }
        lv = "top" if self.cb_top.isChecked() else "bottom"
        self.api["window"] = {
            "level": lv,
            "sink_on_fullscreen": self.cb_sink.isChecked(),
        }
        w = self.api.setdefault("watcher", {})
        w["title_proactive_interval"] = interval_sec

        try:
            json.dump(self.api, open(CONFIG_PATH, "w", encoding="utf-8"),
                      ensure_ascii=False, indent=2)
        except Exception as e:
            QMessageBox.critical(self, "保存失败", str(e))
            return

        # 热更新
        try:
            self.pet.apply_settings()
        except Exception as e:
            print("[设置] 热更新失败：", e, flush=True)
        self.accept()
