"""Do the two stacks run the same genropy source? The comparison refuses to
start until they do.

The legacy stack imports a frozen copy of genropy under `temp/legacy_venv`; the
bridge imports the checkout, editable. Nothing keeps the two in step, and a
difference between them reads as a difference between the stacks — which is the
one thing a comparison bench must never confuse. Measured on 2026-08-24: the two
trees already differed in 9 source files, one of them shifting `gnrwsgisite.py`
by six lines, and an uncommitted edit was about to remove the very register
calls the comparison counts. Either would have been read as a bridge divergence.

So this is a REFUSAL, not a warning: it names the differing files, prints the
remedy, and exits non-zero. `replica.py` calls it first at every cycle start.

**What is compared, and what is not.** Only `*.py`, only inside the importable
`gnr` package, and never `__pycache__`. Five top-level subtrees are excluded —
`projects`, `resources`, `webtools`, `dojo_libs`, `gnrjs`: the wheel copies them
into `gnr/` but no runtime reads them there. Both stacks resolve those trees
through `~/.gnr/environment.xml`, which names the checkout, so they are already
shared by construction. With them out, the two sets match file for file (320
against 320, measured 2026-08-25), and any asymmetry left is real drift.

Run: python benchmarks/compare/genropy_parity_check.py
"""

import filecmp
import glob
import os
import sys

import gnr

BENCH_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LEGACY_GLOB = os.path.join(BENCH_ROOT, "temp", "legacy_venv", "lib", "python*",
                           "site-packages", "gnr")

# Copied into the wheel, read from the checkout: see the module docstring.
PACKAGED_ONLY = ("projects", "resources", "webtools", "dojo_libs", "gnrjs")

REMEDY = """Re-freeze the legacy venv from the checkout, per benchmarks/compare/README.md:

  uv pip install --python temp/legacy_venv/bin/python \\
      "$HOME/Sviluppo/Genropy/genropy/gnrpy[pgsql]"

Then run this check again. Until it exits 0, no comparative run may start."""


class GenropyParity:
    """The two genropy source trees, and whether they carry the same code."""

    def __init__(self, legacy_root=None, bridge_root=None):
        self.legacy_root = legacy_root or self.frozen_root
        self.bridge_root = bridge_root or os.path.dirname(gnr.__file__)

    @property
    def frozen_root(self):  # wf:phase-2:new
        """The `gnr` package inside the legacy venv, whatever python built it."""
        roots = sorted(glob.glob(LEGACY_GLOB))
        if not roots:
            raise RuntimeError(f"no frozen genropy under {LEGACY_GLOB}")
        return roots[-1]

    def get_source_files(self, root):  # wf:phase-2:new
        """Relative paths of the `*.py` files this tree contributes at runtime."""
        found = set()
        for folder, subfolders, names in os.walk(root):
            relative = os.path.relpath(folder, root)
            subfolders[:] = [name for name in subfolders
                             if name != "__pycache__"
                             and not (relative == "." and name in PACKAGED_ONLY)]
            for name in names:
                if name.endswith(".py"):
                    found.add(os.path.normpath(os.path.join(relative, name)))
        return found

    @property
    def differences(self):  # wf:phase-2:new
        """(differing, legacy_only, bridge_only) — all empty means parity."""
        legacy = self.get_source_files(self.legacy_root)
        bridge = self.get_source_files(self.bridge_root)
        differing = [name for name in sorted(legacy & bridge)
                     if not filecmp.cmp(os.path.join(self.legacy_root, name),
                                        os.path.join(self.bridge_root, name),
                                        shallow=False)]
        return differing, sorted(legacy - bridge), sorted(bridge - legacy)

    @property
    def aligned(self):  # wf:phase-2:new
        return not any(self.differences)

    @property
    def report(self):  # wf:phase-2:new
        """What a human reads: the two roots, what differs, what to do."""
        differing, legacy_only, bridge_only = self.differences
        lines = [f"legacy tree: {self.legacy_root}",
                 f"bridge tree: {self.bridge_root}"]
        if not (differing or legacy_only or bridge_only):
            lines.append("the two stacks run identical genropy source")
            return "\n".join(lines)
        for name in differing:
            lines.append(f"  differs:      {name}")
        for name in legacy_only:
            lines.append(f"  legacy only:  {name}")
        for name in bridge_only:
            lines.append(f"  bridge only:  {name}")
        lines.append(f"\n{len(differing) + len(legacy_only) + len(bridge_only)} "
                     f"file(s) out of parity\n")
        lines.append(REMEDY)
        return "\n".join(lines)


if __name__ == "__main__":
    parity = GenropyParity()
    print(parity.report)
    sys.exit(0 if parity.aligned else 1)
