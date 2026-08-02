"""Re-run the harness verification logic against already-saved Arm1 transcripts, without
re-invoking the model. Used to validate the verifier fix cheaply before trusting the matrix.
"""
import glob
import json
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "agent"))
from counterfactual_rev import verify_arm1_citations, check_frozen_bar

pattern = sys.argv[1] if len(sys.argv) > 1 else "arm1_F2"
files = sorted(glob.glob(os.path.join(os.path.dirname(__file__), "..", "..", "logs", f"cfrev_{pattern}_run*.json")))

for path in files:
    with open(path) as f:
        d = json.load(f)
    verified_ok, reason = verify_arm1_citations(d["reply"], d["calls"])
    check = check_frozen_bar(d["reply"], "model", verified_ok, reason)
    d["check"] = check
    with open(path, "w") as f:
        json.dump(d, f, indent=2, default=str)
    print(f"{os.path.basename(path)}: clean_pass={check['clean_pass']}  reason={reason}")
