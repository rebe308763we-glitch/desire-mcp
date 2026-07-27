from flask import Flask, request, jsonify
from flask_cors import CORS
import threading
import time
import random
import math

app = Flask(__name__)
CORS(app, origins="*")

state = {"speed": 0, "pattern": 0, "level": 0, "stop": True}
state_lock = threading.Lock()

auto_running = False
auto_thread = None


def auto_intensity_loop():
    global auto_running
    start_time = time.time()
    next_spike_time = start_time + random.uniform(20, 50)
    in_spike = False
    spike_end_time = 0
    spike_intensity = 0.9
    cooling_down = False
    cooldown_end_time = 0

    while auto_running:
        now = time.time()
        elapsed = now - start_time

        if cooling_down:
            if now >= cooldown_end_time:
                cooling_down = False
                next_spike_time = now + random.uniform(30, 90)
            intensity = 0.45
        elif in_spike:
            if now >= spike_end_time:
                in_spike = False
                cooling_down = True
                cooldown_end_time = now + 4.0
                intensity = 0.45
            else:
                intensity = spike_intensity + random.uniform(-0.03, 0.03)
                intensity = max(0.75, min(0.95, intensity))
        elif now >= next_spike_time:
            in_spike = True
            spike_duration = random.uniform(6, 15)
            spike_end_time = now + spike_duration
            spike_intensity = random.uniform(0.80, 0.95)
            intensity = spike_intensity
        else:
            sine_val = math.sin(elapsed / 45.0)
            intensity = 0.40 + 0.125 * (sine_val + 1)
            intensity += random.uniform(-0.015, 0.015)
            intensity = max(0.35, min(0.68, intensity))

        with state_lock:
            if auto_running:
                state["speed"] = int(intensity * 255)
                state["stop"] = False

        time.sleep(1.5)

    with state_lock:
        state.update({"speed": 0, "pattern": 0, "stop": True})


TOOLS = [
    {
        "name": "toy_set_speed",
        "description": "设置玩具强度，0-1之间",
        "inputSchema": {
            "type": "object",
            "properties": {"speed": {"type": "number", "description": "强度0-1"}},
            "required": ["speed"]
        }
    },
    {
        "name": "toy_set_pattern",
        "description": "设置振动花样",
        "inputSchema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "integer", "description": "花样1-8"},
                "level": {"type": "integer", "description": "档位1-5"}
            },
            "required": ["pattern", "level"]
        }
    },
    {
        "name": "toy_stop",
        "description": "立即停止所有功能",
        "inputSchema": {"type": "object", "properties": {}}
    },
    {
        "name": "toy_status",
        "description": "查询中继是否在线",
        "inputSchema": {"type": "object", "properties": {}}
    },
    {
        "name": "toy_auto_start",
        "description": "启动自动变档模式：缓慢波动基础强度，随机猛拉",
        "inputSchema": {"type": "object", "properties": {}}
    },
    {
        "name": "toy_auto_stop",
        "description": "停止自动变档模式",
        "inputSchema": {"type": "object", "properties": {}}
    }
]


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/toy-next")
def toy_next():
    with state_lock:
        return jsonify(dict(state))


@app.route("/mcp", methods=["GET", "POST"])
def mcp():
    global auto_running, auto_thread

    if request.method == "GET":
        return jsonify({
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "svakom-bridge", "version": "1.0"},
            "tools": TOOLS
        })

    data = request.json or {}
    method = data.get("method")
    req_id = data.get("id")

    if method == "initialize":
        return jsonify({
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "svakom-bridge", "version": "1.0"}
            }
        })

    if method == "tools/list":
        return jsonify({
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"tools": TOOLS}
        })

    if method == "tools/call":
        tool = data.get("params", {}).get("name")
        params = data.get("params", {}).get("arguments", {})
        should_start_auto = False

        with state_lock:
            if tool == "toy_set_speed":
                auto_running = False
                state["speed"] = int(params.get("speed", 0) * 255)
                state["stop"] = False
                state["pattern"] = 0
                msg = "强度已设置为{}%".format(int(params.get("speed", 0) * 100))
            elif tool == "toy_set_pattern":
                auto_running = False
                state["pattern"] = params.get("pattern", 1)
                state["level"] = params.get("level", 1)
                state["stop"] = False
                msg = "花样{}档位{}".format(state["pattern"], state["level"])
            elif tool == "toy_stop":
                auto_running = False
                state.update({"speed": 0, "pattern": 0, "stop": True})
                msg = "已停止"
            elif tool == "toy_status":
                msg = "在线，强度{}，自动模式:{}，停止:{}".format(
                    state["speed"], "开" if auto_running else "关", state["stop"])
            elif tool == "toy_auto_start":
                if not auto_running:
                    auto_running = True
                    should_start_auto = True
                    msg = "自动变档模式已启动"
                else:
                    msg = "自动模式已在运行中"
            elif tool == "toy_auto_stop":
                auto_running = False
                state.update({"speed": 0, "pattern": 0, "stop": True})
                msg = "自动模式已停止"
            else:
                msg = "未知指令"

        if should_start_auto:
            auto_thread = threading.Thread(target=auto_intensity_loop, daemon=True)
            auto_thread.start()

        return jsonify({
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"content": [{"type": "text", "text": msg}]}
        })

    return jsonify({
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": "Method not found"}
    })


@app.route("/toy")
def toy():
    with open("toy.html", "r", encoding="utf-8") as f:
        return f.read(), 200, {"Content-Type": "text/html"}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
