"""Do the two stacks run the same genropy? The comparison refuses to start until
they do.

**One pinned tree, and both stacks read it.** The bench does not follow the
genropy the developer happens to have checked out — that tree moves under the
bench between one run and the next, and a difference between two runs then reads
as a difference between the stacks. Measured on 2026-08-25: the checkout had
moved onto a branch whose one commit removes the register read for services not
configured in the database, which is 242 of the 384 register calls a login
makes. The bridge was already running it. The legacy stack was not.

So the bench declares a genropy of its own — a detached worktree — and the two
stacks reach it differently, because they are installed differently:

- the **bridge** imports it directly, put first on the import path by
  `PYTHONPATH`, which wins over the editable install of the developer's own
  checkout (measured 2026-08-25: the editable finder appends itself to
  `sys.meta_path`, so the ordinary path lookup answers first);
- the **legacy** stack imports a frozen copy inside `temp/legacy_venv`, so what
  is asked of it is not a path but the same content.

Hence the two questions this asks, and they are not the same question:

1. is this interpreter importing the pinned tree, or something else?
2. does the frozen copy carry the same source as the pinned tree?

**What the two stacks share by construction.** `resources/`, `packages/`,
`webtools/` and the two static trees are named by the bench's own
`environment.xml`, which both launchers point at through `GENRO_GNRFOLDER`. Both
stacks therefore read the same directories, and there is nothing to compare: a
shared tree is parity by definition. `projects/` stays on the developer's
checkout, where the bench instances live untracked — the bench application is
not genropy, and both stacks read that one too.

**What is compared, then.** Only `*.py`, only inside the `gnr` package, never
`__pycache__`, and never the five top-level subtrees the wheel copies in but no
runtime reads there — `projects`, `resources`, `webtools`, `dojo_libs`, `gnrjs`.
With them out the two sets match file for file (320 against 320, measured
2026-08-25), and any asymmetry left is real drift.

This is a REFUSAL, not a warning: it names what is wrong, prints the remedy, and
exits non-zero. `replica.py` calls it first at every cycle start.

Run, from the repository root:

  GENRO_GNRFOLDER=$PWD/temp/gnr PYTHONPATH=<pinned>/gnrpy \\
      python benchmarks/compare/genropy_parity_check.py
"""

import filecmp
import glob
import os
import sys
import xml.etree.ElementTree

import gnr

BENCH_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LEGACY_GLOB = os.path.join(BENCH_ROOT, "temp", "legacy_venv", "lib", "python*",
                           "site-packages", "gnr")
# The folder MUST be called `gnr`: genropy loads the whole configuration folder
# as one Bag whose top node is the folder's own name, and every lookup is written
# `gnr.environment_xml...`. A folder named otherwise loads fine and answers None
# to every question (measured 2026-08-25, on a folder called `bench_gnr`).
BENCH_GNR_FOLDER = os.path.join(BENCH_ROOT, "temp", "gnr")

# genropy's own name for "read the configuration from here instead of ~/.gnr"
GNR_FOLDER_ENV = "GENRO_GNRFOLDER"

# Copied into the wheel, read from the trees the bench config names: see above.
PACKAGED_ONLY = ("projects", "resources", "webtools", "dojo_libs", "gnrjs")


class GenropyParity:
    """The pinned genropy, and whether both stacks are actually on it."""

    def __init__(self, pinned_root=None, legacy_root=None, bridge_root=None):
        self.pinned_root = pinned_root or self.declared_root
        self.legacy_root = legacy_root or self.frozen_root
        self.bridge_root = bridge_root or os.path.dirname(gnr.__file__)

    @property
    def gnr_folder(self):
        """The configuration folder this process is running under."""
        return os.environ.get(GNR_FOLDER_ENV)

    @property
    def declared_root(self):
        """The `gnr` package of the tree the bench's own environment.xml names."""
        if not self.gnr_folder:
            raise RuntimeError(
                f"{GNR_FOLDER_ENV} is unset: the bench cannot say which genropy "
                f"it pinned. Set it to {BENCH_GNR_FOLDER}.")
        environment = os.path.join(self.gnr_folder, "environment.xml")
        home = xml.etree.ElementTree.parse(environment).find("./environment/gnrhome")
        return os.path.join(home.get("value"), "gnrpy", "gnr")

    @property
    def frozen_root(self):
        """The `gnr` package inside the legacy venv, whatever python built it."""
        roots = sorted(glob.glob(LEGACY_GLOB))
        if not roots:
            raise RuntimeError(f"no frozen genropy under {LEGACY_GLOB}")
        return roots[-1]

    def get_source_files(self, root):
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
    def bridge_on_pin(self):
        """Is this interpreter importing the pinned tree, and not another one?"""
        return os.path.realpath(self.bridge_root) == os.path.realpath(self.pinned_root)

    @property
    def differences(self):
        """(differing, pinned_only, frozen_only) between the pin and the copy."""
        pinned = self.get_source_files(self.pinned_root)
        frozen = self.get_source_files(self.legacy_root)
        differing = [name for name in sorted(pinned & frozen)
                     if not filecmp.cmp(os.path.join(self.pinned_root, name),
                                        os.path.join(self.legacy_root, name),
                                        shallow=False)]
        return differing, sorted(pinned - frozen), sorted(frozen - pinned)

    @property
    def aligned(self):
        return self.bridge_on_pin and not any(self.differences)

    @property
    def report(self):
        """What a human reads: where each stack stands, and what to do about it."""
        lines = [f"pinned genropy: {self.pinned_root}",
                 f"bridge imports: {self.bridge_root}",
                 f"legacy copy:    {self.legacy_root}"]
        if not self.bridge_on_pin:
            lines.append("\nthe bridge is NOT importing the pinned tree. Remedy:\n"
                         f"  export PYTHONPATH={os.path.dirname(self.pinned_root)}")
        differing, pinned_only, frozen_only = self.differences
        for name in differing:
            lines.append(f"  differs:      {name}")
        for name in pinned_only:
            lines.append(f"  pinned only:  {name}")
        for name in frozen_only:
            lines.append(f"  frozen only:  {name}")
        if differing or pinned_only or frozen_only:
            lines.append(
                f"\n{len(differing) + len(pinned_only) + len(frozen_only)} file(s): "
                f"the frozen copy is not the pinned tree. Remedy:\n"
                f'  uv pip install --python temp/legacy_venv/bin/python '
                f'"{os.path.dirname(os.path.dirname(self.pinned_root))}/gnrpy[pgsql]"')
        if self.aligned:
            lines.append("\nboth stacks run the pinned genropy")
        else:
            lines.append("\nUntil this exits 0, no comparative run may start.")
        return "\n".join(lines)


if __name__ == "__main__":
    parity = GenropyParity()
    print(parity.report)
    sys.exit(0 if parity.aligned else 1)
