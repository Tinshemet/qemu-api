import json
import os

_here = os.path.dirname(__file__)

with open(os.path.join(_here, "tools.json")) as _f:
    TOOLS = json.load(_f)
