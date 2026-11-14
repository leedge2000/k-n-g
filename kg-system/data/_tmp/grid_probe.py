import json
import os
import sys


def load(p):
    with open(p, encoding="utf-8-sig") as f:
        return json.load(f)


def rows(d, ytol=12):
    items = []
    for line in d["lines"]:
        for w in line["words"]:
            items.append(w)
    items.sort(key=lambda w: (w["y"] + w["h"] / 2))
    out = []
    for w in items:
        cy = w["y"] + w["h"] / 2
        if out and abs(cy - out[-1]["cy"]) <= ytol:
            out[-1]["words"].append(w)
            out[-1]["cy"] = (out[-1]["cy"] + cy) / 2
        else:
            out.append({"cy": cy, "words": [w]})
    for r in out:
        r["words"].sort(key=lambda w: w["x"])
        r["text"] = " ".join(w["text"] for w in r["words"])
    return out


if __name__ == "__main__":
    for p in sys.argv[1:]:
        d = load(p)
        rs = rows(d)
        print(f"=== {os.path.basename(p)} lines={d['lineCount']} rows={len(rs)}")
        for r in rs[:25]:
            print(f"y={r['cy']:7.1f} | {r['text']}")
