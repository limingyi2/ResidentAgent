# -*- coding: utf-8 -*-
"""远程大脑：本地桌宠的"薄客户端"模式。

她的真身在云服务器上（记忆 / 人设 / 生活 / 日记只有那一份）。本地桌宠只是壳 ——
立绘、气泡、聊天窗、活动感知都还在，聊天时把话送到云上的大脑、拿回复显示。
走 SSH 隧道（127.0.0.1:18787 → 云机 8788），不暴露公网。

接口和 brain.Brain 对齐，pet.py 可以无感切换。
"""
import base64
import json
import os
import urllib.parse
import urllib.request


def _as_b64(img):
    """把这一轮的图统一成云上要的 base64。

    两边的 img 语义不一样，必须在这里对齐：本地递过来的是**已落盘的文件路径**，
    云上的 /api/chat 要的是 **base64 原始字节**。已经是 base64 的就原样透传。
    """
    if not img:
        return ""
    s = str(img)
    if os.path.exists(s):
        try:
            with open(s, "rb") as f:
                return base64.b64encode(f.read()).decode("ascii")
        except Exception:
            return ""
    return s


class RemoteBrain:

    def __init__(self, api_config):
        """两种喂法都得认：pet.py 传的是**已经挑好的** {"base":…, "token":…}，
        别处可能直接把**整个 config** 丢进来、指望这里自己去挖 brain_remote / brain_token。

        只认后者的话，pet.py 传进来的 token 被整个忽略、self.token 恒为 ""，请求到云上就是
        401，而且报出来的是"她走神了"，完全看不出是鉴权问题。
        """
        raw = api_config if isinstance(api_config, dict) else {}
        cfg = raw.get("brain_remote") or {}
        if isinstance(cfg, str):
            cfg = {"base": cfg}
        elif not isinstance(cfg, dict):
            cfg = {}
        # 访问码优先级：显式传进来的 > brain_token（新配置）> mobile.token（老配置）
        mob = raw.get("mobile") or {}
        self.base = str(cfg.get("base") or raw.get("base")
                        or "http://127.0.0.1:18787").rstrip("/")
        self.token = str(cfg.get("token") or raw.get("token")
                         or raw.get("brain_token")
                         or mob.get("token") or "")
        # 这几个属性是 pet.py 会 getattr 探测的，本地模型时代留下的，保留只为兼容
        self.model = None
        self.tok = None
        self.life = None
        # 人设名从云端拉，拉不到就用角色自己的名字（见 /api/persona）。
        # 不写死任何角色名 —— 换了人设这里不用改。
        self.persona = {"name": ""}
        try:
            d = self._get("/api/persona")
            if d.get("name"):
                self.persona = {"name": d["name"]}
        except Exception:
            pass

    # --- 底层 ---
    def _post(self, path, payload, timeout=90):
        """POST 一份 JSON 到云上（鉴权走 Authorization 头，不再拼进 URL）。

        URL 里的 token 会进各级访问日志和 Referer；媒体 URL（<img>/<audio>）
        没法带头，那些还保留 ?token=，服务端两种都认。
        """
        url = self.base + path
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + self.token},
            method="POST")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    def _get(self, path, timeout=10):
        """GET 一份 JSON（同样走 Authorization 头）。

        这里走的是 /api/persona、/api/history、/api/diary/* 这类**纯 JSON 接口**，
        全都能带请求头 —— 所以 token 不该再出现在 URL 上。真正带不了头的只有
        <img>/<audio> 的媒体地址，那些在客户端侧单独拼 ?token=。
        """
        req = urllib.request.Request(
            self.base + path,
            headers={"Authorization": "Bearer " + self.token})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    # --- pet.py 用到的 Brain 接口 ---
    def chat(self, q, img=None):
        """img 必须留着 —— pet.py 与 App 两条通道都会无条件 `brain.chat(text, img=img)`
        调，哪怕这一轮没图也会传 img=None。签名里少一个 img，整条通道**每一条消息**都报
        TypeError，不是只有带图的才挂。
        """
        payload = {"text": q}
        if img:
            b64 = _as_b64(img)
            if b64:
                payload["img"] = b64
        d = self._post("/api/chat", payload)
        err = d.get("err") or ""
        if err and not d.get("reply"):
            raise RuntimeError(err)
        return d.get("reply") or "", d.get("mode") or "远程"

    def speak_on_scene(self, scene):
        d = self._post("/api/proactive", {"scene": scene})
        return d.get("reply") or "", d.get("mode") or "远程"

    def seed_history(self):
        return 0        # 聊天记录在云上，本地不用灌

    def read_history(self, limit=40):
        """读云上的聊天存档（pet.py 启动时灌回聊天窗用）。

        Brain.read_history 是本地时代的接口，远程大脑上原本没有这个方法 ——
        pet.py 直接调 self.pet.brain.read_history()，AttributeError 被 except 吞掉，
        于是每次启动聊天窗都是空的，看着像"她重启后忘了刚才聊的"。
        存档只有云上那一份，走 /api/history。
        """
        try:
            d = self._get("/api/history?limit=%d" % int(limit or 40))
            return d.get("items") or []
        except Exception:
            return []

    def clear_history(self):
        """清空云上的聊天存档（语义和本地 Brain.clear_history 对齐）。

        远程模式下必须打到云上：本地 Brain.clear_history() 删的是**本机**文件，
        云上那份一动不动 —— 看着清了其实没清。
        """
        try:
            d = self._post("/api/history/clear", {})
            return bool(d.get("ok"))
        except Exception:
            return False

    def reset(self):
        try:
            self._post("/api/reset", {})
        except Exception:
            pass

    def reload_persona(self):
        try:
            self._post("/api/reload_persona", {})
        except Exception:
            pass


class RemoteLife:
    """她的生活的远程视图 —— 给日记窗口和后台补算用。

    生活只有云上那一份在过，所以四个方法全打到云上：
    list_diaries / read_diary 读，catch_up / write_diary 写。
    以前后两个是**空操作**，于是 LifeWorker 每次开机都空转一趟、
    托盘里的"让她现在写篇日记"永远只会报失败。接口和 life_engine.LifeEngine 对齐。
    """

    def __init__(self, base, token=""):
        self.base = str(base).rstrip("/")
        self.token = str(token or "")

    def _get(self, path, timeout=15):
        url = self.base + path
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        req = urllib.request.Request(
            url, headers={"Authorization": "Bearer " + self.token})
        with opener.open(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    def _post(self, path, payload, timeout=180):
        url = self.base + path
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + self.token},
            method="POST")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    # --- DiaryDialog 用 ---
    def list_diaries(self):
        try:
            return self._get("/api/diary/list").get("days") or []
        except Exception:
            return []

    def read_diary(self, date_str):
        try:
            sep = "&" if "?" in "/api/diary/read" else "?"
            d = self._get("/api/diary/read" + sep + "date=" +
                          urllib.parse.quote(str(date_str)))
            return d.get("body") or ""
        except Exception:
            return ""

    # --- pet.py 会调到的 ---
    def catch_up(self, force=False):
        """让云上把她的生活补到当前时刻（会调模型，可能几十秒，超时给到 180s）。"""
        try:
            d = self._post("/api/life/catchup", {"force": bool(force)}) or {}
            if not d.get("ok"):
                return {}
            return {"events": int(d.get("events") or 0),
                    "diary": int(d.get("diary") or 0),
                    "skip": d.get("skip") or ""}
        except Exception:
            return {}

    def write_diary(self, date_str):
        """让云上给她写某天的日记，返回正文（失败返回 ""）。"""
        try:
            d = self._post("/api/diary/write",
                           {"date": str(date_str or "")}) or {}
            return d.get("body") or ""
        except Exception:
            return ""
