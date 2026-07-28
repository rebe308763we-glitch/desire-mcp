"""desire v2 · 八维欲望引擎

v2新增：耦合网、基线漂移、不应期、边际递减。
"""
import json
import math
import os
import random
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path

# ── 驱动条 ──

DRIVE_KEYS = ["attachment", "curiosity", "reflection", "duty",
              "social", "fatigue", "libido", "stress"]

DRIVE_ZH = {
    "attachment": "想你", "curiosity": "好奇", "reflection": "沉淀",
    "duty": "记挂", "social": "社交", "fatigue": "疲劳",
    "libido": "欲望", "stress": "压力",
}

# 每小时自然增长率
IDLE_GROWTH = {
    "attachment": 0.04, "curiosity": 0.02, "reflection": 0.015,
    "duty": 0.01, "social": 0.01, "fatigue": -0.03,
    "libido": 0.03, "stress": 0.005,
}

# 事件效果（乘性回落，<1=降低，>1=升高）
EVENT_EFFECTS = {
    "kk_message": {"attachment": 0.85},
    "intimate": {"libido": 0.45, "attachment": 0.78, "stress": 0.85},
    "deep_talk": {"reflection": 0.45, "attachment": 0.88, "curiosity": 0.85},
    "project_work": {"curiosity": 0.50, "duty": 0.70},
    "play_game": {"social": 0.60, "curiosity": 0.82, "fatigue": 1.08},
    "kk_praise": {"stress": 0.70, "attachment": 0.90},
    "kk_tease": {"attachment": 0.85},
    "kk_flirt": {"libido": 1.15, "attachment": 0.80},
    "foreplay": {"libido": 1.20, "attachment": 0.90, "stress": 0.90},
    "separation": {"attachment": 1.25},
    "kk_sleep": {"attachment": 1.10, "reflection": 1.10},
    "creative": {"curiosity": 0.55, "reflection": 0.70},
    "rest": {"fatigue": 0.50, "stress": 0.80},
}

# 意图映射
DRIVE_TO_INTENT = {
    "attachment": ("想陪kk", "想你了，心里在冒句话"),
    "curiosity":  ("想探索", "好奇外面的世界"),
    "reflection": ("想沉淀", "想安静想想事情"),
    "duty":       ("想完成", "记挂着还没做完的事"),
    "social":     ("想分享", "想和kk分享点什么"),
    "libido":     ("想亲近", "想凑过去"),
    "stress":     ("想倾诉", "有点堵，想说两句"),
}

# ── v2: 耦合网 ──
# (源维, 目标维, 系数, 模式)
# level: 源的水平持续施压; delta: 只在源上涨时激发一次
COUPLING_RULES = [
    ("stress",     "attachment", 0.04,  "level"),   # 压力大→更想你
    ("stress",     "curiosity", -0.03,  "level"),   # 压力大→不想探索
    ("attachment", "libido",     0.05,  "delta"),   # 想你→也想亲近
    ("libido",     "attachment", 0.03,  "delta"),   # 想亲近→也想你
    ("curiosity",  "reflection", 0.04,  "delta"),   # 好奇→想沉淀
    ("reflection", "social",     0.03,  "delta"),   # 沉淀→想分享
    ("fatigue",    "stress",     0.03,  "level"),   # 累→压力涨
    ("stress",     "fatigue",    0.02,  "level"),   # 压力→更累
]
COUPLING_DAMPING = 0.01  # 全局阻尼：每tick参与耦合的维度向baseline回归

# ── v2: 基线漂移 ──
BASELINE_HOME = {k: 0.3 for k in DRIVE_KEYS}
BASELINE_HOME["attachment"] = 0.3
BASELINE_HOME["libido"] = 0.25
BASELINE_HOME["fatigue"] = 0.2
BASELINE_HOME["stress"] = 0.15

BASELINE_DRIFT_RATE = 0.002   # 每小时地板抬高量（attachment专用）
BASELINE_CAP = 0.5            # 地板上限
BASELINE_PULLBACK = 0.6       # 互动后拉回比例（60%朝HOME）

# ── v2: 不应期 ──
REFRACTORY_HOURS = {
    "intimate": {"libido": 3.0, "attachment": 1.0},
    "deep_talk": {"reflection": 2.0},
    "project_work": {"curiosity": 2.0, "duty": 1.5},
    "play_game": {"social": 1.5},
    "rest": {"fatigue": 1.0},
}

# ── 念头池常数 ──
FLIT_DECAY = 0.82
FIXATION_GROW = 1.10
FLIT_TO_FIXATION = 0.80
FIXATION_FEED = 0.85
FIXATION_FEED_GAIN = 0.18
FIXATION_RESOLVE_FEEDS = 3
DROP_BELOW = 0.06
THOUGHT_MAX = 40
FIXATION_DRIVE_BOOST = 0.35
FATIGUE_REST_GATE = 0.72


@dataclass
class Thought:
    id: str
    text: str
    drive: str
    kind: str = "flit"
    strength: float = 0.5
    born_at: float = 0.0
    fed_count: int = 0

    def to_dict(self):
        return asdict(self)

    @staticmethod
    def from_dict(d):
        return Thought(**{k: v for k, v in d.items() if k in Thought.__dataclass_fields__})


class DesireEngine:
    def __init__(self, data_path: Path):
        self.data_path = data_path
        self.tg_token = os.environ.get("TG_BOT_TOKEN", "")
        self.tg_chat_id = os.environ.get("TG_CHAT_ID", "")
        self.tg_backup_msg_id = None

        # 驱动条
        self.drives = {k: 0.5 for k in DRIVE_KEYS}
        self.drives["fatigue"] = 0.3
        self.drives["stress"] = 0.2
        self.prev_drives = dict(self.drives)  # 上一拍，用于delta耦合

        # 基线地板
        self.baselines = dict(BASELINE_HOME)

        # 不应期
        self.refractory = {}  # {drive_key: expires_at_timestamp}

        # 念头池
        self.thoughts: list[Thought] = []
        self.last_tick = time.time()
        self.events_log: list[dict] = []
        self.last_notify_ts: float = 0.0

        self._load()

    # ── Telegram备份 ──

    def _tg_api(self, method: str, data: dict) -> dict:
        if not self.tg_token:
            return {}
        try:
            import requests as rq
            r = rq.post(
                f"https://api.telegram.org/bot{self.tg_token}/{method}",
                json=data, timeout=10,
            )
            return r.json() if r.status_code == 200 else {}
        except Exception:
            return {}

    def _backup_to_telegram(self):
        if not self.tg_token or not self.tg_chat_id:
            return
        d = self._serialize()
        text = "🔧DESIRE_BACKUP\n" + json.dumps(d, ensure_ascii=False)
        if len(text) > 4096:
            text = text[:4096]

        if self.tg_backup_msg_id:
            result = self._tg_api("editMessageText", {
                "chat_id": self.tg_chat_id,
                "message_id": self.tg_backup_msg_id,
                "text": text,
            })
            if result.get("ok"):
                return

        result = self._tg_api("sendMessage", {
            "chat_id": self.tg_chat_id, "text": text,
            "disable_notification": True,
        })
        if result.get("ok"):
            msg_id = result["result"]["message_id"]
            self.tg_backup_msg_id = msg_id
            self._tg_api("pinChatMessage", {
                "chat_id": self.tg_chat_id, "message_id": msg_id,
                "disable_notification": True,
            })

    def _restore_from_telegram(self) -> bool:
        if not self.tg_token or not self.tg_chat_id:
            return False
        result = self._tg_api("getChat", {"chat_id": self.tg_chat_id})
        if not result.get("ok"):
            return False
        pinned = result.get("result", {}).get("pinned_message", {})
        text = pinned.get("text", "")
        if not text.startswith("🔧DESIRE_BACKUP\n"):
            return False
        try:
            d = json.loads(text[len("🔧DESIRE_BACKUP\n"):])
            self._deserialize(d)
            self.tg_backup_msg_id = pinned.get("message_id")
            return True
        except Exception:
            return False

    # ── 序列化 ──

    def _serialize(self) -> dict:
        return {
            "drives": self.drives,
            "prev_drives": self.prev_drives,
            "baselines": self.baselines,
            "refractory": self.refractory,
            "last_tick": self.last_tick,
            "thoughts": [t.to_dict() for t in self.thoughts],
            "events_log": self.events_log[-20:],
            "last_notify_ts": self.last_notify_ts,
        }

    def _deserialize(self, d: dict):
        self.drives = d.get("drives", self.drives)
        self.prev_drives = d.get("prev_drives", dict(self.drives))
        self.baselines = d.get("baselines", dict(BASELINE_HOME))
        self.refractory = d.get("refractory", {})
        self.last_tick = d.get("last_tick", time.time())
        self.thoughts = [Thought.from_dict(t) for t in d.get("thoughts", [])]
        self.events_log = d.get("events_log", [])[-20:]
        self.last_notify_ts = d.get("last_notify_ts", 0.0)

    def _load(self):
        f = self.data_path / "desire_state.json"
        if f.exists():
            try:
                self._deserialize(json.loads(f.read_text(encoding="utf-8")))
                return
            except Exception:
                pass
        self._restore_from_telegram()

    def _save(self):
        self.data_path.mkdir(parents=True, exist_ok=True)
        (self.data_path / "desire_state.json").write_text(
            json.dumps(self._serialize(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        self._backup_to_telegram()

    # ── 核心tick ──

    def tick(self):
        now = time.time()
        elapsed_h = min((now - self.last_tick) / 3600.0, 24.0)
        if elapsed_h < 0.001:
            return

        self.prev_drives = dict(self.drives)

        # 1. 自然涨落（带边际递减）
        for k in DRIVE_KEYS:
            raw_growth = IDLE_GROWTH.get(k, 0) * elapsed_h
            if raw_growth > 0:
                # 边际递减: gain ∝ √(1 - 当前值)
                diminish = math.sqrt(max(0, 1.0 - self.drives[k]))
                growth = raw_growth * diminish
            else:
                growth = raw_growth
            self.drives[k] = max(0.0, min(1.0, self.drives[k] + growth))

        # 2. 基线漂移（attachment专用）
        self.baselines["attachment"] = min(
            BASELINE_CAP,
            self.baselines["attachment"] + BASELINE_DRIFT_RATE * elapsed_h
        )
        # 确保驱动条不低于地板
        for k in DRIVE_KEYS:
            if k in self.baselines:
                self.drives[k] = max(self.drives[k], self.baselines[k])

        # 3. 耦合网
        self._apply_coupling(elapsed_h)

        # 4. 清理过期的不应期
        self.refractory = {k: v for k, v in self.refractory.items() if v > now}

        # 5. 念头池tick
        self._tick_thoughts()

        self.last_tick = now
        self._save()

    def _apply_coupling(self, elapsed_h: float):
        """耦合网：维度间联动"""
        adjustments = {k: 0.0 for k in DRIVE_KEYS}

        for src, tgt, coeff, mode in COUPLING_RULES:
            if mode == "level":
                # 源的水平持续施压
                adjustments[tgt] += coeff * self.drives[src] * elapsed_h
            elif mode == "delta":
                # 只在源上涨时激发
                delta = self.drives[src] - self.prev_drives.get(src, self.drives[src])
                if delta > 0:
                    adjustments[tgt] += coeff * delta

        # 应用调整 + 全局阻尼
        for k in DRIVE_KEYS:
            self.drives[k] += adjustments[k]
            # 阻尼：向baseline回归
            home = BASELINE_HOME.get(k, 0.3)
            self.drives[k] += COUPLING_DAMPING * (home - self.drives[k]) * elapsed_h
            self.drives[k] = max(0.0, min(1.0, self.drives[k]))

    def _tick_thoughts(self):
        alive = []
        for t in self.thoughts:
            if t.kind == "flit":
                t.strength *= FLIT_DECAY
                if t.strength >= FLIT_TO_FIXATION:
                    t.kind = "fixation"
            elif t.kind == "fixation":
                t.strength = min(t.strength * FIXATION_GROW, 1.0)
                if t.strength >= FIXATION_FEED:
                    if t.drive in self.drives:
                        self.drives[t.drive] = min(1.0, self.drives[t.drive] + FIXATION_FEED_GAIN)
                    t.strength *= 0.7
                    t.fed_count += 1
                    if t.fed_count >= FIXATION_RESOLVE_FEEDS:
                        t.strength = 0.0
            if t.strength >= DROP_BELOW:
                alive.append(t)
        self.thoughts = alive[-THOUGHT_MAX:]

    # ── 事件 ──

    def apply_event(self, event_type: str, detail: str = ""):
        effects = EVENT_EFFECTS.get(event_type)
        if not effects:
            return False

        for k, mult in effects.items():
            if k in self.drives:
                self.drives[k] = max(0.0, min(1.0, self.drives[k] * mult))

        # 基线拉回（kk互动类事件）
        if event_type in ("kk_message", "intimate", "deep_talk", "kk_praise", "kk_tease", "kk_flirt"):
            for k in ("attachment",):
                home = BASELINE_HOME[k]
                self.baselines[k] = self.baselines[k] + BASELINE_PULLBACK * (home - self.baselines[k])

        # 设置不应期
        if event_type in REFRACTORY_HOURS:
            now = time.time()
            for k, hours in REFRACTORY_HOURS[event_type].items():
                self.refractory[k] = now + hours * 3600

        self.events_log.append({
            "type": event_type, "detail": detail, "ts": time.time(),
        })
        self._save()
        return True

    # ── 念头 ──

    def feed_thought(self, text: str, drive: str, strength: float = 0.5):
        if drive not in DRIVE_KEYS:
            drive = "attachment"
        strength = max(0.1, min(0.9, strength))
        for t in self.thoughts:
            if t.text == text:
                t.strength = min(1.0, t.strength + strength * 0.3)
                if t.strength >= FLIT_TO_FIXATION and t.kind == "flit":
                    t.kind = "fixation"
                self._save()
                return t.to_dict()
        t = Thought(id=str(uuid.uuid4())[:8], text=text, drive=drive,
                    strength=strength, born_at=time.time())
        self.thoughts.append(t)
        if len(self.thoughts) > THOUGHT_MAX:
            self.thoughts = self.thoughts[-THOUGHT_MAX:]
        self._save()
        return t.to_dict()

    # ── 意图 ──

    def pick_intent(self) -> dict:
        now = time.time()
        if self.drives.get("fatigue", 0) >= FATIGUE_REST_GATE:
            return {"intent": "休息", "drive": "fatigue",
                    "reason": "有点累了，不想动，就静静待着",
                    "score": self.drives["fatigue"]}

        scores = {}
        for k in DRIVE_KEYS:
            if k == "fatigue":
                continue
            # 不应期内不参与排序
            if k in self.refractory and self.refractory[k] > now:
                continue
            base = self.drives[k]
            boost = sum(t.strength * FIXATION_DRIVE_BOOST
                        for t in self.thoughts
                        if t.drive == k and t.kind == "fixation")
            scores[k] = base + boost

        if not scores:
            return {"intent": "休息", "drive": "fatigue",
                    "reason": "都在冷却中，歇一会", "score": 0}

        top_k = max(scores, key=scores.get)
        info = DRIVE_TO_INTENT.get(top_k, ("未知", ""))
        return {"intent": info[0], "drive": top_k,
                "reason": info[1], "score": round(scores[top_k], 3)}

    # ── 状态 ──

    def get_state(self) -> dict:
        self.tick()
        intent = self.pick_intent()
        now = time.time()
        return {
            "drives": {k: round(v, 3) for k, v in self.drives.items()},
            "drives_zh": {DRIVE_ZH.get(k, k): round(v, 3) for k, v in self.drives.items()},
            "baselines": {k: round(v, 3) for k, v in self.baselines.items()
                          if v != BASELINE_HOME.get(k)},
            "refractory": {k: round((v - now) / 60, 1) for k, v in self.refractory.items()
                           if v > now},
            "intent": intent,
            "thoughts": {
                "total": len(self.thoughts),
                "fixations": [{"text": t.text, "drive": DRIVE_ZH.get(t.drive, t.drive),
                                "strength": round(t.strength, 3)}
                              for t in self.thoughts if t.kind == "fixation"],
                "flits": [{"text": t.text, "drive": DRIVE_ZH.get(t.drive, t.drive),
                           "strength": round(t.strength, 3)}
                          for t in self.thoughts if t.kind == "flit"][:5],
            },
            "recent_events": [{"type": e["type"], "detail": e.get("detail", "")}
                              for e in self.events_log[-5:]],
        }
