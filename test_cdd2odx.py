"""Round-trip check: every message cdd_utils encodes must decode and re-encode byte-identically
from the written-and-reloaded PDX.  Usage: python test_cdd2odx.py INPUT.cdd"""
import sys
import tempfile

import odxtools

from cdd2odx import Converter, load


def check(cdd_path: str) -> list[str]:
    db = load(cdd_path)
    conv = Converter(db)
    with tempfile.NamedTemporaryFile(suffix=".pdx") as tmp:
        odxtools.write_pdx_file(tmp.name, conv.convert())
        layer = odxtools.load_pdx_file(tmp.name).diag_layers[0]

    by_name = {s.short_name: s for s in layer.services}  # NamedItemList mangles keys starting with a digit
    failures = []
    for _, svc in conv._used_services():
        odx_svc = by_name[conv.service_names[svc.qualifier]]
        pairs = [("RQ", svc.request, odx_svc.request),
                 ("PR", svc.positive_responses, odx_svc.positive_responses[0]),
                 ("NR", svc.negative_responses, odx_svc.negative_responses[0])]
        for kind, cdd_msg, odx_msg in pairs:
            raw = cdd_msg.encode_default()
            try:
                again = odx_msg.encode(**odx_msg.decode(raw))
                if again != raw:
                    failures.append(f"{svc.qualifier} {kind}: {raw.hex()} -> {again.hex()}")
            except Exception as exc:
                failures.append(f"{svc.qualifier} {kind}: {raw.hex()} {type(exc).__name__}: {exc}")
    return failures


if __name__ == "__main__":
    failures = check(sys.argv[1])
    print("\n".join(failures[:40]))
    print(f"{len(failures)} failures")
    sys.exit(1 if failures else 0)
