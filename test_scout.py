"""Run with plain python, no pytest: python test_scout.py"""
import ea
import scout

# Discord mobile hands back an autocomplete choice's label instead of its
# value, which broke /pubscout for a real user who picked off the picker.
assert ea.strip_label("McC x 45 — G, 131 GP") == "McC x 45"
assert ea.strip_label("Williamson20 — LW, 57 GP") == "Williamson20"
assert ea.strip_label("Someone — G, 1,204 GP") == "Someone"   # thousands separator
# a gamertag is never mangled just because it contains a dash
for _t in ("McC x 45", "x-Nasty93-_", "Some — Guy", "Tag - C, 5 GP extra"):
    assert ea.strip_label(_t) == _t, _t

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
