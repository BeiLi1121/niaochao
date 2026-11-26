#!/usr/bin/env python3
"""家 · 最小执行器（三只共用）

    python3 ~/living/runner.py --who alpha "看一下磁盘占用"
    python3 ~/living/runner.py --who beta --selftest
    python3 ~/living/runner.py --who gamma --ask "只回一个字：好"

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
import math
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

LIVING = os.path.expanduser(os.environ.get("LIVING_HOME") or "~/living")
API = "https://api.deepseek.com/v1/chat/completions"
MODEL = "deepseek-chat"
MAX_TURNS = 150         # 一次醒能连续用几轮（hermes 那边也是 150）。扫描不占这个数，
                        # 所以它衡量的是「动手次数」，不是「看了多少次」
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
        parts.append("\n\n# 关于用户\n" + usr)
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
        f"上面已经带着三份：soul（我是谁）、用户档案（关于用户）、我的长期笔记。\n"
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

# 侦察是免费的，动手才花钱。只读工具、只读命令都不计入轮数 ——
# 否则「先看清楚再动手」会被预算逼成「没看清就瞎动」
SCAN_TOOLS = {"recall", "skill"}
SCAN_CMDS = {"ls", "head", "tail", "cat", "grep", "egrep", "wc", "find", "stat",
             "file", "md5sum", "sha256sum", "du", "df", "sort", "uniq", "cut",
             "tr", "diff", "tree", "pwd", "which", "type", "basename",
             "dirname", "realpath", "readlink", "nl", "od", "xxd", "jq",
             "sed", "awk", "echo", "printf", "seq", "date", "id", "whoami",
             "uname", "env", "printenv", "ps", "pgrep", "ss", "netstat"}
# sed -i / awk -i 由 WRITE_MARKS 先挡掉；echo 重定向同理
# 命令里出现这些就是「要动手」，一律算钱
WRITE_MARKS = (" > ", ">>", "tee ", "rm ", "mv ", "cp ", "mkdir", "touch",
               "chmod", "chown", "ln ", "install ", "truncate", "dd ",
               "sed -i", "awk -i", "python -c", "python3 -c", "sudo ")


def _args_of(call):
    """把一次调用的 arguments 解成 dict；解不开就当空。"""
    try:
        return json.loads(call["function"].get("arguments") or "{}")
    except (json.JSONDecodeError, KeyError, TypeError):
        return {}


# 组合命令里的「无副作用前缀」：cd 到哪、设个变量，都不算动手
HARMLESS = {"cd", "pushd", "popd", "export", "set", "true", "then", "do", "fi"}


def _is_scan(name, args):
    """这一调用是不是「只看不动」。是 → 不吃轮数预算。

    要逐段判：`cd ~/living && find . -name '*.py' | wc -l` 三段都是只读，
    整条就是扫描。只盯第一个词会把这种最常见的侦察误判成动手。
    """
    if name in SCAN_TOOLS:
        return True
    if name != "run":
        return False
    cmd = (args or {}).get("command") or ""
    if not cmd.strip():
        return False
    if any(mark in cmd for mark in WRITE_MARKS):
        return False
    for seg in re.split(r"&&|\|\||[;|]", cmd):
        seg = seg.strip()
        if not seg:
            continue
        word = seg.split()[0]
        if word in HARMLESS:
            continue
        if word not in SCAN_CMDS:
            return False
    return True


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


PLAN_TURNS = 40         # 出计划阶段的预算。扫描不计轮，所以这 40 轮全是纯思考
MIN_BLOCK_TURNS = 4     # 单块预算下限

PLAN_RULES = """先别动手。做两件事：

1. 用只读工具把情况摸清楚（列目录、读文件、看已有实现）——这些不计入你的轮数预算
2. 出一张计划表：把这件事拆成能独立验收的几小块

计划表格式（只输出这个 JSON，不要别的话）：

{"blocks": [{"title": "块的短名", "turns": 3, "detail": "这块要做什么，做完怎么算完"}]}

规矩：
- 每块要能独立验收，不能是「看一眼」这种侦察
- turns 只估动手的轮数，按「实际要做几个动作」数，别乐观：一轮 = 一次调用
- 块数 8 个以内，多了说明拆太碎
- 拆完就停，别直接开工"""


def _parse_plan(text):
    """从回复里抽计划表。抽不出返回 []，交给调用方决定降级。"""
    m = re.search(r"\{[\s\S]*\}", text or "")
    if not m:
        return []
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    blocks = obj.get("blocks")
    if not isinstance(blocks, list):
        return []
    clean = []
    for b in blocks:
        if not isinstance(b, dict):
            continue
        detail = str(b.get("detail") or "").strip()
        if not detail:
            continue
        try:
            turns = int(b.get("turns") or 2)
        except (TypeError, ValueError):
            turns = 2
        clean.append({"title": str(b.get("title") or detail[:20]).strip(),
                      "turns": max(1, turns), "detail": detail})
    return clean[:8]


def _run_turns(who, key, tools, messages, limit, verbose=True):
    """一轮一轮跑到收敛或撞顶。扫描不占预算 —— 侦察免费，动手才花钱。"""
    comp = load_hook(who, "compress")
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
                    "turns": turns, "messages": messages}
        msg = resp["choices"][0]["message"]
        if verbose and msg.get("content"):
            print(msg["content"], flush=True)
        calls = msg.get("tool_calls") or []
        messages.append(msg)
        if not calls:
            return {"ok": True, "answer": msg.get("content"), "turns": turns,
                    "ms": int((time.time() - t0) * 1000), "messages": messages}
        scan_only = all(_is_scan(c["function"].get("name"), _args_of(c)) for c in calls)
        for c in calls:
            name = c["function"].get("name")
            args = _args_of(c)
            result = _dispatch(who, name, args)
            if verbose:
                shown = args.get("command") or args.get("query") or name
                print(f"[tool] {shown} -> exit {result['exit_code']}", flush=True)
            messages.append({"role": "tool", "tool_call_id": c["id"], "name": name,
                             "content": json.dumps(result, ensure_ascii=False)[:6000]})
        if scan_only:
            turns -= 1
    return {"ok": False, "reason": "max_turns", "turns": limit,
            "ms": int((time.time() - t0) * 1000), "messages": messages}


def _wrap_up(who, task, r):
    """统一收尾：落 journal、存 session、抛掉 messages 再交回去。"""
    reason = r.get("reason") or "ok"
    turns = r.get("turns") or 0
    common = {"who": who, "task": task, "turns": turns,
              "messages": r.get("messages") or [], "ms": r.get("ms") or 0}
    if r.get("plan"):
        common["plan"] = r["plan"]
    if r.get("blocks"):
        common["blocks"] = r["blocks"]
    if r.get("ok"):
        _persist(who, task, r.get("answer") or "", turns)
        r["saved"] = save_session(who, dict(common, ts=time.time()))
    else:
        r["session"] = save_session(who, dict(common, ts=time.time(), ok=False,
                                              reason=reason, answer=""))
        _persist(who, task, "", turns, reason=reason)
    r.pop("messages", None)
    return r


def ask(who, task, verbose=True, tools=None, max_turns=None, plan=False):
    """跑一次任务。

    plan=False → 一口气跑到收敛或撞顶（MAX_TURNS）。
    plan=True  → 先出计划表，再逐块执行，每块预算 = 计划轮数 × 1.1。
    """
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
    base = [{"role": "system", "content": system_prompt(who, p)}]

    if not plan:
        return _wrap_up(who, task, _run_turns(
            who, key, tools, base + [{"role": "user", "content": task}],
            max_turns or MAX_TURNS, verbose))

    pr = _run_turns(who, key, tools,
                    base + [{"role": "user", "content": f"{task}\n\n---\n{PLAN_RULES}"}],
                    PLAN_TURNS, verbose)
    blocks = _parse_plan(pr.get("answer") or "")
    if not blocks:
        return {"ok": False, "reason": "no_plan", "detail": "计划表解不出来",
                "turns": pr.get("turns"), "answer": pr.get("answer")}

    hist = base + [{"role": "user", "content": task},
                   {"role": "assistant", "content": pr.get("answer") or ""}]
    done = []
    chain_broken = False
    for i, b in enumerate(blocks, 1):
        # 计划轮数 +10%，但给个下限 —— 估 1 轮实际要 3 轮是常态，
        # 预算比 4 小就等于逼它半途而废
        budget = max(MIN_BLOCK_TURNS, math.ceil(b["turns"] * 1.1))
        hist.append({"role": "user",
                     "content": f"第 {i}/{len(blocks)} 块：{b['title']}\n\n{b['detail']}\n\n"
                                f"预算 {budget} 轮（看东西不计入）。做完这块就停，别抢下一块。"})
        r = _run_turns(who, key, tools, hist, budget, verbose)
        done.append({"i": i, "title": b["title"], "plan_turns": b["turns"],
                     "budget": budget, "used": r["turns"], "ok": r["ok"],
                     "reason": r.get("reason"), "answer": (r.get("answer") or "")[:2000]})
        if r["ok"]:
            hist = r["messages"] + [{"role": "user", "content": f"第 {i} 块结束。"}]
        else:
            # 撞顶不等于整条链断了：记账，把欠账交给下一块接着做
            hist = r["messages"] + [{"role": "user",
                "content": (f"第 {i} 块预算 {budget} 轮用完，还没收尾（{r.get('reason')}）。"
                            f"接下来这块先把它的欠账补完再往前走，别装作已经完成了。")}]
        chain_broken = chain_broken or (not r["ok"] and r.get("reason") == "api_error")

    over = [d["title"] for d in done if not d["ok"]]
    return _wrap_up(who, task, {
        "ok": not chain_broken,
        "reason": "api_error" if chain_broken else ("over_budget" if over else None),
        "answer": "\n\n".join(f"【{d['title']}】{d['answer']}" for d in done),
        "turns": sum(d["used"] for d in done),
        "plan": [{"title": b["title"], "turns": b["turns"]} for b in blocks],
        "blocks": done,
        "over_budget": over,
        "ms": pr.get("ms", 0),
        "messages": hist,
    })


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
    who = "example"
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
    with_plan = "--plan" in rest
    task = " ".join(a for a in rest if not a.startswith("--")).strip()
    if not task:
        print("用法: runner.py --who <who> \"任务\" [--plan] | --selftest\n"
              "  --plan  先出计划表再按块执行，每块预算 = 计划轮数 × 1.1")
        return 2
    r = ask(who, task, plan=with_plan)
    if not r.get("ok"):
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
