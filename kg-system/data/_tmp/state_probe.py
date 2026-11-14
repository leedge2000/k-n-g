import sqlite3

c = sqlite3.connect("data/grain_oil.db")
print("yrb", c.execute("select count(*) from YrbYearbook").fetchone()[0])
print("doc", c.execute("select count(*) from SourceDoc").fetchone()[0])
print("src", c.execute("select sourceUrl, charset from CrsSource where crawlSourceId='CRS0007'").fetchone())
