"""Offline installed-wheel proof. All operational receipts stay outside Git."""
from __future__ import annotations

import argparse
from email.parser import BytesParser
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import zipfile

CONSUMER_TEST_NAMES = frozenset({
    "test_origin_and_worktree_proof_leave_git_unchanged",
    "test_unresolved_origin_is_partial_not_drift",
    "test_missing_origin_file_or_anchor_is_drift",
    "test_expect_and_observations_do_not_promote_a_planned_flow",
    "test_empty_and_expired_observations_are_not_health_proof",
    "test_public_tour_is_deterministic_and_refusal_preserves_output",
    "test_workspace_and_stale_tour_through_positional_cli",
    "test_positional_route_explain_render_and_docs_are_deterministic",
})


def clean_environment(environment=None):
    env = dict(os.environ if environment is None else environment)
    for name in list(env):
        if (name.upper() in {"PYTHONPATH", "PYTHONHOME", "PYTHONOPTIMIZE"}
                or name.upper().startswith(("PIP_", "ATLAS_QUALIFICATION_"))):
            del env[name]
    env.update(PYTHONIOENCODING="utf-8", GIT_OPTIONAL_LOCKS="0",
               PIP_NO_INDEX="1", PIP_DISABLE_PIP_VERSION_CHECK="1",
               PIP_CONFIG_FILE=os.devnull)
    return env


def validate_installed(probe, consumer):
    expected = Path(consumer).resolve()
    prefix = Path(probe["prefix"]).resolve()
    site = Path(probe["site_packages"]).resolve()
    module = Path(probe["module"]).resolve()
    if prefix != expected or not site.is_relative_to(expected) or not module.is_relative_to(site):
        raise ValueError("installed package must import from the consumer venv's site-packages")
    if probe.get("runtime_requirements") != []:
        raise ValueError("installed package must have no runtime requirements")


def validate_consumer_result(result, commands):
    names = result.get("test_names", [])
    if (not isinstance(names, list) or set(names) != CONSUMER_TEST_NAMES
            or len(names) != len(CONSUMER_TEST_NAMES)
            or result.get("tests_run") != len(CONSUMER_TEST_NAMES)):
        raise ValueError("consumer suite must execute every known qualification test")
    if (result.get("successful") is not True
            or any(result.get(key) != 0 for key in ("failures", "errors", "skipped"))
            or any(result.get(key, 0) != 0 for key in ("expected_failures", "unexpected_successes"))):
        raise ValueError("consumer suite must pass without failures, errors or skips")
    if not commands:
        raise ValueError("consumer suite must record actual CLI commands")


def require_outside_git(path):
    path = Path(path).resolve()
    for parent in (path, *path.parents):
        if (parent / ".git").exists():
            raise ValueError("qualification receipts and work directories must be outside Git checkouts")


def copy_source(source, target):
    shutil.copytree(source, target, ignore=shutil.ignore_patterns(
        ".git", ".venv", "__pycache__", "*.pyc", "build", "dist", "*.egg-info"))


def discover_build_wheels(directories):
    """Inspect local wheels and pip's cached HTTP bodies; never contact an index."""
    found = []
    seen = set()
    for directory in directories:
        directory = Path(directory)
        for path in sorted(directory.rglob("*")):
            if path.suffix not in {".whl", ".body"} or path in seen:
                continue
            seen.add(path)
            try:
                with zipfile.ZipFile(path) as archive:
                    metadata = [n for n in archive.namelist()
                                if n.count("/") == 1 and n.endswith(".dist-info/METADATA")]
                    if len(metadata) != 1:
                        continue
                    message = BytesParser().parsebytes(archive.read(metadata[0]))
                    name, version = message["Name"], message["Version"]
                    if name not in {"setuptools", "wheel", "packaging"} or not version:
                        continue
                    wheel = BytesParser().parsebytes(archive.read(metadata[0].rsplit("/", 1)[0] + "/WHEEL"))
                    tags = wheel.get_all("Tag", [])
                    if not tags:
                        continue
                    # Build tooling is pure Python; let pip enforce Requires-Python.
                    if any(tag.split("-")[1:] != ["none", "any"] for tag in tags):
                        continue
                    python_tags = ".".join(sorted({tag.split("-")[0] for tag in tags}))
                    filename = f"{name}-{version}-{python_tags}-none-any.whl"
                    found.append({"path": str(path), "filename": filename,
                                  "name": name, "version": version})
            except (OSError, zipfile.BadZipFile, KeyError):
                continue
    return found


def venv_python(directory):
    return directory / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def tree_digest(directory):
    digest = hashlib.sha256()
    for path in sorted(directory.rglob("*")):
        if not path.is_file() or any(part in {".git", "__pycache__", ".venv", "build", "dist"}
                                     or part.endswith(".egg-info") for part in path.relative_to(directory).parts):
            continue
        digest.update(path.relative_to(directory).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def qualify(source, python, report_path, wheelhouses=()):
    source = Path(source).resolve()
    report_path = Path(report_path).resolve()
    require_outside_git(report_path)
    report = {"schema": "estate-atlas-wheel-qualification@1", "status": "failed", "commands": []}
    env = clean_environment()

    def run(command, cwd, *, environment=None, check=True):
        command = list(map(str, command))
        try:
            result = subprocess.run(command, cwd=cwd, env=env if environment is None else environment,
                                    capture_output=True, timeout=300)
        except subprocess.TimeoutExpired as error:
            report["commands"].append({"command": command, "cwd": str(cwd), "returncode": None,
                                       "error": "timeout after 300 seconds",
                                       "stdout": (error.stdout or b"").decode("utf-8", errors="replace"),
                                       "stderr": (error.stderr or b"").decode("utf-8", errors="replace")})
            raise
        except OSError as error:
            report["commands"].append({"command": command, "cwd": str(cwd), "returncode": None,
                                       "error": str(error)})
            raise
        report["commands"].append({"command": command, "cwd": str(cwd), "returncode": result.returncode,
                                   "stdout": result.stdout.decode("utf-8", errors="replace"),
                                   "stderr": result.stderr.decode("utf-8", errors="replace")})
        if check and result.returncode:
            raise RuntimeError(f"command failed ({result.returncode}): {command!r}")
        return result

    try:
        identity = run([python, "-c", "import json,sys; print(json.dumps({'version':sys.version,'executable':sys.executable,'version_info':list(sys.version_info[:3])}))"], report_path.parent)
        report["interpreter"] = json.loads(identity.stdout)
        if report["interpreter"]["version_info"] < [3, 11, 0]:
            raise RuntimeError("qualification requires Python 3.11 or later")
        git = ["git", "-c", f"safe.directory={source}", "-C", source]
        report["source_head"] = run([*git, "rev-parse", "HEAD"], report_path.parent).stdout.decode().strip()
        report["source_status"] = run([*git, "status", "--porcelain=v1", "--untracked-files=all"], report_path.parent).stdout.decode()
        directories = list(map(Path, wheelhouses))
        cached = run([python, "-m", "pip", "cache", "dir"], report_path.parent, check=False)
        if cached.returncode == 0:
            directories.append(Path(cached.stdout.decode().strip()))
        report["cache_directories"] = list(map(str, directories))
        report["cached_build_wheels"] = discover_build_wheels(directories)
        if not any(wheel["name"] == "setuptools" for wheel in report["cached_build_wheels"]):
            raise RuntimeError("no cached setuptools wheel found; supply an offline --wheelhouse with setuptools>=68 and wheel")
        with tempfile.TemporaryDirectory(prefix="atlas wheel qualification ") as temporary:
            workspace = Path(temporary).resolve()
            require_outside_git(workspace)
            disposable = workspace / "disposable source"
            copy_source(source, disposable)
            report["source_sha256"] = tree_digest(disposable)
            tools = workspace / "cached build tools"
            tools.mkdir()
            for wheel in report["cached_build_wheels"]:
                target = tools / wheel["filename"]
                shutil.copyfile(wheel["path"], target)
                wheel["sha256"] = hashlib.sha256(target.read_bytes()).hexdigest()
            build = workspace / "build env"
            run([python, "-m", "venv", build], workspace)
            builder = venv_python(build)
            run([builder, "-m", "pip", "install", "--no-index", "--find-links", tools,
                 "setuptools>=68", "wheel"], workspace)
            versions = run([builder, "-c", "import importlib.metadata as m,json; print(json.dumps({n:m.version(n) for n in ['pip','setuptools','wheel']}))"], workspace)
            report["build_tools"] = json.loads(versions.stdout)
            wheels = workspace / "built wheels"
            wheels.mkdir()
            run([builder, "-m", "pip", "wheel", "--no-index", "--no-deps", "--no-build-isolation",
                 "--wheel-dir", wheels, disposable], workspace)
            built = list(wheels.glob("*.whl"))
            if len(built) != 1:
                raise RuntimeError("build must produce exactly one wheel")
            wheel = built[0]
            report["wheel"] = {"filename": wheel.name, "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest()}
            consumer = workspace / "installed consumer env"
            run([python, "-m", "venv", consumer], workspace)
            installed = venv_python(consumer)
            run([installed, "-m", "pip", "install", "--no-index", "--no-deps", wheel], workspace)
            outside = workspace / "outside checkout"
            outside.mkdir()
            shutil.copytree(disposable / "examples", outside / "examples")
            (outside / "tests/fixtures").mkdir(parents=True)
            shutil.copyfile(disposable / "tests/fixtures/shop-overlay.json", outside / "tests/fixtures/shop-overlay.json")
            test = outside / "test_consumer_qualification.py"
            shutil.copyfile(disposable / "tests/test_consumer_qualification.py", test)
            probe = run([installed, "-c", (
                "import estate_atlas,importlib.metadata as m,json,sys,sysconfig,pathlib; "
                "location=pathlib.Path(estate_atlas.__file__).resolve(); "
                "site=pathlib.Path(sysconfig.get_path('purelib')).resolve(); "
                "d=m.distribution('estate-atlas'); "
                "print(json.dumps({'module':str(location),'site_packages':str(site),'prefix':sys.prefix,"
                "'version':d.version,'runtime_requirements':d.requires or [],'pip':m.version('pip')}))"
            ), consumer], outside)
            report["installed"] = json.loads(probe.stdout)
            validate_installed(report["installed"], consumer)
            run([installed, "-m", "pip", "check"], outside)
            console = consumer / ("Scripts/estate-atlas.exe" if os.name == "nt" else "bin/estate-atlas")
            for name, command in (("module", [installed, "-m", "estate_atlas"]), ("console", [console])):
                run([*command, "--help"], outside)
                command_log = outside / f"{name}-commands.jsonl"
                result_path = outside / f"{name}-result.json"
                test_env = dict(env, ATLAS_QUALIFICATION_COMMAND=json.dumps(list(map(str, command))),
                                ATLAS_QUALIFICATION_ROOT=str(outside),
                                ATLAS_QUALIFICATION_COMMAND_LOG=str(command_log),
                                ATLAS_QUALIFICATION_RESULT=str(result_path))
                result = run([installed, test, "-v"], outside, environment=test_env, check=False)
                report.setdefault("consumer_commands", {})[name] = [
                    json.loads(line) for line in command_log.read_text(encoding="utf-8").splitlines()
                ] if command_log.exists() else []
                suite_result = json.loads(result_path.read_text(encoding="utf-8")) if result_path.exists() else {}
                report.setdefault("consumer_results", {})[name] = suite_result
                validate_consumer_result(suite_result, report["consumer_commands"][name])
                if result.returncode:
                    raise RuntimeError(f"{name} consumer qualification failed ({result.returncode})")
                report.setdefault("consumer_entrypoints", []).append(name)
            report["status"] = "passed"
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        report["error"] = str(error)
    finally:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--python", default=sys.executable, help="Python 3.11+ interpreter to qualify")
    parser.add_argument("--wheelhouse", type=Path, action="append", default=[], help="additional offline build-tool cache (repeatable)")
    parser.add_argument("--report", type=Path, required=True, help="JSON receipt outside every Git checkout")
    args = parser.parse_args(argv)
    try:
        require_outside_git(args.report)
        args.report.resolve().parent.mkdir(parents=True, exist_ok=True)
        report = qualify(args.source, args.python, args.report, args.wheelhouse)
    except (OSError, ValueError) as error:
        parser.exit(2, f"qualification refused: {error}\n")
    print(f"qualification {report['status']}; receipt: {args.report.resolve()}")
    if report.get("error"):
        print(report["error"], file=sys.stderr)
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
