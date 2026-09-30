"""Insert (or replace) the agent-lab blocks in a LiteLLM config read from stdin; write to stdout.

Text-level edit (no YAML round-trip) so comments and formatting of the existing config are
preserved byte-for-byte. The existing api_key line is reused verbatim so its value is never printed.
"""
import re
import sys

models_tpl = open(sys.argv[1]).read()
router_tpl = open(sys.argv[2]).read()
cfg = sys.stdin.read()

# Drop any previous agent-lab blocks so reruns converge on the current templates.
cfg = re.sub(r"\n?[ \t]*# >>> agent-lab models.*?# <<< agent-lab models <<<\n", "\n", cfg, flags=re.S)
cfg = re.sub(r"\n*# >>> agent-lab router settings.*?# <<< agent-lab router settings <<<\n", "\n", cfg, flags=re.S)

if re.search(r"^router_settings:", cfg, re.M):
    sys.exit("config already has its own router_settings; merge the agent-lab router block by hand")

key_line = re.search(r"^(\s+api_key:.*)$", cfg, re.M)
models = models_tpl.replace("__API_KEY_LINE__\n", key_line.group(1) + "\n" if key_line else "")

m = re.search(r"^litellm_settings:", cfg, re.M)
if not m:
    sys.exit("could not find litellm_settings: section to insert before")
head = cfg[: m.start()].rstrip("\n") + "\n"
out = head + models + "\n" + cfg[m.start():]
sys.stdout.write(out.rstrip("\n") + "\n" + router_tpl)
