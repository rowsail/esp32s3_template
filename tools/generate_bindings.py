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


# ---------------------------------------------------------------------------
# Step 4: merge remaining *_types packages into driver packages
# ---------------------------------------------------------------------------

# Ordered list of (source_types_pkg, target_pkg) after the _h-suffix rename.
# Process in this order: leaves of any chain must appear BEFORE their parent
# so that intermediate packages receive their merged content first.
_TYPES_MERGE: list[tuple[str, str]] = [
    # machine types -> stdint foundation
    ("machine_udefault_types",       "sys_ustdint"),
    # TWAI chain (hal_twai_types -> hal_twai_types_deprecated -> driver_twai)
    ("hal_twai_types",               "hal_twai_types_deprecated"),
    ("hal_twai_types_deprecated",    "driver_twai"),
    ("driver_twai_types_legacy",     "driver_twai"),
    # HAL/driver types -> primary driver packages
    ("hal_ana_cmpr_types",           "driver_ana_cmpr"),
    ("hal_glitch_filter_types",      "driver_gpio_filter"),
    ("hal_ledc_types",               "driver_ledc"),
    ("hal_pcnt_types",               "driver_pulse_cnt"),
    ("hal_rtc_io_types",             "driver_rtc_io"),
    ("hal_sdm_types",                "driver_sdm"),
    ("hal_temperature_sensor_types", "driver_temperature_sensor"),
    ("hal_timer_types",              "driver_gptimer"),
    ("hal_gpio_types",               "driver_gpio"),
    ("hal_uart_types",               "driver_uart"),
    ("hal_spi_types",                "driver_spi_common"),
    ("driver_i2c_types",             "driver_i2c_master"),
    ("hal_i2c_types",                "driver_i2c_master"),
    ("driver_i2s_types",             "driver_i2s_common"),
    ("hal_i2s_types",                "driver_i2s_common"),
    ("driver_mcpwm_types",           "driver_mcpwm_timer"),
    ("hal_mcpwm_types",              "driver_mcpwm_timer"),
    ("driver_parlio_types",          "driver_parlio_tx"),
    ("hal_parlio_types",             "driver_parlio_tx"),
    ("driver_rmt_types",             "driver_rmt_common"),
    ("hal_rmt_types",                "driver_rmt_common"),
    ("esp_intr_types",               "esp_intr_alloc"),
    # esp_lcd_types depends on hal_lcd_types, so merge esp first (middle)
    # then hal second (top), ensuring hal types are declared before use.
    ("esp_lcd_types",                "esp_lcd_panel_io"),
    ("hal_lcd_types",                "esp_lcd_panel_io"),
    ("hal_adc_types",                "esp_adc_adc_oneshot"),
]

# Files with no remaining driver references: delete without merging.
_TYPES_ORPHAN: frozenset[str] = frozenset({
    "hal_color_types",
    "hal_hal_utils",
    "esp_cpu",
    "esp_system",
})


def _merge_all_types(out_dir: Path) -> None:
    """Merge all remaining *_types packages into their respective driver packages."""
    # Build direct map (immediate parent) for chain resolution.
    direct: dict[str, str] = {src: dst for src, dst in _TYPES_MERGE}

    def _resolve(src: str) -> str:
        """Follow the chain to find the ultimate destination."""
        visited = {src}
        cur = direct.get(src, src)
        while cur in direct and cur not in visited:
            visited.add(cur)
            cur = direct[cur]
        return cur

    # Step 4a: perform the actual merges in listed (topological) order.
    merged = 0
    for src, dst in _TYPES_MERGE:
        types_path  = out_dir / (src + ".ads")
        parent_path = out_dir / (dst + ".ads")
        if not types_path.exists():
            continue
        if not parent_path.exists():
            print(f"  skip   {src} -> {dst}  (parent not found)")
            continue
        print(f"  merge  {src} -> {dst}")
        _merge_types_into_parent(types_path, parent_path, src, dst)
        merged += 1

    # Step 4b: build a rename map so all files update their references.
    rename: dict[str, str] = {}
    for src, _ in _TYPES_MERGE:
        final = _resolve(src)
        if final != src:
            rename[src] = final

    if rename:
        for f in sorted(out_dir.glob("*.ads")):
            text = f.read_text()
            new_text = _apply_rename_map(text, rename)
            if new_text != text:
                f.write_text(new_text)

    # After renaming, a package may end up with 'with <itself>;' or with
    # self-qualified identifiers like 'Pkg.Type' inside the same package.
    # Remove the self-with clause and strip the redundant self-qualifier.
    for f in sorted(out_dir.glob("*.ads")):
        text = f.read_text()
        m = re.search(r"^package\s+(\S+)\s+is", text, re.MULTILINE)
        if not m:
            continue
        pkg = m.group(1)
        cleaned = re.sub(
            r"^(?:limited\s+)?with\s+" + re.escape(pkg) + r"\s*;\n",
            "", text, flags=re.MULTILINE,
        )
        # Strip self-qualified names inside the package body
        cleaned = re.sub(r"\b" + re.escape(pkg) + r"\.(\w)", r"\1", cleaned)
        if cleaned != text:
            f.write_text(cleaned)

    # Step 4c: delete orphaned types files and clean up their references.
    orphan_deleted = 0
    for stem in _TYPES_ORPHAN:
        p = out_dir / (stem + ".ads")
        if p.exists():
            p.unlink()
            orphan_deleted += 1

    if orphan_deleted:
        for f in sorted(out_dir.glob("*.ads")):
            text = f.read_text()
            cleaned = _drop_excl_with_clauses(text, _TYPES_ORPHAN)
            cleaned = _drop_excl_decls(cleaned, _TYPES_ORPHAN)
            if cleaned != text:
                f.write_text(cleaned)

    print(f"  {merged} types packages merged, {orphan_deleted} orphans removed.")


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

    # Step 4: merge remaining *_types packages into their driver packages.
    _merge_all_types(out_dir)

    # Step 5: replace sys_ustdint.T with direct Interfaces.C equivalents.
    _expand_stdint(out_dir)

    # Step 6: insert missing 'when 0 =>' in malformed variant records.
    fixed = _fix_variant_records(out_dir)
    if fixed:
        print(f"  {fixed} files repaired (missing 'when 0 =>' in variant records).")

    # Step 7: convert --  arg-macro: comment pairs to Ada expression functions.
    _expand_arg_macros(out_dir)

    # Step 8: convert pseudo-enum subtypes to proper Ada enumeration types.
    _expand_pseudo_enums(out_dir)


# ---------------------------------------------------------------------------
# Step 5: replace sys_ustdint with direct Interfaces.C types
# ---------------------------------------------------------------------------

# Maps every qualified sys_ustdint.X to the corresponding Interfaces.C name.
# With 'use Interfaces.C', these names are directly visible; only
# Extensions.unsigned_long_long still needs the Extensions. qualifier.
_SYS_USTDINT_EXPAND: dict[str, str] = {
    "sys_ustdint.uint8_t":           "unsigned_char",
    "sys_ustdint.uint16_t":          "unsigned_short",
    "sys_ustdint.uint32_t":          "unsigned_long",
    "sys_ustdint.uint64_t":          "Extensions.unsigned_long_long",
    "sys_ustdint.int8_t":            "signed_char",
    "sys_ustdint.int16_t":           "short",
    "sys_ustdint.int32_t":           "long",
    "sys_ustdint.int64_t":           "Long_Long_Integer",
    "sys_ustdint.intptr_t":          "int",
    "sys_ustdint.uintptr_t":         "unsigned",
    "sys_ustdint.intmax_t":          "Long_Long_Integer",
    "sys_ustdint.uintmax_t":         "Extensions.unsigned_long_long",
    # uu_* and _least_* variants (merged in from machine_udefault_types)
    "sys_ustdint.uu_int8_t":         "signed_char",
    "sys_ustdint.uu_uint8_t":        "unsigned_char",
    "sys_ustdint.uu_int16_t":        "short",
    "sys_ustdint.uu_uint16_t":       "unsigned_short",
    "sys_ustdint.uu_int32_t":        "long",
    "sys_ustdint.uu_uint32_t":       "unsigned_long",
    "sys_ustdint.uu_int64_t":        "Long_Long_Integer",
    "sys_ustdint.uu_uint64_t":       "Extensions.unsigned_long_long",
    "sys_ustdint.uu_int_least8_t":   "signed_char",
    "sys_ustdint.uu_uint_least8_t":  "unsigned_char",
    "sys_ustdint.uu_int_least16_t":  "short",
    "sys_ustdint.uu_uint_least16_t": "unsigned_short",
    "sys_ustdint.uu_int_least32_t":  "long",
    "sys_ustdint.uu_uint_least32_t": "unsigned_long",
    "sys_ustdint.uu_int_least64_t":  "Long_Long_Integer",
    "sys_ustdint.uu_uint_least64_t": "Extensions.unsigned_long_long",
    "sys_ustdint.uu_intmax_t":       "Long_Long_Integer",
    "sys_ustdint.uu_uintmax_t":      "Extensions.unsigned_long_long",
    "sys_ustdint.uu_intptr_t":       "int",
    "sys_ustdint.uu_uintptr_t":      "unsigned",
}


def _fix_variant_records(out_dir: Path) -> int:
    """Insert missing 'when 0 =>' before orphaned first variant arms.

    gcc -fdump-ada-spec sometimes emits a field declaration immediately after
    'case discr is' with no 'when X =>' guard, which is invalid Ada.
    """
    # Match 'case discr is\n<indent><field>' with no intervening 'when'.
    broken = re.compile(
        r"([ \t]*case\s+\w+\s+is\n)"   # the case line
        r"([ \t]+)(\w+\s*:)",           # field line with no 'when' guard
    )
    count = 0
    for f in sorted(out_dir.glob("*.ads")):
        text = f.read_text()
        new_text = broken.sub(lambda m: m.group(1) + m.group(2) + "when 0 =>\n" + m.group(2) + "   " + m.group(3), text)
        if new_text != text:
            f.write_text(new_text)
            count += 1
    return count


# ---------------------------------------------------------------------------
# Step 7: expand --  arg-macro: comment pairs into Ada expression functions
# ---------------------------------------------------------------------------

# Packages whose arg-macros are too complex or irrelevant to auto-expand.
_SKIP_ARGMACRO_PKGS: frozenset[str] = frozenset({"freertos_portmacro", "stddef"})


def _split_top_commas(s: str) -> list[str]:
    """Split s on commas at brace-depth 0."""
    parts: list[str] = []
    depth = 0
    buf: list[str] = []
    for ch in s:
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
        if ch == ',' and depth == 0:
            parts.append(''.join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    tail = ''.join(buf).strip()
    if tail:
        parts.append(tail)
    return parts


def _parse_struct_init(body: str) -> tuple[str | None, list[tuple[str, str]]]:
    """
    Parse a C struct-initializer body.
    Returns (explicit_type_or_None, [(field, value), ...]).
    Returns (None, []) if the body is not a struct initializer.
    """
    body = body.strip()
    explicit: str | None = None
    m = re.match(r'\(\s*(\w+)\s*\)\s*', body)
    if m:
        explicit = m.group(1)
        body = body[m.end():].strip()
    close = body.rfind('}')
    if not body.startswith('{') or close < 0:
        return None, []
    body = body[: close + 1]
    inner = body[1:-1].strip().rstrip(',').strip()
    pairs: list[tuple[str, str]] = []
    for part in _split_top_commas(inner):
        part = part.strip()
        fm = re.match(r'^\.(\w+)\s*:=\s*(.+)$', part, re.DOTALL)
        if fm:
            pairs.append((fm.group(1).strip(), fm.group(2).strip()))
    return explicit, pairs


def _is_complex_expr(value: str) -> bool:
    """Return True if a C expression value is too complex to auto-convert."""
    if '?' in value:
        return True
    if re.search(r'\d+\.\d+', value):  # floating-point literal
        return True
    if re.search(r'\(\s*[a-z]\w*\s*\)\s*[/*%]', value):  # arithmetic on a param
        return True
    return False


def _c_suffix(ada_name: str) -> str | None:
    """Extract the C constant name that an Ada constant name encodes."""
    if re.match(r'^[A-Z][A-Z0-9_]+$', ada_name):
        return ada_name
    idx = ada_name.find('_t_')
    if idx >= 0:
        suf = ada_name[idx + 3:]
        if suf:
            return suf
    return None


def _scan_file_constants(text: str) -> list[tuple[str, str]]:
    """Return [(ada_name, c_name)] for every constant declaration in text."""
    result: list[tuple[str, str]] = []
    for m in re.finditer(r'^\s+([A-Za-z]\w+)\s*:\s*constant\b', text, re.MULTILINE):
        ada = m.group(1)
        c = _c_suffix(ada)
        if c:
            result.append((ada, c))
    return result


def _build_id_maps(
    text: str, pkg_stem: str, all_texts: dict[str, str]
) -> tuple[dict[str, str], dict[str, tuple[str, str, bool]]]:
    """
    Build identifier resolution maps for a file.

    Returns:
      local:  c_name → ada_name (unqualified, same package)
      remote: c_name → (pkg_stem, ada_name, needs_new_with)
    """
    local: dict[str, str] = {}
    remote: dict[str, tuple[str, str, bool]] = {}

    # Unsupported macros in the current file — extract numeric values.
    for m in re.finditer(r'--\s+unsupported macro:\s+(\w+)\s+(.+)', text):
        name, expr = m.group(1), m.group(2).strip()
        stripped = re.sub(r'\([A-Za-z_]\w*\s*\)', '', expr).strip('() ')
        if re.match(r'^-?\d+$', stripped):
            local[name] = stripped

    # Constants declared in the current file.
    for ada, c in _scan_file_constants(text):
        if c not in local:
            local[c] = ada

    # Build the set of packages already directly imported.
    direct_set: set[str] = set(re.findall(r'^with\s+(\w+)\s*;', text, re.MULTILINE))
    seen: set[str] = set(direct_set)

    for imp in list(direct_set):
        imp_text = all_texts.get(imp, '')
        for ada, c in _scan_file_constants(imp_text):
            if c not in local and c not in remote:
                remote[c] = (imp, ada, False)  # direct import, no new 'with' needed
        for trans in re.findall(r'^with\s+(\w+)\s*;', imp_text, re.MULTILINE):
            if trans not in seen:
                seen.add(trans)
                trans_text = all_texts.get(trans, '')
                for ada, c in _scan_file_constants(trans_text):
                    if c not in local and c not in remote:
                        remote[c] = (trans, ada, True)  # transitive — will need 'with'

    return local, remote


def _global_fallback(
    c_name: str, all_texts: dict[str, str], pkg_stem: str
) -> tuple[str, str] | None:
    """Search all packages for c_name. Return (pkg, ada_name) only if unique."""
    found: list[tuple[str, str]] = []
    for stem, text in all_texts.items():
        if stem == pkg_stem:
            continue
        for ada, c in _scan_file_constants(text):
            if c == c_name:
                found.append((stem, ada))
    return found[0] if len(found) == 1 else None


def _resolve_id(
    name: str,
    params: set[str],
    local: dict[str, str],
    remote: dict[str, tuple[str, str, bool]],
    all_texts: dict[str, str],
    pkg_stem: str,
) -> tuple[str, str | None]:
    """
    Resolve a C identifier to its Ada form.
    Returns (ada_expr, new_with_package_or_None).
    """
    if name == 'false':
        return 'False', None
    if name == 'true':
        return 'True', None
    if name in params:
        return name, None
    if name in local:
        return local[name], None
    if name in remote:
        pkg, ada, needs_with = remote[name]
        return f'{pkg}.{ada}', pkg if needs_with else None
    r = _global_fallback(name, all_texts, pkg_stem)
    if r:
        pkg, ada = r
        return f'{pkg}.{ada}', pkg
    return name, None  # unknown — leave as-is


def _extract_records(text: str) -> dict[str, list[tuple[str, str]]]:
    """Return {type_name: [(field_name, ada_type)]} for every record in text."""
    records: dict[str, list[tuple[str, str]]] = {}
    for m in re.finditer(
        r'type\s+(\w+)(?:\s*\([^)]*\))?\s+is\s+record\b(.*?)end\s+record\b',
        text, re.DOTALL,
    ):
        name = m.group(1)
        body = m.group(2)
        fields: list[tuple[str, str]] = []
        for fm in re.finditer(
            r'^\s{6,}(\w+)\s*:\s*(?:aliased\s+)?(.+?)\s*;',
            body, re.MULTILINE,
        ):
            fn = fm.group(1)
            ft = fm.group(2).strip()
            if fn not in ('when', 'case', 'discr', 'end', 'record'):
                fields.append((fn, ft))
        records[name] = fields
    return records


def _extract_subprograms(text: str) -> dict[str, dict]:
    """Extract declared subprograms: {name: {kind, params: [(n,t)], ret}}."""
    result: dict[str, dict] = {}
    for m in re.finditer(r'^\s+(function|procedure)\s+(\w+)\b', text, re.MULTILINE):
        kind = m.group(1)
        name = m.group(2)
        rest = text[m.end():]
        params: list[tuple[str, str]] = []
        ret: str | None = None
        pm = re.match(r'\s*\n?\s*\(([^)]*)\)', rest)
        if not pm:
            pm = re.match(r'\s*\(([^)]*)\)', rest)
        if pm:
            for prm in re.finditer(
                r'(\w+)\s*:\s*(?:access\s+(?:constant\s+)?)?([^;]+?)(?:;|$)',
                pm.group(1),
            ):
                params.append((prm.group(1).strip(), prm.group(2).strip()))
            rest = rest[pm.end():]
        rm = re.match(r'\s*(?:\n\s*)?\breturn\s+([\w.]+)', rest)
        if rm:
            ret = rm.group(1)
        result[name] = {'kind': kind, 'params': params, 'ret': ret}
    return result


def _find_return_type(
    field_names: list[str],
    records: dict[str, list[tuple[str, str]]],
) -> str | None:
    """Find the first non-anonymous record whose fields contain all given names."""
    fset = set(field_names)
    if not fset:
        return None
    for name, fields in records.items():
        if name.startswith('anon_'):
            continue
        if fset <= {f[0] for f in fields}:
            return name
    return None


def _convert_nested(
    value: str,
    field_type: str,
    records: dict[str, list[tuple[str, str]]],
) -> str:
    """Convert a nested C struct initializer  { ... }  to an Ada aggregate."""
    inner = value[1:-1].strip().rstrip(',').strip()
    _, named = _parse_struct_init(value)
    if named:
        pairs = [f'{fn} => {fv}' for fn, fv in named]
        return '(' + ', '.join(pairs) + ')'
    # Plain value like {0} — look up the nested type.
    lookup = re.sub(r'\b(?:aliased|access)\b\s*', '', field_type).strip().split('.')[-1]
    if lookup in records:
        flds = [(fn, ft) for fn, ft in records[lookup] if not fn.startswith('anon')]
        if len(flds) == 1:
            return f'({flds[0][0]} => {inner})'
        if flds:
            return '(' + ', '.join(f'{fn} => {inner}' for fn, _ in flds) + ')'
    return f'({inner})'


def _convert_struct_init(
    fields: list[tuple[str, str]],
    ret_type: str,
    params: set[str],
    rec_fields: dict[str, str],
    records: dict[str, list[tuple[str, str]]],
    local: dict[str, str],
    remote: dict[str, tuple[str, str, bool]],
    all_texts: dict[str, str],
    pkg_stem: str,
) -> tuple[str, set[str]]:
    """
    Generate an Ada qualified aggregate expression for a struct initializer.
    Returns (aggregate_str, new_with_packages).
    """
    new_withs: set[str] = set()
    pairs: list[str] = []
    for fname, fval in fields:
        fval = fval.strip()
        if fval.startswith('{'):
            ftype = rec_fields.get(fname, '')
            ada_val = _convert_nested(fval, ftype, records)
        elif fval == 'false':
            ada_val = 'False'
        elif fval == 'true':
            ada_val = 'True'
        else:
            # Resolve all uppercase identifiers in the value.
            ada_val = fval
            for ident in re.findall(r'\b([A-Z][A-Z0-9_]+)\b', fval):
                if ident not in params:
                    resolved, new_w = _resolve_id(ident, params, local, remote, all_texts, pkg_stem)
                    if resolved != ident:
                        ada_val = re.sub(r'\b' + re.escape(ident) + r'\b', resolved, ada_val)
                    if new_w:
                        new_withs.add(new_w)
            ada_val = re.sub(r'\bfalse\b', 'False', ada_val)
            ada_val = re.sub(r'\btrue\b', 'True', ada_val)
        pairs.append(f'{fname} => {ada_val}')
    return f"{ret_type}'({', '.join(pairs)})", new_withs


def _gen_expr_func(
    name: str,
    params: list[str],
    param_types: dict[str, str],
    ret: str,
    body: str,
    indent: str = '   ',
) -> str:
    """Generate an Ada expression function declaration."""
    if params:
        param_list = '; '.join(f'{p} : {param_types.get(p, "int")}' for p in params)
        return (
            f'{indent}function {name}\n'
            f'{indent}  ({param_list})\n'
            f'{indent}  return {ret} is\n'
            f'{indent}  ({body});'
        )
    return f'{indent}function {name} return {ret} is\n{indent}  ({body});'


def _gen_rename(
    kind: str,
    name: str,
    params: list[tuple[str, str]],
    ret: str | None,
    target: str,
    indent: str = '   ',
) -> str:
    """Generate a subprogram renaming declaration."""
    param_list = '; '.join(f'{n} : {t}' for n, t in params) if params else ''
    sig = f'{name} ({param_list})' if param_list else name
    if kind == 'function' and ret:
        return f'{indent}function {sig} return {ret} renames {target};'
    return f'{indent}procedure {sig} renames {target};'


def _insert_after_type(text: str, type_name: str, new_code: str) -> str:
    """Insert new_code immediately after the record type declaration of type_name."""
    type_m = re.search(
        r'^\s+type\s+' + re.escape(type_name) + r'\b',
        text, re.MULTILINE,
    )
    if not type_m:
        end_m = re.search(r'^end\s+\w+\s*;', text, re.MULTILINE)
        if end_m:
            return text[: end_m.start()] + new_code + '\n\n' + text[end_m.start():]
        return text + '\n\n' + new_code

    end_rec = re.search(r'\bend\s+record\b', text[type_m.start():])
    if not end_rec:
        return text
    abs_end = type_m.start() + end_rec.end()

    sc = re.search(r';', text[abs_end:])
    insert_pos = abs_end + sc.end() if sc else abs_end
    return text[:insert_pos] + '\n\n' + new_code + text[insert_pos:]


def _process_file_arg_macros(
    text: str,
    pkg_stem: str,
    all_texts: dict[str, str],
) -> str:
    """Expand arg-macro comment pairs in one .ads file into Ada declarations."""
    if pkg_stem in _SKIP_ARGMACRO_PKGS or '--  arg-macro:' not in text:
        return text

    macro_re = re.compile(
        r'[ \t]*--\s+arg-macro:\s+(?:procedure|function)\s+(\w+)\s*\(([^)]*)\)\n'
        r'[ \t]*--[ \t]+([^\n]*)',
    )
    macro_pairs: list[tuple[str, list[str], str]] = []
    for m in macro_re.finditer(text):
        name = m.group(1)
        params_str = m.group(2).strip()
        body = m.group(3).strip()
        params = [p.strip() for p in params_str.split(',') if p.strip()]
        macro_pairs.append((name, params, body))

    if not macro_pairs:
        return text

    records = _extract_records(text)
    subprograms = _extract_subprograms(text)
    local, remote = _build_id_maps(text, pkg_stem, all_texts)

    # Signatures of successfully generated macro functions (for delegate resolution).
    gen_sigs: dict[str, tuple[list[str], dict[str, str], str]] = {}

    generated: list[tuple[str | None, str]] = []  # (ret_type_or_None, ada_code)
    new_withs_all: set[str] = set()

    for name, params, body in macro_pairs:
        body = body.strip()
        params_set = set(params)

        explicit_type, fields = _parse_struct_init(body)

        if fields or explicit_type:
            # ---- struct initializer ----
            if any(_is_complex_expr(v) for _, v in fields):
                continue

            if explicit_type:
                ret_type = explicit_type
            else:
                ret_type = _find_return_type([f for f, _ in fields], records)
                if not ret_type:
                    continue

            rec_fields = {fn: ft for fn, ft in records.get(ret_type, [])}

            # Infer parameter types from field assignments.
            param_types: dict[str, str] = {}
            for fname, fval in fields:
                stripped = re.sub(r'^\(\s*(\w+)\s*\)$', r'\1', fval.strip())
                if stripped in params_set and stripped not in param_types:
                    ft = rec_fields.get(fname, 'int')
                    param_types[stripped] = re.sub(r'\b(?:aliased|access)\b\s*', '', ft).strip()
            for p in params:
                if p not in param_types:
                    param_types[p] = 'int'

            agg, new_withs = _convert_struct_init(
                fields, ret_type, params_set, rec_fields, records,
                local, remote, all_texts, pkg_stem,
            )
            new_withs_all.update(new_withs)
            code = _gen_expr_func(name, params, param_types, ret_type, agg)
            gen_sigs[name] = (params, param_types, ret_type)
            generated.append((ret_type, code))

        elif re.match(r'^(\w+)\s*\(', body):
            # ---- call expression (delegate or wrapper) ----
            call_m = re.match(r'^(\w+)\s*\(([^)]*)\)', body)
            if not call_m:
                continue
            called = call_m.group(1)
            call_args = [a.strip() for a in call_m.group(2).split(',') if a.strip()]

            if called in gen_sigs:
                c_params, c_ptypes, c_ret = gen_sigs[called]
                param_types = {}
                for i, arg in enumerate(call_args):
                    stripped = re.sub(r'^\(\s*(\w+)\s*\)$', r'\1', arg)
                    if stripped in params_set and stripped not in param_types and i < len(c_params):
                        param_types[stripped] = c_ptypes.get(c_params[i], 'int')
                for p in params:
                    if p not in param_types:
                        param_types[p] = 'int'
                code = _gen_expr_func(
                    name, params, param_types, c_ret,
                    f"{called}({', '.join(call_args)})",
                )
                gen_sigs[name] = (params, param_types, c_ret)
                generated.append((c_ret, code))

            elif called in subprograms:
                sub = subprograms[called]
                sub_params = sub['params']
                ret = sub['ret']
                exact = (call_args == params)
                if sub['kind'] == 'procedure':
                    code = _gen_rename('procedure', name, sub_params, None, called)
                    generated.append((None, code))
                else:
                    if exact and ret:
                        code = _gen_rename('function', name, sub_params, ret, called)
                    else:
                        ptypes = {n: t for n, t in sub_params}
                        code = _gen_expr_func(
                            name, params, ptypes, ret or 'int',
                            f"{called}({', '.join(call_args)})",
                        )
                    gen_sigs[name] = (params, {n: t for n, t in sub_params}, ret or 'int')
                    generated.append((None, code))

    if not generated:
        return text

    # Remove all arg-macro comment pairs.
    text = re.sub(
        r'[ \t]*--\s+arg-macro:[^\n]*\n[ \t]*--[ \t]+[^\n]*\n',
        '', text,
    )

    # Insert generated code after the relevant record type declarations.
    type_groups: dict[str | None, list[str]] = {}
    for ret_type, code in generated:
        type_groups.setdefault(ret_type, []).append(code)

    for ret_type, codes in type_groups.items():
        block = '\n\n'.join(codes)
        if ret_type is None:
            end_m = re.search(r'^end\s+\w+\s*;', text, re.MULTILINE)
            if end_m:
                text = text[: end_m.start()] + block + '\n\n' + text[end_m.start():]
        else:
            text = _insert_after_type(text, ret_type, block)

    # Add any new with-clauses that were needed.
    existing_withs = set(re.findall(r'^with\s+(\w+)\s*;', text, re.MULTILINE))
    new_to_add = sorted(new_withs_all - existing_withs)
    if new_to_add:
        pkg_m = re.search(r'^package\s+', text, re.MULTILINE)
        if pkg_m:
            clauses = '\n'.join(f'with {w};' for w in new_to_add) + '\n'
            text = text[: pkg_m.start()] + clauses + text[pkg_m.start():]

    return text


def _expand_arg_macros(out_dir: Path) -> None:
    """Step 7: convert --  arg-macro: comment pairs to Ada expression functions."""
    all_texts = {f.stem: f.read_text() for f in sorted(out_dir.glob('*.ads'))}
    count = 0
    for f in sorted(out_dir.glob('*.ads')):
        if '--  arg-macro:' not in all_texts.get(f.stem, ''):
            continue
        new_text = _process_file_arg_macros(all_texts[f.stem], f.stem, all_texts)
        if new_text != all_texts[f.stem]:
            f.write_text(new_text)
            count += 1
    print(f'  {count} files had arg-macros expanded.')


# ---------------------------------------------------------------------------
# Step 8: convert pseudo-enum subtypes to proper Ada enumeration types
# ---------------------------------------------------------------------------

def _is_bitmask_values(values: list[int]) -> bool:
    """Return True if there are 3+ distinct non-zero values and all are powers of two."""
    non_zero = list({v for v in values if v != 0})
    return len(non_zero) >= 3 and all(v > 0 and (v & (v - 1)) == 0 for v in non_zero)


def _gen_modular_type_decl(
    type_name: str,
    entries: list[tuple[str, str, int]],
    indent: str,
) -> str:
    """Generate Ada modular type + named constants (for bitmask C enums)."""
    lines = [
        f'{indent}type {type_name} is mod 2**32',
        f'{indent}with Convention => C;',
    ]
    for _, literal, value in entries:
        lines.append(f'{indent}{literal} : constant {type_name} := {value};')
    return '\n'.join(lines)


def _gen_enum_type_decl(
    type_name: str,
    canonical: list[tuple[str, int]],
    duplicates: list[tuple[str, int]],
    indent: str,
) -> str:
    """Generate Ada enum type + representation clause + duplicate alias constants."""
    lits = [lit for lit, _ in canonical]
    vals = [val for _, val in canonical]
    n = len(lits)
    lines = []

    lines.append(f'{indent}type {type_name} is')
    for i, lit in enumerate(lits):
        if n == 1:
            lines.append(f'{indent}  ({lit})')
        elif i == 0:
            lines.append(f'{indent}  ({lit},')
        elif i == n - 1:
            lines.append(f'{indent}   {lit})')
        else:
            lines.append(f'{indent}   {lit},')
    lines.append(f'{indent}with Convention => C;')

    # Representation clause — omit when values are already 0, 1, 2, ..., n-1.
    if vals != list(range(n)):
        max_lit_len = max(len(lit) for lit in lits)
        lines.append(f'{indent}for {type_name} use')
        for i, (lit, val) in enumerate(canonical):
            pad = ' ' * (max_lit_len - len(lit))
            trailer = ');' if i == n - 1 else ','
            if i == 0:
                lines.append(f'{indent}  ({lit}{pad} => {val}{trailer}')
            else:
                lines.append(f'{indent}   {lit}{pad} => {val}{trailer}')

    value_to_lit = {val: lit for lit, val in canonical}
    for alias_lit, dup_val in duplicates:
        canon_lit = value_to_lit.get(dup_val, lits[0])
        lines.append(f'{indent}{alias_lit} : constant {type_name} := {canon_lit};')

    return '\n'.join(lines)


def _convert_pseudo_enums_in_file(text: str) -> tuple[str, dict[str, str]]:
    """
    Convert 'subtype T is unsigned' + constant blocks to proper Ada enum types.
    Returns (new_text, rename_map) mapping old Ada constant names to new literal names.
    """
    rename_map: dict[str, str] = {}

    subtype_re = re.compile(
        r'^([ \t]*)subtype\s+(\w+)\s+is\s+unsigned\s*;',
        re.MULTILINE,
    )
    conversions: list[tuple[str, str, list[tuple[str, str, int]]]] = []

    for sm in subtype_re.finditer(text):
        indent = sm.group(1)
        type_name = sm.group(2)

        const_re = re.compile(
            r'^[ \t]*(' + re.escape(type_name) + r'_(\w+))\s*:\s*constant\s+'
            + re.escape(type_name) + r'\s*:=\s*(-?\d+)\s*;',
            re.MULTILINE,
        )
        entries: list[tuple[str, str, int]] = []
        for cm in const_re.finditer(text):
            entries.append((cm.group(1), cm.group(2), int(cm.group(3))))

        if not entries:
            continue

        values = [v for _, _, v in entries]

        # Skip if any value is outside 32-bit signed range.
        if any(v > 2_147_483_647 or v < -2_147_483_648 for v in values):
            continue

        conversions.append((indent, type_name, entries, _is_bitmask_values(values)))

    if not conversions:
        return text, {}

    for indent, type_name, entries, is_bitmask in conversions:
        for full_name, literal, _ in entries:
            rename_map[full_name] = literal

        if is_bitmask:
            new_decl = _gen_modular_type_decl(type_name, entries, indent)
        else:
            seen: dict[int, str] = {}
            canonical: list[tuple[str, int]] = []
            duplicates: list[tuple[str, int]] = []
            for _, literal, value in entries:
                if value not in seen:
                    seen[value] = literal
                    canonical.append((literal, value))
                else:
                    duplicates.append((literal, value))

            canonical.sort(key=lambda x: x[1])
            new_decl = _gen_enum_type_decl(type_name, canonical, duplicates, indent)

        text = re.sub(
            r'^[ \t]*subtype\s+' + re.escape(type_name) + r'\s+is\s+unsigned\s*;\n',
            new_decl + '\n',
            text, flags=re.MULTILINE,
        )
        text = re.sub(
            r'^[ \t]*' + re.escape(type_name) + r'_\w+\s*:\s*constant\s+'
            + re.escape(type_name) + r'\s*:=\s*-?\d+\s*;\n',
            '',
            text, flags=re.MULTILINE,
        )

    return text, rename_map


def _expand_pseudo_enums(out_dir: Path) -> int:
    """Step 8: convert pseudo-enum subtypes to proper Ada enumeration types."""
    all_renames: dict[str, str] = {}
    changed = 0

    for f in sorted(out_dir.glob('*.ads')):
        text = f.read_text()
        new_text, file_renames = _convert_pseudo_enums_in_file(text)
        all_renames.update(file_renames)
        if new_text != text:
            f.write_text(new_text)
            changed += 1

    # Update cross-file references: old T_X constant names -> new enum literal names.
    if all_renames:
        rename_pairs = sorted(all_renames.items(), key=lambda x: len(x[0]), reverse=True)
        pattern = re.compile(
            r'(?<![A-Za-z0-9_])('
            + '|'.join(re.escape(old) for old, _ in rename_pairs)
            + r')(?![A-Za-z0-9_])',
        )
        lut = dict(rename_pairs)
        for f in sorted(out_dir.glob('*.ads')):
            text = f.read_text()
            new_text = pattern.sub(lambda m: lut[m.group(1)], text)
            if new_text != text:
                f.write_text(new_text)

    print(f'  {changed} files had pseudo-enums converted to proper Ada enum types.')
    return changed


def _expand_stdint(out_dir: Path) -> None:
    """Replace sys_ustdint.T with direct Interfaces.C equivalents in all files."""
    pairs = sorted(_SYS_USTDINT_EXPAND.items(), key=lambda x: len(x[0]), reverse=True)
    pattern = re.compile("|".join(re.escape(old) for old, _ in pairs))
    lut = dict(pairs)
    ext_clause = "with Interfaces.C.Extensions;"

    changed = 0
    for f in sorted(out_dir.glob("*.ads")):
        text = f.read_text()
        new_text = pattern.sub(lambda m: lut[m.group(0)], text)
        if new_text == text:
            continue

        new_text = re.sub(r"^with\s+sys_ustdint\s*;\n", "", new_text, flags=re.MULTILINE)

        # Add 'with Interfaces.C.Extensions;' if needed and not already present.
        if "Extensions.unsigned_long_long" in new_text and ext_clause not in new_text:
            m = re.search(r"^package\s+", new_text, re.MULTILINE)
            if m:
                new_text = new_text[: m.start()] + ext_clause + "\n" + new_text[m.start():]

        f.write_text(new_text)
        changed += 1

    for stem in ("sys_ustdint", "stdint"):
        (out_dir / (stem + ".ads")).unlink(missing_ok=True)

    print(f"  {changed} files expanded sys_ustdint -> Interfaces.C; sys_ustdint.ads deleted.")


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
