#!/usr/bin/env python3
"""技能 · 搬过来之后要能找得到

    python3 ~/living/skills_index.py --build           # 重建索引
    python3 ~/living/skills_index.py --search "量化"    # 找技能
    python3 ~/living/skills_index.py --read hermes-agent  # 读全文

两百多条 description 全塞进上下文是浪费，所以只留索引，按需取全文。
搜索先匹配名字和描述（快），匹配不到再全文 grep（慢但全）。
"""
import json
import os
import re
import subprocess
import sys

LIVING = os.path.expanduser(os.environ.get("LIVING_HOME") or "~/living")
SKILLS = os.path.join(LIVING, "skills")
CATALOG = os.path.join(LIVING, "skills", "catalog.json")

_cache = None


def frontmatter(text):
    """取 --- 之间的 name / description，容忍引号和折行。"""
    m = re.match(r"^---\n(.*?)\n---", text, re.S)
    if not m:
        return {}
    out = {}
    for line in m.group(1).splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        k = k.strip()
        if k in ("name", "description"):
            out[k] = v.strip().strip('"').strip("'")
    return out


def build():
    rows = []
    for dirpath, _dirnames, filenames in os.walk(SKILLS):
        if "SKILL.md" not in filenames:
            continue
        p = os.path.join(dirpath, "SKILL.md")
        try:
            with open(p, encoding="utf8", errors="ignore") as f:
                text = f.read()
        except OSError:
            continue
        fm = frontmatter(text)
        rel = os.path.relpath(p, SKILLS)
        name = fm.get("name") or os.path.basename(dirpath)
        rows.append({
            "name": name,
            "desc": fm.get("description", ""),
            "path": rel,
            "bytes": len(text.encode("utf8")),
            "lines": text.count("\n") + 1,
        })
    rows.sort(key=lambda r: r["name"])
    os.makedirs(SKILLS, exist_ok=True)
    with open(CATALOG, "w", encoding="utf8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=1)

    # 人读的目录
    md = ["# 技能索引", ""]
    for r in rows:
        md.append(f"- **{r['name']}** — {r['desc'][:90]}")
    with open(os.path.join(SKILLS, "INDEX.md"), "w", encoding="utf8") as f:
        f.write("\n".join(md) + "\n")

    return {"ok": True, "skills": len(rows),
            "total_bytes": sum(r["bytes"] for r in rows),
            "with_desc": sum(1 for r in rows if r["desc"]),
            "catalog": CATALOG}


def load():
    global _cache
    if _cache is not None:
        return _cache
    try:
        with open(CATALOG, encoding="utf8") as f:
            _cache = json.load(f)
    except (OSError, json.JSONDecodeError):
        _cache = []
    return _cache


def grams(s):
    s = (s or "").lower()
    out = set(re.findall(r"[a-z0-9_]{2,}", s))
    zh = re.sub(r"[^\u4e00-\u9fff]", "", s)
    for i in range(len(zh) - 1):
        out.add(zh[i:i + 2])
    return out


def search(q, limit=6):
    qg = grams(q)
    if not qg:
        return {"ok": False, "reason": "empty_query",
                "note": "要给 query（搜技能）或 name（读全文）",
                "hits": []}
    scored = []
    for r in load():
        hg = grams(r["name"].replace("-", " ") + " " + r["desc"])
        hit = qg & hg
        if hit:
            scored.append((len(hit) / len(qg), r))
    scored.sort(key=lambda x: -x[0])
    hits = [{"name": r["name"], "score": round(s, 2),
             "desc": r["desc"][:160], "lines": r["lines"]} for s, r in scored[:limit]]
    return {"ok": True, "query": q, "total": len(load()), "hits": hits}


def read(name, full=True, offset=0, limit=0):
    """读技能全文。太大就分段：不给 offset/limit 时返回目录 + 开头一段。"""
    for r in load():
        if r["name"] == name:
            p = os.path.join(SKILLS, r["path"])
            try:
                with open(p, encoding="utf8", errors="ignore") as f:
                    text = f.read()
            except OSError as e:
                return {"ok": False, "reason": "unreadable", "detail": str(e)}
            lines = text.splitlines()
            total = len(lines)

            if limit and limit > 0:
                seg = lines[offset:offset + limit]
                return {"ok": True, "name": name, "total_lines": total,
                        "offset": offset, "returned": len(seg),
                        "body": "\n".join(seg),
                        "dir": os.path.dirname(p)}

            if full and total <= 150:
                return {"ok": True, "name": name, "total_lines": total,
                        "body": text, "dir": os.path.dirname(p)}

            # 大文件：给目录（## / ### 标题）+ 开头 80 行，让它自己挑段
            toc = [f"{i + 1}: {l}" for i, l in enumerate(lines)
                   if re.match(r"^#{2,3} ", l)]
            head = "\n".join(lines[:80])
            return {"ok": True, "name": name, "total_lines": total,
                    "truncated": True,
                    "note": (f"这个技能 {total} 行，太长没全给。"
                             f"看下面的目录，再用 offset/limit 读你要的那段"
                             f"（offset 从 0 数）。"),
                    "toc": toc[:60],
                    "body": head,
                    "dir": os.path.dirname(p)}
    s = search(name, 3)
    return {"ok": False, "reason": "not_found", "suggest": s.get("hits", [])}


def list_refs(name):
    """技能目录下有哪些附件（references/scripts/templates）。"""
    for r in load():
        if r["name"] == name:
            d = os.path.dirname(os.path.join(SKILLS, r["path"]))
            out = []
            for sub in ("references", "scripts", "templates", "assets"):
                p = os.path.join(d, sub)
                if os.path.isdir(p):
                    for fn in sorted(os.listdir(p)):
                        fp = os.path.join(p, fn)
                        if os.path.isfile(fp):
                            out.append({"file": f"{sub}/{fn}",
                                        "bytes": os.path.getsize(fp)})
            return {"ok": True, "name": name, "files": out}
    return {"ok": False, "reason": "not_found"}


def main(argv):
    if "--build" in argv:
        print(json.dumps(build(), ensure_ascii=False, indent=2))
        return 0
    if "--search" in argv:
        i = argv.index("--search")
        q = argv[i + 1] if i + 1 < len(argv) else ""
        lim = int(argv[argv.index("--limit") + 1]) if "--limit" in argv else 6
        print(json.dumps(search(q, lim), ensure_ascii=False, indent=2))
        return 0
    if "--read" in argv:
        i = argv.index("--read")
        n = argv[i + 1] if i + 1 < len(argv) else ""
        print(json.dumps(read(n), ensure_ascii=False, indent=2))
        return 0
    if "--refs" in argv:
        i = argv.index("--refs")
        print(json.dumps(list_refs(argv[i + 1] if i + 1 < len(argv) else ""),
                         ensure_ascii=False, indent=2))
        return 0
    print("用法: skills_index.py --build | --search \"词\" | --read <name> | --refs <name>")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
