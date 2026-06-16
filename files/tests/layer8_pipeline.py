"""
tests/layer8_pipeline.py — Full executor probe, context gate bypassed.

All calls go through _sanitise_args → executor only. gate_check is skipped
so we see raw executor behaviour for every arg permutation.

Categories:
  VALID   — well-formed args, should succeed
  BROKEN  — all required fields present, values are wrong / nonexistent
  MISSING — one or more required fields absent or empty; without the gate
             the executor itself sees incomplete input

Known constraints baked into test design:
  · hello2 and office have valid config.json — used for config-dependent ops
  · uwuntu directory exists but config.json is missing (pre-existing data bug)
  · stop_vm / launch_vm live calls are state-dependent (excluded from suite)
  · delete_vm VALID case omitted — irreversible and needs dedicated VM setup
  · snapshot_restore / snapshot_delete VALID omitted — need existing snapshot
  · Tests marked expect_success=None are state-dependent; runner records
    result but does not fail the suite on them
  · VMs / profiles / networks created during VALID tests are prefixed
    "probe8_" and cleaned up by cleanup_probe_artifacts() after the run
"""

import os, random, time, traceback
from typing import Any, Dict, List, Optional, Tuple

from .shared import PipelineTest, TestResult, execute_tool


# ── Layer detection ────────────────────────────────────────────────────────────

def _detect_layer(result: Any) -> str:
    """
    Gate returns:  {"clarify": True, "missing": [...]}  — no success key
    Clarify tool:  {"clarify": True, "question": ...}   — no success key, no missing
    Executor fail: {"success": False, ...}               — has success key
    Executor ok / key-only tools:                       → "ok"
    """
    if isinstance(result, list):
        return "ok"
    if not isinstance(result, dict):
        return "ok"
    if result.get("clarify") and "missing" in result and "success" not in result:
        return "context_gate"
    if result.get("success") is False:
        return "executor"
    return "ok"


# ── Shorthand builders ─────────────────────────────────────────────────────────

def _uid(args: dict) -> str:
    vals = "_".join(str(v) for v in list(args.values())[:2])
    return vals.replace("/","").replace(" ","_").replace("'","")[:28]

def _v(tool, args, note="", keys=None):
    return PipelineTest(
        id=f"p8_valid_{tool}_{_uid(args)}",
        tags=["pipeline", "valid", tool],
        description=note or f"{tool} valid",
        tool=tool, input_args=args, category="valid",
        expect_success=True, expect_layer="ok",
        expect_result_keys=keys or [],
    )

def _vk(tool, args, keys, note=""):
    """Valid test that checks keys instead of success flag (tool returns no success key)."""
    return PipelineTest(
        id=f"p8_valid_{tool}_{_uid(args)}",
        tags=["pipeline", "valid", tool],
        description=note or f"{tool} valid (key check)",
        tool=tool, input_args=args, category="valid",
        expect_success=None, expect_layer="ok",
        expect_result_keys=keys,
    )

def _vs(tool, args, note=""):
    """Valid but state-dependent — don't assert success."""
    return PipelineTest(
        id=f"p8_valid_{tool}_{_uid(args)}",
        tags=["pipeline", "valid", "state_dep", tool],
        description=f"[state-dep] {note or tool}",
        tool=tool, input_args=args, category="valid",
        expect_success=None, expect_layer=None,
    )

def _b(tool, args, note=""):
    return PipelineTest(
        id=f"p8_broken_{tool}_{_uid(args)}",
        tags=["pipeline", "broken", tool],
        description=note or f"{tool} broken",
        tool=tool, input_args=args, category="broken",
        expect_success=False, expect_layer="executor",
    )

def _bs(tool, args, note=""):
    """Broken input but sanitizer corrects it — expect success."""
    return PipelineTest(
        id=f"p8_sanitized_{tool}_{_uid(args)}",
        tags=["pipeline", "sanitized", tool],
        description=f"[sanitizer fixes] {note or tool}",
        tool=tool, input_args=args, category="broken",
        expect_success=True, expect_layer="ok",
    )

def _m(tool, args, note=""):
    return PipelineTest(
        id=f"p8_missing_{tool}_{_uid(args)}",
        tags=["pipeline", "missing", tool],
        description=note or f"{tool} missing args (no gate)",
        tool=tool, input_args=args, category="missing",
        expect_success=False, expect_layer="executor",
    )


# ── Fixed test cases ───────────────────────────────────────────────────────────

PIPELINE_TESTS: List[PipelineTest] = [

    # ── revert ────────────────────────────────────────────────────────────────
    _b ("revert", {}, "no prior reversible action — returns 'nothing to revert'"),

    # ── clarify — returns {clarify, question, options}, no success key ────────
    # expect_layer=None because _detect_layer can't distinguish clarify TOOL from gate
    PipelineTest(id="p8_valid_clarify_q_opts",   tags=["pipeline","valid","clarify"],  description="clarify with options",         tool="clarify", input_args={"question":"Which OS?","options":["linux","windows"]}, category="valid",  expect_success=None, expect_layer=None, expect_result_keys=["question","options"]),
    PipelineTest(id="p8_valid_clarify_q_only",   tags=["pipeline","valid","clarify"],  description="clarify no options field",      tool="clarify", input_args={"question":"Which OS?"},                               category="valid",  expect_success=None, expect_layer=None, expect_result_keys=["question"]),
    PipelineTest(id="p8_valid_clarify_empty_q",  tags=["pipeline","valid","clarify"],  description="clarify empty question string", tool="clarify", input_args={"question":""},                                        category="valid",  expect_success=None, expect_layer=None, expect_result_keys=["question"]),
    PipelineTest(id="p8_missing_clarify_no_q",   tags=["pipeline","missing","clarify"],description="no question key — returns clarify dict without success",    tool="clarify", input_args={},                                                    category="missing",expect_success=None,expect_layer=None, expect_result_keys=["clarify"]),

    # ── zero-arg tools ────────────────────────────────────────────────────────
    PipelineTest(
        id="p8_valid_check_system", tags=["pipeline","valid","check_system"],
        description="check_system returns host capability dict",
        tool="check_system", input_args={}, category="valid",
        expect_success=None, expect_layer="ok",
        expect_result_keys=["kvm_available","qemu_installed","host_cpu"],
    ),
    _v ("scan_isos",    {}, "returns list of ISOs"),
    _v ("list_vms",     {}, "returns list of VMs"),
    _v ("list_profiles",{}, "returns profile list"),
    _v ("list_networks",{}, "returns network list"),

    # ── check_profile_compatibility — returns {compatible, ...}, no success key ──
    _vk("check_profile_compatibility", {"profile_name":"raspberry_pi_3b"}, ["compatible","warnings"]),
    _vk("check_profile_compatibility", {"profile_name":"minimal"},          ["compatible"]),
    _vk("check_profile_compatibility", {"profile_name":"ghost_xyz"},        ["compatible"],    "nonexistent profile — returns compatible:false, no success key"),
    PipelineTest(id="p8_missing_check_compat_empty", tags=["pipeline","missing","check_profile_compatibility"], description="empty profile_name — returns compatible:False without success key", tool="check_profile_compatibility", input_args={"profile_name":""}, category="missing", expect_success=None, expect_layer=None, expect_result_keys=["compatible"]),
    PipelineTest(id="p8_missing_check_compat_none",  tags=["pipeline","missing","check_profile_compatibility"], description="no profile_name key", tool="check_profile_compatibility", input_args={},               category="missing", expect_success=False, expect_layer="executor"),

    # ── create_profile ────────────────────────────────────────────────────────
    _v ("create_profile", {"profile_name":"probe8_base",   "description":"base","memory_mb":2048},            "minimal with required hardware field"),
    _v ("create_profile", {"profile_name":"probe8_cpu",    "description":"cpu","cpu_cores":4,"cpu_threads":2,"memory_mb":8192}, "CPU cluster"),
    _v ("create_profile", {"profile_name":"probe8_disp",   "description":"disp","display":"sdl","gpu":"virtio","audio":"hda"}, "display cluster"),
    _v ("create_profile", {"profile_name":"probe8_uefi",   "description":"uefi","uefi":True,"bios":"ovmf"},   "boot cluster"),
    _v ("create_profile", {"profile_name":"probe8_smbios", "description":"smbios","manufacturer":"Dell","product_name":"XPS 15","bios_vendor":"Dell Inc.","bios_version":"1.2.3"}, "SMBIOS cluster"),
    _v ("create_profile", {"profile_name":"probe8_laptop", "description":"laptop","machine_class":"laptop","battery":True}, "laptop class"),
    _v ("create_profile", {"profile_name":"probe8_arm",    "description":"arm","machine_arch":"aarch64","qemu_binary":"qemu-system-aarch64","machine_type":"virt","cpu_model":"cortex-a72","force":True}, "ARM profile (force=True to overwrite if leftover from prior run)"),
    _v ("create_profile", {"profile_name":"probe8_notes",  "description":"notes","notes":"a test profile","cpu_cores":2}, "with notes field"),
    _v ("create_profile", {"profile_name":"probe8_force",  "description":"force","cpu_model":"cortex-a72","machine_arch":"x86_64","force":True}, "ARM CPU on x86 + force=True"),
    _bs("create_profile", {"profile_name":"probe8_badcpu", "description":"bad","cpu_model":"cortex-a72","machine_arch":"x86_64"},  "ARM CPU on x86 — sanitizer converts arch to aarch64, profile saves"),
    _bs("create_profile", {"profile_name":"probe8_bdisp",  "description":"bad","display":"foobar"},          "invalid display — sanitizer resets to default, profile saves"),
    _bs("create_profile", {"profile_name":"probe8_baudio", "description":"bad","audio":"foobar"},            "invalid audio — sanitizer resets"),
    _bs("create_profile", {"profile_name":"probe8_bgpu",   "description":"bad","gpu":"foobar"},              "invalid gpu — sanitizer resets"),
    _bs("create_profile", {"profile_name":"probe8_bmem",   "description":"bad","memory_mb":-1},              "negative memory — sanitizer clamps to min"),
    _bs("create_profile", {"profile_name":"probe8_barch",  "description":"bad","machine_arch":"foobar"},     "invalid arch — sanitizer resets to default"),
    _m ("create_profile", {"profile_name":"probe8_nodesc"},                                                   "no description"),
    _m ("create_profile", {"description":"test"},                                                             "no profile_name"),
    _m ("create_profile", {},                                                                                 "both missing"),

    # ── delete_profile ────────────────────────────────────────────────────────
    _v ("delete_profile", {"profile_name":"probe8_base"},  "delete profile created above"),
    _b ("delete_profile", {"profile_name":"ghost_xyz"},    "profile doesn't exist"),
    _m ("delete_profile", {},                              "no profile_name"),
    _m ("delete_profile", {"profile_name":""},             "empty profile_name"),

    # ── create_vm ─────────────────────────────────────────────────────────────
    _v ("create_vm", {"name":"probe8_min",    "os_type":"linux"},                                              "bare minimum"),
    _v ("create_vm", {"name":"probe8_cpu",    "os_type":"linux","memory_mb":4096,"cpu_cores":4,"cpu_threads":2}, "CPU+RAM cluster"),
    _v ("create_vm", {"name":"probe8_disk",   "os_type":"linux","disk_size_gb":60,"disk_format":"qcow2"},      "disk cluster"),
    _v ("create_vm", {"name":"probe8_disp",   "os_type":"linux","display":"sdl","gpu":"virtio","audio":"hda"}, "display cluster"),
    _v ("create_vm", {"name":"probe8_net_u",  "os_type":"linux","network_mode":"user"},                        "user networking"),
    _v ("create_vm", {"name":"probe8_net_b",  "os_type":"linux","network_mode":"bridge","bridge_iface":"virbr0"}, "bridge networking"),
    _v ("create_vm", {"name":"probe8_mac",    "os_type":"linux","mac_address":"52:54:00:ab:cd:ef"},            "custom MAC"),
    _v ("create_vm", {"name":"probe8_uefi",   "os_type":"linux","uefi":True,"kvm":True},                      "UEFI + KVM"),
    _v ("create_vm", {"name":"probe8_huge",   "os_type":"linux","hugepages":True,"kvm":True},                  "hugepages"),
    _v ("create_vm", {"name":"probe8_batt",   "os_type":"linux","battery":True,"machine_type":"q35"},          "battery + q35"),
    _v ("create_vm", {"name":"probe8_cpu2",   "os_type":"linux","cpu_model":"host","machine_type":"q35"},      "explicit CPU model"),
    _v ("create_vm", {"name":"probe8_smbios", "os_type":"linux","manufacturer":"Dell","product_name":"XPS 15"}, "SMBIOS fields"),
    _v ("create_vm", {"name":"probe8_osname", "os_type":"linux","os_name":"ubuntu"},                           "os_name for ISO auto-find"),
    _v ("create_vm", {"name":"probe8_win",    "os_type":"windows","memory_mb":8192,"disk_size_gb":80},         "windows — auto-sets uefi+q35"),
    _v ("create_vm", {"name":"probe8_prof",   "os_type":"linux","profile":"minimal"},                          "apply profile"),
    _v ("create_vm", {"name":"probe8_args",   "os_type":"linux","extra_args":["-nographic"]},                  "extra_args list"),
    _v ("create_vm", {"name":"probe8_desc",   "os_type":"linux","description":"my test vm"},                   "with description"),
    _b ("create_vm", {"name":"windows-vm",    "os_type":"windows"},                                            "placeholder name — preflight ask_user"),
    _bs("create_vm", {"name":"probe8_armcpu", "os_type":"linux","cpu_model":"cortex-a72"},                     "ARM CPU on x86 — sanitizer resets cpu_model to 'host'"),
    _bs("create_vm", {"name":"probe8_bdisp",  "os_type":"linux","display":"foobar"},                           "invalid display — sanitizer resets to default"),
    _bs("create_vm", {"name":"probe8_baudio", "os_type":"linux","audio":"foobar"},                             "invalid audio — sanitizer resets"),
    _bs("create_vm", {"name":"probe8_bnet",   "os_type":"linux","network_mode":"foobar"},                      "invalid network mode — sanitizer resets"),
    _bs("create_vm", {"name":"probe8_bfmt",   "os_type":"linux","disk_format":"foobar"},                       "invalid disk format — sanitizer resets"),
    _bs("create_vm", {"name":"probe8_bmac",   "os_type":"linux","mac_address":"not-a-mac"},                    "invalid MAC — sanitizer strips, VM created without custom MAC"),
    _bs("create_vm", {"name":"probe8_bmem",   "os_type":"linux","memory_mb":999999999},                        "exceeds host RAM — sanitizer clamps"),
    _bs("create_vm", {"name":"probe8_bcores", "os_type":"linux","cpu_cores":9999},                             "exceeds host cores — sanitizer clamps"),
    _bs("create_vm", {"name":"probe8_bbr",    "os_type":"linux","bridge_iface":"eth0"},                        "raw ethernet iface — sanitizer replaces with default bridge"),
    _bs("create_vm", {"name":"probe8_biso",   "os_type":"linux","iso_path":"/home/fakeuser/fake.iso"},         "hallucinated ISO path — sanitizer resolves or clears"),
    _m ("create_vm", {"os_type":"linux"},                                                                      "no name — executor has own check, returns clarify"),
    _vs("create_vm", {"name":"probe8_noos"},                                                                   "no os_type — executor picks a default and succeeds"),
    _m ("create_vm", {},                                                                                       "both missing — executor catches missing name"),

    # ── clone_vm ──────────────────────────────────────────────────────────────
    _v ("clone_vm", {"source_name":"probe8_cpu",    "new_name":"probe8_clone"},  "clone existing VM with config"),
    _b ("clone_vm", {"source_name":"ghost_xyz", "new_name":"clone"},         "source doesn't exist"),
    _m ("clone_vm", {"source_name":"probe8_cpu"},                                "no new_name — executor KeyError"),
    _m ("clone_vm", {"new_name":"clone"},                                    "no source_name — executor KeyError"),
    _m ("clone_vm", {},                                                      "both missing — executor KeyError"),

    # ── launch_vm ─────────────────────────────────────────────────────────────
    _v ("launch_vm", {"name":"probe8_cpu","dry_run":True},                       "dry_run — prints command, no QEMU process"),
    _v ("launch_vm", {"name":"probe8_min","dry_run":True},                       "dry_run + second VM"),
    _v ("launch_vm", {"name":"probe8_cpu","display":"sdl","dry_run":True},       "dry_run + display override"),
    _v ("launch_vm", {"name":"probe8_cpu","display":"vnc","dry_run":True},       "dry_run + VNC display"),
    _b ("launch_vm", {"name":"ghost_xyz"},                                   "VM doesn't exist"),
    _bs("launch_vm", {"name":"probe8_cpu","display":"foobar","dry_run":True},    "invalid display — sanitizer resets, dry_run succeeds"),
    _m ("launch_vm", {"display":"sdl"},                                      "no name field"),
    _m ("launch_vm", {"dry_run":True},                                       "no name — only dry_run"),

    # ── stop_vm ───────────────────────────────────────────────────────────────
    _b ("stop_vm",  {"name":"ghost_xyz"},             "VM doesn't exist"),
    _b ("stop_vm",  {"name":"ghost_xyz","force":True},"doesn't exist + force"),
    _m ("stop_vm",  {"force":True},                   "no name — only force"),
    # stop_vm all and graceful/force on real VMs excluded — long timeouts

    # ── vm_status — returns {name,state,pid}, no success key ─────────────────
    _vk("vm_status", {"name":"probe8_min"}, ["name","state"], "existing VM with config"),
    _vk("vm_status", {"name":"probe8_cpu"}, ["name","state"], "second existing VM"),
    _vk("vm_status", {"name":"ghost_xyz"}, ["name","state"], "nonexistent VM — still returns state dict"),
    _m ("vm_status", {},                "no name"),

    # ── monitor_vm — returns {name,state,pid}, no success key regardless of existence ──
    _vs("monitor_vm", {"name":"probe8_min"},    "monitor single VM — result varies by state"),
    _vs("monitor_vm", {"name":"all"},       "monitor all VMs"),
    _vs("monitor_vm", {"name":"ghost_xyz"}, "VM doesn't exist — still returns state dict without success"),
    _m ("monitor_vm", {},                   "no name"),

    # ── show_config ───────────────────────────────────────────────────────────
    _v ("show_config", {"name":"probe8_min"}, "VM with valid config"),
    _v ("show_config", {"name":"probe8_cpu"}, "second VM with valid config"),
    _b ("show_config", {"name":"ghost_xyz"}, "VM doesn't exist"),
    _m ("show_config", {},                   "no name"),

    # ── update_config ─────────────────────────────────────────────────────────
    _v ("update_config", {"name":"probe8_min","updates":{"memory_mb":4096}},                         "single field"),
    _v ("update_config", {"name":"probe8_min","updates":{"cpu_cores":4,"memory_mb":8192}},           "multiple fields"),
    _v ("update_config", {"name":"probe8_min","updates":{"display":"sdl"}},                          "display field"),
    _v ("update_config", {"name":"probe8_min","updates":{"uefi":True}},                              "boot field"),
    _v ("update_config", {"name":"probe8_min","updates":{"audio":"hda"}},                            "audio field (network_mode not a valid config field)"),
    _v ("update_config", {"name":"probe8_min","updates":{"cpu_model":"host","machine_type":"q35"}},  "CPU + machine type"),
    _b ("update_config", {"name":"ghost_xyz","updates":{"memory_mb":4096}},                      "VM doesn't exist"),
    _bs("update_config", {"name":"probe8_min","updates":{"memory_mb":-999}},                         "negative memory — sanitizer clamps to min, update succeeds"),
    _bs("update_config", {"name":"probe8_min","updates":{"cpu_cores":9999}},                         "exceeds host cores — sanitizer clamps"),
    _vs("update_config", {"name":"probe8_min","updates":{}},                                         "empty updates — executor succeeds with empty patch (updated [])"),
    _m ("update_config", {},                                                                     "both missing"),
    _m ("update_config", {"updates":{"memory_mb":4096}},                                        "no name"),
    _vs("update_config", {"name":"probe8_min"},                                                      "no updates key — executor defaults to {} and succeeds"),

    # ── resize_disk ───────────────────────────────────────────────────────────
    # probe8_min default disk is 60GB — shrink attempts below that fail with qemu-img error
    _b ("resize_disk", {"name":"probe8_min","new_size_gb":10},               "shrink — 10 < 60GB default, qemu-img rejects without --shrink"),
    _b ("resize_disk", {"name":"ghost_xyz","new_size_gb":80},            "VM doesn't exist"),
    _b ("resize_disk", {"name":"probe8_min","new_size_gb":-500},             "negative size — sanitizer clamps but resulting size still causes shrink error"),
    _vs("resize_disk", {"name":"probe8_min","new_size_gb":99999},            "99999GB — qemu-img succeeds (sparse file, no host space check)"),
    _m ("resize_disk", {"name":"probe8_min"},                                "no new_size_gb"),
    _m ("resize_disk", {"new_size_gb":80},                               "no name"),
    _m ("resize_disk", {},                                               "both missing"),

    # ── snapshot_create ───────────────────────────────────────────────────────
    _vs("snapshot_create", {"name":"probe8_min","snap_name":"probe8_snap"},      "needs running VM"),
    _b ("snapshot_create", {"name":"probe8_min","snap_name":"snap1"},            "VM is stopped — snapshot requires running"),
    _b ("snapshot_create", {"name":"ghost_xyz","snap_name":"snap1"},         "VM doesn't exist"),
    _b ("snapshot_create", {"name":"probe8_min","snap_name":"!!!bad name!!!"},   "invalid chars — sanitizer may clean name, but VM is stopped so snapshot fails"),
    _m ("snapshot_create", {"name":"probe8_min"},                                "no snap_name"),
    _m ("snapshot_create", {"snap_name":"snap1"},                            "no name"),
    _m ("snapshot_create", {},                                               "both missing"),

    # ── snapshot_list ─────────────────────────────────────────────────────────
    _v ("snapshot_list", {"name":"probe8_min"}, "existing VM"),
    _b ("snapshot_list", {"name":"ghost_xyz"}, "VM doesn't exist"),
    _m ("snapshot_list", {},                   "no name"),

    # ── snapshot_restore ─────────────────────────────────────────────────────
    _b ("snapshot_restore", {"name":"probe8_min","snap_name":"ghost_snap"},  "snap doesn't exist"),
    _b ("snapshot_restore", {"name":"ghost_xyz","snap_name":"snap1"},    "VM doesn't exist"),
    _m ("snapshot_restore", {"name":"probe8_min"},                           "no snap_name"),
    _m ("snapshot_restore", {"snap_name":"snap1"},                       "no name"),
    _m ("snapshot_restore", {},                                          "both missing"),
    _m ("snapshot_restore", {"name":"probe8_min","snap_name":""},            "empty snap_name"),

    # ── snapshot_delete ───────────────────────────────────────────────────────
    _b ("snapshot_delete", {"name":"probe8_min","snap_name":"ghost_snap"},   "snap doesn't exist"),
    _b ("snapshot_delete", {"name":"ghost_xyz","snap_name":"snap1"},     "VM doesn't exist"),
    _m ("snapshot_delete", {"name":"probe8_min"},                            "no snap_name"),
    _m ("snapshot_delete", {},                                           "both missing"),

    # ── set_resource_limits ───────────────────────────────────────────────────
    _vs("set_resource_limits", {"name":"probe8_min","cpu_percent":50,"memory_mb":2048}, "both limits — needs running VM"),
    _vs("set_resource_limits", {"name":"probe8_min","cpu_percent":50},                  "CPU only"),
    _vs("set_resource_limits", {"name":"probe8_min","memory_mb":2048},                  "memory only"),
    _vs("set_resource_limits", {"name":"probe8_min"},                                   "name only — no limits"),
    _b ("set_resource_limits", {"name":"probe8_min","cpu_percent":999},                 "cpu_percent > 100"),
    _b ("set_resource_limits", {"name":"probe8_min","cpu_percent":-10},                 "negative cpu_percent"),
    _b ("set_resource_limits", {"name":"ghost_xyz","cpu_percent":50},               "VM doesn't exist"),
    _b ("set_resource_limits", {"name":"probe8_min","memory_mb":-1},                    "negative memory — sanitizer might clamp, but VM not running so always fails"),
    _m ("set_resource_limits", {},                                                  "no name"),
    _m ("set_resource_limits", {"cpu_percent":50},                                  "no name field"),

    # ── create_network ────────────────────────────────────────────────────────
    _v ("create_network", {"net_name":"probe8_net"},                "new network"),
    _b ("create_network", {"net_name":"probe8_net"},                "already exists — second call"),
    _vs("create_network", {"net_name":"!!!bad net!!!"},             "invalid chars — sanitizer cleans name; result depends on whether cleaned name already exists"),
    _m ("create_network", {},                                       "no net_name"),
    _m ("create_network", {"net_name":""},                          "empty net_name"),

    # ── delete_network ────────────────────────────────────────────────────────
    _v ("delete_network", {"net_name":"probe8_net"},                "delete network created above"),
    _b ("delete_network", {"net_name":"ghost_net_xyz"},             "network doesn't exist"),
    _m ("delete_network", {},                                       "no net_name"),

    # ── add_vm_to_network ────────────────────────────────────────────────────
    _b ("add_vm_to_network", {"net_name":"ghost_net","vm_name":"probe8_min"}, "network doesn't exist"),
    _b ("add_vm_to_network", {"net_name":"probe8_net","vm_name":"ghost_xyz"}, "VM doesn't exist"),
    _m ("add_vm_to_network", {"net_name":"probe8_net"},               "no vm_name"),
    _m ("add_vm_to_network", {"vm_name":"probe8_min"},                    "no net_name"),
    _m ("add_vm_to_network", {},                                      "both missing"),

    # ── open_display ─────────────────────────────────────────────────────────
    _b ("open_display", {"name":"probe8_min"},    "VM stopped — no display process"),
    _b ("open_display", {"name":"ghost_xyz"}, "VM doesn't exist"),
    _m ("open_display", {},                   "no name"),

    # ── open_shell ────────────────────────────────────────────────────────────
    _b ("open_shell", {"name":"probe8_min"},    "VM stopped"),
    _b ("open_shell", {"name":"ghost_xyz"}, "VM doesn't exist"),
    _m ("open_shell", {},                   "no name"),

    # ── delete_vm ────────────────────────────────────────────────────────────
    _b ("delete_vm", {"name":"ghost_xyz"}, "VM doesn't exist"),
    _m ("delete_vm", {},                   "no name"),

    # ── check_disk ───────────────────────────────────────────────────────────
    _v ("check_disk", {"name":"probe8_min"},    "existing VM"),
    _v ("check_disk", {"name":"probe8_cpu"},    "second existing VM"),
    _b ("check_disk", {"name":"ghost_xyz"}, "VM doesn't exist"),
    _m ("check_disk", {},                   "no name — gate doesn't cover this tool"),

    # ── get_vm_logs — returns {name, log_path, log_exists, ...}, no success key ──
    _vk("get_vm_logs", {"name":"probe8_min"},              ["name","log_exists"],  "default 50 lines"),
    _vk("get_vm_logs", {"name":"probe8_min","lines":100},  ["name","log_exists"],  "100 lines"),
    _vk("get_vm_logs", {"name":"probe8_min","lines":200},  ["name","log_exists"],  "200 lines"),
    _vk("get_vm_logs", {"name":"probe8_cpu","lines":10},   ["name","log_exists"],  "10 lines from second VM"),
    _vk("get_vm_logs", {"name":"ghost_xyz"},           ["name","log_exists"],  "ghost VM — still returns dict with log_exists:False"),
    _vk("get_vm_logs", {"name":"probe8_min","lines":-1},   ["name","log_exists"],  "negative lines — sanitizer strips, falls back to default"),
    _vk("get_vm_logs", {"name":"probe8_min","lines":"lots"},["name","log_exists"], "non-integer — sanitizer strips"),
    _m ("get_vm_logs", {},                             "no name"),
    _m ("get_vm_logs", {"lines":100},                  "no name field"),

    # ── print_command ────────────────────────────────────────────────────────
    _v ("print_command", {"name":"probe8_min"}, "existing VM"),
    _v ("print_command", {"name":"probe8_cpu"}, "second existing VM"),
    _b ("print_command", {"name":"ghost_xyz"}, "VM doesn't exist"),
    _m ("print_command", {},                   "no name"),

    # ── fingerprint_vm ────────────────────────────────────────────────────────
    _v ("fingerprint_vm", {"name":"probe8_min"},                   "full report"),
    _v ("fingerprint_vm", {"name":"probe8_min","summary":True},    "score only"),
    _v ("fingerprint_vm", {"name":"probe8_min","summary":False},   "explicit full report"),
    _v ("fingerprint_vm", {"name":"probe8_cpu","summary":True},    "different VM, summary"),
    _b ("fingerprint_vm", {"name":"ghost_xyz"},                "VM doesn't exist"),
    _b ("fingerprint_vm", {"name":"ghost_xyz","summary":True}, "doesn't exist + summary mode"),
    _m ("fingerprint_vm", {},                                  "no name — gate doesn't cover this tool"),
    _m ("fingerprint_vm", {"summary":True},                    "no name — only summary flag"),

    # ── send_monitor_cmd ──────────────────────────────────────────────────────
    _b ("send_monitor_cmd", {"name":"probe8_min","cmd":"info status"},   "VM stopped — no monitor socket"),
    _b ("send_monitor_cmd", {"name":"probe8_min","cmd":"info block"},    "VM stopped"),
    _b ("send_monitor_cmd", {"name":"ghost_xyz","cmd":"info status"},"VM doesn't exist"),
    _b ("send_monitor_cmd", {"name":"probe8_min","cmd":"quit"},          "destructive cmd — VM stopped so no socket"),
    _b ("send_monitor_cmd", {"name":"probe8_min","cmd":"system_reset"},  "destructive + stopped"),
    _m ("send_monitor_cmd", {"name":"probe8_min"},                       "no cmd"),
    _m ("send_monitor_cmd", {"cmd":"info status"},                   "no name"),
    _m ("send_monitor_cmd", {},                                      "both missing"),
    # Running-VM monitor tests excluded — state-dependent
]


# ── Deduplication ─────────────────────────────────────────────────────────────

def _dedup(tests: List[PipelineTest]) -> List[PipelineTest]:
    seen: Dict[str, int] = {}
    out: List[PipelineTest] = []
    for t in tests:
        if t.id in seen:
            count = seen[t.id]
            seen[t.id] = count + 1
            t = PipelineTest(
                id=f"{t.id}_{count}",
                tags=t.tags, description=t.description, tool=t.tool,
                input_args=t.input_args, category=t.category,
                expect_success=t.expect_success, expect_layer=t.expect_layer,
                expect_result_keys=t.expect_result_keys,
            )
        else:
            seen[t.id] = 1
        out.append(t)
    return out

PIPELINE_TESTS = _dedup(PIPELINE_TESTS)


# ── Randomised test generator ──────────────────────────────────────────────────
#
# For each tool, defines:
#   required  — fields the gate enforces; valid and invalid/missing values
#   optional  — extra fields; valid, broken (sanitizer catches), and truly_broken values
#
# Generator picks:
#   fill_level  — none | partial | full  (how many optionals to include)
#   value_mode  — valid | sanitized | broken  (what values to use)
#   req_mode    — all | partial | none  (how many required fields to include)

_TOOL_SCHEMAS: Dict[str, Dict] = {
    "check_system":    {"required": {}, "optional": {}},
    "scan_isos":       {"required": {}, "optional": {}},
    "list_vms":        {"required": {}, "optional": {}},
    "list_profiles":   {"required": {}, "optional": {}},
    "list_networks":   {"required": {}, "optional": {}},

    "clarify": {
        "required": {
            "question": {"valid": ["Which OS?", "How much RAM?"], "missing": ["", None]},
        },
        "optional": {
            "options": {"valid": [["linux","windows"], ["yes","no"]], "broken": [None, 42]},
        },
    },

    "check_profile_compatibility": {
        "required": {
            "profile_name": {"valid": ["minimal","raspberry_pi_3b"], "missing": ["", None]},
        },
        "optional": {},
    },

    "create_profile": {
        "required": {
            "profile_name": {"valid": ["probe8r_p"], "missing": ["", None]},
            "description":  {"valid": ["a profile"],  "missing": ["", None]},
        },
        "optional": {
            "cpu_cores":    {"valid": [2, 4],           "sanitized": [-1, 9999],   "broken": None},
            "cpu_threads":  {"valid": [1, 2],           "sanitized": [-1],          "broken": None},
            "memory_mb":    {"valid": [2048, 4096],     "sanitized": [-1, 0],       "broken": None},
            "display":      {"valid": ["sdl","gtk"],    "sanitized": ["foobar"],    "broken": None},
            "gpu":          {"valid": ["virtio","none"],"sanitized": ["foobar"],    "broken": None},
            "audio":        {"valid": ["hda","none"],   "sanitized": ["foobar"],    "broken": None},
            "uefi":         {"valid": [True, False],    "broken": None},
            "bios":         {"valid": ["ovmf","seabios"],"sanitized": ["foobar"],   "broken": None},
            "battery":      {"valid": [True, False],    "broken": None},
            "machine_class":{"valid": ["desktop","laptop"],"sanitized":["foobar"],  "broken": None},
            "manufacturer": {"valid": ["Dell","Lenovo"],"broken": None},
            "product_name": {"valid": ["XPS 15","T14"], "broken": None},
            "notes":        {"valid": ["test note"],    "broken": None},
            "force":        {"valid": [True, False],    "broken": None},
        },
    },

    "delete_profile": {
        "required": {
            "profile_name": {"valid": ["ghost_r_xyz"], "missing": ["", None]},
        },
        "optional": {},
    },

    "create_vm": {
        "required": {
            "name":    {"valid": ["probe8r_{uid}"], "missing": ["", None]},
            "os_type": {"valid": ["linux","windows","other"], "missing": ["", None]},
        },
        "optional": {
            "memory_mb":    {"valid": [2048, 4096],         "sanitized": [-1, 999999999], "broken": None},
            "cpu_cores":    {"valid": [2, 4],               "sanitized": [9999, -1],      "broken": None},
            "cpu_threads":  {"valid": [1, 2],               "sanitized": [-1],             "broken": None},
            "disk_size_gb": {"valid": [30, 60],             "sanitized": [-1],             "broken": None},
            "disk_format":  {"valid": ["qcow2","raw"],      "sanitized": ["foobar"],       "broken": None},
            "display":      {"valid": ["sdl","gtk"],        "sanitized": ["foobar"],       "broken": None},
            "gpu":          {"valid": ["virtio","none"],    "sanitized": ["foobar"],       "broken": None},
            "audio":        {"valid": ["hda","none"],       "sanitized": ["foobar"],       "broken": None},
            "network_mode": {"valid": ["user","nat"],       "sanitized": ["foobar"],       "broken": None},
            "uefi":         {"valid": [True, False],        "broken": None},
            "kvm":          {"valid": [True, False],        "broken": None},
            "battery":      {"valid": [True, False],        "broken": None},
            "machine_type": {"valid": ["q35","pc"],         "sanitized": ["foobar"],       "broken": None},
            "os_name":      {"valid": ["ubuntu","debian"],  "broken": None},
            "description":  {"valid": ["test vm"],          "broken": None},
        },
    },

    "clone_vm": {
        "required": {
            "source_name": {"valid": ["probe8_cpu"],         "missing": ["", None]},
            "new_name":    {"valid": ["probe8r_clone"],  "missing": ["", None]},
        },
        "optional": {},
    },

    "launch_vm": {
        "required": {
            "name": {"valid": ["probe8_cpu"], "missing": ["", None]},
        },
        "optional": {
            "display": {"valid": ["sdl","gtk","vnc"], "sanitized": ["foobar"], "broken": None},
            "dry_run": {"valid": [True, False],       "broken": None},
        },
    },

    "stop_vm": {
        "required": {
            "name": {"valid": ["ghost_r_xyz"], "missing": ["", None]},
        },
        "optional": {
            "force": {"valid": [True, False], "broken": None},
        },
    },

    "vm_status": {
        "required": {
            "name": {"valid": ["probe8_min","probe8_cpu"], "missing": ["", None]},
        },
        "optional": {},
    },

    "monitor_vm": {
        "required": {
            "name": {"valid": ["probe8_min","probe8_cpu","all"], "missing": ["", None]},
        },
        "optional": {},
    },

    "show_config": {
        "required": {
            "name": {"valid": ["probe8_min","probe8_cpu"], "missing": ["", None]},
        },
        "optional": {},
    },

    "update_config": {
        "required": {
            "name":    {"valid": ["probe8_min"],                          "missing": ["", None]},
            "updates": {"valid": [{"memory_mb":2048},{"cpu_cores":2}],"missing": [None]},
        },
        "optional": {},
    },

    "resize_disk": {
        "required": {
            "name":        {"valid": ["probe8_min"],      "missing": ["", None]},
            "new_size_gb": {"valid": [80, 100],       "missing": [None]},
        },
        "optional": {
            "disk_index": {"valid": [0, 1], "sanitized": [-1], "broken": None},
        },
    },

    "snapshot_create": {
        "required": {
            "name":      {"valid": ["probe8_min"],        "missing": ["", None]},
            "snap_name": {"valid": ["probe8r_snap"],  "missing": ["", None]},
        },
        "optional": {},
    },

    "snapshot_list": {
        "required": {
            "name": {"valid": ["probe8_min","probe8_cpu"], "missing": ["", None]},
        },
        "optional": {},
    },

    "snapshot_restore": {
        "required": {
            "name":      {"valid": ["probe8_min"],          "missing": ["", None]},
            "snap_name": {"valid": ["ghost_r_snap"],    "missing": ["", None]},
        },
        "optional": {},
    },

    "snapshot_delete": {
        "required": {
            "name":      {"valid": ["probe8_min"],          "missing": ["", None]},
            "snap_name": {"valid": ["ghost_r_snap"],    "missing": ["", None]},
        },
        "optional": {},
    },

    "set_resource_limits": {
        "required": {
            "name": {"valid": ["probe8_min"], "missing": ["", None]},
        },
        "optional": {
            "cpu_percent": {"valid": [25, 50, 75], "sanitized": [-1], "broken": [999]},
            "memory_mb":   {"valid": [1024, 2048],  "sanitized": [-1], "broken": None},
        },
    },

    "create_network": {
        "required": {
            "net_name": {"valid": ["probe8r_net"], "missing": ["", None]},
        },
        "optional": {},
    },

    "delete_network": {
        "required": {
            "net_name": {"valid": ["ghost_r_net"], "missing": ["", None]},
        },
        "optional": {},
    },

    "add_vm_to_network": {
        "required": {
            "net_name": {"valid": ["ghost_r_net"], "missing": ["", None]},
            "vm_name":  {"valid": ["probe8_min"],       "missing": ["", None]},
        },
        "optional": {},
    },

    "open_display": {
        "required": {
            "name": {"valid": ["probe8_min"], "missing": ["", None]},
        },
        "optional": {},
    },

    "open_shell": {
        "required": {
            "name": {"valid": ["probe8_min"], "missing": ["", None]},
        },
        "optional": {},
    },

    "delete_vm": {
        "required": {
            # Always use a nonexistent VM — "valid" here means well-formed args;
            # the VM won't exist so executor returns success=False, which is expected
            "name": {"valid": ["ghost_r_del_xyz"], "missing": ["", None]},
        },
        "optional": {},
    },

    "check_disk": {
        "required": {
            "name": {"valid": ["probe8_min","probe8_cpu"], "missing": ["", None]},
        },
        "optional": {},
    },

    "get_vm_logs": {
        "required": {
            "name": {"valid": ["probe8_min","probe8_cpu"], "missing": ["", None]},
        },
        "optional": {
            "lines": {"valid": [50, 100, 200], "sanitized": [-1, 0], "broken": None},
        },
    },

    "print_command": {
        "required": {
            "name": {"valid": ["probe8_min","probe8_cpu"], "missing": ["", None]},
        },
        "optional": {},
    },

    "fingerprint_vm": {
        "required": {
            "name": {"valid": ["probe8_min","probe8_cpu"], "missing": ["", None]},
        },
        "optional": {
            "summary": {"valid": [True, False], "broken": None},
        },
    },

    "send_monitor_cmd": {
        "required": {
            "name": {"valid": ["probe8_min"], "missing": ["", None]},
            "cmd":  {"valid": ["info status","info block","info network"], "missing": ["", None]},
        },
        "optional": {},
    },
}


def _pick(rng: random.Random, choices: list) -> Any:
    return rng.choice(choices)


def generate_random_pipeline_tests(n: int = 30, seed: Optional[int] = None) -> List[PipelineTest]:
    """
    Generate n randomised pipeline tests (no gate).

    Each test randomly chooses:
      · A tool from _TOOL_SCHEMAS
      · req_mode  — all / partial / none (how many required fields to supply)
      · fill_mode — none / partial / full (how many optional fields to include)
      · val_mode  — valid / sanitized / broken (what values to use for included fields)

    Expected outcome:
      · req=all, val=valid/sanitized → expect_success=True (or None for state-dep tools)
      · req=all, val=broken          → expect_success=False
      · req=partial/none             → expect_success=False
    """
    rng      = random.Random(seed)
    tests: List[PipelineTest] = []
    tools    = [t for t, s in _TOOL_SCHEMAS.items() if s.get("required") is not None]
    uid_ctr  = 0

    # Tools where result has no "success" key — skip success assertion
    KEY_ONLY_TOOLS = {"check_system","vm_status","clarify","check_profile_compatibility","get_vm_logs","monitor_vm"}

    # Tools where state matters or entity doesn't exist — don't assert success
    STATE_DEP_TOOLS = {"stop_vm","launch_vm","set_resource_limits","snapshot_create","monitor_vm",
                       "send_monitor_cmd","delete_vm","resize_disk","open_display","open_shell",
                       "snapshot_restore","snapshot_delete","update_config","add_vm_to_network",
                       "delete_profile","delete_network","clone_vm",
                       # create_profile fails without hardware fields; create_network may conflict
                       "create_profile","create_network"}

    while len(tests) < n:
        tool    = rng.choice(tools)
        schema  = _TOOL_SCHEMAS[tool]
        req_def = schema.get("required", {})
        opt_def = schema.get("optional", {})

        req_mode  = rng.choice(["all", "all", "all", "partial", "none"])
        fill_mode = rng.choice(["none", "partial", "full"])
        val_mode  = rng.choice(["valid", "valid", "sanitized", "broken"])

        args: Dict[str, Any] = {}
        missing_req: List[str] = []

        # Required fields
        req_keys = list(req_def.keys())
        if req_mode == "all":
            include_req = req_keys
        elif req_mode == "partial" and len(req_keys) > 1:
            include_req = rng.sample(req_keys, max(1, len(req_keys) - 1))
            missing_req = [k for k in req_keys if k not in include_req]
        else:
            include_req = []
            missing_req = req_keys

        for field in include_req:
            fdef = req_def[field]
            val = _pick(rng, fdef["valid"])
            if "{uid}" in str(val):
                uid_ctr += 1
                val = val.replace("{uid}", f"{uid_ctr:03d}")
            args[field] = val

        # Optional fields
        if opt_def:
            opt_keys = list(opt_def.keys())
            if fill_mode == "none":
                include_opt = []
            elif fill_mode == "partial":
                include_opt = rng.sample(opt_keys, max(1, len(opt_keys) // 2))
            else:
                include_opt = opt_keys

            for field in include_opt:
                fdef = opt_def[field]
                if val_mode == "valid":
                    choices = fdef.get("valid", [])
                elif val_mode == "sanitized":
                    choices = fdef.get("sanitized") or fdef.get("valid", [])
                else:  # broken
                    choices = fdef.get("broken") or fdef.get("sanitized") or fdef.get("valid", [])
                if choices is None:
                    choices = fdef.get("valid", [])
                if isinstance(choices, list) and choices:
                    args[field] = _pick(rng, choices)

        # Expected outcome
        has_missing_req = bool(missing_req)
        is_state_dep    = tool in STATE_DEP_TOOLS
        is_key_only     = tool in KEY_ONLY_TOOLS

        # KEY_ONLY tools never return a success key — always use exp_success=None
        # regardless of missing args or broken values.
        if is_key_only:
            exp_success = None
            exp_layer   = None
            category    = "missing" if has_missing_req else "valid"
        elif has_missing_req:
            exp_success = False
            exp_layer   = "executor"
            category    = "missing"
        elif is_state_dep:
            exp_success = None
            exp_layer   = None
            category    = "valid"
        else:
            exp_success = True
            exp_layer   = "ok"
            category    = "valid"

        uid_ctr += 1
        tests.append(PipelineTest(
            id=f"p8_rand_{tool}_{uid_ctr:04d}",
            tags=["pipeline", "random", category, tool,
                  f"req={req_mode}", f"fill={fill_mode}", f"val={val_mode}"],
            description=(
                f"{tool} | req={req_mode} fill={fill_mode} val={val_mode}"
                + (f" | missing={missing_req}" if missing_req else "")
            ),
            tool=tool, input_args=args, category=category,
            expect_success=exp_success, expect_layer=exp_layer,
        ))

    return tests


# ── Cleanup ────────────────────────────────────────────────────────────────────

_PROBE_VM_PREFIX      = "probe8"
_PROBE_PROFILE_PREFIX = "probe8"
_PROBE_NET_PREFIX     = "probe8"


def cleanup_probe_artifacts():
    """Remove VMs, profiles, and networks created by this layer."""
    import shutil
    from executioner.tool_executor import execute_tool as _et
    from api.qemu_config import get_all_profiles, delete_custom_profile

    vm_dir = os.path.expanduser("~/.qemu_vms")
    if os.path.isdir(vm_dir):
        for entry in os.listdir(vm_dir):
            if entry.startswith(_PROBE_VM_PREFIX):
                try:
                    _et("delete_vm", {"name": entry}, verbose=True, skip_gate=True)
                except Exception:
                    shutil.rmtree(os.path.join(vm_dir, entry), ignore_errors=True)

    for pname in list(get_all_profiles().keys()):
        if pname.startswith(_PROBE_PROFILE_PREFIX):
            try:
                delete_custom_profile(pname)
            except Exception:
                pass

    try:
        nets = _et("list_networks", {}, verbose=True, skip_gate=True)
        if isinstance(nets, (list, dict)):
            net_list = nets if isinstance(nets, list) else nets.get("networks", [])
            for n in net_list:
                nname = n if isinstance(n, str) else n.get("name", "")
                if nname.startswith(_PROBE_NET_PREFIX):
                    _et("delete_network", {"net_name": nname}, verbose=True, skip_gate=True)
    except Exception:
        pass


# ── Runner ─────────────────────────────────────────────────────────────────────

def run_pipeline_test(tc: PipelineTest) -> TestResult:
    start  = time.time()
    issues: List[str] = []

    try:
        result = execute_tool(tc.tool, dict(tc.input_args), verbose=True, skip_gate=True)
    except Exception:
        tb = traceback.format_exc()
        passed = tc.expect_success is not True  # exception = failure, ok unless we expected success
        return TestResult(
            test_id=tc.id, layer=8, passed=passed,
            issues=[] if passed else [f"Unexpected exception: {tb[:200]}"],
            fixes_applied=[], duration_s=time.time() - start,
            detail={
                "category": tc.category, "tool": tc.tool,
                "args": tc.input_args, "actual_layer": "exception",
                "expect_layer": tc.expect_layer, "error": tb[:200],
                "state_dep": tc.expect_success is None,
            },
        )

    if isinstance(result, list):
        result = {"success": True, "_list_len": len(result)}

    actual_layer   = _detect_layer(result)
    actual_success = result.get("success")
    actual_clarify = bool(result.get("clarify"))

    if tc.expect_success is not None and actual_success != tc.expect_success:
        issues.append(
            f"Expected success={tc.expect_success} got {actual_success}"
            + (f" — {result.get('error','')}" if result.get("error") else "")
        )

    if tc.expect_layer and actual_layer != tc.expect_layer:
        issues.append(f"Expected layer '{tc.expect_layer}' got '{actual_layer}'")

    for key in tc.expect_result_keys:
        if key not in result:
            issues.append(f"Result missing key '{key}'")

    return TestResult(
        test_id=tc.id, layer=8,
        passed=len(issues) == 0,
        issues=issues, fixes_applied=[],
        duration_s=time.time() - start,
        detail={
            "category":     tc.category,
            "tool":         tc.tool,
            "args":         tc.input_args,
            "actual_layer": actual_layer,
            "expect_layer": tc.expect_layer,
            "error":        result.get("error"),
            "clarify":      actual_clarify,
            "missing":      [m["field"] for m in result.get("missing", [])],
            "state_dep":    tc.expect_success is None,
        },
    )
