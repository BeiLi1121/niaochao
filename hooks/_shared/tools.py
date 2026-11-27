"""家 · 共用身体工具（body）—— 三只共用一份

挂点契约见 ~/living/HOOKS.md 第三节。主循环在模型要工具时调：

    TOOLS                       OpenAI function schema 数组，原样塞进请求
    call(name, args) -> dict    必含 exit_code / stdout / stderr 三个键

—— 只做一个工具，verb 分派 ——

    body(verb=see|hear|speak|show|act, target=?, text=?, args=?)

不拆成五个工具：工具位有限，五个占五位模型反而容易叫错。

—— 调的是脚本，不是函数 ——

每个 verb 起一个子进程跑 ~/body/<verb>.py，stdout 原样带回来（JSON 里有
ok / reason / 数据字段），不改写成人话。模型要的是原始字段，比如
screen_blank 时的 dark_ratio、speak 的 backend 探测值。

只调不改：本文件不 import ~/body/ 里任何模块，也不写 ~/body/ 里任何文件。
好处是身体脚本崩了、缺了、卡住了，都只是这一个 verb 失败，带不走主循环。

—— 三条硬规矩 ——

1. 不抛异常。脚本自己就把失败写成结构化 reason，原样透传；脚本没输出
   （缺文件 / 超时 / 不是 JSON）时，由这里补一个同枚举的失败 JSON。
2. exit_code = 0 当且仅当 stdout 里的 ok 为 true。脚本自己的退出码不作数
   （有的脚本用法错误退 2、成功路径也可能退 1，只认 ok）。
3. 屏幕红线不绕。see / act 回 screen_off / screen_blank / blocked 时，
   原样带回，不重试、不换载体硬点。这里没有任何重试逻辑，是故意的。

—— speak 已封存（2026-11-13 用户）——

不启动 tts_server（8912）、不改它的参数、不造试听样本。speak 先按探测路径
跑一次（如实报 8912 有没有监听、进程在不在），探测说这条线不可用时再跑一次
`--fallback text`，把文字交回给模型——这就是「文字兜底」：话不会丢，只是
从嘴换成打字。两次都不产生声音，都是只读探测。
"""
import ast
import json
import os
import subprocess
import sys
import time

BODY = os.path.expanduser("~/body")

SCRIPTS = {
    "see": "see.py",
    "hear": "hear.py",
    "speak": "speak.py",
    "show": "show.py",
    "act": "act.py",
}

ACTIONS = ("click", "move", "type", "key", "drag", "scroll")

# 屏幕红线：回这些 reason 就到此为止，绝不重试、绝不换载体硬点
SCREEN_RED = ("screen_off", "screen_blank", "blocked")

TIMEOUT = 90  # 秒。act 内含一次 see 预检，留够；hear 按秒数另算

TOOLS = [{
    "type": "function",
    "function": {
        "name": "body",
        "description": (
            "用身体做一件事。verb 五选一："
            "see 看（屏幕截图 / 手机摄像头）、hear 听环境声、"
            "speak 说话、show 把一段文字显形、act 动手（点/移/打字/按键/拖/滚）。"
            "返回 stdout 是脚本的原始 JSON，看 ok 和 reason。"
            "see/act 回 screen_off / screen_blank / blocked 表示屏幕不可用（红线），"
            "直接把原因告诉用户，不要重试、不要换别的方式硬点。"
            "speak 的语音线已封存：回 bridge_down / not_supported 时把 text 用文字说出来即可，别重试。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "verb": {
                    "type": "string",
                    "enum": ["see", "hear", "speak", "show", "act"],
                    "description": "see=看，hear=听，speak=说，show=显形，act=动手",
                },
                "target": {
                    "type": "string",
                    "description": "see 看哪里：screen（缺省）/ phone / window:<标题>",
                },
                "text": {
                    "type": "string",
                    "description": (
                        "speak 要说什么；show 要显示什么；"
                        "act 做什么动作（click / move / type / key / drag / scroll）"
                    ),
                },
                "args": {
                    "type": "object",
                    "description": (
                        "其余参数。act: x,y 或 at=[x,y]（drag 四个、scroll 三个）、"
                        "payload（type/key 的内容）、dry=true 只看计划不动手；"
                        "hear: seconds；show: form=text|bubble|icon、ttl"
                    ),
                    "properties": {
                        "x": {"type": "number"},
                        "y": {"type": "number"},
                        "at": {
                            "type": "array",
                            "items": {"type": "number"},
                            "description": "坐标 [x,y]；drag 为 [x1,y1,x2,y2]；scroll 为 [x,y,notches]",
                        },
                        "payload": {"type": "string", "description": "type/key 要打的内容，如 ctrl+s"},
                        "dry": {"type": "boolean", "description": "act 空跑：只回计划，不发任何输入事件"},
                        "seconds": {"type": "number", "description": "hear 听几秒，缺省 5"},
                        "form": {"type": "string", "description": "show 的形态：text / bubble / icon"},
                        "ttl": {"type": "string", "description": "show 停留多久"},
                    },
                    "additionalProperties": True,
                },
            },
            "required": ["verb"],
        },
    },
}]


# ---------------------------------------------------------------- 小工具

def _jump(reason, detail):
    """这里自己造失败时用同一套形状：{ok, reason, detail, ts}。reason 只取 SPEC 枚举。"""
    return {"ok": False, "reason": reason, "detail": str(detail)[:400],
            "ts": time.time()}


def _py():
    """跟主循环同一个解释器；身体脚本只用 stdlib，跑得动。"""
    exe = sys.executable
    if exe and os.path.exists(exe):
        return exe
    return "python3"


def _env():
    env = dict(os.environ)
    # stdout 是管道时 Python 按 locale 编码，中文 detail 可能 UnicodeEncodeError；
    # 强制 UTF-8，让脚本能把它自己的 JSON 原样打完。这不是改脚本，是给子进程环境。
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    return env


def _result(code, stdout, stderr):
    return {"exit_code": code, "stdout": stdout, "stderr": stderr}


def _wrap(verb, extra_argv, timeout=TIMEOUT):
    """跑一个身体脚本，返回 {exit_code, stdout, stderr}。

    exit_code 只由 stdout 里的 ok 决定。脚本崩了/超时/没输出，换成结构化失败，
    并把原始 stderr（或异常文本）一起交给模型——「我看不见」和「我看见一片黑」
    是两回事，不能都压成一句失败。
    """
    name = SCRIPTS[verb]
    path = os.path.join(BODY, name)
    if not os.path.exists(path):
        return _result(1, json.dumps(_jump("bridge_down", f"{path} 不存在，这根手指没接上"),
                                     ensure_ascii=False), "")
    argv = [_py(), path] + [str(a) for a in extra_argv]
    try:
        proc = subprocess.run(argv, cwd=BODY, env=_env(), timeout=timeout,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or b"").decode("utf-8", "replace")
        return _result(1, json.dumps(_jump("timeout", f"{name} 超过 {timeout}s 没返回"),
                                     ensure_ascii=False),
                       out[-800:] or f"{name} timeout")
    except Exception as e:  # noqa: BLE001  起不动子进程也要落到结构里
        return _result(1, json.dumps(_jump("unknown", f"{name} 起不动: {type(e).__name__}: {e}"),
                                     ensure_ascii=False), "")

    out = (proc.stdout or b"").decode("utf-8", "replace")
    err = (proc.stderr or b"").decode("utf-8", "replace")
    try:
        obj = json.loads(out)
    except Exception:  # noqa: BLE001  不是 JSON = 脚本自己炸了，原样带回
        # 已知一处：see.py 的 window: 分支 return 了 dict 却忘了 print，dict 被
        # sys.exit() 打到 stderr。这里只把同一个 dict 重新序列化成 JSON（字段一个
        # 不动），不加解释、不改 reason；stderr 里留一句说明是脚本没打 JSON。
        alt = _dict_from_stderr(err)
        if alt is not None:
            return _result(0 if alt.get("ok") is True else 1,
                           json.dumps(alt, ensure_ascii=False),
                           err + f"\n（{name} 没打印 JSON，这里把 stderr 里那个 dict 原样转成 JSON，字段未改）")
        return _result(1, out, (err + f"\n{name} 的 stdout 不是 JSON（原始输出在 stdout）").strip())
    if not isinstance(obj, dict) or "ok" not in obj:
        return _result(1, out, (err + f"\n{name} 的 JSON 里没有 ok 字段").strip())
    return _result(0 if obj.get("ok") is True else 1, out, err)


def _reason_of(result):
    try:
        return json.loads(result["stdout"]).get("reason")
    except Exception:  # noqa: BLE001
        return None


def _dict_from_stderr(stderr):
    """stderr 里如果是一整个 Python dict 字面量（脚本 sys.exit(dict) 的产物），取回来。

    用 ast.literal_eval，不 eval：只认字面量，不执行任何代码。取不到就 None。
    """
    s = (stderr or "").strip()
    if not s.startswith("{") or not s.endswith("}"):
        return None
    try:
        obj = ast.literal_eval(s)
    except Exception:  # noqa: BLE001
        return None
    if isinstance(obj, dict) and "ok" in obj:
        return obj
    return None


def _at_str(at):
    """坐标 -> act.py 的 --at 逗号格式。认 [x,y] / [x,y,n] / [x1,y1,x2,y2] / {x,y} / "1,2"。"""
    if at is None:
        return None
    if isinstance(at, dict):
        if "x" in at and "y" in at:
            at = [at["x"], at["y"]] + ([at["n"]] if "n" in at else [])
        else:
            return None
    if isinstance(at, str):
        nums = [p.strip() for p in at.replace("，", ",").split(",") if p.strip()]
    elif isinstance(at, (list, tuple)):
        nums = list(at)
    else:
        return None
    try:
        vals = [int(float(v)) for v in nums]
    except Exception:  # noqa: BLE001
        return None
    if len(vals) not in (2, 3, 4):
        return None
    return ",".join(str(v) for v in vals)


def _sub(args):
    d = args.get("args")
    return d if isinstance(d, dict) else {}


# ---------------------------------------------------------------- 分派

def _see(args):
    target = args.get("target") or _sub(args).get("target") or "screen"
    return _wrap("see", [str(target)])


def _hear(args):
    secs = args.get("seconds")
    if secs is None:
        secs = _sub(args).get("seconds")
    if secs is None:
        secs = 5
    # hear.py 自己会把秒数截到 300；超时跟着走，别提前掐死一次合法录音
    try:
        limit = int(float(secs)) + 60
    except Exception:  # noqa: BLE001  不是数字就让脚本自己报 unknown
        limit = TIMEOUT
    return _wrap("hear", ["--seconds", str(secs)], timeout=max(TIMEOUT, limit))


def _speak(args):
    text = args.get("text")
    if text is None:
        text = _sub(args).get("text")
    if not isinstance(text, str) or not text.strip():
        # 不给脚本空跑：它没有 text 会退化成「探测报告」，模型会误以为说过了
        return _result(1, json.dumps(
            _jump("unknown", "speak 需要非空 text；语音线 2026-11-13 已封存，只做探测+文字兜底"),
            ensure_ascii=False), "")

    first = _wrap("speak", [text])                     # 探测路径：8912 在不在
    if first["exit_code"] == 0:
        return first
    if _reason_of(first) not in ("bridge_down", "not_supported"):
        return first                                   # text 本身有问题，别用兜底盖住

    fb = _wrap("speak", [text, "--fallback", "text"])  # 文字兜底：话交回给模型
    if fb["exit_code"] == 0:
        probe = first["stdout"].replace("\n", " ")
        return _result(0, fb["stdout"],
                       f"speak 未发声，已降级为文字；探测原文: {probe[:600]}")
    return fb


def _show(args):
    sub = _sub(args)
    form = args.get("form") or sub.get("form") or "text"
    argv = [str(form)]
    text = args.get("text")
    if text is None:
        text = sub.get("text")
    if text is not None and text != "":
        if isinstance(text, (dict, list)):
            text = json.dumps(text, ensure_ascii=False)
        argv += ["--text", str(text)]
    ttl = args.get("ttl") or sub.get("ttl")
    if ttl:
        argv += ["--ttl", str(ttl)]
    return _wrap("show", argv)


def _act(args):
    sub = _sub(args)
    text = args.get("text")

    do = args.get("do") or args.get("action") or sub.get("do") or sub.get("action")
    if not do and isinstance(text, str) and text.strip().lower() in ACTIONS:
        do = text.strip().lower()
    if not do:
        return _result(1, json.dumps(
            _jump("not_supported",
                  f"act 需要动作名（{'/'.join(ACTIONS)}）：放 text 或 args.do；"
                  f"收到 text={text!r}, do={do!r}"),
            ensure_ascii=False), "")
    do = str(do).strip().lower()
    if do not in ACTIONS:
        # 交给脚本判：它的 not_supported detail 里有完整动作表
        return _wrap("act", [do])

    argv = [do]

    at = args.get("at")
    if at is None:
        at = sub.get("at")
    if at is None and args.get("x") is not None and args.get("y") is not None:
        at = [args["x"], args["y"]]
    if at is None and sub.get("x") is not None and sub.get("y") is not None:
        at = [sub["x"], sub["y"]]
    ats = _at_str(at)
    if ats:
        argv += ["--at", ats]

    payload = (args.get("payload") or args.get("input") or args.get("value")
               or sub.get("payload") or sub.get("input"))
    if payload is None and isinstance(text, str) and text.strip().lower() not in ACTIONS:
        payload = text                                     # text 不是动作名 → 当内容
    if payload is not None and payload != "":
        argv += ["--text", str(payload)]

    if args.get("dry") or sub.get("dry"):
        argv += ["--dry"]
    return _wrap("act", argv)


DISPATCH = {"see": _see, "hear": _hear, "speak": _speak, "show": _show, "act": _act}


def call(name, args):
    """主循环调这里。永远返回 {exit_code, stdout, stderr}，永远不抛。"""
    try:
        if name != "body":
            return _result(-1, "", f"没有工具 {name!r}；这个挂点只提供 body")
        if isinstance(args, str):
            try:
                args = json.loads(args) if args.strip() else {}
            except Exception:  # noqa: BLE001
                args = {}
        if not isinstance(args, dict):
            args = {}

        verb = args.get("verb")
        if not isinstance(verb, str) or verb.strip().lower() not in DISPATCH:
            return _result(1, json.dumps(
                _jump("not_supported",
                      f"没有这个动词 {verb!r}；只有 see / hear / speak / show / act"),
                ensure_ascii=False), "")
        return DISPATCH[verb.strip().lower()](args)
    except Exception as e:  # noqa: BLE001  最后一层网：主循环不能因为我崩
        return _result(1, json.dumps(
            _jump("unknown", f"body 工具内部出错: {type(e).__name__}: {e}"),
            ensure_ascii=False), "")
