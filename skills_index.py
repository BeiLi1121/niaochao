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
import sys

LIVING = os.path.expanduser(os.environ.get("LIVING_HOME") or "~/living")
SKILLS = os.path.join(LIVING, "skills")
CATALOG = os.path.join(SKILLS, "catalog.json")
SUB_DIRS = ("references", "scripts", "templates", "assets")

_catalog = None
_byname = None


# ---------- 索引 ----------

def frontmatter(text):
    """取 --- 之间的 name / description，容忍引号和折行。"""
    m = re.match(r"^---\n(.*?)\n---", text, re.S)
    if not m:
        return {}
    out = {}
    for line in m.group(1).splitlines():
        k, sep, v = line.partition(":")
        if not sep or k.strip() not in ("name", "description"):
            continue
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _skill_rows():
    """走一遍 skills/，每个 SKILL.md 一条。"""
    for dirpath, _dirnames, filenames in os.walk(SKILLS):
        if "SKILL.md" not in filenames:
            continue
        p = os.path.join(dirpath, "SKILL.md")
        try:
            text = open(p, encoding="utf8", errors="ignore").read()
        except OSError:
            continue
        fm = frontmatter(text)
        yield {"name": fm.get("name") or os.path.basename(dirpath),
               "desc": fm.get("description", ""),
               "path": os.path.relpath(p, SKILLS),
               "bytes": len(text.encode("utf8")),
               "lines": text.count("\n") + 1}


def build():
    rows = sorted(_skill_rows(), key=lambda r: r["name"])
    os.makedirs(SKILLS, exist_ok=True)
    with open(CATALOG, "w", encoding="utf8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=1)

    md = ["# 技能索引", ""] + [f"- **{r['name']}** — {r['desc'][:90]}" for r in rows]
    with open(os.path.join(SKILLS, "INDEX.md"), "w", encoding="utf8") as f:
        f.write("\n".join(md) + "\n")

    return {"ok": True, "skills": len(rows),
            "total_bytes": sum(r["bytes"] for r in rows),
            "with_desc": sum(1 for r in rows if r["desc"]),
            "catalog": CATALOG}


def load():
    global _catalog
    if _catalog is None:
        try:
            with open(CATALOG, encoding="utf8") as f:
                _catalog = json.load(f)
        except (OSError, json.JSONDecodeError):
            _catalog = []
    return _catalog


def entry(name):
    """名字 → 索引条目。建一次表，别每个调用都扫 228 条。"""
    global _byname
    if _byname is None:
        _byname = {r["name"]: r for r in load()}
    return _byname.get(name)


# ---------- 搜 ----------

def grams(s):
    s = (s or "").lower()
    out = set(re.findall(r"[a-z0-9_]{2,}", s))
    zh = re.sub(r"[^\u4e00-\u9fff]", "", s)
    out.update(zh[i:i + 2] for i in range(len(zh) - 1))
    return out


def _empty_query():
    return {"ok": False, "reason": "empty_query",
            "note": "要给 query（搜技能）或 name（读全文）", "hits": []}


def search(q, limit=6):
    qg = grams(q)
    if not qg:
        return _empty_query()
    scored = []
    for r in load():
        hit = qg & grams(r["name"].replace("-", " ") + " " + r["desc"])
        if hit:
            scored.append((len(hit) / len(qg), r))
    scored.sort(key=lambda x: -x[0])
    hits = [{"name": r["name"], "score": round(s, 2),
             "desc": r["desc"][:160], "lines": r["lines"]} for s, r in scored[:limit]]
    return {"ok": True, "query": q, "total": len(load()), "hits": hits}


# ---------- 读 ----------

def _seg(name, p, lines, offset, limit):
    seg = lines[offset:offset + limit]
    return {"ok": True, "name": name, "total_lines": len(lines),
            "offset": offset, "returned": len(seg),
            "body": "\n".join(seg), "dir": os.path.dirname(p)}


def _whole(name, p, text, total):
    return {"ok": True, "name": name, "total_lines": total,
            "body": text, "dir": os.path.dirname(p)}


def _head(name, p, lines):
    """大文件：给目录 + 开头 80 行，让人自己挑段。"""
    toc = [f"{i + 1}: {l}" for i, l in enumerate(lines) if re.match(r"^#{2,3} ", l)]
    return {"ok": True, "name": name, "total_lines": len(lines), "truncated": True,
            "note": (f"这个技能 {len(lines)} 行，太长没全给。"
                     f"看下面的目录，再用 offset/limit 读你要的那段（offset 从 0 数）。"),
            "toc": toc[:60], "body": "\n".join(lines[:80]),
            "dir": os.path.dirname(p)}


def read(name, full=True, offset=0, limit=0):
    """读技能全文。太大就分段：不给 offset/limit 时返回目录 + 开头一段。"""
    r = entry(name)
    if r is None:
        return {"ok": False, "reason": "not_found",
                "suggest": search(name, 3).get("hits", [])}

    p = os.path.join(SKILLS, r["path"])
    try:
        text = open(p, encoding="utf8", errors="ignore").read()
    except OSError as e:
        return {"ok": False, "reason": "unreadable", "detail": str(e)}

    lines = text.splitlines()
    if limit and limit > 0:
        return _seg(name, p, lines, offset, limit)
    if full and len(lines) <= 150:
        return _whole(name, p, text, len(lines))
    return _head(name, p, lines)


def list_refs(name):
    """技能目录下有哪些附件（references/scripts/templates）。"""
    r = entry(name)
    if r is None:
        return {"ok": False, "reason": "not_found"}

    d = os.path.dirname(os.path.join(SKILLS, r["path"]))
    out = []
    for sub in SUB_DIRS:
        p = os.path.join(d, sub)
        if not os.path.isdir(p):
            continue
        out.extend({"file": f"{sub}/{fn}", "bytes": os.path.getsize(os.path.join(p, fn))}
                   for fn in sorted(os.listdir(p))
                   if os.path.isfile(os.path.join(p, fn)))
    return {"ok": True, "name": name, "files": out}


# ---------- 命令行 ----------

def _args(argv):
    """--key value / --key 拆开；不是 -- 开头的按顺序收进 0,1,2…"""
    out, pos, i = {}, 0, 0
    while i < len(argv):
        a = argv[i]
        if not a.startswith("--"):
            out[pos] = a
            pos += 1
            i += 1
            continue
        nxt = argv[i + 1] if i + 1 < len(argv) else None
        if nxt is None or nxt.startswith("--"):
            out[a[2:]] = True
            i += 1
        else:
            out[a[2:]] = nxt
            i += 2
    return out


def _v(a, key):
    """取 --key 后面的值；只写了 --key 没给值就当空串。"""
    v = a.get(key)
    return v if isinstance(v, str) else ""


HANDLERS = {
    "build": lambda a: build(),
    "search": lambda a: search(_v(a, "search"), int(a.get("limit") or 6)),
    "read": lambda a: read(_v(a, "read")),
    "refs": lambda a: list_refs(_v(a, "refs")),
}

USAGE = '用法: skills_index.py --build | --search "词" | --read <name> | --refs <name>'


def main(argv):
    """命令表：加一个子命令只加一行，不用再抄一段 if。"""
    a = _args(argv[1:])
    name = next((k for k in a if isinstance(k, str) and k in HANDLERS), None)
    if name is None:
        print(USAGE)
        return 2
    print(json.dumps(HANDLERS[name](a), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
