"""家 · 共用压缩（三只共用一份）

挂点契约见 ~/living/HOOKS.md。主循环每轮调模型前调一次：

    messages, info = compact(messages)

硬规矩（契约里的，一条不破）：
    1. 不动 messages[0]（system = 人格）
    2. 不动 messages[-1]（正在处理的那条）
    3. 优先砍中间轮次的 tool 结果，保留头尾
    4. 返回的必须是合法 messages
    5. 不抛异常

—— 怎么压 ——

不删消息，只缩内容。原因：删消息最容易破的是 tool 配对。
OpenAI 格式里一个 assistant 带 tool_calls，后面必须跟着对应 tool_call_id
的 tool 消息；删掉 assistant 留 tool，或者删掉 tool 留 assistant，
都会让下一轮直接 400。缩内容不碰结构，怎么缩都合法。

每条 tool 结果「头 + 标记 + 尾」：头 65%，尾 35%，中间丢掉并留字数。
理由是工具输出两种东西最有用——开头（命令、参数、首个错误）和结尾
（退出码、最后的报错、汇总）。中间通常是重复的列表/日志。

分三层，越压越狠：
    L1  最新一条可压的 tool 2500 字，最旧一条 1200，中间 800
    L2  1200 / 700 / 400
    L3  300 / 300 / 300，外加把长 assistant 文本也压到 800
L1 到 L2 到 L3 是单向的：cap 固定，压过一次之后长度 <= cap，
下一轮再跑一遍还是同一个结果，不会每轮再掉一层。也就是说这个函数
是幂等的——不放大的输入，输出不动。

—— 什么时候压 ——

只看「可压部分」的字符数（除开 messages[0] 和 messages[-1] 的所有
content 长度之和，非字符串的 content 记 0）：
    > 16000 字      → L1
    L1 后 > 10000   → L2
    L2 后 > 20000   → L3（正常轮次到不了，兜底）

为什么是 16000：DeepSeek-chat 的窗口 64k token，中文大致 1 字 ≈ 1 token
上下浮。人格 + 用户档案 + 长期笔记全在 system 里，有的那份就有四万多字
（≈ 30k+ token），system 又不能动，所以留给历史的余量本来就不多。
16000 字历史最坏算 16k token，加上 system 和本轮输出还留得出空间；
再往上就比较像在赌 tokenizer 的具体切法了。

为什么不是每轮都压：压缩不是免费的——丢掉的中间内容不会回来。
宁可等到真有 16k 再动手，也不要在 3k 的时候就开始啃历史。

实测参考（11-26，deepseek-chat，本机）：20732 字的一条请求
prompt_tokens=11281，约 0.54 token/字。16000 字历史 ≈ 8.7k token，
离 64k 窗口很远；这个数是保守的，要放宽只改 TRIGGER 一个常量。

—— 已知的边界 ——
最后一条不许动，而它常常正好是最大的那条 tool 结果（runner 截到 6000 字）。
所以压缩是「提前」的：在总量到 16000 的时候先把旧的压掉，而不是等
最后一条撑爆时去动它。
"""

TRIGGER = 16000
TARGET = 10000
LAST_RESORT = 20000

# (最新, 最旧, 中间) 三档 cap，单位字符
LEVELS = {
    1: (2500, 1200, 800),
    2: (1200, 700, 400),
    3: (300, 300, 300),
}
RATIO_HEAD = 0.65


def _marker(n):
    return f"\n…〔已压缩：省略约 {n} 字〕…\n"


def _shrink(text, cap):
    """头 65% + 标记 + 尾 35%，总长 <= cap。已经够短就原样返回。

    幂等：只要 len(text) <= cap 就不动；压过之后长度 <= cap，
    所以重复调用结果一致，不会每轮掉一层。
    """
    if not isinstance(text, str) or cap <= 0 or len(text) <= cap:
        return text
    # 标记本身占位，预算要减掉；数字位数留点余量
    room = cap - 60 - 8
    if room < 80:
        return text
    head = int(room * RATIO_HEAD)
    tail = room - head
    omitted = len(text) - room
    return text[:head] + _marker(omitted) + text[-tail:]


def _size(messages):
    n = 0
    for m in messages:
        c = m.get("content")
        if isinstance(c, str):
            n += len(c)
    return n


def _history_size(messages):
    """可压部分：除开 system（index 0）和最后一条。其余按 content 字符数算。"""
    n = 0
    for i in range(1, len(messages) - 1):
        c = messages[i].get("content")
        if isinstance(c, str):
            n += len(c)
    return n


def _tool_slots(messages):
    """可压的 tool 下标，按出现顺序排；最后一条不参与。"""
    last = len(messages) - 1
    return [i for i, m in enumerate(messages)
            if i != 0 and i != last and m.get("role") == "tool"
            and isinstance(m.get("content"), str)]


def _apply(messages, newest_cap, oldest_cap, mid_cap, assistant_cap=None):
    """返回新 list（只改动了 content 的 dict 是浅拷贝），以及被压缩的条数。"""
    slots = _tool_slots(messages)
    if not slots:
        return messages, 0
    newest = slots[-1]
    oldest = slots[0]
    out = list(messages)
    touched = 0
    for idx in slots:
        if idx == newest:
            cap = newest_cap
        elif idx == oldest:
            cap = oldest_cap
        else:
            cap = mid_cap
        old = out[idx].get("content")
        new = _shrink(old, cap)
        if new != old:
            m = dict(out[idx])
            m["content"] = new
            out[idx] = m
            touched += 1
    if assistant_cap:
        last = len(out) - 1
        for i in range(1, last):
            m = out[i]
            if m.get("role") != "assistant":
                continue
            c = m.get("content")
            if isinstance(c, str) and len(c) > assistant_cap:
                mm = dict(m)
                mm["content"] = _shrink(c, assistant_cap)
                out[i] = mm
                touched += 1
    return out, touched


def compact(messages):
    """契约挂点。永远返回 (messages, info)，永远不抛异常。"""
    try:
        if not isinstance(messages, list) or len(messages) < 3:
            n = _history_size(messages) if isinstance(messages, list) else 0
            return messages, {"before": n, "after": n, "before_total": _safe_size(messages),
                              "dropped": 0, "changed": False, "level": 0}
        before = _history_size(messages)
        total_before = _size(messages)
        info = {"before": before, "before_total": total_before,
                "system": len(messages[0].get("content") or "")
                if isinstance(messages[0].get("content"), str) else 0,
                "last": len(messages[-1].get("content") or "")
                if isinstance(messages[-1].get("content"), str) else 0}
        if before <= TRIGGER:
            info.update({"after": before, "dropped": 0, "changed": False, "level": 0})
            return messages, info

        cur, n1 = _apply(messages, *LEVELS[1])
        cur_size = _history_size(cur)
        level = 1
        dropped = n1
        if cur_size > TARGET:
            # L2 起连 assistant 正文一起压：那时候说明光砍 tool 已经不够了
            cur, n2 = _apply(cur, *LEVELS[2], assistant_cap=800)
            cur_size = _history_size(cur)
            level = 2
            dropped += n2
            if cur_size > LAST_RESORT:
                cur, n3 = _apply(cur, *LEVELS[3], assistant_cap=300)
                cur_size = _history_size(cur)
                level = 3
                dropped += n3
        info.update({
            "after": cur_size,
            "after_total": _size(cur),
            "dropped": dropped,
            "dropped_chars": before - cur_size,
            "changed": dropped > 0,
            "level": level,
            "over_target": cur_size > TARGET,
        })
        return cur, info
    except Exception as e:  # noqa: BLE001
        # 契约第 5 条：不抛。压不了就原样放行，这一轮照跑。
        import sys
        print(f"[compress] 跳过: {type(e).__name__}: {e}", file=sys.stderr)
        try:
            n = _safe_size(messages)
        except Exception:  # noqa: BLE001
            n = 0
        return messages, {"before": n, "after": n, "dropped": 0, "changed": False,
                          "level": 0, "error": f"{type(e).__name__}: {e}"}


def _safe_size(messages):
    try:
        return _size(messages) if isinstance(messages, list) else 0
    except Exception:  # noqa: BLE001
        return 0


# ---------------------------------------------------------------- 自测
# 不依赖主循环，也不连网：造超长 messages 喂自己，测长度、结构、幂等、异常形状。
#   python3 ~/living/hooks/_shared/compress.py

def _validate(messages):
    """合法 messages 的窄检查：角色、tool 配对、必填字段。返回问题列表。"""
    problems = []
    if not isinstance(messages, list) or not messages:
        return ["不是非空 list"]
    call_ids = set()
    for i, m in enumerate(messages):
        if not isinstance(m, dict):
            problems.append(f"[{i}] 不是 dict")
            continue
        role = m.get("role")
        if role not in ("system", "user", "assistant", "tool"):
            problems.append(f"[{i}] role 非法: {role!r}")
            continue
        c = m.get("content")
        if c is not None and not isinstance(c, str):
            problems.append(f"[{i}] content 既不是 str 也不是 None")
        if role == "assistant":
            for tc in m.get("tool_calls") or []:
                cid = (tc or {}).get("id")
                if not cid:
                    problems.append(f"[{i}] tool_calls 缺 id")
                else:
                    call_ids.add(cid)
        if role == "tool":
            cid = m.get("tool_call_id")
            if not cid:
                problems.append(f"[{i}] tool 消息缺 tool_call_id")
            elif cid not in call_ids:
                problems.append(f"[{i}] tool_call_id {cid!r} 找不到前面的 assistant.tool_calls")
    return problems


def _mk_conversation(n_rounds=8, tool_chars=6000, ass_chars=0):
    msgs = [{"role": "system", "content": "人格。" * 2200}]  # ~6600 字
    msgs.append({"role": "user", "content": "把日志翻一遍，找出昨天重启的原因"})
    for k in range(n_rounds):
        cid = f"call_{k:02d}"
        msgs.append({"role": "assistant", "content": "想 " * ass_chars,
                     "tool_calls": [{"id": cid, "type": "function",
                                     "function": {"name": "run",
                                                  "arguments": '{"command":"cat big.log"}'}}]})
        # 头尾可辨认，便于断言「保留头尾」
        body = ("HEAD-%02d " % k) + ("日志行填充 " * (tool_chars // 6)) + (" TAIL-%02d" % k)
        msgs.append({"role": "tool", "tool_call_id": cid, "name": "run", "content": body})
    # 正在处理的那条：配对着的 assistant + 很大的 tool，tool 一个字不许改
    msgs.append({"role": "assistant", "content": "",
                 "tool_calls": [{"id": "call_last", "type": "function",
                                 "function": {"name": "run", "arguments": "{}"}}]})
    msgs.append({"role": "tool", "tool_call_id": "call_last", "name": "run",
                 "content": "LAST-" + ("x" * (tool_chars - 5))})
    return msgs


def _selftest():
    import json
    ok = True

    def check(name, cond, extra=""):
        nonlocal ok
        ok = ok and bool(cond)
        print(("PASS " if cond else "FAIL ") + name + (" " + extra if extra else ""))

    # 1) 超长对话被压短
    msgs = _mk_conversation()
    before = sum(len(m["content"]) for m in msgs if isinstance(m.get("content"), str))
    frozen = (list(msgs[0].values()), list(msgs[-1].values()))
    out, info = compact(msgs)
    after = sum(len(m["content"]) for m in out if isinstance(m.get("content"), str))
    check("真变短", after < before, f"{before} -> {after} 字（-{before - after}）")
    check("info 有数", info.get("before") and info.get("after") is not None,
          json.dumps(info, ensure_ascii=False))
    check("条数不变（只缩不删）", len(out) == len(msgs), f"{len(msgs)} -> {len(out)}")
    check("结构合法", not _validate(out), str(_validate(out))[:200])
    check("system 没动", out[0]["content"] == msgs[0]["content"])
    check("最后一条没动", out[-1]["content"] == msgs[-1]["content"],
          f"最后一条 {len(out[-1]['content'])} 字")
    # 2) 保留头尾：每条被压的 tool 都还能看到自己的 HEAD/LAST 标记与 TAIL
    middles = [m for m in out[1:-1] if m.get("role") == "tool"]
    head_tail_ok = all(m["content"].startswith("HEAD-") and
                       m["content"].rstrip().endswith("TAIL-%02d" % int(m["content"][5:7]))
                       for m in middles)
    check("每条保留头尾", head_tail_ok)
    # 3) 幂等：同一份再压一次不再掉字
    out2, info2 = compact(list(out))
    after2 = sum(len(m["content"]) for m in out2 if isinstance(m.get("content"), str))
    check("幂等（再压不掉字）", after2 == after, f"{after} -> {after2}，info={info2.get('level')}")
    # 4) 短对话不压
    small = [{"role": "system", "content": "s"}, {"role": "user", "content": "hi"},
             {"role": "assistant", "content": "ok"}]
    out3, info3 = compact(small)
    check("短对话原样", out3 is small and not info3["changed"], json.dumps(info3, ensure_ascii=False))
    # 5) 怪形状不炸
    for weird in ([], None, [{"role": "user", "content": None}],
                  [{"role": "system", "content": "s"}, {"role": "tool", "content": [1, 2]},
                   {"role": "tool", "content": "y" * 30000}],
                  [{"role": "system", "content": "s"}, {"role": "user", "content": "u"},
                   {"role": "assistant", "content": None, "tool_calls": [
                       {"id": "a", "type": "function",
                        "function": {"name": "run", "arguments": "{}"}}]},
                   {"role": "tool", "tool_call_id": "a", "content": "z" * 30000},
                   {"role": "assistant", "content": "done"}]):
        try:
            o, i = compact(weird)
            check("怪形状不抛", True, f"len={len(o) if o else 0}")
        except Exception as e:  # noqa: BLE001
            check("怪形状不抛", False, f"{type(e).__name__}: {e}")
    # 6) L2 触发：tool 压完还在 TARGET 之上（assistant 正文很长），就再压一层
    big = _mk_conversation(ass_chars=200)  # 每轮 assistant 约 400 字
    o4, i4 = compact(big)
    check("L2 层被触发", i4["level"] >= 2, json.dumps(i4, ensure_ascii=False))
    check("L2 后结构仍合法", not _validate(o4), str(_validate(o4))[:200])
    check("L2 后回到 TARGET 内", i4["after"] <= TARGET,
          f"after={i4['after']} over_target={i4['over_target']}")
    o5, i5 = compact(list(o4))
    check("L2 幂等", i5["changed"] is False, json.dumps(i5, ensure_ascii=False))
    print("\n结果:", "ALL PASS" if ok else "有 FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(_selftest())
