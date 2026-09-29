# -*- coding: utf-8 -*-
"""远程大脑：本地桌宠的"薄客户端"模式。

她的真身在云服务器上（记忆/人设/生活/日记只有那一份）。本地桌宠
只是壳——立绘、气泡、聊天窗、活动感知都还在，聊天时把话送到云上
的大脑，拿回回复显示。走 SSH 隧道（127.0.0.1:18787 → 云机 8788），
不暴露公网。

接口和 brain.Brain 对齐，pet.py 可以无感切换。
"""
import base64
import json
import os
import urllib.parse
import urllib.request


def _as_b64(img):
    """把这一轮的图统一成云上要的 base64。

    两边的 img 语义不一样，必须在这里对齐（2026-09-21 踩过）：
      本地 mobile_server 递过来的是**存好的文件路径**（它自己已经落过盘，
      还要拿这个路径塞进本地聊天存档给 App 显示）；
      云上的 /api/chat 要的是**base64 原始字节**（它那边再解码、调视觉模型）。
    已经是 base64 的就原样透传。
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
        """两种喂法都得认（2026-09-21 踩过这个坑）：

        - pet.py 传的是**已经挑好的** {"base":..., "token":...}
        - 别处可能直接把**整个 config** 丢进来，指望这里自己去挖
          brain_remote / brain_token

        以前只认后者：pet.py 传进来的 token 被整个忽略，self.token 恒为 ""
        → 请求到云上就是 401 Unauthorized，而且报的是"她走神了"，
        完全看不出是鉴权问题。现在两种都认，谁传都不丢。
        """
        raw = api_config if isinstance(api_config, dict) else {}
        cfg = raw.get("brain_remote") or {}
        if isinstance(cfg, str):
            cfg = {"base": cfg}
        elif not isinstance(cfg, dict):
            cfg = {}
        # 访问码按优先级找：显式传进来的 > brain_token（新配置）
        # > mobile.token（2026-09-29 之前的老配置，迁移期继续认）
        mob = raw.get("mobile") or {}
        self.base = str(cfg.get("base") or raw.get("base")
                        or "http://127.0.0.1:18787").rstrip("/")
        self.token = str(cfg.get("token") or raw.get("token")
                         or raw.get("brain_token")
                         or mob.get("token") or "")
        # 这几个属性是 pet.py 老代码会探的（本地模型时代留下的）。
        # 本地 4B 已于 2026-09-21 整体移除，远程模式本来也用不到它们，
        # 保留只为兼容任何还在 getattr 兜底的调用点。
        self.model = None
        self.tok = None
        self.life = None
        self.persona = {"name": "角色"}
        try:
            d = self._get("/api/persona")
            if d.get("name"):
                self.persona = {"name": d["name"]}
        except Exception:
            pass

    # ---------- 底层 ----------
    def _post(self, path, payload, timeout=90):
        body = dict(payload)
        url = self.base + path
        if self.token:
            sep = "&" if "?" in url else "?"
            url += sep + "token=" + urllib.parse.quote(self.token)
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    def _get(self, path, timeout=10):
        sep = "&" if "?" in path else "?"
        url = self.base + path
        if self.token:
            url += sep + "token=" + urllib.parse.quote(self.token)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(urllib.request.Request(url), timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    # ---------- pet.py 用到的 Brain 接口 ----------
    def chat(self, q, img=None):
        """img 必须留着 —— pet.py 与 App 两条通道都会无条件
        `brain.chat(text, img=img)` 调，哪怕这一轮没图也会传 img=None。
        签名里少一个 img，整条通道就每一条消息都报 TypeError 全废
        （2026-09-21 实测：「RemoteBrain.chat() got an unexpected keyword
        argument 'img'」，而且是每一条消息都挂，不是只有带图的才挂）。"""
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
    """只读的"她的生活"远程视图 —— 给日记窗口用。

    生活只有云上那一份在过：list_diaries / read_diary 从云上取，
    catch_up / write_diary 是空操作（本地不写，避免分叉）。
    接口和 life_engine.LifeEngine 对齐，DiaryDialog 可以直接用。
    """

    def __init__(self, base, token=""):
        self.base = str(base).rstrip("/")
        self.token = str(token or "")

    def _get(self, path, timeout=15):
        url = self.base + path
        if self.token:
            sep = "&" if "?" in url else "?"
            url += sep + "token=" + urllib.parse.quote(self.token)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(urllib.request.Request(url), timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    # ---------- DiaryDialog 用 ----------
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

    # ---------- pet.py 会调到的（远程模式下都是空操作） ----------
    def catch_up(self, force=False):
        return {}

    def write_diary(self, date_str):
        return ""
