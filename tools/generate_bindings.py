#!/usr/bin/env python3
"""
Generate Ada thin bindings for ESP32-S3 IDF peripherals.

Usage:
    python3 tools/generate_bindings.py [--out-dir DIR] [--gcc PATH]
    python3 tools/generate_bindings.py --module ESP32.GPIO

The script reads tools/modules.toml, then for each enabled module calls
`gcc -fdump-ada-spec` on the listed IDF headers.  All generated .ads files
(direct bindings and transitive C-header dependencies) are written to
source/idf/.

After generation, a post-processing pass:
  * Merges *_types_h packages into their parent *_h package (when present).
  * Drops the trailing _h suffix from every package name and file name.
  * Updates all with-clauses and qualified names throughout every file.

Final package names:
  driver/gpio.h         -> package driver_gpio   in driver_gpio.ads
  esp_adc/adc_oneshot.h -> package esp_adc_adc_oneshot

Requirements:
  - Run 'idf.py build' at least once so build/compile_commands.json exists.
  - The GNAT Xtensa cross-compiler must be on PATH or given via --gcc.
"""

import argparse
import json
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


# ---------------------------------------------------------------------------
# Compiler / include-path helpers
# ---------------------------------------------------------------------------

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
    """Return (deduplicated -I flags, idf_path) from compile_commands.json."""
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
        raw = entry.get("arguments")
        args = raw if raw else entry.get("command", "").split()
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


def add_idf_component_includes(base_flags: list[str], idf_path: Path) -> list[str]:
    """Add all components/*/include directories from the IDF tree."""
    seen: set[str] = {f[2:] for f in base_flags if f.startswith("-I")}
    extra: list[str] = []
    components_dir = idf_path / "components"
    if not components_dir.is_dir():
        return base_flags
    for inc_dir in sorted(components_dir.rglob("include")):
        if not inc_dir.is_dir():
            continue
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


# ---------------------------------------------------------------------------
# Binding generation
# ---------------------------------------------------------------------------

def header_to_package_name(header: str) -> str:
    """Predict the Ada package name gcc will use for a C header path."""
    name = header.lower()
    name = re.sub(r"\.h$", "_h", name)
    name = re.sub(r"[/\-\.]", "_", name)
    return name


def dump_ada_spec(gcc: Path, header: str, include_flags: list[str], work_dir: Path) -> list[Path]:
    """Run gcc -fdump-ada-spec on a single header; return all produced .ads files."""
    wrapper = work_dir / "_wrap_.h"
    wrapper.write_text(f"#include <{header}>\n")

    cmd = (
        [str(gcc), "-x", "c", "-fsyntax-only", "-fdump-ada-spec", "-DESP_PLATFORM"]
        + include_flags
        + [str(wrapper)]
    )
    result = subprocess.run(cmd, cwd=work_dir, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"gcc -fdump-ada-spec failed for {header} (exit {result.returncode}):\n"
            f"{result.stderr[-1500:]}"
        )

    (work_dir / "_wrap__h.ads").unlink(missing_ok=True)
    return sorted(work_dir.glob("*.ads"))


_ABS_PATH_RE = re.compile(r"--\s+/[^\s]+")


def strip_abs_paths(text: str) -> str:
    """Remove machine-specific absolute paths from generated Ada comments."""
    text = _ABS_PATH_RE.sub("", text)
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

    total_new = total_skipped = failed = 0

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
                new += n; skipped += s

            total_new += new; total_skipped += skipped
            print(f"  [{new} new, {skipped} skipped]")

    return total_new, failed


# ---------------------------------------------------------------------------
# Post-processing: merge _types_h packages and drop _h suffix
# ---------------------------------------------------------------------------

def _pkg_body(text: str, pkg: str) -> str:
    """Extract everything between 'package PKG is' and 'end PKG;'."""
    m = re.search(
        r"^package\s+" + re.escape(pkg) + r"\s+is\n(.*?)\nend\s+" + re.escape(pkg) + r"\s*;",
        text, re.DOTALL | re.MULTILINE,
    )
    return m.group(1) if m else ""


def _with_lines(text: str) -> list[str]:
    """Return context-clause lines (with/limited-with/use) from the file header.

    Only lines that appear before 'package ... is' are considered; this avoids
    matching 'with Convention => C' or similar record-component syntax.
    """
    result = []
    for ln in text.splitlines():
        if re.match(r"^package\s+", ln):
            break
        if re.match(r"\s*(limited\s+)?with\s+\w|\s*use\s+\w", ln):
            result.append(ln)
    return result


def _merge_types_into_parent(types_path: Path, parent_path: Path,
                              types_pkg: str, parent_pkg: str) -> None:
    """Inline the content of types_path into parent_path and delete types_path."""
    types_text  = types_path.read_text()
    parent_text = parent_path.read_text()

    body = _pkg_body(types_text, types_pkg)

    # Add any new with-clauses from the types file (skip self-references).
    for wl in _with_lines(types_text):
        ref = re.match(r"\s*(?:limited\s+)?with\s+(\S+?)(?:,|\s*;)", wl)
        if ref and ref.group(1) in (types_pkg, parent_pkg):
            continue
        if wl.strip() not in parent_text:
            m = re.search(r"^package\s+", parent_text, re.MULTILINE)
            if m:
                parent_text = parent_text[: m.start()] + wl + "\n" + parent_text[m.start():]

    # Remove the 'with <types_pkg>;' line from the parent (it's now inlined).
    parent_text = re.sub(
        r"^(?:limited\s+)?with\s+" + re.escape(types_pkg) + r"\s*;\n",
        "", parent_text, flags=re.MULTILINE,
    )
    parent_text = re.sub(
        r"^use\s+" + re.escape(types_pkg) + r"\s*;\n",
        "", parent_text, flags=re.MULTILINE,
    )

    if body.strip():
        # Types must come before they are used, so insert at the TOP of the
        # parent package body (immediately after 'package X is').
        open_m = re.search(
            r"^package\s+" + re.escape(parent_pkg) + r"\s+is\n",
            parent_text, re.MULTILINE,
        )
        if open_m:
            insert = open_m.end()
            parent_text = parent_text[:insert] + body + "\n\n" + parent_text[insert:]

    parent_path.write_text(parent_text)
    types_path.unlink()


def _build_rename_map(stems: set[str]) -> dict[str, str]:
    """Map every old package/file stem to its new name.

    Rules:
      *_types_h  where *_h exists  ->  * (merged; same new name as parent)
      *_types_h  standalone        ->  *_types  (drop _h only)
      *_h                          ->  *        (drop _h)
      anything else                ->  unchanged
    """
    rename: dict[str, str] = {}
    for stem in stems:
        if stem.endswith("_types_h"):
            parent = stem[: -len("_types_h")] + "_h"
            if parent in stems:
                rename[stem] = parent[:-2]   # merged: both point to same new name
            else:
                rename[stem] = stem[:-2]     # standalone: drop just _h
        elif stem.endswith("_h"):
            rename[stem] = stem[:-2]
        else:
            rename[stem] = stem
    return rename


def _apply_rename_map(text: str, rename: dict[str, str]) -> str:
    """Replace every old package identifier with its new name in one pass."""
    pairs = sorted(rename.items(), key=lambda x: len(x[0]), reverse=True)
    pattern = re.compile(
        r"(?<![A-Za-z0-9_])("
        + "|".join(re.escape(old) for old, _ in pairs)
        + r")(?![A-Za-z0-9_])"
    )
    lut = dict(pairs)
    return pattern.sub(lambda m: lut[m.group(1)], text)


# ---------------------------------------------------------------------------
# System-dependency filtering
# ---------------------------------------------------------------------------

# Packages that are purely C-runtime / OS / logging infrastructure and have
# no place in a peripheral hardware API binding.  The _h suffix has already
# been dropped by the rename pass when this set is consulted.
_SYSTEM_PKGS: frozenset[str] = frozenset({
    # C standard library
    "assert", "stdio", "stdlib", "string", "strings", "stdarg", "inttypes",
    # C-library internals (newlib / musl reentrant structs)
    "sys_reent", "sys_utypes", "sys_ulocale", "sys_lock", "lock", "reent",
    # Low-level synchronisation (not part of the driver API surface)
    "spinlock",
    # FreeRTOS scheduler internals (TickType_t lives in freertos_portmacro
    # which is deliberately kept so TWAI/UART timeouts remain typed)
    "freertos_freertos", "freertos_projdefs", "freertos_list",
    "freertos_portable", "freertos_task", "freertos_queue",
    "freertos_semphr", "freertos_event_groups", "freertos_timers",
    "freertos_stream_buffer", "freertos_message_buffer",
    "freertos_idf_additions",
    # Xtensa CPU internals
    "xtensa_api", "xtensa_config_core", "xtensa_context", "xtensa_hal",
    "xtensa_xtruntime", "xtensa_xtruntime_core_state",
    "xtensa_xtruntime_frames", "xt_utils",
    # ESP-IDF system services that are not peripheral access
    "esp_log", "esp_log_args", "esp_log_buffer", "esp_log_config",
    "esp_log_level", "esp_log_timestamp", "esp_log_write",
    "esp_heap_caps", "multi_heap",
    "esp_idf_version", "esp_ipc",
    "esp_memory_utils", "esp_newlib",
    "esp_private_crosscore_int", "esp_rom_sys",
    # Stray / internal
    "uwrap_uh",
})


def _drop_excl_with_clauses(text: str, excl: frozenset[str]) -> str:
    """Remove 'with/limited with/use EXCL_PKG;' lines from the file header."""
    lines = text.splitlines(keepends=True)
    past_pkg = False
    result = []
    for ln in lines:
        if not past_pkg and re.match(r"^package\s+", ln):
            past_pkg = True
        if not past_pkg:
            m = re.match(r"\s*(?:limited\s+)?with\s+(\w+)", ln)
            if m and m.group(1) in excl:
                continue
            m = re.match(r"\s*use\s+(\w+)", ln)
            if m and m.group(1) in excl:
                continue
        result.append(ln)
    return "".join(result)


def _drop_excl_decls(text: str, excl: frozenset[str]) -> str:
    """Remove declaration blocks whose text references any excluded package.

    A 'declaration block' is a run of non-blank lines (one declaration with
    its aspect specification) separated from its neighbours by blank lines.
    The package header and footer are left untouched.
    """
    if not excl:
        return text

    excl_re = re.compile(
        r"(?<![A-Za-z0-9_])(" + "|".join(re.escape(p) for p in sorted(excl, key=len, reverse=True)) + r")\."
    )

    pkg_open = re.search(r"^package\s+\S+\s+is\s*$", text, re.MULTILINE)
    pkg_end  = re.search(r"^end\s+\S+\s*;",          text, re.MULTILINE)
    if not pkg_open or not pkg_end:
        return text

    header = text[: pkg_open.end() + 1]
    body   = text[pkg_open.end() + 1 : pkg_end.start()]
    footer = text[pkg_end.start() :]

    # Split on one-or-more consecutive blank lines.
    blocks = re.split(r"\n{2,}", body)
    kept   = [b for b in blocks if not excl_re.search(b)]
    return header + "\n\n".join(kept) + footer


def _filter_system_deps(out_dir: Path) -> tuple[int, int]:
    """Delete system-package .ads files and clean up references in kept files."""
    deleted = removed_decls = 0

    # Delete the excluded files themselves.
    for f in sorted(out_dir.glob("*.ads")):
        if f.stem in _SYSTEM_PKGS:
            f.unlink()
            deleted += 1

    # In each remaining file, remove with-clauses and declarations that
    # reference excluded packages.
    for f in sorted(out_dir.glob("*.ads")):
        text = f.read_text()
        cleaned = _drop_excl_with_clauses(text, _SYSTEM_PKGS)
        cleaned = _drop_excl_decls(cleaned, _SYSTEM_PKGS)
        if cleaned != text:
            removed_decls += 1
            f.write_text(cleaned)

    return deleted, removed_decls


def postprocess(out_dir: Path) -> None:
    """Merge _types_h packages into parents, drop _h suffix, filter system deps."""
    ads_files = sorted(out_dir.glob("*.ads"))
    stems = {f.stem for f in ads_files}

    rename = _build_rename_map(stems)

    # Step 1: merge types files that have a parent.
    merged_count = 0
    for stem in sorted(stems):
        if not stem.endswith("_types_h"):
            continue
        parent = stem[: -len("_types_h")] + "_h"
        if parent not in stems:
            continue
        types_path  = out_dir / (stem   + ".ads")
        parent_path = out_dir / (parent + ".ads")
        print(f"  merge  {stem} -> {parent}")
        _merge_types_into_parent(types_path, parent_path, stem, parent)
        merged_count += 1

    # Step 2: rename every remaining file and update all identifiers inside.
    renamed_count = 0
    for f in sorted(out_dir.glob("*.ads")):
        old_stem = f.stem
        new_stem = rename.get(old_stem, old_stem)

        text     = f.read_text()
        new_text = _apply_rename_map(text, rename)

        new_path = out_dir / (new_stem + ".ads")
        if new_path == f:
            if new_text != text:
                f.write_text(new_text)
        else:
            new_path.write_text(new_text)
            f.unlink()
            renamed_count += 1

    print(f"  {merged_count} types files merged, {renamed_count} files renamed.")

    # Step 3: remove system/OS/logging packages and clean their references.
    deleted, cleaned = _filter_system_deps(out_dir)
    print(f"  {deleted} system files removed, {cleaned} files cleaned of system references.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out-dir", default=str(OUT_DIR),
        help=f"Output directory for .ads files (default: {OUT_DIR})",
    )
    parser.add_argument(
        "--gcc", default=None,
        help="Path to xtensa-esp32-elf-gcc (auto-detected if not given)",
    )
    parser.add_argument(
        "--module", metavar="ADA_PACKAGE", default=None,
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
    total_new = total_fail = 0

    for module in modules:
        new, fail = generate_module(module, gcc, include_flags, out_dir, already_written)
        total_new += new; total_fail += fail

    print(f"\nGenerated: {total_new} .ads files, {total_fail} header(s) failed.")
    if total_fail:
        sys.exit(1)

    print("\nPost-processing:")
    postprocess(out_dir)
    print("Done.")


if __name__ == "__main__":
    main()
