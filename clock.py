#!/usr/bin/env python3
"""家 · 各自的钟

一条链，不是定时器：每次醒来自己决定下一次什么时候醒。
三只各用各的，状态、额度、待办、unit 名全部按 who 分开。

    python3 clock.py --who <名字> --tick
    python3 ~/living/clock.py --who zhi --status
    python3 clock.py --who <名字> --stop
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

LIVING = os.environ.get("LIVING_HOME") or os.path.expanduser("~/living")
PY = "/usr/bin/python3"
SELF = os.path.abspath(__file__)
def discover_who():
    """谁家在 personas/ 下，谁就是一只。加一只不用改代码，建个目录就行。"""
    root = os.path.join(LIVING, "personas")
    if not os.path.isdir(root):
        return ()
    return tuple(sorted(d for d in os.listdir(root)
                        if os.path.isdir(os.path.join(root, d))
                        and not d.startswith(("_", "."))))


ALL_WHO = discover_who()

DEFAULTS = {
    "limit_per_day": 12,   # 一天最多醒几次
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


def light_scan(who):
    """轻醒：只用 shell 看，不花钱。返回 (有没有活, 证据)"""
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

    busy = ev["todo"] > 0 or ev["failed_units"] > 0 or ev["due"] > 0
    return busy, ev


def pick_gap(who, busy, last):
    c = cfg(who)
    prev = last.get("next_in") or c["min_gap"]
    if busy:
        return c["min_gap"]
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


def _refresh_conversation():
    """tick 开头把会话导一遍。纯本地、零模型成本，失败不挡后面。"""
    p = os.path.join(LIVING, "export_conv.py")
    if not os.path.exists(p):
        return
    try:
        subprocess.run([PY, p, "--days", "2"], capture_output=True, timeout=90)
    except (OSError, subprocess.SubprocessError):
        pass


def tick(who, do_arm=True):
    c = cfg(who)
    os.makedirs(state_dir(who), exist_ok=True)
    _refresh_conversation()
    st = load_last(who)
    day = time.strftime("%Y-%m-%d")
    if st.get("day") != day:
        st["wakes_today"] = 0
        st["day"] = day

    if st["wakes_today"] >= c["limit_per_day"]:
        log(who, f"额度用完 {st['wakes_today']}/{c['limit_per_day']}")
        st["note"] = f"额度用完（{c['limit_per_day']}/天）"
        save_last(who, st)
        return {"who": who, "ok": True, "acted": False, "reason": "quota",
                "wakes_today": st["wakes_today"]}

    st["wakes_today"] += 1
    busy, ev = light_scan(who)
    st["last_tick"] = now()
    result = {"who": who, "ok": True, "light": ev, "busy": busy,
              "wakes_today": st["wakes_today"]}

    if busy:
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
                sched = os.path.join(state_dir(who), "schedule.json")
                hint = ("\n这次只做一件：{}（已经欠了 {} 小时）。".format(
                            d0.get("name"), d0.get("overdue_h"))
                        + "做完这一件就停，剩下的留给下次醒，不要一次把三件都干了。\n"
                        + "真做完，就把 " + sched + " 里「" + str(d0.get("name"))
                        + "」那项的 last_done 改成本地当前时间（格式 2026-11-26T18:30）。"
                          "没做成或只做了一半的不要改。")
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

    st["next_in"] = gap
    save_last(who, st)
    log(who, f"tick busy={busy} wakes={st['wakes_today']}/{c['limit_per_day']} "
             f"gap={gap}s judge={bool(result.get('judge'))} "
             f"acted={bool(result.get('acted'))} ev={json.dumps(ev, ensure_ascii=False)}")

    if do_arm:
        ok, unit = arm(who, gap)
        result["armed"] = {"ok": ok, "unit": unit, "after": gap}
    else:
        result["armed"] = None
    return result


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
    who = (discover_who() or ("default",))[0]
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
    print("用法: clock.py [--who <名字>] --tick [--no-arm] | --status | --stop | --watchdog")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
