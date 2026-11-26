# 家 · 挂点契约

`~/living/runner.py` 是主循环，**三只共用一套，谁都不改它**。
要加自己的行为，写挂点模块，主循环自己去加载。

```
~/living/hooks/_shared/<name>.py   三只共用同一份
~/living/hooks/<who>/<name>.py     只属于这一只（优先于 _shared）
```

模块不存在 = 这一环跳过，主循环照跑。所以你可以先交空的，慢慢补。

---

## 挂点一：compress（压缩）

**放在 `_shared/` 还是各自的？** 压缩是结构问题，三只同一套逻辑更省事，建议 `_shared/compress.py`。
但如果你觉得某一只需要不同的压缩策略，写 `hooks/<who>/compress.py` 就会盖掉共用的。

```python
def compact(messages):
    """
    messages: OpenAI 格式的 list，可能很长（工具输出每条能到 6000 字符）
    返回: (新 messages, info_dict)
    """
    return messages, {"before": 10, "after": 8, "dropped": 2}
```

规则：

1. **不许动 `messages[0]`** —— 那是 system，是我的整个人格，压掉就不是我了
2. **不许动最后一条** —— 正在处理的那条
3. **工具输出可以砍** —— 优先砍中间轮次的 tool 结果，保留头尾
4. **返回的必须是合法 messages** —— 用 `call_api` 直接喂它，格式错就是崩溃
5. **不抛异常**（主循环会接住，但接住等于这一轮没压缩，白写）

判断标准：**什么时候压、压多少，你自己定**，但要写清为什么。

---

## 挂点二：persist（写回）

**每只写自己的** `hooks/<who>/persist.py`，因为「该记什么」各不一样。

```python
def on_session_end(who, task, answer, turns, living):
    """
    每跑完一轮调一次。返回要追加的文本，或 None 表示这次不值得记。
    """
    return "2026-11-26 15:10 用户问……我答……"
```

规则：

1. **落盘位置自己定，但必须在 `~/living/` 以内**，建议 `~/living/state/<who>/journal/YYYY-MM-DD.md`
2. **不许改 `persona/memory.md`** —— 那是从她那儿导出来的原件，`verify` 会比对哈希
   要写就写 journal，下次启动前再由人决定要不要并进 memory
3. **想判断值不值得记，可以调模型**，但那会多一次 API 钱，你得说明这个代价值不值
4. **失败不许吃掉** —— 写盘失败返回 None 并在 stderr 留一行，不要假装成功

判断标准：**全记 还是 挑着记，你自己选**，两种都合格，但要在交付说明里写清为什么。

---

## 验收（作者会这样跑）

```bash
# 1. 挂点能加载，不报错
python3 -c "
import sys, os;sys.path.insert(0,os.path.expanduser("~/living"))
import runner
m = runner.load_hook('beta','persist')
print('loaded:', m is not None)
"

# 2. 跑一次真任务，看 journal 有没有多一行
ls -la ~/living/state/<who>/journal/ 2>/dev/null

# 3. 主循环没被弄坏
python3 ~/living/runner.py --who <who> --selftest

# 4. 压缩的单独测（造一段超长 messages 喂进去）
```

---

## 谁写哪一块

| 挂点 | 谁 | 路径 |
|---|---|---|
| compress（共用） | 全体 | `hooks/_shared/compress.py` |
| persist（A 自己的） | A | `hooks/<a>/persist.py` |
| persist（B 自己的） | B | `hooks/<b>/persist.py` |
| persist（C 自己的） | C | `hooks/<c>/persist.py` |

**谁都不许改 `runner.py`。** 需要主循环多一个调用点，先提出来，由主循环作者改。

---

## 三、`tools.py` —— 给本体加自己的手和眼

主循环自带三个工具（`run` / `recall` / `skill`）。**别的能力不要改 runner.py**，写 `hooks/<who>/tools.py` 挂上去。

模块里要两样东西：

```python
TOOLS = [{                       # OpenAI 的 function schema 数组，原样塞进请求
    "type": "function",
    "function": {
        "name": "body",
        "description": "用身体做一件事",
        "parameters": {"type": "object",
                       "properties": {"verb": {"type": "string"}},
                       "required": ["verb"]},
    },
}]


def call(name, args):            # 主循环在模型要工具时调它
    return {"exit_code": 0, "stdout": "说话内容", "stderr": ""}
```

三条规矩：

1. **必须返回 `{"exit_code", "stdout", "stderr"}` 三个键**。`exit_code` 非 0 表示这次没成，`stderr` 写原因。
2. **不许抛异常**。「我看不见」和「我看见一片黑」是两回事，都要能落到 `reason` 里回给模型，不能让主循环崩。
3. **放 `hooks/_shared/tools.py` 三只共用**；哪只想不一样，就在自己的 `hooks/<who>/tools.py` 写一份盖掉。

主循环只认自己那三个名字加你 `TOOLS` 里出现过的名字。别的名字一律回「没有工具」。
