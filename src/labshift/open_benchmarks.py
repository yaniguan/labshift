"""Importers for open scheduling & wet-lab benchmarks (PSPLIB, OR-Library/Taillard JSSP,
Opentrons/Protocols, WLP-Dataset, and BioPlanner)."""

from __future__ import annotations

import ast
import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from labshift.models import BASELINE_INSTRUMENTS, Instance, Job, Operation
from labshift.protocol import instance_from_protocols, job_from_protocol

_TIME_MENTION_RE = re.compile(
    r"(?P<val>\d+(?:\.\d+)?)\s*(?P<unit>ms|seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h|days?|d)\b",
    re.IGNORECASE,
)


def parse_jssp_instance(
    text: str,
    *,
    name: str = "jssp_instance",
    max_wait: int | None = None,
) -> Instance:
    """Parse a standard JSPLIB / OR-Library / Taillard job-shop instance into ``Instance``.

    Supports both:
      1. Standard OR-Library / JSPLIB interleaved ``(machine_idx, duration)`` rows
         preceded by ``n_jobs n_machines`` (ignoring ``#`` / ``+`` banner lines).
      2. Taillard ``Times`` + ``Machines`` two-matrix format.
    """
    lines = [
        ln.strip()
        for ln in text.splitlines()
        if ln.strip() and not ln.strip().startswith(("#", "+", "*"))
    ]
    if not lines:
        raise ValueError(f"Empty JSSP text for '{name}'.")

    # Detect Taillard two-matrix format ("Times" / "Machines" sections)
    lower_lines = [ln.lower() for ln in lines]
    if any(ln.startswith("times") for ln in lower_lines) and any(
        ln.startswith("machines") for ln in lower_lines
    ):
        header_nums: list[int] = []
        t_idx = next(i for i, ln in enumerate(lower_lines) if ln.startswith("times"))
        m_idx = next(i for i, ln in enumerate(lower_lines) if ln.startswith("machines"))
        for ln in lines[:t_idx]:
            ints = [int(tok) for tok in re.findall(r"\d+", ln)]
            if len(ints) >= 2:
                header_nums = ints
                break
        if len(header_nums) < 2:
            raise ValueError(f"Missing (n_jobs, n_machines) header in Taillard file '{name}'.")
        n_jobs, n_machines = header_nums[0], header_nums[1]
        dur_rows = [
            [int(x) for x in lines[t_idx + 1 + r].split()] for r in range(n_jobs)
        ]
        mach_rows = [
            [int(x) for x in lines[m_idx + 1 + r].split()] for r in range(n_jobs)
        ]
        min_mach_id = min(min(r) for r in mach_rows)
        caps = {f"machine_{m:02d}": 1 for m in range(n_machines)}
        jobs: list[Job] = []
        for j in range(n_jobs):
            ops: list[Operation] = []
            for k in range(n_machines):
                m_norm = mach_rows[j][k] - min_mach_id
                dur = max(1, dur_rows[j][k])
                ops.append(
                    Operation(
                        name=f"op_{k + 1:02d}",
                        instrument=f"machine_{m_norm:02d}",
                        duration=dur,
                        min_wait=0,
                        max_wait=max_wait if k > 0 else None,
                    )
                )
            jobs.append(Job(job_id=f"job_{j + 1:02d}", ops=tuple(ops)))
        return Instance(
            name=name,
            preset="jssp",
            capacities=caps,
            jobs=tuple(jobs),
        )

    # Standard JSPLIB / OR-Library single-block format
    start_row = 0
    n_jobs = 0
    n_machines = 0
    for idx, ln in enumerate(lines):
        parts = ln.split()
        if len(parts) >= 2 and all(p.lstrip("-").isdigit() for p in parts[:2]):
            n_jobs = int(parts[0])
            n_machines = int(parts[1])
            start_row = idx + 1
            break
    if n_jobs <= 0 or n_machines <= 0:
        raise ValueError(f"Could not parse (n_jobs, n_machines) header for '{name}'.")

    body_tokens: list[int] = []
    for ln in lines[start_row:]:
        parts = ln.split()
        if not all(p.lstrip("-").isdigit() for p in parts):
            break
        body_tokens.extend(int(p) for p in parts)

    expected = n_jobs * n_machines * 2
    if len(body_tokens) < expected:
        raise ValueError(
            f"JSSP '{name}' expected {expected} (machine, duration) ints, got {len(body_tokens)}."
        )

    raw_mach_ids = [body_tokens[2 * i] for i in range(n_jobs * n_machines)]
    mach_offset = 1 if min(raw_mach_ids) >= 1 and max(raw_mach_ids) == n_machines else 0
    caps = {f"machine_{m:02d}": 1 for m in range(n_machines)}
    jobs = []
    pos = 0
    for j in range(n_jobs):
        ops = []
        for k in range(n_machines):
            m_id = body_tokens[pos] - mach_offset
            dur = max(1, body_tokens[pos + 1])
            pos += 2
            ops.append(
                Operation(
                    name=f"op_{k + 1:02d}",
                    instrument=f"machine_{m_id:02d}",
                    duration=dur,
                    min_wait=0,
                    max_wait=max_wait if k > 0 else None,
                )
            )
        jobs.append(Job(job_id=f"job_{j + 1:02d}", ops=tuple(ops)))

    return Instance(
        name=name,
        preset="jssp",
        capacities=caps,
        jobs=tuple(jobs),
    )


def parse_orlib_jobshop_archive(
    text: str,
    *,
    names_filter: Sequence[str] | None = None,
    max_wait: int | None = None,
) -> list[Instance]:
    """Parse multiple named JSSP instances (e.g. ``ft06``, ``la01``) from OR-Library ``jobshop1.txt``."""
    wanted = {n.lower() for n in names_filter} if names_filter is not None else None
    lines = text.splitlines()
    instances: list[Instance] = []
    i = 0
    while i < len(lines):
        ln = lines[i].strip()
        if ln.lower().startswith("instance "):
            inst_name = ln.split()[1].strip()
            i += 1
            block: list[str] = []
            while i < len(lines) and not lines[i].strip().lower().startswith("instance "):
                s = lines[i].strip()
                if s and not s.startswith("+") and any(ch.isdigit() for ch in s):
                    if all(tok.lstrip("-").isdigit() for tok in s.split()):
                        block.append(s)
                i += 1
            if block and (wanted is None or inst_name.lower() in wanted):
                instances.append(
                    parse_jssp_instance(
                        "\n".join(block), name=inst_name, max_wait=max_wait
                    )
                )
        else:
            i += 1
    return instances


def _rcpsp_dag_to_instance(
    name: str,
    preset: str,
    durations: dict[int, int],
    predecessors: dict[int, list[int]],
    resource_caps: dict[str, int],
    demands: dict[int, dict[str, int]],
    *,
    max_wait: int | None = None,
) -> Instance:
    """Convert an RCPSP activity DAG into a chain-decomposed multi-pool labshift Instance."""
    act_ids = sorted(aid for aid, dur in durations.items() if dur > 0)
    if not act_ids:
        raise ValueError(f"PSPLIB instance '{name}' has no positive-duration activities.")

    # Compute DAG topological earliest starts ES[i]
    es: dict[int, int] = {}
    for aid in sorted(durations):
        preds = predecessors.get(aid, [])
        es[aid] = max((es.get(p, 0) + durations.get(p, 0) for p in preds), default=0)

    # Normalize multi-unit resource capacities to unit-demand slot counts
    pos_demands_by_res: dict[str, list[int]] = {r: [] for r in resource_caps}
    for aid in act_ids:
        for r_name, dem in demands.get(aid, {}).items():
            if dem > 0 and r_name in pos_demands_by_res:
                pos_demands_by_res[r_name].append(dem)

    norm_caps: dict[str, int] = {}
    for r_name, raw_cap in resource_caps.items():
        d_list = pos_demands_by_res.get(r_name, [])
        if not d_list:
            norm_caps[r_name] = max(1, raw_cap)
        else:
            avg_d = sum(d_list) / len(d_list)
            norm_caps[r_name] = max(1, round(raw_cap / max(1.0, avg_d)))

    default_res = sorted(norm_caps)[0]

    def _pick_instrument(aid: int) -> str:
        d_map = demands.get(aid, {})
        best_r = default_res
        best_ratio = -1.0
        for r_name, dem in d_map.items():
            if dem > 0 and r_name in resource_caps:
                ratio = dem / max(1, resource_caps[r_name])
                if ratio > best_ratio:
                    best_ratio = ratio
                    best_r = r_name
        return best_r

    # Greedy maximal precedence-chain decomposition over positive-duration activities
    successors: dict[int, list[int]] = {aid: [] for aid in act_ids}
    act_set = set(act_ids)
    for aid in act_ids:
        for p in predecessors.get(aid, []):
            if p in act_set:
                successors[p].append(aid)

    unassigned = set(act_ids)
    chains: list[list[int]] = []
    for start_aid in sorted(act_ids, key=lambda a: (es[a], a)):
        if start_aid not in unassigned:
            continue
        chain = [start_aid]
        unassigned.remove(start_aid)
        cur = start_aid
        while True:
            cands = [s for s in successors[cur] if s in unassigned]
            if not cands:
                break
            nxt = min(cands, key=lambda s: (es[s] - (es[cur] + durations[cur]), -durations[s], s))
            chain.append(nxt)
            unassigned.remove(nxt)
            cur = nxt
        chains.append(chain)

    jobs: list[Job] = []
    for c_idx, chain in enumerate(chains):
        rel = es[chain[0]]
        ops: list[Operation] = []
        for pos_in_c, aid in enumerate(chain):
            inst = _pick_instrument(aid)
            dur = max(1, durations[aid])
            if pos_in_c == 0:
                min_w = 0
                op_max_w = None
            else:
                prev_aid = chain[pos_in_c - 1]
                dag_lag = max(0, es[aid] - (es[prev_aid] + durations[prev_aid]))
                min_w = dag_lag
                op_max_w = (min_w + max_wait) if max_wait is not None else None
            ops.append(
                Operation(
                    name=f"act_{aid:02d}",
                    instrument=inst,
                    duration=dur,
                    min_wait=min_w,
                    max_wait=op_max_w,
                )
            )
        jobs.append(Job(job_id=f"chain_{c_idx + 1:02d}", release=rel, ops=tuple(ops)))

    return Instance(
        name=name,
        preset=preset,
        capacities=norm_caps,
        jobs=tuple(jobs),
    )


def parse_psplib_sm(
    text: str,
    *,
    name: str = "psplib_sm",
    max_wait: int | None = None,
) -> Instance:
    """Parse a standard PSPLIB single-mode ``.sm`` file (e.g. ``j301_1.sm``, ``j601_1.sm``)."""
    lines = text.splitlines()
    section = ""
    successors_raw: dict[int, list[int]] = {}
    durations: dict[int, int] = {}
    demands: dict[int, dict[str, int]] = {}
    resource_caps: dict[str, int] = {}

    i = 0
    while i < len(lines):
        ln = lines[i].strip()
        upper = ln.upper()
        if "PRECEDENCE RELATIONS" in upper:
            section = "prec"
            i += 2
            continue
        if "REQUESTS/DURATIONS" in upper:
            section = "req"
            i += 3
            continue
        if "RESOURCEAVAILABILITIES" in upper:
            section = "cap"
            i += 1
            continue
        if ln.startswith("*"):
            section = ""
            i += 1
            continue

        if section == "prec" and ln:
            nums = [int(x) for x in ln.split() if x.isdigit()]
            if len(nums) >= 3:
                job_nr = nums[0]
                n_succ = nums[2]
                successors_raw[job_nr] = nums[3 : 3 + n_succ]
        elif section == "req" and ln:
            nums = [int(x) for x in ln.split() if x.isdigit()]
            if len(nums) >= 4:
                job_nr = nums[0]
                durations[job_nr] = nums[2]
                demands[job_nr] = {
                    f"resource_{r_idx + 1}": val
                    for r_idx, val in enumerate(nums[3:])
                }
        elif section == "cap" and ln:
            nums = [int(x) for x in ln.split() if x.isdigit()]
            if nums and "R" not in ln.upper():
                resource_caps = {
                    f"resource_{r_idx + 1}": max(1, val)
                    for r_idx, val in enumerate(nums)
                }
                section = ""
        i += 1

    predecessors: dict[int, list[int]] = {aid: [] for aid in durations}
    for u, succs in successors_raw.items():
        for v in succs:
            predecessors.setdefault(v, []).append(u)

    n_non_dummy = sum(1 for d in durations.values() if d > 0)
    preset = "psplib_j60" if n_non_dummy >= 45 else "psplib_j30"
    return _rcpsp_dag_to_instance(
        name=name,
        preset=preset,
        durations=durations,
        predecessors=predecessors,
        resource_caps=resource_caps,
        demands=demands,
        max_wait=max_wait,
    )


def parse_psplib_ampl_dat(
    text: str,
    *,
    name: str = "psplib_dat",
    max_wait: int | None = None,
) -> Instance:
    """Parse an AMPL ``.dat`` PSPLIB ``j30`` / ``j60`` RCPSP file into ``Instance``."""
    p_match = re.search(r"param\s+p\s*:=\s*([^;]+);", text, re.DOTALL)
    r_cap_match = re.search(r"param\s+R\s*:=\s*([^;]+);", text, re.DOTALL)
    r_dem_match = re.search(r"param\s+r\s*:=\s*([^;]+);", text, re.DOTALL)
    if p_match is None or r_cap_match is None or r_dem_match is None:
        raise ValueError(f"Invalid PSPLIB AMPL .dat file for '{name}'.")

    p_tokens = p_match.group(1).split()
    durations: dict[int, int] = {
        int(p_tokens[idx]): int(p_tokens[idx + 1])
        for idx in range(0, len(p_tokens) - 1, 2)
    }

    cap_tokens = r_cap_match.group(1).split()
    resource_caps: dict[str, int] = {
        f"resource_{cap_tokens[idx].lower()}": max(1, int(cap_tokens[idx + 1]))
        for idx in range(0, len(cap_tokens) - 1, 2)
    }

    dem_tokens = r_dem_match.group(1).split()
    demands: dict[int, dict[str, int]] = {}
    for idx in range(0, len(dem_tokens) - 2, 3):
        aid = int(dem_tokens[idx])
        r_key = f"resource_{dem_tokens[idx + 1].lower()}"
        demands.setdefault(aid, {})[r_key] = int(dem_tokens[idx + 2])

    predecessors: dict[int, list[int]] = {aid: [] for aid in durations}
    for m_pred in re.finditer(r"set\s+P\[(\d+)\]\s*:=\s*([^;]*);", text):
        aid = int(m_pred.group(1))
        preds = [int(tok) for tok in m_pred.group(2).split() if tok.isdigit()]
        predecessors[aid] = preds

    n_non_dummy = sum(1 for d in durations.values() if d > 0)
    preset = "psplib_j60" if n_non_dummy >= 45 else "psplib_j30"
    return _rcpsp_dag_to_instance(
        name=name,
        preset=preset,
        durations=durations,
        predecessors=predecessors,
        resource_caps=resource_caps,
        demands=demands,
        max_wait=max_wait,
    )


def parse_opentrons_python_protocol(
    source_code: str,
    *,
    protocol_id: str = "opentrons_protocol",
    release: int = 0,
    transfer_time: int = 1,
) -> Job:
    """Extract liquid-handling, delay, and module steps from an Opentrons Python API v2 script."""
    tree = ast.parse(source_code)
    resolved_id = protocol_id

    for stmt_node in tree.body:
        if isinstance(stmt_node, ast.Assign):
            for target in stmt_node.targets:
                if isinstance(target, ast.Name) and target.id == "metadata":
                    try:
                        meta = ast.literal_eval(stmt_node.value)
                        if isinstance(meta, dict):
                            pname = meta.get("protocolName") or meta.get("name")
                            if isinstance(pname, str) and pname.strip():
                                resolved_id = f"{protocol_id}_{re.sub(r'[^a-zA-Z0-9]+', '_', pname.strip()).strip('_')[:24]}"
                    except Exception:
                        pass

    steps: list[dict[str, Any]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        method = node.func.attr
        if method in ("transfer", "distribute", "consolidate"):
            vol = 100.0
            if node.args and isinstance(node.args[0], ast.Constant) and isinstance(
                node.args[0].value, (int, float)
            ):
                vol = max(1.0, min(1000.0, float(node.args[0].value)))
            steps.append(
                {
                    "type": "distribute" if method == "distribute" else "transfer",
                    "volume": vol,
                    "unit": "uL",
                }
            )
        elif method == "mix":
            reps = 3
            if node.args and isinstance(node.args[0], ast.Constant) and isinstance(
                node.args[0].value, int
            ):
                reps = max(1, min(20, int(node.args[0].value)))
            steps.append({"type": "mix", "cycles": reps, "volume": 100.0, "unit": "uL"})
        elif method in ("aspirate", "dispense"):
            steps.append({"type": method, "volume": 50.0, "unit": "uL"})
        elif method in ("pick_up_tip", "drop_tip"):
            steps.append({"type": method})
        elif method == "delay":
            secs = 10.0
            mins = 0.0
            for kw in node.keywords:
                if (
                    kw.arg == "seconds"
                    and isinstance(kw.value, ast.Constant)
                    and isinstance(kw.value.value, (int, float))
                ):
                    secs = max(0.0, float(kw.value.value))
                elif (
                    kw.arg == "minutes"
                    and isinstance(kw.value, ast.Constant)
                    and isinstance(kw.value.value, (int, float))
                ):
                    mins = max(0.0, float(kw.value.value))
            steps.append({"type": "delay", "seconds": secs, "minutes": mins})
        elif method in ("set_temperature", "execute_profile", "engage", "wait_for_temperature"):
            steps.append({"type": "incubate", "time": 10, "time_unit": "min"})

    if not steps:
        steps.append({"type": "transfer", "volume": 100.0, "unit": "uL"})

    return job_from_protocol(
        {"protocol_id": resolved_id, "steps": steps},
        job_id=resolved_id,
        release=release,
        transfer_time=transfer_time,
    )


def _classify_wetlab_verb(verb_or_sentence: str) -> str | None:
    low = verb_or_sentence.lower()
    if any(w in low for w in ("centrifug", "spin", "pellet")):
        return "spin"
    if any(w in low for w in ("wash", "rinse")):
        return "wash"
    if any(w in low for w in ("incubat", "thermocyc", "pcr", "anneal", "denatur", "heat", "boil", "equilibrat")):
        return "incubate"
    if any(w in low for w in ("read", "measur", "absorbance", "fluorescen", "spectrophotom", "enumerat", "count", "microscop", "image")):
        return "read"
    if any(w in low for w in ("mix", "vortex", "resuspend", "invert", "shake")):
        return "mix"
    if any(w in low for w in ("transfer", "pipet", "add", "dispens", "aliquot", "dilut", "inoculat", "place", "load", "remove")):
        return "transfer"
    return None


def _extract_capped_minutes(text_span: str, *, max_step_minutes: int = 90) -> int | None:
    m = _TIME_MENTION_RE.search(text_span)
    if m is None:
        return None
    val = float(m.group("val"))
    if val <= 0:
        return None
    u = m.group("unit").lower()
    if u == "ms":
        mins = val / 60_000.0
    elif u.startswith("s"):
        mins = val / 60.0
    elif u.startswith("m"):
        mins = val
    elif u.startswith("h"):
        mins = val * 60.0
    else:
        mins = val * 1440.0
    return max(1, min(max_step_minutes, round(mins)))


def parse_wlp_brat_protocol(
    txt_text: str,
    ann_text: str,
    *,
    protocol_id: str = "wlp_protocol",
    release: int = 0,
    transfer_time: int = 1,
    max_step_minutes: int = 90,
) -> Job:
    """Parse a Wet-Lab Protocol Dataset (WLP) BRAT ``.txt`` + ``.ann`` pair into a ``Job``."""
    actions: list[tuple[int, str]] = []
    time_spans: list[tuple[int, str]] = []

    for ln in ann_text.splitlines():
        if not ln.startswith("T"):
            continue
        parts = ln.split("\t")
        if len(parts) < 3:
            continue
        meta_tokens = parts[1].split()
        if len(meta_tokens) < 2:
            continue
        label = meta_tokens[0]
        nums = [int(x) for x in re.findall(r"\d+", " ".join(meta_tokens[1:]))]
        start_char = nums[0] if nums else 0
        span_text = parts[2].strip()
        if label == "Action":
            actions.append((start_char, span_text))
        elif label in ("Time", "Duration"):
            time_spans.append((start_char, span_text))

    actions.sort(key=lambda x: x[0])
    time_spans.sort(key=lambda x: x[0])

    steps: list[dict[str, Any]] = []
    for pos_char, act_word in actions:
        mapped = _classify_wetlab_verb(act_word)
        if mapped is None:
            continue
        step_dict: dict[str, Any] = {"type": mapped}
        if mapped in ("incubate", "spin", "wash", "read"):
            # Match nearest Time entity within 120 characters after the action verb
            nearby = [
                t_str for t_pos, t_str in time_spans if 0 <= t_pos - pos_char <= 120
            ]
            context = nearby[0] if nearby else txt_text[pos_char : pos_char + 120]
            capped_m = _extract_capped_minutes(
                context, max_step_minutes=max_step_minutes
            )
            if capped_m is not None:
                step_dict["time"] = capped_m
                step_dict["time_unit"] = "min"
        steps.append(step_dict)

    if not steps:
        steps.append({"type": "transfer", "volume": 100.0, "unit": "uL"})

    return job_from_protocol(
        {"protocol_id": protocol_id, "steps": steps},
        job_id=protocol_id,
        release=release,
        transfer_time=transfer_time,
    )


def parse_bioplanner_json_protocol(
    data: Mapping[str, Any],
    *,
    protocol_id: str | None = None,
    release: int = 0,
    transfer_time: int = 1,
    max_step_minutes: int = 90,
) -> Job:
    """Parse a BioPlanner ``protocols.io`` JSON record into a scheduled ``labshift.Job``."""
    raw_id = str(data.get("id", "bioplanner"))
    resolved_id = protocol_id or f"bioplanner_{raw_id}"
    raw_steps = data.get("steps", "")
    step_lines: list[str]
    if isinstance(raw_steps, str):
        step_lines = [ln.strip() for ln in raw_steps.splitlines() if ln.strip()]
    elif isinstance(raw_steps, Sequence):
        step_lines = [str(item).strip() for item in raw_steps if str(item).strip()]
    else:
        step_lines = []

    steps: list[dict[str, Any]] = []
    for ln in step_lines:
        mapped = _classify_wetlab_verb(ln)
        if mapped is None:
            continue
        step_dict: dict[str, Any] = {"type": mapped}
        if mapped in ("incubate", "spin", "wash", "read"):
            capped_m = _extract_capped_minutes(ln, max_step_minutes=max_step_minutes)
            if capped_m is not None:
                step_dict["time"] = capped_m
                step_dict["time_unit"] = "min"
        steps.append(step_dict)

    if not steps:
        steps.append({"type": "transfer", "volume": 100.0, "unit": "uL"})

    return job_from_protocol(
        {"protocol_id": resolved_id, "steps": steps},
        job_id=resolved_id,
        release=release,
        transfer_time=transfer_time,
    )


def build_wetlab_campaign_instance(
    jobs: Sequence[Job],
    *,
    name: str = "wetlab_multi_protocol_campaign",
    capacities: Mapping[str, int] | None = None,
    stagger_release_min: int = 4,
) -> Instance:
    """Combine heterogeneous Opentrons + WLP + BioPlanner protocol Jobs into a shared workcell Instance."""
    if not jobs:
        raise ValueError("'jobs' must contain at least one protocol Job.")

    caps = (
        dict(capacities)
        if capacities is not None
        else {
            "liquid_handler": 2,
            "incubator": 3,
            "plate_reader": 1,
            "centrifuge": 1,
            "washer": 1,
            "robot_arm": 1,
        }
    )
    for inst_base in BASELINE_INSTRUMENTS:
        caps.setdefault(inst_base, 1)

    staggered: list[Job] = []
    for idx, j in enumerate(jobs):
        for op in j.ops:
            caps.setdefault(op.instrument, 1)
        rel = j.release if j.release > 0 else idx * stagger_release_min
        staggered.append(
            Job(
                job_id=j.job_id,
                ops=j.ops,
                release=rel,
                deadline=j.deadline,
                assay_tag=j.assay_tag,
            )
        )

    _ = instance_from_protocols
    return Instance(
        name=name,
        preset="wetlab_campaign",
        capacities=caps,
        jobs=tuple(staggered),
    )


def load_bioplanner_json_file(path: str | Path) -> Job:
    """Load a BioPlanner JSON file from disk into a ``Job``."""
    p = Path(path)
    raw = json.loads(p.read_text(encoding="utf-8"))
    return parse_bioplanner_json_protocol(raw)
