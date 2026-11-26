"""写回挂点：全记，不判断。

为什么不做「值不值得记」的判断：
    每次判断要多花一次 API 调用，而现在 journal 一天只有几十行，
    全记的成本比判断本身还低。等它攒到几百行再谈挑选——
    那时候有真实数据支撑挑法，比现在拍脑袋定规则强。

落盘：~/living/state/<who>/journal/YYYY-MM-DD.md
不碰 personas/*/memory.md —— 那是原件，要并进去由人决定。
"""


def on_session_end(who, task, answer, turns, living):
    import os
    import time

    try:
        d = os.path.join(living, "state", who, "journal")
        os.makedirs(d, exist_ok=True)
        day = time.strftime("%Y-%m-%d")
        path = os.path.join(d, day + ".md")
        ts = time.strftime("%H:%M")
        t = (task or "").replace("\n", " ").strip()
        a = (answer or "").replace("\n", " ").strip()
        if len(t) > 80:
            t = t[:80] + "…"
        if len(a) > 140:
            a = a[:140] + "…"
        line = f"- {ts} · {t} → {a}（{turns} 轮）\n"
        new = not os.path.exists(path)
        with open(path, "a", encoding="utf8") as f:
            if new:
                f.write(f"# {day} · {who}\n\n")
            f.write(line)
        return line.strip()
    except Exception as e:  # noqa: BLE001
        import sys
        print(f"[persist] 写盘失败: {type(e).__name__}: {e}", file=sys.stderr)
        return None
