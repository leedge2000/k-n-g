import sqlite3

c = sqlite3.connect("data/grain_oil.db")
c.row_factory = sqlite3.Row
print("--commodity--")
for r in c.execute("select commodityId, commodityCode, commodityName, commodityCategory from CmdCommodity"):
    print(dict(r))
print("--indicator--")
for r in c.execute("select indicatorId, indicatorCode, indicatorName, unit from Indicator limit 40"):
    print(dict(r))
print("--yrb rows--")
for r in c.execute("select * from YrbYearbook limit 6"):
    print(dict(r))
print("--crs source--")
for r in c.execute("select crawlSourceId, sourceName, sourceUrl, extractorType from CrsSource"):
    print(dict(r))
