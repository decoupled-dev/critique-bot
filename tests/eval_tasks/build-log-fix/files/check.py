"""Pass when the misspelled field is fixed and nothing else about the helper broke."""

import re
import sys

text = open("frameworks/car/src/HvacTemperatureHelper.java", encoding="utf-8").read()
problems = []
if "mCarPropertyManger" in text:
    problems.append("the misspelled field mCarPropertyManger is still used")
if not re.search(r"private final CarPropertyManager mCarPropertyManager;", text):
    problems.append("the field declaration changed")
if text.count("{") != text.count("}"):
    problems.append("braces no longer balance")
if "public float getDriverSetpoint()" not in text or "public void setDriverSetpoint(float celsius)" not in text:
    problems.append("a public method was removed or renamed")
print("\n".join(problems) or "ok")
sys.exit(1 if problems else 0)
