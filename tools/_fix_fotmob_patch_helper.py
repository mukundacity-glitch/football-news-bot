from pathlib import Path

path = Path("tools/_apply_fotmob_contract_patch.py")
text = path.read_text(encoding="utf-8")
old = '''    revised = block[:-2].rstrip() + ',\\n    "renewal",\\n})'\n'''
new = '''    prefix = block[:-2].rstrip()\n    if prefix.endswith(","):\n        revised = prefix + '\\n    "renewal",\\n})'\n    else:\n        revised = prefix + ',\\n    "renewal",\\n})'\n'''
if text.count(old) != 1:
    raise RuntimeError(f"expected one helper insertion block, found {text.count(old)}")
path.write_text(text.replace(old, new, 1), encoding="utf-8")
print("Corrected renewal insertion helper")
