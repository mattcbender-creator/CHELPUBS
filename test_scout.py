"""Run with plain python, no pytest: python test_scout.py"""
import scout

pack = scout.build_pack("12521", "22423")
assert set(pack["clubs"]) == {"12521", "22423"}
assert pack["players"], "no players built from careers"
for cid, c in pack["clubs"].items():
    assert c["games_seen"] > 0, f"no games harvested for {cid}"
    assert c["default"].get("LW") in pack["players"], f"bad default lineup for {cid}"
    assert all(n in pack["players"] for n in c["pool"])
w = pack["players"].get("Williamson20")
assert w and w["axes"] and 0 <= min(w["axes"].values()) <= max(w["axes"].values()) <= 100

html = scout.render("12521", "22423", "T")
assert "__DATA__" not in html and "__TITLE__" not in html
assert "<title>T</title>" in html
print("ok  test_scout")
