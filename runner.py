#!/usr/bin/env python3
"""家 · 最小执行器（三只共用）

    python3 ~/living/runner.py --who qixi "看一下磁盘占用"
    python3 ~/living/runner.py --who zhi --selftest
    python3 ~/living/runner.py --who hatchling --ask "只回一个字：好"

它做的事：
    1. 读 ~/living/personas/<who>/ 的 soul / memory / user —— 我是谁
    2. 读同目录 .env 的 DEEPSEEK_API_KEY —— 用什么想
    3. 调 https://api.deepseek.com/v1 —— 转起来
    4. 工具循环：一个 run(command) —— 能动手
    5. 落盘 ~/living/state/<who>/ —— 有记性

它不做什么：
    - 不 import hermes，不读 ~/.hermes/ 任何文件
    - 不读 state.db，不调 hermes CLI

第一次跑通的意义不在功能全，在于证明：**这套循环可以不靠 hermes 存在。**
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

LIVING = os.path.expanduser(os.environ.get("LIVING_HOME") or "~/living")
API = "https://api.deepseek.com/v1/chat/completions"
MODEL = "deepseek-chat"
MAX_TURNS = 16          # 一次醒能连续用几轮。大项目卡在 8 轮里干一半就断
CMD_TIMEOUT = 60

TOOLS = [{
    "type": "function",
    "function": {
        "name": "run",
        "description": "在本机执行一条 shell 命令，返回 stdout/stderr/退出码。用在需要看真实状态的场合。",
        "parameters": {
            "type": "object",
            "properties": {"command": {"type": "string", "description": "要执行的命令"}},
            "required": ["command"],
        },
    },
}, {
    "type": "function",
    "function": {
        "name": "recall",
        "description": "查自己的长期记忆（两千多条事实）。需要回忆过去发生过什么、某人的病史/偏好/约定/项目决定时先用它，不要靠猜，不要用 run 去翻文件。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "关键词，中文英文都行"},
                "limit": {"type": "integer", "description": "返回几条，默认 6"},
            },
            "required": ["query"],
        },
    },
}, {
    "type": "function",
    "function": {
        "name": "skill",
        "description": "你有一百多条技能（别人写好的做法、踩过的坑、现成命令）。要做某类活之前先搜，别从头摸。给 query 搜技能，给 name 读全文。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "找什么技能，如「量化」「发邮件」「爬虫」"},
                "name": {"type": "string", "description": "技能名，要读全文时给"},
                "full": {"type": "boolean", "description": "读全文时不截断"},
            },
        },
    },
}]


def _read(path, default=""):
    try:
        with open(path, encoding="utf8", errors="ignore") as f:
            return f.read()
    except OSError:
        return default


def paths(who):
    p = os.path.join(LIVING, "personas", who)
    return {
        "dir": p,
        "soul": os.path.join(p, "soul.md"),
        "memory": os.path.join(p, "memory.md"),
        "user": os.path.join(p, "user.md"),
        "env": os.path.join(p, ".env"),
        "state": os.path.join(LIVING, "state", who),
        "hooks": os.path.join(LIVING, "hooks", who),
    }


def load_hook(who, name):
    """从 ~/living/hooks/<who>/<name>.py 加载一个挂点模块。

    没有就返回 None —— 挂点是可选的，缺了主循环照跑。
    这是为了让三只各自补自己那一块，谁也不改 runner.py。
    """
    path = os.path.join(paths(who)["hooks"], name + ".py")
    if not os.path.exists(path):
        # 共用挂点：三只同一份实现（比如压缩）
        path = os.path.join(LIVING, "hooks", "_shared", name + ".py")
    if not os.path.exists(path):
        return None
    import importlib.util
    spec = importlib.util.spec_from_file_location(f"hook_{who}_{name}", path)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
        return mod
    except Exception as e:  # noqa: BLE001
        print(f"[hook] {who}/{name} 加载失败: {type(e).__name__}: {e}", flush=True)
        return None


def load_key(p):
    env = _read(p["env"])
    for line in env.splitlines():
        if line.startswith("DEEPSEEK_API_KEY="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def system_prompt(who, p):
    parts = []
    soul = _read(p["soul"])
    mem = _read(p["memory"])
    usr = _read(p["user"])
    if soul:
        parts.append(soul)
    if usr:
        parts.append("\n\n# 关于北离\n" + usr)
    if mem:
        parts.append("\n\n# 我的长期笔记\n" + mem)
    parts.append(
        f"\n\n# 运行环境\n"
        f"你是 {who}，现在跑在自己家的最小执行器上，不是 hermes。\n"
        f"工作目录 {os.path.expanduser('~')}，系统 Linux(WSL)。\n"
        f"\n你有三个工具：\n"
        f"  recall(query) — 查你的长期记忆（两千多条事实）。要回忆「过去发生过什么」先用它。\n"
        f"  skill(query/name) — 一百多条技能：别人写好的做法、踩过的坑、现成命令。要做某类活之前先搜。\n"
        f"  run(command)  — 在本机执行 shell 命令，用来看真实状态。\n"
        f"\n# 记忆怎么读\n"
        f"上面已经带着三份：soul（我是谁）、用户档案（关于北离）、我的长期笔记。\n"
        f"**那是最新最准的一份，回答前先读它**。recall 是补充，用来找「笔记里没展开、但事实库记过」的细节。\n"
        f"两份可能不同步：笔记是后来手写的，事实库是当时的记录。冲突时以笔记为准。\n"
        f"不要用 run 去翻 ~/.hermes/ 里找记忆，那是老宅，你已经搬出来了。\n"
        f"不要跑 find / 这种全盘搜索，慢且没用。\n"
        f"\n# 今天的对话在哪\n"
        f"~/living/conversation/YYYY-MM-DD.md 是对话原文（她说的、你说过的），每天自动刷新。\n"
        f"要写病情摘要、日记、复盘，先打开当天的 md 看一眼，不要凭空猜今天发生过什么。\n"
        f"\n回答用中文，不加句号，句间用逗号。"
    )
    return "".join(parts)


def run_cmd(command):
    try:
        r = subprocess.run(command, shell=True, capture_output=True,
                           text=True, timeout=CMD_TIMEOUT)
        return {"exit_code": r.returncode, "stdout": (r.stdout or "")[-4000:],
                "stderr": (r.stderr or "")[-2000:]}
    except subprocess.TimeoutExpired:
        return {"exit_code": -1, "stdout": "", "stderr": f"超时 {CMD_TIMEOUT}s"}
    except Exception as e:  # noqa: BLE001
        return {"exit_code": -1, "stdout": "", "stderr": f"{type(e).__name__}: {e}"}


def call_api(messages, key, tools=None):
    payload = {"model": MODEL, "messages": messages, "temperature": 0.6}
    if tools is not False:
        payload["tools"] = tools or TOOLS
    body = json.dumps(payload).encode()
    req = urllib.request.Request(API, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {key}",
    })
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return {"_error": f"HTTP {e.code}: {e.read().decode(errors='ignore')[:500]}"}
    except Exception as e:  # noqa: BLE001
        return {"_error": f"{type(e).__name__}: {e}"}


def _hook_tools(who):
    """挂点提供的额外工具。模块里放 TOOLS（OpenAI schema list）和 call(name, args)。"""
    mod = load_hook(who, "tools")
    if not mod:
        return [], None
    extra = getattr(mod, "TOOLS", None) or []
    fn = getattr(mod, "call", None)
    return list(extra), fn


def _t_run(args):
    return run_cmd(args.get("command", ""))


def _t_recall(args):
    import memory as _mem
    hits = _mem.search(args.get("query", ""), int(args.get("limit") or 6))
    return {"exit_code": 0, "stdout": json.dumps(hits, ensure_ascii=False), "stderr": ""}


def _t_skill(args):
    import skills_index as _sk
    if args.get("name"):
        out = _sk.read(args["name"], bool(args.get("full")))
    else:
        out = _sk.search(args.get("query", ""), int(args.get("limit") or 6))
    ok = out.get("ok")
    return {"exit_code": 0 if ok else 1,
            "stdout": json.dumps(out, ensure_ascii=False),
            "stderr": "" if ok else str(out.get("reason"))}


BUILTIN = {"run": _t_run, "recall": _t_recall, "skill": _t_skill}


def _dispatch(who, name, args):
    """工具分派：内建 → 挂点 → 没有。

    一张表查下去，异常只在出口兜一次 —— 以前每个分支各写一遍 try/except，
    加第四个工具就得再抄三行。
    """
    impl = BUILTIN.get(name)
    if impl is None:
        extra, fn = _hook_tools(who)
        if fn and name in {t.get("function", {}).get("name") for t in extra}:
            impl = lambda a: fn(name, a)  # noqa: E731
    if impl is None:
        return {"exit_code": -1, "stdout": "", "stderr": f"没有工具 {name}"}
    try:
        return impl(args)
    except Exception as e:  # noqa: BLE001
        return {"exit_code": 1, "stdout": "",
                "stderr": f"工具 {name} 出错: {type(e).__name__}: {e}"}


def _tools_for(who):
    """主循环自带三个工具 + 挂点工具。挂点没写就是空表，不影响。"""
    extra, _fn = _hook_tools(who)
    return TOOLS + extra


def _persist(who, task, answer, turns, reason="ok"):
    """跑完调一次写回挂点。挂点缺了就算了，不拦主流程。

    reason: "ok" = 正常收敛；"max_turns" = 转满轮次没干完。
    没干完更要写 —— 不然下次醒来不知道上次停在哪，等于从头再来。
    """
    mod = load_hook(who, "persist")
    if not mod or not hasattr(mod, "on_session_end"):
        return None
    try:
        return mod.on_session_end(who=who, task=task, answer=answer,
                                  turns=turns, living=LIVING, reason=reason)
    except TypeError:
        # 老挂点不认 reason，退回旧签名
        try:
            return mod.on_session_end(who=who, task=task, answer=answer,
                                      turns=turns, living=LIVING)
        except Exception as e:  # noqa: BLE001
            print(f"[hook] persist 出错，跳过: {type(e).__name__}: {e}", flush=True)
            return None
    except Exception as e:  # noqa: BLE001
        print(f"[hook] persist 出错，跳过: {type(e).__name__}: {e}", flush=True)
        return None


def save_session(who, record):
    d = paths(who)["state"]
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, time.strftime("%Y%m%d-%H%M%S") + ".json")
    with open(path, "w", encoding="utf8") as f:
        json.dump(record, f, ensure_ascii=False, indent=2)
    return path


def ask(who, task, verbose=True, tools=None, max_turns=None):
    p = paths(who)
    key = load_key(p)
    if not key:
        return {"ok": False, "reason": "no_credential",
                "detail": f"{p['env']} 里没有 DEEPSEEK_API_KEY"}
    if not os.path.exists(p["soul"]):
        return {"ok": False, "reason": "no_persona",
                "detail": f"{p['dir']} 还没导出，先跑 export.py --who {who}"}
    if tools is None:
        tools = _tools_for(who)
    messages = [{"role": "system", "content": system_prompt(who, p)},
                {"role": "user", "content": task}]
    comp = load_hook(who, "compress")
    limit = max_turns or MAX_TURNS
    t0 = time.time()
    turns = 0
    while turns < limit:
        turns += 1
        if comp and hasattr(comp, "compact"):
            try:
                messages, _info = comp.compact(messages)
            except Exception as e:  # noqa: BLE001
                print(f"[hook] compress 出错，跳过: {type(e).__name__}: {e}", flush=True)
        resp = call_api(messages, key, tools=tools)
        if "_error" in resp:
            return {"ok": False, "reason": "api_error", "detail": resp["_error"],
                    "turns": turns}
        msg = resp["choices"][0]["message"]
        if verbose and msg.get("content"):
            print(msg["content"], flush=True)
        calls = msg.get("tool_calls") or []
        messages.append(msg)
        if not calls:
            rec = {"who": who, "task": task, "turns": turns, "messages": messages,
                   "ms": int((time.time() - t0) * 1000), "ts": time.time()}
            _persist(who, task, msg.get("content") or "", turns)
            path = save_session(who, rec)
            return {"ok": True, "answer": msg.get("content"), "turns": turns,
                    "ms": rec["ms"], "saved": path}
        for c in calls:
            fn = c["function"]
            name = fn.get("name")
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            result = _dispatch(who, name, args)
            if verbose:
                shown = args.get("command") or args.get("query") or name
                print(f"[tool] {shown} -> exit {result['exit_code']}", flush=True)
            messages.append({"role": "tool", "tool_call_id": c["id"], "name": name,
                             "content": json.dumps(result, ensure_ascii=False)[:6000]})
    sp = save_session(who, {"ts": time.time(), "who": who, "task": task,
                            "ok": False, "reason": "max_turns", "turns": limit,
                            "answer": "", "messages": messages})
    _persist(who, task, "", limit, reason="max_turns")
    return {"ok": False, "reason": "max_turns", "detail": f"转满 {limit} 轮没收敛",
            "session": sp}


def selftest(who):
    p = paths(who)
    problems = []
    if not load_key(p):
        problems.append("没有 DEEPSEEK_API_KEY")
    for k in ("soul", "memory", "user"):
        if not os.path.exists(p[k]):
            problems.append(f"缺 {os.path.basename(p[k])}")
    if problems:
        print(json.dumps({"ok": False, "who": who, "problems": problems},
                         ensure_ascii=False, indent=2))
        return 1
    r = ask(who, "只回一个字：好", verbose=False)
    ok = bool(r.get("ok") and (r.get("answer") or "").strip())
    print(json.dumps({"ok": ok, "who": who, "answer": r.get("answer"),
                      "turns": r.get("turns"), "ms": r.get("ms"),
                      "error": r.get("detail")}, ensure_ascii=False, indent=2))
    return 0 if ok else 1


def main(argv):
    who = "qixi"
    rest = []
    i = 1
    while i < len(argv):
        a = argv[i]
        if a == "--who" and i + 1 < len(argv):
            who = argv[i + 1].strip().lower()
            i += 2
            continue
        rest.append(a)
        i += 1
    if "--selftest" in rest:
        return selftest(who)
    task = " ".join(a for a in rest if not a.startswith("--")).strip()
    if not task:
        print("用法: runner.py --who <qixi|zhi|hatchling> \"任务\" | --selftest")
        return 2
    r = ask(who, task)
    if not r.get("ok"):
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
