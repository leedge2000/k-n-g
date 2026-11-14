import sqlite3

c = sqlite3.connect("data/grain_oil.db")
c.row_factory = sqlite3.Row
doc = c.execute('select "docId","title","summary","fileFormat" from "SourceDoc" where "docId"=\'SOU0035\'').fetchone()
print(dict(doc))
seg = c.execute('select "segmentText" from "SourceDoc_Segment" where "docId"=\'SOU0035\' limit 1').fetchone()
print(repr(seg["segmentText"][:200]) if seg else None)
print("-- imported rows --")
rows = c.execute(
    'select "statYear","commodityId","value" from "YrbYearbook" '
    'where "caliberNote" like \'%图像识别%\' order by "statYear", "commodityId"'
).fetchall()
print("count", len(rows))
for r in rows:
    print(r["statYear"], r["commodityId"], r["value"])
