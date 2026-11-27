#!/usr/bin/env python3
"""记忆 · 从老宅搬出来，新家能自己查

    python3 ~/living/memory.py --export        # 从 hermes 记忆库导出（只读，不动原件）
    python3 ~/living/memory.py --search "关键词"  # 查
    python3 ~/living/memory.py --stats         # 有多少条

搬的是「新家能独立用」，不是「文件复制一份」：
导出成 jsonl，检索自己实现，不 import hermes、不连 sqlite。
导出之后老宅倒了也不影响。

检索用字符二元组，中文不用分词也能命中；
排序看命中比例，再让 trust 和 retrieval_count 微调——
常用的、可信的往前放，但不让它们压过「真的相关」。
"""
import datetime
import glob
import json
import os
import re
import sqlite3
import sys

LIVING = os.path.expanduser(os.environ.get("LIVING_HOME") or "~/living")
MEM = os.path.join(LIVING, "memory")
FACT_DIR = os.path.join(MEM, "facts")
# 老路径：所有 who 共用一份，2026-11-26 前的实现。留着只读兼容，不再写入。
LEGACY_FACTS = os.path.join(MEM, "facts.jsonl")
# 每个实例一个库，自动扫 personas/<who>/memory_store.db，不写死名字。
SRC = {
    os.path.basename(os.path.dirname(p)): p
    for p in sorted(glob.glob(os.path.expanduser(
        os.path.join(LIVING, "personas", "*", "memory_store.db"))))
}

WHO_DEFAULT = "example"
_cache = {}


def facts_path(who=None):
    """一人一份。不传 who 就用默认那只（兼容旧调用）。

    以前是一份公共的 facts.jsonl，所有实例都读它——等于把一个人的私事
    摊在公共路径上。2026-11-19 定的分层里，这类内容只本人可读。
    """
    return os.path.join(FACT_DIR, (who or WHO_DEFAULT) + ".jsonl")


def grams(s):
    """英文按词、中文按二元组。中文不分词也能命中。"""
    s = (s or "").lower()
    out = set(re.findall(r"[a-z0-9_]{2,}", s))
    zh = re.sub(r"[^\u4e00-\u9fff]", "", s)
    for i in range(len(zh) - 1):
        out.add(zh[i:i + 2])
    if len(zh) == 1:
        out.add(zh)
    return out


def export(who=None):
    who = who or WHO_DEFAULT
    src = SRC.get(who)
    if not src or not os.path.exists(src):
        return {"ok": False, "reason": "no_source", "src": src}
    c = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    ents = {}
    try:
        for fid, name in c.execute(
                "select fe.fact_id, e.name from fact_entities fe "
                "join entities e on e.entity_id = fe.entity_id"):
            ents.setdefault(fid, []).append(name)
    except Exception:  # noqa: BLE001
        pass

    rows = []
    for r in c.execute("select fact_id, content, category, tags, trust_score, "
                       "retrieval_count, created_at from facts"):
        fid, content, cat, tags, trust, rc, created = r
        if not content:
            continue
        rows.append({"id": fid, "content": content, "cat": cat or "",
                     "tags": tags or "", "trust": trust,
                     "seen": rc or 0, "created": str(created or ""),
                     "ents": ents.get(fid, [])})
    c.close()

    path = facts_path(who)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, path)
    os.chmod(path, 0o600)
    _cache.pop(who or WHO_DEFAULT, None)
    return {"ok": True, "who": who, "facts": len(rows), "path": path,
            "with_entities": sum(1 for r in rows if r["ents"]),
            "bytes": os.path.getsize(path)}


def load(who=None):
    key = who or WHO_DEFAULT
    try:
        stamp = os.stat(facts_path(key)).st_mtime_ns
    except OSError:
        stamp = None
    hit = _cache.get(key)
    if hit and hit[0] == stamp:      # 文件没动过才复用，导出新事实后立刻能查到
        return hit[1]
    out = []
    try:
        with open(facts_path(key), encoding="utf8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
    except FileNotFoundError:
        pass
    _cache[key] = (stamp, out)
    return out


def add(content, cat="general", tags="", ents=None, who=None):
    """追加一条事实到自己的库。只增不改。

    这是 recall 的写入端：以前只有 export/search/stats，等于库是只读的，
    11-27 之后每轮 remember 进来的东西都进不了 search。
    """
    content = (content or "").strip()
    if not content:
        return {"ok": False, "reason": "empty"}
    key = who or WHO_DEFAULT
    path = facts_path(key)
    cur = load(key)
    fid = (max((f.get("id") or 0) for f in cur) + 1) if cur else 1
    now = datetime.datetime.now(datetime.timezone.utc)
    row = {
        "id": fid,
        "content": content,
        "cat": cat or "general",
        "tags": tags or "",
        "trust": 0.5,
        "seen": 0,
        "created": now.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
        "ents": ents or [],
    }
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, (json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8"))
    finally:
        os.close(fd)
    _cache.pop(key, None)
    return {"ok": True, "id": fid, "path": path}


def search(q, limit=8, who=None):
    qg = grams(q)
    if not qg:
        return {"ok": False, "reason": "empty_query", "hits": []}
    pool = load(who)
    scored = []
    for r in pool:
        hay = r["content"] + " " + r.get("tags", "") + " " + " ".join(r.get("ents", []))
        hg = grams(hay)
        hit = qg & hg
        if not hit:
            continue
        base = len(hit) / len(qg)
        # trust 和 seen 只做微调，不压过相关性
        bonus = 0.06 * float(r.get("trust") or 0) + 0.004 * min(r.get("seen", 0), 20)
        scored.append((base + bonus, base, r))
    scored.sort(key=lambda x: -x[0])
    hits = [{"id": r["id"], "score": round(s, 3), "match": round(b, 2),
             "content": r["content"][:300], "tags": r.get("tags", "")}
            for s, b, r in scored[:limit]]
    return {"ok": True, "query": q, "who": who or WHO_DEFAULT,
            "total": len(pool), "hits": hits}


def stats(who=None):
    d = load(who)
    path = facts_path(who)
    by_cat = {}
    for r in d:
        by_cat[r.get("cat") or "?"] = by_cat.get(r.get("cat") or "?", 0) + 1
    return {"ok": True, "who": who or WHO_DEFAULT, "facts": len(d),
            "bytes": os.path.getsize(path) if os.path.exists(path) else 0,
            "by_category": by_cat,
            "exported": os.path.exists(path)}


def _who(arg):
    return arg[arg.index("--who") + 1] if "--who" in arg else WHO_DEFAULT


def main(argv):
    if "--export" in argv:
        print(json.dumps(export(_who(argv)), ensure_ascii=False, indent=2))
        return 0
    if "--stats" in argv:
        print(json.dumps(stats(_who(argv)), ensure_ascii=False, indent=2))
        return 0
    if "--search" in argv:
        i = argv.index("--search")
        q = argv[i + 1] if i + 1 < len(argv) else ""
        lim = 8
        if "--limit" in argv:
            lim = int(argv[argv.index("--limit") + 1])
        r = search(q, lim, _who(argv))
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0
    if "--add" in argv:
        i = argv.index("--add")
        text = argv[i + 1] if i + 1 < len(argv) else ""
        cat = argv[argv.index("--cat") + 1] if "--cat" in argv else "general"
        print(json.dumps(add(text, cat=cat, who=_who(argv)), ensure_ascii=False))
        return 0
    print("用法: memory.py --export [--who example] | --search \"词\" [--limit 8] [--who X] | --add \"事\" [--cat C] [--who X] | --stats [--who X]")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
