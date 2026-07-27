#!/usr/bin/env python3
"""Generate or compare spoolctl differential behavior signatures."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
DEFAULT_TIMEOUT = 5.0
VERSION_PLACEHOLDER = "VERSION"


@dataclass(frozen=True)
class Case:
    label: str
    argv: list[str]
    db_name: str | None = None
    text_hash: bool = False
    timeout: float = DEFAULT_TIMEOUT
    expect_hang: bool = False
    detail: str | None = None


def target_prefix(target: str, artifact: Path | None) -> list[str]:
    if target == "source":
        return [sys.executable, "-m", "spoolctl"]
    path = artifact or REPO / "dist" / "spoolctl.py"
    return [sys.executable, str(path)]


def tz_label() -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", os.environ.get("TZ", "local"))


def default_baseline(target: str) -> Path:
    return REPO / "tests" / "golden" / f"signature-{target}-{tz_label()}.txt"


def run(prefix: list[str], argv: list[str], cwd: Path, timeout: float) -> tuple[int | str, str, str]:
    env = os.environ.copy()
    env.setdefault("LC_ALL", "C")
    try:
        proc = subprocess.run(
            [*prefix, *argv],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        return "HANG", exc.stdout or "", exc.stderr or ""
    return proc.returncode, proc.stdout, proc.stderr


def parse_envelope(stdout: str) -> tuple[str, dict[str, Any] | None]:
    try:
        return "ok", json.loads(stdout)
    except Exception:
        return "none", None


def normalize_roots(tmp: Path, cwd: Path) -> list[tuple[str, str]]:
    roots: list[tuple[str, str]] = []
    for path, placeholder in ((tmp, "<TMP>"), (cwd, "<CWD>")):
        for spelling in (os.path.realpath(path), str(path)):
            if spelling and (spelling, placeholder) not in roots:
                roots.append((spelling, placeholder))
    return roots


def normalize_paths(text: str, roots: list[tuple[str, str]]) -> str:
    for spelling, placeholder in roots:
        text = text.replace(spelling, placeholder)
    return text


def assert_no_stray_absolute_path(label: str, fields: list[str]) -> None:
    for field in fields:
        stripped = re.sub(r"<(?:TMP|CWD)>[^ |]*", "", field)
        if "/" in stripped:
            raise SystemExit(f"{label} has unnormalized absolute path in signature field: {field}")


def normalize_text(text: str, roots: list[tuple[str, str]] | None = None) -> str:
    if roots:
        text = normalize_paths(text, roots)
    text = re.sub(r"\b20\d\d-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z\b", "<ISO>", text)
    text = re.sub(r"\b1[6-9]\d{8,}(?:\.\d+)?\b", "<EPOCH>", text)
    text = re.sub(r"\b[12]\.\d+e\+\d+\b", "<EPOCH>", text)
    text = re.sub(r"worker=[A-Za-z0-9_.-]+", "worker=<WORKER>", text)
    text = re.sub(r"req_[0-9a-f]+", "req_PINNED", text)
    text = re.sub(r"sha256:[0-9a-f]+", "sha256:PINNED", text)
    text = re.sub(r"/var/folders/[^ \n]+", "<TMP>", text)
    text = re.sub(r"/tmp/[^ \n]+", "<TMP>", text)
    return text


def stable_hash(text: str, roots: list[tuple[str, str]] | None = None) -> str:
    return hashlib.sha256(normalize_text(text, roots).encode("utf-8")).hexdigest()[:16]


def one_line(value: Any, normalize_version: bool) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.6f}".rstrip("0").rstrip(".")
    if isinstance(value, list):
        return ",".join(str(v) for v in value) or "-"
    if normalize_version and isinstance(value, str) and re.fullmatch(r"\d+\.\d+\.\d+", value):
        return VERSION_PLACEHOLDER
    return str(value)


def base_for(verb: str, db: str) -> list[str] | None:
    bases = {
        "add": ["add", "--json", "--db", db, "--", "true"],
        "cancel": ["cancel", "1", "--json", "--db", db],
        "events": ["events", "--json", "--db", db],
        "config-show": ["config-show", "--json", "--db", db],
        "config-validate": ["config-validate", "--json"],
        "doctor": ["doctor", "--json", "--db", db],
        "list": ["list", "--json", "--db", db],
        "output": ["output", "1", "--json", "--db", db],
        "prune": ["prune", "--dry-run", "--older-than", "30d", "--json", "--db", db],
        "retry": ["retry", "1", "--json", "--db", db],
        "show": ["show", "1", "--json", "--db", db],
        "status": ["status", "--json", "--db", db],
        "wait": ["wait", "1", "--json", "--db", db, "--timeout", "0.01"],
        "work": ["work", "--once", "--json", "--db", db],
        "brief": ["brief", "--json"],
        "capabilities": ["capabilities", "--json"],
        "schema": ["schema", "--json"],
        "robot-docs": ["robot-docs", "guide", "--json"],
    }
    return bases.get(verb)


def bad_value(flag: dict[str, Any]) -> str | None:
    expectations = flag.get("malformed_expectations") or {}
    ftype = flag.get("type")
    if "bad_type" in expectations:
        return {
            "duration": "not-a-duration",
            "enum": "__bad__",
            "float": "nan",
            "integer": "9223372036854775808",
            "timestamp": "not-a-time",
        }.get(ftype)
    if "empty_string" in expectations:
        return ""
    if "bad_path" in expectations:
        return "/dev/null/spoolctl.db"
    return None


def insert_flag(argv: list[str], flag: str, value: str | None) -> list[str]:
    tail_index = argv.index("--") if "--" in argv else len(argv)
    extra = [flag] if value is None else [flag, value]
    return [*argv[:tail_index], *extra, *argv[tail_index:]]


def capabilities(prefix: list[str], cwd: Path) -> dict[str, Any]:
    code, out, err = run(prefix, ["capabilities", "--json"], cwd, DEFAULT_TIMEOUT)
    if code != 0:
        raise SystemExit(f"capabilities failed while building signature cases: {err}{out}")
    return json.loads(out)["data"]


def generated_invalid_cases(prefix: list[str], cwd: Path, db: str) -> list[Case]:
    caps = capabilities(prefix, cwd)
    missing_bases = sorted(set(caps["verbs"]) - {
        verb for verb in caps["verbs"] if base_for(verb, db) is not None
    })
    if missing_bases:
        raise SystemExit("missing signature base command(s): " + ", ".join(missing_bases))
    cases: list[Case] = []
    for verb, meta in sorted(caps["verbs"].items()):
        base = base_for(verb, db)
        assert base is not None
        for flag in meta.get("flags", []):
            value = bad_value(flag)
            if value is None:
                continue
            name = flag["flag"]
            label = f"invalid_flag:{verb}:{name}"
            cases.append(Case(label, insert_flag(base, name, value), detail="generated-invalid"))
    return cases


def cases(prefix: list[str], cwd: Path, tmp: Path) -> list[Case]:
    shared_db = str(tmp / "shared.db")
    roundtrip_db = str(tmp / "roundtrip.db")
    add_at_db = str(tmp / "add-at.db")
    add_after_db = str(tmp / "add-after.db")
    rows = [
        Case("meta:capabilities", ["capabilities", "--json"]),
        Case("meta:schema", ["schema", "--json"]),
        Case("meta:brief-text", ["brief"], text_hash=True),
        Case("meta:robot-docs", ["robot-docs", "guide", "--json"]),
        Case("meta:config-show", ["config-show", "--json"]),
        Case("meta:config-validate", ["config-validate", "--json"]),
        Case("invalid:unknown-verb", ["statu", "--json"]),
        Case("invalid:unknown-flag", ["status", "--json", "--bogus", "--db", shared_db]),
        Case("invalid:missing-required", ["--json"]),
        Case("invalid:mutually-exclusive", ["output", "1", "--raw", "--json", "--stream", "stdout", "--db", shared_db]),
        Case("invalid:safety-gate", ["prune", "--older-than", "0", "--db", shared_db, "--json"]),
        Case("schedule:add-at", ["add", "--db", add_at_db, "--json", "--at", "2030-01-02T03:04:05", "--", "echo", "at"]),
        Case("schedule:add-after", ["add", "--db", add_after_db, "--json", "--after", "1h", "--", "echo", "after"]),
        Case("roundtrip:add", ["add", "--db", roundtrip_db, "--json", "--", "printf", "sig-roundtrip"], db_name=roundtrip_db),
        Case("roundtrip:work", ["work", "--once", "--db", roundtrip_db, "--json"], db_name=roundtrip_db),
        Case("roundtrip:wait", ["wait", "1", "--db", roundtrip_db, "--json"], db_name=roundtrip_db),
        Case("roundtrip:doctor", ["doctor", "--db", roundtrip_db, "--json"], db_name=roundtrip_db),
        Case("roundtrip:output-json", ["output", "1", "--stream", "stdout", "--db", roundtrip_db, "--json"], db_name=roundtrip_db),
        Case("roundtrip:show-json", ["show", "1", "--db", roundtrip_db, "--json"], db_name=roundtrip_db),
        Case("roundtrip:list-json", ["list", "--db", roundtrip_db, "--json"], db_name=roundtrip_db),
        Case("text:status", ["status", "--db", roundtrip_db], db_name=roundtrip_db, text_hash=True),
        Case("text:list", ["list", "--db", roundtrip_db], db_name=roundtrip_db, text_hash=True),
        Case("text:show", ["show", "1", "--db", roundtrip_db], db_name=roundtrip_db, text_hash=True),
        Case("text:output", ["output", "1", "--stream", "stdout", "--db", roundtrip_db], db_name=roundtrip_db, text_hash=True),
        Case("text:wait", ["wait", "1", "--db", roundtrip_db], db_name=roundtrip_db, text_hash=True),
    ]
    rows.extend(generated_invalid_cases(prefix, cwd, str(tmp / "generated-invalid.db")))
    return rows


def detail_for(
    case: Case,
    env: dict[str, Any] | None,
    prefix: list[str],
    cwd: Path,
) -> list[str]:
    if env is None:
        return []
    data = env.get("data")
    if case.label == "schedule:add-at" and isinstance(data, dict):
        return [f"next_run_at={one_line(data.get('next_run_at'), False)}"]
    if case.label == "schedule:add-after" and isinstance(data, dict):
        db_path = case.argv[case.argv.index("--db") + 1]
        job_id = str(data["job_id"])
        shown = subprocess.run(
            [*prefix, "show", job_id, "--db", db_path, "--json"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=DEFAULT_TIMEOUT,
        )
        shown_data = json.loads(shown.stdout)["data"]["job"]
        delta = float(shown_data["next_run_at"]) - float(shown_data["created_at"])
        if round(delta, 6) != 3600.0:
            raise SystemExit(f"add --after delta was {delta}, expected 3600.0")
        return [f"after_delta={one_line(delta, False)}"]
    if case.label.startswith("roundtrip:") and isinstance(data, dict):
        if "job_id" in data:
            return [f"job_id={data['job_id']}"]
        if "job" in data:
            return [f"state={data['job'].get('state')}"]
        if "jobs" in data:
            return [f"count={len(data['jobs'])}"]
        if "all_succeeded" in data:
            return [f"all_succeeded={data['all_succeeded']}"]
    return []


def signature(target: str, artifact: Path | None, normalize_version: bool) -> str:
    prefix = target_prefix(target, artifact)
    cwd = REPO if target == "source" else Path(tempfile.gettempdir())
    with tempfile.TemporaryDirectory(prefix="spoolctl-signature-") as td:
        tmp = Path(td)
        roots = normalize_roots(tmp, cwd)
        lines = [
            f"# spoolctl-signature target={target} tz={tz_label()} normalize_version={normalize_version}",
        ]
        for case in cases(prefix, cwd, tmp):
            code, out, err = run(prefix, case.argv, cwd, case.timeout)
            if code == "HANG":
                lines.append(f"{case.label} | exit=HANG")
                continue
            json_state, env = parse_envelope(out)
            first_error = "-"
            warnings: list[str] = []
            ok: Any = "-"
            version: Any = "-"
            if env is not None:
                ok = env.get("ok")
                version = env.get("tool_version")
                errors = env.get("errors") or []
                first_error = errors[0].get("code", "-") if errors else "-"
                warnings = [w.get("code", "-") for w in env.get("warnings") or []]
            fields = [
                case.label,
                f"exit={code}",
                f"json={json_state}",
                f"ok={one_line(ok, normalize_version)}",
                f"err={first_error}",
                f"warnings={one_line(warnings, normalize_version)}",
                f"traceback={'Traceback' in out or 'Traceback' in err}",
                f"version={one_line(version, normalize_version)}",
            ]
            if case.text_hash:
                fields.append(f"stdout_sha={stable_hash(out, roots)}")
            fields.extend(detail_for(case, env, prefix, cwd))
            if case.detail:
                fields.append(f"detail={case.detail}")
            fields = [normalize_paths(field, roots) for field in fields]
            assert_no_stray_absolute_path(case.label, fields)
            lines.append(" | ".join(fields))
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", choices=["source", "single-file"], required=True)
    parser.add_argument("--artifact", type=Path)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--write-baseline", action="store_true")
    parser.add_argument("--normalize-version", action="store_true")
    args = parser.parse_args(argv)

    text = signature(args.target, args.artifact, args.normalize_version)
    baseline = args.baseline or default_baseline(args.target)
    if args.write_baseline:
        baseline.parent.mkdir(parents=True, exist_ok=True)
        baseline.write_text(text, encoding="utf-8")
        print(f"wrote {baseline}")
        return 0
    if baseline.exists():
        expected = baseline.read_text(encoding="utf-8")
        if text != expected:
            sys.stderr.write(f"signature drifted from {baseline}\n")
            for got, want in zip(text.splitlines(), expected.splitlines()):
                if got != want:
                    sys.stderr.write(f"expected: {want}\n")
                    sys.stderr.write(f"got:      {got}\n")
                    break
            if len(text.splitlines()) != len(expected.splitlines()):
                sys.stderr.write(
                    f"line count expected {len(expected.splitlines())},"
                    f" got {len(text.splitlines())}\n"
                )
            return 1
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
