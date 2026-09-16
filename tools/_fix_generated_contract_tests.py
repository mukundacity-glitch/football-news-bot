from pathlib import Path

paths = [
    Path("tests/test_fotmob_contract_extensions.py"),
    Path("tests/test_verification_v2.py"),
]
for path in paths:
    text = path.read_text(encoding="utf-8")
    text = text.replace("assert not mandatory.passed", "assert mandatory.state.value != \"PASS\"")
    text = text.replace("assert not temporal.passed", "assert temporal.state.value != \"PASS\"")
    path.write_text(text, encoding="utf-8")
print("Corrected generated GateResult assertions")
