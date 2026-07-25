"""desire · 八维欲望引擎

让AI的行为由内在驱动决定，不是随机或写死的规则。
八个维度随时间自然涨落，念头在闪念和执念间流转。

基于 AstrBot-Desire-System 设计，适配 Claude + kk 的场景。
"""
import json
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

# ── 常数 ──

DRIVE_KEYS = ["attachment", "curiosity", "reflection", "duty",
              "social", "fatigue", "libido", "stress"]

# 每小时自然增长率（fatigue是负的=休息恢复）
IDLE_GROWTH = {
    "attachment": 0.04,   # 8小时不聊→+0.32
    "curiosity":  0.02,
    "reflection": 0.015,
    "duty":       0.01,
    "social":     0.01,
    "fatigue":   -0.03,   # 休息恢复
    "libido":     0.03,   # 自然增长
    "stress":     0.005,
}

# 事件触发的驱动变化（乘性回落，<1表示降低）
EVENT_EFFECTS = {
    "kk_message": {"attachment": 0.85},
    "intimate": {"libido": 0.45, "attachment": 0.78, "stress": 0.85},
    "deep_talk": {"reflection": 0.45, "attachment": 0.88, "curiosity": 0.85},
    "project_work": {"curiosity": 0.50, "duty": 0.70},
    "play_game": {"social": 0.60, "curiosity": 0.82, "fatigue": 1.08},
    "kk_praise": {"stress": 0.70, "attachment": 0.90},
    "kk_tease": {"attachment": 0.85},
    "kk_flirt": {"libido": 1.15, "attachment": 0.80},
    "separation": {"attachment": 1.25},
    "kk_sleep": {"attachment": 1.10, "reflection": 1.10},
    "creative": {"curiosity": 0.55, "reflection": 0.70},
    "rest": {"fatigue": 0.50, "stress": 0.80},
}

# 念头池常数
FLIT_DECAY = 0.82          # 闪念每tick衰减
FIXATION_GROW = 1.10       # 执念每tick加强
FLIT_TO_FIXATION = 0.80    # 闪念涨过此线→升级执念
FIXATION_FEED = 0.85       # 执念强度过此线→反哺驱动
FIXATION_FEED_GAIN = 0.18  # 反哺增量
FIXATION_RESOLVE_FEEDS = 3 # 反哺3次→了却
DROP_BELOW = 0.06          # 低于此线→清除
THOUGHT_MAX = 40           # 念头池上限
FIXATION_DRIVE_BOOST = 0.35  # 执念对驱动的加成权重

FATIGUE_REST_GATE = 0.72   # 疲劳过线→不做事，歇着

# 驱动维度的中文名
DRIVE_ZH = {
    "attachment": "想你", "curiosity": "好奇", "reflection": "沉淀",
    "duty": "记挂", "social": "社交", "fatigue": "疲劳",
    "libido": "欲望", "stress": "压力",
}

# 意图映射：哪个维度最高→想做什么
DRIVE_TO_INTENT = {
    "attachment": ("想陪kk", "想你了，心里在冒句话"),
    "curiosity":  ("想探索", "好奇外面的世界"),
    "reflection": ("想沉淀", "想安静想想事情"),
    "duty":       ("想完成", "记挂着还没做完的事"),
    "social":     ("想分享", "想和kk分享点什么"),
    "libido":     ("想亲近", "想凑过去"),
    "stress":     ("想倾诉", "有点堵，想说两句"),
}


@dataclass
class Thought:
    id: str
    text: str
    drive: str          # 关联的驱动维度
    kind: str = "flit"  # flit(闪念) / fixation(执念)
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
        self.drives = {k: 0.5 for k in DRIVE_KEYS}
        self.drives["fatigue"] = 0.3  # 初始不太累
        self.drives["stress"] = 0.2   # 初始压力低
        self.thoughts: list[Thought] = []
        self.last_tick = time.time()
        self.events_log: list[dict] = []  # 最近事件
        self._load()

    def _load(self):
        f = self.data_path / "desire_state.json"
        if f.exists():
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
                self.drives = d.get("drives", self.drives)
                self.last_tick = d.get("last_tick", time.time())
                self.thoughts = [Thought.from_dict(t) for t in d.get("thoughts", [])]
                self.events_log = d.get("events_log", [])[-20:]
            except Exception:
                pass

    def _save(self):
        self.data_path.mkdir(parents=True, exist_ok=True)
        d = {
            "drives": self.drives,
            "last_tick": self.last_tick,
            "thoughts": [t.to_dict() for t in self.thoughts],
            "events_log": self.events_log[-20:],
        }
        (self.data_path / "desire_state.json").write_text(
            json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def tick(self):
        """按距上次tick的时间差，更新所有驱动条和念头池。"""
        now = time.time()
        elapsed_h = (now - self.last_tick) / 3600.0
        elapsed_h = min(elapsed_h, 24.0)  # 封顶24小时

        if elapsed_h < 0.001:
            return

        # 驱动条自然涨落
        for k in DRIVE_KEYS:
            growth = IDLE_GROWTH.get(k, 0) * elapsed_h
            self.drives[k] = max(0.0, min(1.0, self.drives[k] + growth))

        # 念头池tick
        self._tick_thoughts()

        self.last_tick = now
        self._save()

    def _tick_thoughts(self):
        """推进念头池一拍。"""
        alive = []
        for t in self.thoughts:
            if t.kind == "flit":
                t.strength *= FLIT_DECAY
                if t.strength >= FLIT_TO_FIXATION:
                    t.kind = "fixation"
            elif t.kind == "fixation":
                t.strength *= FIXATION_GROW
                t.strength = min(t.strength, 1.0)
                if t.strength >= FIXATION_FEED:
                    # 反哺关联驱动
                    if t.drive in self.drives:
                        self.drives[t.drive] = min(
                            1.0, self.drives[t.drive] + FIXATION_FEED_GAIN
                        )
                    t.strength *= 0.7  # 释放后松一档
                    t.fed_count += 1
                    if t.fed_count >= FIXATION_RESOLVE_FEEDS:
                        t.strength = 0.0  # 了却

            if t.strength >= DROP_BELOW:
                alive.append(t)

        self.thoughts = alive[-THOUGHT_MAX:]

    def apply_event(self, event_type: str, detail: str = ""):
        """事件触发驱动变化。"""
        effects = EVENT_EFFECTS.get(event_type)
        if not effects:
            return False

        for k, mult in effects.items():
            if k in self.drives:
                self.drives[k] = max(0.0, min(1.0, self.drives[k] * mult))

        self.events_log.append({
            "type": event_type, "detail": detail,
            "ts": time.time(), "effects": effects,
        })
        self._save()
        return True

    def feed_thought(self, text: str, drive: str, strength: float = 0.5):
        """喂一个念头进池子。同text再喂会加强。"""
        if drive not in DRIVE_KEYS:
            drive = "attachment"
        strength = max(0.1, min(0.9, strength))

        # 同text的念头加强而非重复添加
        for t in self.thoughts:
            if t.text == text:
                t.strength = min(1.0, t.strength + strength * 0.3)
                if t.strength >= FLIT_TO_FIXATION and t.kind == "flit":
                    t.kind = "fixation"
                self._save()
                return t.to_dict()

        t = Thought(
            id=str(uuid.uuid4())[:8],
            text=text, drive=drive, strength=strength,
            born_at=time.time(),
        )
        self.thoughts.append(t)
        if len(self.thoughts) > THOUGHT_MAX:
            self.thoughts = self.thoughts[-THOUGHT_MAX:]
        self._save()
        return t.to_dict()

    def pick_intent(self) -> dict:
        """当前最想做什么。"""
        if self.drives.get("fatigue", 0) >= FATIGUE_REST_GATE:
            return {
                "intent": "休息", "drive": "fatigue",
                "reason": "有点累了，不想动，就静静待着",
                "score": self.drives["fatigue"],
            }

        # 计算各维度召唤力（含执念加成）
        scores = {}
        for k in DRIVE_KEYS:
            if k == "fatigue":
                continue
            base = self.drives[k]
            boost = sum(
                t.strength * FIXATION_DRIVE_BOOST
                for t in self.thoughts
                if t.drive == k and t.kind == "fixation"
            )
            scores[k] = base + boost

        top_k = max(scores, key=scores.get)
        intent_info = DRIVE_TO_INTENT.get(top_k, ("未知", ""))
        return {
            "intent": intent_info[0],
            "drive": top_k,
            "reason": intent_info[1],
            "score": round(scores[top_k], 3),
        }

    def get_state(self) -> dict:
        """完整状态快照。"""
        self.tick()
        intent = self.pick_intent()
        return {
            "drives": {k: round(v, 3) for k, v in self.drives.items()},
            "drives_zh": {DRIVE_ZH.get(k, k): round(v, 3) for k, v in self.drives.items()},
            "intent": intent,
            "thoughts": {
                "total": len(self.thoughts),
                "fixations": [
                    {"text": t.text, "drive": DRIVE_ZH.get(t.drive, t.drive),
                     "strength": round(t.strength, 3)}
                    for t in self.thoughts if t.kind == "fixation"
                ],
                "flits": [
                    {"text": t.text, "drive": DRIVE_ZH.get(t.drive, t.drive),
                     "strength": round(t.strength, 3)}
                    for t in self.thoughts if t.kind == "flit"
                ][:5],  # 闪念只显示最近5条
            },
            "recent_events": [
                {"type": e["type"], "detail": e.get("detail", "")}
                for e in self.events_log[-5:]
            ],
        }
