#!/usr/bin/env python3
"""家 · 各自的钟

一条链，不是定时器：每次醒来自己决定下一次什么时候醒。
三只各用各的，状态、额度、待办、unit 名全部按 who 分开。

    python3 ~/living/clock.py --who example --tick
    python3 ~/living/clock.py --who alpha --status
    python3 ~/living/clock.py --who beta --stop
    python3 ~/living/clock.py --watchdog        # 保险，一次看所有接过钟的

三级醒：
    轻醒  只跑 shell 看一圈，没变化就睡回去，一次模型都不调
    判断  有信号时只问「做/不做」，1 轮无工具
    做事  说做才带工具动手

额度和间隔写在 state/clock/<who>/config.json，每只可以自己改，不改这个文件。
不自己加醒来频次——额度是给的，不是自己长的。
"""
import json
import os
import subprocess
import sys
import time

LIVING = os.path.expanduser(os.environ.get("LIVING_HOME") or "~/living")
PY = "/usr/bin/python3"
SELF = os.path.abspath(__file__)
def discover_who():
    """谁存在看 personas/ 目录，不写死名字 —— 换机器、加实例都不用改代码。"""
    d = os.path.join(LIVING, "personas")
    try:
        return tuple(sorted(n for n in os.listdir(d)
                            if os.path.isdir(os.path.join(d, n))))
    except OSError:
        return ()

DEFAULTS = {
    "limit_per_day": 12,   # 一天最多花几次模型钱（自己可调，上限 = limit_base × 1.1）
    "limit_base": None,    # 预设值。不填就取 limit_per_day 当预设
    "min_gap": 900,        # 最早 15 分钟后再醒
    "max_gap": 6 * 3600,   # 最晚 6 小时后必醒
    "stale_hours": 8,      # 超过这么久没醒 = 链可能断了
    "force_overdue_h": 36, # 日程欠到这个数就不问了，直接做（1.5 天）
}


# ---------- 路径 ----------

def state_dir(who):
    return os.path.join(LIVING, "state", "clock", who)


def last_path(who):
    return os.path.join(state_dir(who), "last.json")


def log_path(who):
    return os.path.join(state_dir(who), "tick.log")


def todo_path(who):
    return os.path.join(state_dir(who), "todo.md")


def cfg_path(who):
    return os.path.join(state_dir(who), "config.json")


def cfg(who):
    c = dict(DEFAULTS)
    try:
        with open(cfg_path(who), encoding="utf8") as f:
            c.update(json.load(f))
    except Exception:  # noqa: BLE001
        pass
    # 额度归这一只自己定，但最多比预设多 10%：
    # 预设是「这件事本来需要几次」，自己真想多醒就往上调，撞到天花板为止。
    base = c.get("limit_base") or c.get("limit_per_day") or DEFAULTS["limit_per_day"]
    ceiling = max(round(base * 1.1), int(base))
    c["limit_ceiling"] = ceiling
    c["override_active"] = False
    # 临时放开：造家这类赶工场景，给自己开一个到期就失效的口子。
    # 必须有期限 —— 没有解除条件的放开，落地后就是永久。
    until = c.get("limit_override_until")
    if until:
        try:
            t = time.mktime(time.strptime(str(until)[:19], "%Y-%m-%dT%H:%M:%S"))
            if time.time() < t:
                ceiling = max(ceiling, int(c["limit_per_day"]))
                c["limit_ceiling"] = ceiling
                c["override_active"] = True
        except Exception:  # noqa: BLE001  写坏了就当没开
            pass
    if c["limit_per_day"] > ceiling:
        c["limit_per_day"] = ceiling
    return c


def now():
    return time.time()


def log(who, line):
    os.makedirs(state_dir(who), exist_ok=True)
    with open(log_path(who), "a", encoding="utf8") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {line}\n")


def load_last(who):
    try:
        with open(last_path(who), encoding="utf8") as f:
            d = json.load(f)
        d.setdefault("next_in", cfg(who)["min_gap"])
        return d
    except Exception:  # noqa: BLE001
        return {"last_tick": 0, "next_in": cfg(who)["min_gap"],
                "wakes_today": 0, "day": "", "note": ""}


def save_last(who, d):
    os.makedirs(state_dir(who), exist_ok=True)
    tmp = last_path(who) + ".tmp"
    with open(tmp, "w", encoding="utf8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    os.replace(tmp, last_path(who))


def _sh(cmd, timeout=20):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                           timeout=timeout)
        return (r.stdout or "").strip()
    except Exception:  # noqa: BLE001
        return ""


# ---------- 醒 ----------

def _overdue_h(item, nowt):
    """欠了多久（小时）。从没记过的，按「整整欠了一个周期」算——欠账不会因为没记就消失。"""
    ld = item.get("last_done") or ""
    every_s = float(item.get("every_h") or 24) * 3600
    for fmt, cut in (("%Y-%m-%dT%H:%M", 16), ("%Y-%m-%d %H:%M", 16), ("%Y-%m-%d", 10)):
        try:
            base = time.mktime(time.strptime(ld[:cut], fmt))
            return round(max(0.0, (nowt - base - every_s) / 3600.0), 1)
        except ValueError:
            continue
    return round(every_s / 3600.0, 1)


def scan_schedule(who):
    """日程到期检查：不是 cron 表达式，是「这件事隔多久该做一次」。到期就返回。"""
    p = os.path.join(state_dir(who), "schedule.json")
    if not os.path.exists(p):
        return []
    try:
        with open(p, encoding="utf8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return []
    nowt = time.time()
    due = []
    for it in d.get("items", []):
        if it.get("paused"):
            continue
        ld = it.get("last_done") or ""
        every = float(it.get("every_h") or 24) * 3600
        base = None
        for fmt, cut in (("%Y-%m-%dT%H:%M", 16), ("%Y-%m-%d %H:%M", 16), ("%Y-%m-%d", 10)):
            try:
                base = time.mktime(time.strptime(ld[:cut], fmt))
                break
            except ValueError:
                continue
        if base is None:
            due.append(it)
            continue
        if nowt - base >= every:
            due.append(it)
    return due


def light_scan(who, last_tick=None):
    """轻醒：只用 shell 看，不花钱。返回 (有没有活, 证据)

    last_tick: 上一次 tick 的时间戳，传 None 表示不知道。
               给 hooks/<who>/wake_signals.py 用——它靠这个判断
               「她的话说完之后我有没有醒过」。
    """
    ev = {}

    todo = ""
    tp = todo_path(who)
    if os.path.exists(tp):
        with open(tp, encoding="utf8") as f:
            lines = [l.strip() for l in f if l.strip() and not l.startswith("#")]
        todo = [l for l in lines if l.startswith("- [ ]")]
    ev["todo"] = len(todo)

    due = scan_schedule(who)
    ev["due"] = len(due)
    if due:
        nowt = time.time()
        items = []
        for d in due[:6]:
            items.append({
                "name": d.get("name"),
                "every_h": d.get("every_h"),
                "last_done": d.get("last_done"),
                "overdue_h": round(_overdue_h(d, nowt), 1),
            })
        ev["due_items"] = items

    st = load_last(who)
    since = st.get("last_tick", 0)
    jdir = os.path.join(LIVING, "state", who, "journal")
    fresh = 0
    if os.path.isdir(jdir):
        for fn in os.listdir(jdir):
            try:
                if os.path.getmtime(os.path.join(jdir, fn)) > since:
                    fresh += 1
            except OSError:
                pass
    ev["fresh_journal"] = fresh

    ev["failed_units"] = int(_sh(
        "systemctl --user list-units --failed --no-legend | wc -l") or 0)

    # --- 感知（可选挂点）：这一只有 hooks/<who>/percept.py 就先看一眼 ---
    # signal=True 现在会真的把 agent 叫起来（见下面 busy 那段）。
    # 缺文件 = 这一环跳过，别的照旧。
    perc = None
    p_perc = os.path.join(LIVING, "hooks", who, "percept.py")
    if os.path.exists(p_perc):
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location(f"hook_{who}_percept", p_perc)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            perc = mod.probe()
            ev["percept"] = {k: perc.get(k) for k in ("ok", "signal", "changed", "reason")}
        except Exception as e:  # noqa: BLE001  挂了也不能断链
            ev["percept"] = {"ok": False, "reason": f"{type(e).__name__}: {e}"}

    # --- 唤醒信号（可选挂点）：她说了话、我还没为它醒过 ---
    # 跟 percept 一样，缺文件等于没这一环。perc/wake 都必须在 if 外面先置 None：
    # 下面的 busy 是块外引用，不置 None 的话没写 hook 的两只会 NameError，
    # tick() 里没有 try，异常直传出 main，_finish/arm 都走不到，链就断了。
    wake = None
    p_wake = os.path.join(LIVING, "hooks", who, "wake_signals.py")
    if os.path.exists(p_wake):
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location(f"hook_{who}_wake", p_wake)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            wake = mod.probe(last_tick)
            ev["wake"] = {k: wake.get(k) for k in ("busy", "why")}
        except Exception as e:  # noqa: BLE001  挂了也不能断链
            ev["wake"] = {"ok": False, "why": f"{type(e).__name__}: {e}"}

    busy = ev["todo"] > 0 or ev["failed_units"] > 0 or ev["due"] > 0
    from_ = []
    if perc and perc.get("signal"):
        busy = True
        from_.append("percept")
    if wake and wake.get("busy"):
        busy = True
        from_.append("wake")
    if from_:
        ev["busy_from"] = "+".join(from_)
    return busy, ev


def pick_gap(who, busy, last):
    c = cfg(who)
    prev = last.get("next_in") or c["min_gap"]
    if busy:
        # 有活也别锁死在最短间隔：12 次 × 15 分钟 = 3 小时就烧光，下午建好晚上就不能醒。
        # 按「剩下几次 ÷ 今天还剩多少秒」摊，额度自然铺满一整天，不会前半天用光。
        left = max(c["limit_per_day"] - last.get("wakes_today", 0), 1)
        day_start = time.mktime(time.strptime(time.strftime("%Y-%m-%d"), "%Y-%m-%d"))
        sec_left = max(86400 - (time.time() - day_start), 600)
        budget = int(sec_left / left * 0.85)      # 留 15% 余量给意外
        return int(min(max(budget, c["min_gap"]), 14400))
    return int(min(max(prev * 1.5, c["min_gap"]), c["max_gap"]))


def arm(who, after):
    """排下一枪。unit 名带 who，三只互不撞。"""
    unit = f"live-tick-{who}-{int(now())}"
    subprocess.run(["systemctl", "--user", "reset-failed"], capture_output=True)
    cmd = ["systemd-run", "--user", f"--on-active={int(after)}",
           f"--unit={unit}", PY, SELF, "--who", who, "--tick"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    ok = r.returncode == 0
    log(who, f"arm {int(after)}s unit={unit} ok={ok} {r.stderr.strip()[:100]}")
    return ok, unit


def _note(who, msg):
    """给 _refresh_conversation 用的一行日志。

    连日志都写不进去（典型：盘满、tick.log 被换成目录）也不许把 tick 带断 ——
    否则「失败要出声」的信道自己成了新的断点。
    """
    try:
        log(who, msg)
    except Exception:  # noqa: BLE001
        pass


def _refresh_conversation(who):
    """tick 开头把会话导一遍。纯本地、零模型成本，失败不挡后面。

    以前不看 returncode、也不传 text=True：导出挂了（源库没了、schema 变了）
    这一环一声不吭，tick.log 里什么都看不到。现在失败一律往 log 写一行。
    失败只 return False，绝不抛 —— 这一环不许把后面的 tick 流程带断。
    """
    p = os.path.join(LIVING, "export_conv.py")
    if not os.path.exists(p):
        _note(who, "export_conv.py 不存在：%s" % p)
        return False
    try:
        r = subprocess.run([PY, p, "--days", "2"], capture_output=True,
                           text=True, errors="replace", timeout=90)
    except subprocess.TimeoutExpired:
        _note(who, "export_conv timeout>90s")
        return False
    except (OSError, subprocess.SubprocessError) as e:
        _note(who, "export_conv 起不来: %s" % str(e)[:200])
        return False
    if r.returncode != 0:
        _note(who, "export_conv rc=%d stderr=%s"
              % (r.returncode, (r.stderr or "").strip()[:200]))
        return False
    # 部分库不可读时导出仍算成功（exit 0），警告只在 stderr；capture_output 会把
    # 它吞掉，所以这里落一行 —— 不然「三只库掉了一只」还是无声。
    warn = (r.stderr or "").strip()
    if warn:
        _note(who, "export_conv stderr=%s" % warn.replace("\n", " | ")[:200])


def _finish(who, st, result, gap, do_arm, line=None):
    """收尾：落地、写日志、排下一枪。

    三条出口（额度用完 / 有活做完 / 没活）都走这里 —— 以前各写一遍，
    改一处就得记住另外两处。
    """
    st["next_in"] = gap
    save_last(who, st)
    log(who, line or f"tick gap={gap}s")
    result["gap"] = gap
    if do_arm:
        ok, unit = arm(who, gap)
        result["armed"] = {"ok": ok, "unit": unit, "after": gap}
    else:
        result["armed"] = None
    return result


def tick(who, do_arm=True):
    c = cfg(who)
    os.makedirs(state_dir(who), exist_ok=True)
    _refresh_conversation(who)
    st = load_last(who)
    day = time.strftime("%Y-%m-%d")
    if st.get("day") != day:
        st["wakes_today"] = 0
        st["day"] = day

    # 轻醒免费：看状态不花钱，不受额度限制。额度管的是「要不要花钱叫模型」。
    # last_tick 传上一轮的水位（下面 313 行才推进这个值），wake_signals 靠它判断
    # 「她说完话之后我醒过没」。注意 clock 在额度判断之前就推水位，所以 hook
    # 自己的「已为这句醒过」记号不能只靠 last_tick——二号的账本口径就是这个原因。
    busy, ev = light_scan(who, st.get("last_tick"))
    st["last_tick"] = now()
    result = {"who": who, "ok": True, "light": ev, "busy": busy,
              "wakes_today": st["wakes_today"]}

    if busy:
        if st["wakes_today"] >= c["limit_per_day"]:
            # 有活但审批额度用完了：不叫模型，把活记下来等人批
            st["note"] = f"有活待批：额度 {c['limit_per_day']}/天 已用完"
            result.update(judge=None, acted=None, reason="quota", pending=True)
            return _finish(
                who, st, result, pick_gap(who, False, st), do_arm,
                line=f"有活但额度用完 {st['wakes_today']}/{c['limit_per_day']}")
        st["wakes_today"] += 1        # 这一笔是「花了一次模型钱」
        sys.path.insert(0, LIVING)
        import runner
        overdue_max = max([float(d.get("overdue_h") or 0)
                           for d in ev.get("due_items", [])] or [0])
        has_due = ev.get("due", 0) > 0
        force_th = float(c.get("force_overdue_h", 36))
        forced = has_due or overdue_max >= force_th
        if forced:
            do_it = True
            why = (("日程到期（最多欠 %.1fh），直接做" % overdue_max) if has_due
                   else ("欠账 %.1fh ≥ %.0fh，不看判断直接做" % (overdue_max, force_th)))
            result["judge"] = {"ok": True, "do_it": True, "forced": True, "verdict": why}
            st["note"] = why
        else:
            judge = ("你刚自己醒来，轻醒扫到这些信号："
                     + json.dumps(ev, ensure_ascii=False)
                     + "。\n字段说明：due_items[].overdue_h 是这件事已经欠了多少小时（正数就是欠账，"
                       "24.0 就是整整欠了一天）。\n"
                       "第一行只写「做」或「不做」，第二行一句话说理由。不要调工具，不要展开。")
            j = runner.ask(who, judge, verbose=False, tools=False, max_turns=1)
            verdict = (j.get("answer") or "").strip()
            first = verdict.splitlines()[0].strip() if verdict else ""
            do_it = first.startswith("做") or first.lower().startswith("yes")
            result["judge"] = {"ok": j.get("ok"), "verdict": first[:120],
                               "do_it": do_it, "error": j.get("detail")}
            st["note"] = verdict[:200] or "判断阶段没答出来"

        if do_it:
            hint = ""
            if ev.get("due_items"):
                d0 = ev["due_items"][0]
                # 不让模型自己改 schedule.json：那文件有两个写者（还有二号的
                # words_board），模型读-改-写一轮几十秒，中间别人写进去就被盖了。
                # 走这个工具，它带 CAS，文件变过就放弃。
                hint = ("\n这次只做一件：{}（已经欠了 {} 小时）。".format(
                            d0.get("name"), d0.get("overdue_h"))
                        + "做完这一件就停，剩下的留给下次醒，不要一次把三件都干了。\n"
                        + "真做完了就跑这一句（别自己改 json，那文件别人也在写）：\n"
                        + "  python3 ~/living/tools/schedule_mark.py --who " + who
                        + " --name \"" + str(d0.get("name")) + "\"\n"
                        + "没做成、或者只做了一半的，不要跑这句。")
            r = runner.ask(who,
                           "（你刚判断该动手，信号：" + json.dumps(ev, ensure_ascii=False)
                           + "）现在去做，做完说一句结论。\n" + hint + "\n"
                           "如果看完发现这件事其实不用动手（只是占位、或已经做过了），"
                           "直接回答「无需动手」就结束，不要反复查证。\n"
                           "注意：~/living/conversation/ 当天那份可能有四五百条、五十万字，"
                           "不要整篇读，用 grep 或 python 按关键词筛。", verbose=False, max_turns=8)
            result["acted"] = {"ok": r.get("ok"),
                               "answer": (r.get("answer") or "")[:400],
                               "error": r.get("detail")}
            gap = pick_gap(who, True, st)
        else:
            # 判断不做 → 退远一点，别 15 分钟后又问一遍同一件事
            result["acted"] = None
            gap = pick_gap(who, False, st)
    else:
        result["judge"] = None
        result["acted"] = None
        st["note"] = "轻醒：没事"
        gap = pick_gap(who, False, st)

    return _finish(
        who, st, result, gap, do_arm,
        line=(f"tick busy={busy} wakes={st['wakes_today']}/{c['limit_per_day']} "
              f"gap={gap}s judge={bool(result.get('judge'))} "
              f"acted={bool(result.get('acted'))} ev={json.dumps(ev, ensure_ascii=False)}"))


def status(who):
    c = cfg(who)
    st = load_last(who)
    age = now() - (st.get("last_tick") or 0)
    pending = int(_sh("systemctl --user list-timers --all --no-legend 2>/dev/null | "
                      f"grep -c 'live-tick-{who}-'") or 0)
    return {
        "who": who,
        "last_tick_age_h": round(age / 3600, 2) if st.get("last_tick") else None,
        "stale": bool(st.get("last_tick")) and age > c["stale_hours"] * 3600,
        "wakes_today": st.get("wakes_today", 0),
        "limit_per_day": c["limit_per_day"],
        "next_in_s": st.get("next_in"),
        "pending_timers": pending,
        "note": st.get("note", ""),
        "todo_file": os.path.exists(todo_path(who)),
    }


def stop(who):
    # list-timers 列：NEXT LEFT LAST PASSED UNIT ACTIVATES
    # $NF 是 service，$(NF-1) 才是 timer —— 停 timer 才叫真断链
    out = _sh("systemctl --user list-timers --all --no-legend | "
              f"awk '{{print $(NF-1)}}' | grep 'live-tick-{who}-'")
    killed = []
    for u in out.splitlines():
        u = u.strip()
        if u:
            _sh(f"systemctl --user stop {u} 2>/dev/null")
            _sh(f"systemctl --user reset-failed {u} 2>/dev/null")
            killed.append(u)
    log(who, f"stop: {len(killed)} 个 timer")
    return {"who": who, "stopped": killed}


def watchdog():
    """保险：一次看所有接了钟的，断了就补一枪。

    只认「有 last.json 的」——没接过钟的不归我操心，
    不然会替还没表态的那只强行上电。
    """
    whos = [w for w in discover_who() if os.path.exists(last_path(w))]
    out = {}
    for who in whos:
        c = cfg(who)
        s = status(who)
        pending, stale, age_h = s["pending_timers"], s["stale"], s["last_tick_age_h"]
        if pending > 0 and not stale:
            log(who, f"watchdog: 活着（pending={pending} age={age_h}h），不动")
            out[who] = {"action": "none", "pending": pending, "age_h": age_h}
            continue
        why = []
        if pending == 0:
            why.append("没有待发的枪")
        if stale:
            why.append(f"太久没醒 {age_h}h")
        ok, unit = arm(who, c["min_gap"])
        log(who, f"watchdog: 断链（{'/'.join(why)}）→ 补一枪 {unit} ok={ok}")
        out[who] = {"action": "rearm", "why": why, "unit": unit, "ok": ok}
    if not whos:
        out["_"] = {"action": "none", "why": "还没有谁接过钟"}
    return out


def main(argv):
    who = "example"
    if "--who" in argv:
        i = argv.index("--who")
        if i + 1 < len(argv):
            who = argv[i + 1]
    if who not in discover_who():
        print(json.dumps({"ok": False, "reason": "unknown_who", "who": who},
                         ensure_ascii=False))
        return 2

    if "--status" in argv:
        print(json.dumps(status(who), ensure_ascii=False, indent=2))
        return 0
    if "--stop" in argv:
        print(json.dumps(stop(who), ensure_ascii=False, indent=2))
        return 0
    if "--watchdog" in argv:
        print(json.dumps(watchdog(), ensure_ascii=False, indent=2))
        return 0
    if "--tick" in argv:
        r = tick(who, do_arm="--no-arm" not in argv)
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0
    print("用法: clock.py [--who example|alpha|beta] --tick [--no-arm] | --status | --stop | --watchdog")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
