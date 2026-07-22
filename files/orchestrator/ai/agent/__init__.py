"""
agent — the code-resident agent layer: the contract engine, the field classes, the
forge authoring stack, and the built-in doorman.grgn template.

AGENT_DIR is the directory holding the code-resident agent templates (doorman.grgn,
forge_fields.json). Callers use it as the "code fallback" dir for bundle resolution
and migration, so it stays correct even as contract/ and forge/ become sub-packages
(``dirname(forge.__file__)`` would not).
"""

import os

AGENT_DIR = os.path.dirname(os.path.abspath(__file__))
