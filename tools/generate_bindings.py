#!/usr/bin/env python3
"""
Generate Ada thin bindings for ESP32-S3 IDF peripherals.

Usage:
    python3 tools/generate_bindings.py [--idf-path PATH] [--out-dir DIR] [--gcc PATH]
    python3 tools/generate_bindings.py --module ESP32.GPIO

The script reads tools/modules.toml, then for each enabled module calls
`gcc -fdump-ada-spec` on the listed IDF headers.  All generated .ads files
(direct bindings and transitive C-header dependencies) are written to
source/idf/.  Files already present are skipped so running the script a
second time is idempotent.

Package naming follows the gcc convention:
  driver/gpio.h            -> package driver_gpio_h     in driver_gpio_h.ads
  esp_adc/adc_oneshot.h    -> package esp_adc_adc_oneshot_h

Users write e.g.:
  with driver_gpio_h;  use driver_gpio_h;
  with driver_rmt_tx_h; use driver_rmt_tx_h;

Requirements:
  - Run 'idf.py build' at least once so build/compile_commands.json exists.
  - The GNAT Xtensa cross-compiler must be on PATH or given via --gcc.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR    = PROJECT_ROOT / "tools"
BUILD_DIR    = PROJECT_ROOT / "build"
OUT_DIR      = PROJECT_ROOT / "source" / "idf"
MODULES_FILE = TOOLS_DIR / "modules.toml"
COMPILE_CMDS = BUILD_DIR / "compile_commands.json"

DEFAULT_GCC = (
    Path.home()
    / ".local/share/alire/toolchains"
    / "gnat_xtensa_esp32_elf_15.2.1_0eebc758"
    / "bin"
    / "xtensa-esp32-elf-gcc"
)


def find_gcc(explicit: str | None) -> Path:
    if explicit:
        p = Path(explicit)
        if not p.exists():
            sys.exit(f"gcc not found at {p}")
        return p
    if DEFAULT_GCC.exists():
        return DEFAULT_GCC
    found = shutil.which("xtensa-esp32-elf-gcc")
    if found:
        return Path(found)
    sys.exit(
        "Could not find xtensa-esp32-elf-gcc. "
        "Pass --gcc or ensure the GNAT Xtensa toolchain is on PATH."
    )


def extract_include_flags(compile_commands: Path) -> tuple[list[str], Path | None]:
    """Return (deduplicated -I flags, idf_path) from compile_commands.json.

    idf_path is inferred from the first component path seen in the flags,
    or None if it cannot be determined.
    """
    if not compile_commands.exists():
        sys.exit(
            f"{compile_commands} not found.\n"
            "Run 'idf.py build' first to generate the build directory."
        )
    with compile_commands.open() as f:
        db = json.load(f)

    seen: set[str] = set()
    flags: list[str] = []
    idf_path: Path | None = None

    for entry in db:
        # compile_commands.json may use either 'arguments' (list) or 'command' (string)
        raw = entry.get("arguments")
        if raw:
            args = raw
        else:
            args = entry.get("command", "").split()

        i = 0
        while i < len(args):
            if args[i] == "-I" and i + 1 < len(args):
                inc = args[i + 1]
                if inc not in seen:
                    seen.add(inc)
                    flags.extend(["-I", inc])
                i += 2
            elif args[i].startswith("-I"):
                inc = args[i][2:]
                if inc not in seen:
                    seen.add(inc)
                    flags.append("-I" + inc)
                i += 1
            else:
                i += 1

    # Infer IDF root from any path that matches .../components/<name>/include
    if idf_path is None:
        for inc in seen:
            p = Path(inc)
            if p.name == "include" and p.parent.parent.name == "components":
                idf_path = p.parent.parent.parent
                break

    return flags, idf_path


# Components that target Linux host builds — their headers shadow Xtensa
# toolchain headers and break cross-compilation.
_EXCLUDED_COMPONENTS = frozenset({"linux", "esp_linux_helper"})


def add_idf_component_includes(
    base_flags: list[str], idf_path: Path
) -> list[str]:
    """Add all components/*/include directories from the IDF tree.

    This ensures headers from driver components not used by the current
    app (e.g. driver/uart.h) can still be found.  Linux-host-only
    component paths are excluded because they shadow Xtensa toolchain headers.
    """
    seen: set[str] = {f[2:] for f in base_flags if f.startswith("-I")}
    extra: list[str] = []
    components_dir = idf_path / "components"
    if not components_dir.is_dir():
        return base_flags
    for inc_dir in sorted(components_dir.rglob("include")):
        if not inc_dir.is_dir():
            continue
        # Determine the component this include dir belongs to.
        try:
            rel = inc_dir.relative_to(components_dir)
            component_name = rel.parts[0]
        except ValueError:
            component_name = ""
        if component_name in _EXCLUDED_COMPONENTS:
            continue
        if str(inc_dir) not in seen:
            seen.add(str(inc_dir))
            extra.append("-I" + str(inc_dir))
    return base_flags + extra


def header_to_package_name(header: str) -> str:
    """Predict the Ada package name gcc will use for a C header path.

    driver/gpio.h         -> driver_gpio_h
    esp_adc/adc_oneshot.h -> esp_adc_adc_oneshot_h
    """
    name = header.lower()
    # strip .h suffix, replace path separators and dashes with underscores
    name = re.sub(r"\.h$", "_h", name)
    name = re.sub(r"[/\-\.]", "_", name)
    return name


def dump_ada_spec(
    gcc: Path,
    header: str,
    include_flags: list[str],
    work_dir: Path,
) -> list[Path]:
    """Run gcc -fdump-ada-spec on a single header; return all produced .ads files."""
    # Write a tiny wrapper so we control the exact file fed to gcc.
    wrapper = work_dir / "_wrap_.h"
    wrapper.write_text(f"#include <{header}>\n")

    cmd = (
        [str(gcc), "-x", "c", "-fsyntax-only", "-fdump-ada-spec", "-DESP_PLATFORM"]
        + include_flags
        + [str(wrapper)]
    )
    result = subprocess.run(cmd, cwd=work_dir, capture_output=True, text=True)
    if result.returncode != 0:
        stderr_tail = result.stderr[-1500:]
        raise RuntimeError(
            f"gcc -fdump-ada-spec failed for {header} (exit {result.returncode}):\n"
            f"{stderr_tail}"
        )

    # Remove the wrapper's own (always-empty) .ads file.
    wrapper_ads = work_dir / "_wrap__h.ads"
    wrapper_ads.unlink(missing_ok=True)

    return sorted(work_dir.glob("*.ads"))


_ABS_PATH_RE = re.compile(r"--\s+/[^\s]+")


def strip_abs_paths(text: str) -> str:
    """Remove absolute filesystem paths from generated Ada comments.

    gcc -fdump-ada-spec appends the source file path as a trailing comment on
    each declaration line.  Remove those and tidy up trailing whitespace so the
    output is clean and machine-independent.
    """
    text = _ABS_PATH_RE.sub("", text)
    # Clean up trailing whitespace left behind after path removal.
    text = re.sub(r"[ \t]+$", "", text, flags=re.MULTILINE)
    return text


def install_ads(src: Path, out_dir: Path, already_written: set[str]) -> tuple[int, int]:
    """Copy src to out_dir if not already present. Returns (new, skipped)."""
    dest = out_dir / src.name
    if src.name in already_written or dest.exists():
        return 0, 1
    text = src.read_text(encoding="utf-8", errors="replace")
    dest.write_text(strip_abs_paths(text), encoding="utf-8")
    already_written.add(src.name)
    return 1, 0


def generate_module(
    module: dict,
    gcc: Path,
    include_flags: list[str],
    out_dir: Path,
    already_written: set[str],
) -> tuple[int, int]:
    """Process one module entry. Returns (new_files, failed_headers)."""
    ada_package = module["ada_package"]
    headers     = module["headers"]
    print(f"\n  {ada_package}")

    total_new    = 0
    total_skipped = 0
    failed       = 0

    for header in headers:
        pkg_name = header_to_package_name(header)
        print(f"    {header}  (-> {pkg_name})", end="", flush=True)

        with tempfile.TemporaryDirectory(prefix="esp32_bind_") as tmp:
            tmp_path = Path(tmp)
            try:
                ads_files = dump_ada_spec(gcc, header, include_flags, tmp_path)
            except RuntimeError as e:
                print(f"\n      ERROR: {e}", file=sys.stderr)
                failed += 1
                continue

            new = skipped = 0
            for ads in ads_files:
                n, s = install_ads(ads, out_dir, already_written)
                new     += n
                skipped += s

            total_new     += new
            total_skipped += skipped
            print(f"  [{new} new, {skipped} skipped]")

    return total_new, failed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out-dir",
        default=str(OUT_DIR),
        help=f"Output directory for .ads files (default: {OUT_DIR})",
    )
    parser.add_argument(
        "--gcc",
        default=None,
        help="Path to xtensa-esp32-elf-gcc (auto-detected if not given)",
    )
    parser.add_argument(
        "--module",
        metavar="ADA_PACKAGE",
        default=None,
        help="Generate only this module (e.g. ESP32.GPIO). Default: all enabled.",
    )
    args = parser.parse_args()

    out_dir = Path(args.out_dir)

    if not MODULES_FILE.exists():
        sys.exit(f"Module list not found: {MODULES_FILE}")
    with MODULES_FILE.open("rb") as f:
        config = tomllib.load(f)
    modules = config.get("module", [])

    if args.module:
        modules = [m for m in modules if m["ada_package"] == args.module]
        if not modules:
            sys.exit(f"No module named '{args.module}' found in {MODULES_FILE}")
    else:
        modules = [m for m in modules if m.get("enabled", True)]

    gcc = find_gcc(args.gcc)
    print(f"Compiler : {gcc}")

    include_flags, idf_path = extract_include_flags(COMPILE_CMDS)
    if idf_path:
        include_flags = add_idf_component_includes(include_flags, idf_path)
        print(f"IDF root : {idf_path}")
    print(f"Includes : {len(include_flags) // 2} paths (build + IDF components)")

    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"Output   : {out_dir}")

    already_written: set[str] = set()
    total_new  = 0
    total_fail = 0

    for module in modules:
        new, fail = generate_module(
            module, gcc, include_flags, out_dir, already_written
        )
        total_new  += new
        total_fail += fail

    print(f"\nDone: {total_new} .ads files written, {total_fail} header(s) failed.")
    if total_fail:
        sys.exit(1)


if __name__ == "__main__":
    main()
