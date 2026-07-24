import os
from pathlib import Path
import shutil
import subprocess

import pytest


def test_run_cli_forwards_python_exit_code() -> None:
    script = (Path(__file__).resolve().parent.parent / "run-cli.ps1").read_text(encoding="utf-8-sig")

    assert "exit $LASTEXITCODE" in script


def test_run_gui_forwards_python_exit_code() -> None:
    script = (Path(__file__).resolve().parent.parent / "run-gui.ps1").read_text(encoding="utf-8-sig")

    assert "exit $LASTEXITCODE" in script


def test_install_supports_python_launcher_or_py_launcher_and_reuses_venv() -> None:
    script = (Path(__file__).resolve().parent.parent / "install.ps1").read_text(encoding="utf-8-sig")

    assert 'Get-PythonLauncherInfo -Command "python"' in script
    assert 'Get-PythonLauncherInfo -Command "py" -PrefixArgs @("-3")' in script
    assert "Test-Path -LiteralPath $venvPython -PathType Leaf" in script
    assert "& $pythonLauncher.Command @pythonLauncherArgs -m venv .venv" in script
    assert "& python -m venv .venv" not in script
    assert "& $venvPython -m pip install -r requirements.txt" in script


def test_powershell_launchers_parse_when_powershell_is_available() -> None:
    powershell = shutil.which("pwsh") or shutil.which("powershell.exe") or shutil.which("powershell")
    if not powershell:
        pytest.skip("PowerShell is not installed on this platform")

    root = Path(__file__).resolve().parent.parent
    scripts = [
        root / "install.ps1",
        root / "run-gui.ps1",
        root / "run-cli.ps1",
        root / "build-exe.ps1",
        root / "build-packages.ps1",
    ]
    parser_command = r"""
$hadErrors = $false
foreach ($path in ($env:UGREEN_TEST_PARSE_PATHS -split [System.IO.Path]::PathSeparator)) {
    $tokens = $null
    $errors = $null
    [System.Management.Automation.Language.Parser]::ParseFile(
        $path,
        [ref]$tokens,
        [ref]$errors
    ) | Out-Null
    if ($errors.Count -gt 0) {
        $hadErrors = $true
        foreach ($errorRecord in $errors) {
            [Console]::Error.WriteLine("{0}: {1}" -f $path, $errorRecord.Message)
        }
    }
}
if ($hadErrors) { exit 1 }
"""
    result = subprocess.run(
        [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", parser_command],
        text=True,
        capture_output=True,
        check=False,
        env={**os.environ, "UGREEN_TEST_PARSE_PATHS": os.pathsep.join(str(path) for path in scripts)},
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_build_exe_stages_only_public_config_allowlist() -> None:
    script = (Path(__file__).resolve().parent.parent / "build-exe.ps1").read_text(
        encoding="utf-8-sig"
    )

    assert '$publicConfigFiles = @(' in script
    assert '"config.example.yml"' in script
    assert '"selectors.yml"' in script
    assert '"update-config.example.json"' in script
    assert "Copy-Item -Recurse -Force .\\config\\*" not in script
    assert 'Assert-PublicDistributionSafe ".\\dist"' in script
    assert '$item.Name -ieq "config.yml"' in script
    assert 'Get-ChildItem -LiteralPath $rootPath -Recurse -Force' in script


def test_build_packages_copies_module_from_a_strict_contract_allowlist() -> None:
    script = (Path(__file__).resolve().parent.parent / "build-packages.ps1").read_text(
        encoding="utf-8-sig"
    )

    assert "function Copy-AutoupdateModuleAllowlist" in script
    assert '"automation\\runner.py"' in script
    assert '"config\\forms.json"' in script
    assert '"config\\materials.json"' in script
    assert '$_.Extension -ieq ".py"' in script
    assert 'foreach ($name in @("requirements.txt", "pyproject.toml"))' in script
    assert "Copy-Item -Recurse -Force -LiteralPath $AutoupdateSrc" not in script
    assert "Copy-Item -Recurse -Force .\\config\\*" not in script
    assert '$fullConfigPath' not in script
    assert 'config\\labels.yml' not in script


def test_build_packages_recursively_rejects_private_and_runtime_entries() -> None:
    script = (Path(__file__).resolve().parent.parent / "build-packages.ps1").read_text(
        encoding="utf-8-sig"
    )

    assert "function Assert-DistributionTreeSafe" in script
    assert "function Assert-NoEmbeddedCredential" in script
    assert 'Get-ChildItem -LiteralPath $rootPath -Recurse -Force' in script
    for forbidden in (".git", ".venv", "state", "logs", "tmp", "__pycache__"):
        assert f'"{forbidden}"' in script
    for extension in (".key", ".pem", ".pfx", ".log", ".tmp"):
        assert f'"{extension}"' in script
    assert '$segment.StartsWith(".")' in script
    assert "\\.local\\." in script
    for backup_marker in ("bak", "backup", "old", "orig", "swp"):
        assert backup_marker in script
    assert "PRIVATE KEY-----" in script
    assert 'Assert-DistributionTreeSafe ".\\dist-public"' in script
    assert 'Assert-DistributionTreeSafe ".\\dist-full" -AllowModule -AllowLauncher' in script


def test_module_allowlist_excludes_decoys_and_final_scan_rejects_them(
    tmp_path: Path,
) -> None:
    powershell = shutil.which("pwsh") or shutil.which("powershell.exe") or shutil.which("powershell")
    if not powershell:
        pytest.skip("PowerShell is not installed on this platform")

    source = tmp_path / "module-source"
    output = tmp_path / "package"
    safe_source_files = {
        "automation/__init__.py": "",
        "automation/runner.py": "print('{}')\n",
        "automation/nested/helper.py": "VALUE = 1\n",
        "config/forms.json": "{}\n",
        "config/materials.json": "{}\n",
        "requirements.txt": "requests\n",
    }
    decoy_source_files = {
        "automation/export.local.py": "TOKEN = 'do-not-copy'\n",
        "automation/.secret.py": "TOKEN = 'do-not-copy'\n",
        "automation/.hidden/inside.py": "TOKEN = 'do-not-copy'\n",
        "automation/helper.backup.py": "TOKEN = 'do-not-copy'\n",
        "state/accounts.local.json": '{"token": "do-not-copy"}\n',
        ".env": "TOKEN=do-not-copy\n",
        ".venv/lib/secret.py": "TOKEN = 'do-not-copy'\n",
        "logs/run.log": "do-not-copy\n",
    }
    for relative, contents in {**safe_source_files, **decoy_source_files}.items():
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")

    (output / "config").mkdir(parents=True)
    for relative, contents in {
        "UGREEN-NAS-Test.exe": "placeholder",
        "config/config.example.yml": "admin: {}\n",
        "config/selectors.yml": "pages: {}\n",
        "config/update-config.json": '{"token": ""}\n',
    }.items():
        (output / relative).write_text(contents, encoding="utf-8")

    root = Path(__file__).resolve().parent.parent
    command = r"""
$scriptPath = $env:UGREEN_TEST_PACKAGE_SCRIPT
$sourceRoot = $env:UGREEN_TEST_MODULE_SOURCE
$outputRoot = $env:UGREEN_TEST_PACKAGE_OUTPUT
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    $scriptPath, [ref]$tokens, [ref]$errors
)
if ($errors.Count -gt 0) { throw "build-packages.ps1 did not parse" }
$wanted = @(
    "Get-NormalizedRelativePath",
    "Test-ForbiddenRelativePath",
    "Assert-NoEmbeddedCredential",
    "Copy-AutoupdateModuleAllowlist",
    "Assert-DistributionTreeSafe"
)
$definitions = @($ast.FindAll({
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
        $wanted -contains $node.Name
}, $true))
if ($definitions.Count -ne $wanted.Count) { throw "Could not extract packaging safety functions" }
foreach ($definition in $definitions) {
    . ([scriptblock]::Create($definition.Extent.Text))
}
$forbiddenDistributionSegments = @(
    ".git", ".venv", "venv", "state", "log", "logs", "tmp", "temp",
    "build", "dist", "apk", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"
)
$forbiddenDistributionExtensions = @(
    ".key", ".pem", ".pfx", ".p12", ".ppk", ".kdbx", ".log", ".tmp",
    ".bak", ".backup", ".old", ".orig", ".save", ".swp", ".swo"
)
$testPackages = @()
$moduleOut = Join-Path $outputRoot "ugreen-nas-autoupdate"
Copy-AutoupdateModuleAllowlist -SourceRoot $sourceRoot -DestinationRoot $moduleOut

$moduleRootPath = (Resolve-Path -LiteralPath $moduleOut).Path.TrimEnd('\', '/')
$actual = @(Get-ChildItem -LiteralPath $moduleRootPath -Recurse -File -Force | ForEach-Object {
    $_.FullName.Substring($moduleRootPath.Length).TrimStart([char[]]"\/").Replace('\', '/')
} | Sort-Object)
$expected = @(
    "automation/__init__.py",
    "automation/nested/helper.py",
    "automation/runner.py",
    "config/forms.json",
    "config/materials.json",
    "requirements.txt"
) | Sort-Object
if (@(Compare-Object -ReferenceObject $expected -DifferenceObject $actual).Count -ne 0) {
    throw "Module source allowlist copied an unexpected file: $($actual -join ', ')"
}
Assert-DistributionTreeSafe -Root $outputRoot -AllowModule

foreach ($relative in @(
    "automation/export.local.py",
    "automation/.secret.py",
    "automation/helper.backup.py",
    "automation/credentials.py"
)) {
    $decoy = Join-Path $moduleOut $relative
    [System.IO.File]::WriteAllText($decoy, "TOKEN = 'do-not-ship-secret'")
    $rejected = $false
    try { Assert-DistributionTreeSafe -Root $outputRoot -AllowModule } catch { $rejected = $true }
    if (-not $rejected) { throw "Final scan accepted decoy $relative" }
    Remove-Item -Force -LiteralPath $decoy
}
"allowlist-decoys-rejected"
"""
    result = subprocess.run(
        [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        text=True,
        capture_output=True,
        check=False,
        env={
            **os.environ,
            "UGREEN_TEST_PACKAGE_SCRIPT": str(root / "build-packages.ps1"),
            "UGREEN_TEST_MODULE_SOURCE": str(source),
            "UGREEN_TEST_PACKAGE_OUTPUT": str(output),
        },
    )

    assert result.returncode == 0, result.stderr or result.stdout
    assert "allowlist-decoys-rejected" in result.stdout


def test_full_package_launcher_creates_local_config_before_starting_app() -> None:
    script = (Path(__file__).resolve().parent.parent / "build-packages.ps1").read_text(
        encoding="utf-8-sig"
    )

    copy_template = 'copy /Y ""%~dp0config\\config.example.yml"" ""%~dp0config\\config.yml""'
    open_editor = 'start """" notepad.exe ""%~dp0config\\config.yml""'
    exit_for_edit = '"  exit /b 2"'
    start_app = 'start """" ""%~dp0UGREEN-NAS-Test.exe""'
    assert copy_template in script
    assert open_editor in script
    assert exit_for_edit in script
    assert script.index(copy_template) < script.index(exit_for_edit) < script.index(start_app)
    assert "where.exe python.exe" in script
    assert 'import sys, requests, tkinter; assert sys.version_info >= (3, 11)' in script
    assert "UGREEN_AUTOUPDATE_PYTHON" in script
    assert script.index("sys.version_info >= (3, 11)") < script.index(start_app)


def test_release_workflow_stages_and_verifies_exact_public_allowlist() -> None:
    workflow = (Path(__file__).resolve().parent.parent / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8"
    )

    assert "Copy-Item -Recurse -Force config\\*" not in workflow
    assert '$allowedConfigFiles = @("config.example.yml", "selectors.yml", "update-config.json")' in workflow
    assert "Release config must contain exactly the three public files" in workflow
    assert 'Get-ChildItem -LiteralPath $configRoot -Recurse -Force' in workflow
    assert 'Get-ChildItem -LiteralPath $checkRoot -Recurse -Force' in workflow
    assert "Release zip contains a non-allowlisted file" in workflow
    assert "Release zip contains a forbidden path" in workflow
    for forbidden in (".git", ".venv", "state", "logs", "tmp"):
        assert f'"{forbidden}"' in workflow


def test_manual_release_is_create_only_and_binds_new_tag_to_checked_out_sha() -> None:
    workflow = (Path(__file__).resolve().parent.parent / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8"
    )

    assert "group: release-${{ github.event.inputs.tag || github.ref_name }}" in workflow
    assert 'git ls-remote --exit-code --tags origin "refs/tags/$tag"' in workflow
    assert "workflow_dispatch refuses to reuse existing tag" in workflow
    assert "Release tag must be semantic version form" in workflow
    assert '"target_sha=$headCommit"' in workflow
    assert "TARGET_SHA: ${{ steps.source.outputs.target_sha }}" in workflow
    assert '"--target", $env:TARGET_SHA' in workflow


def test_release_workflow_does_not_interpolate_tag_values_into_powershell_source() -> None:
    workflow = (Path(__file__).resolve().parent.parent / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8"
    )

    assert 'VERSION_NAME: ${{ steps.tag.outputs.name }}' in workflow
    assert 'VERSION_CODE: ${{ steps.code.outputs.code }}' in workflow
    assert 'RELEASE_TAG: ${{ steps.tag.outputs.tag }}' in workflow
    assert 'VERSION_NAME = "${{ steps.tag.outputs.name }}"' not in workflow
    assert '[int]"${{ steps.code.outputs.code }}"' not in workflow
    assert '"${{ steps.tag.outputs.tag }}" | Out-String' not in workflow
    assert '"UGREEN-NAS-Test-${{ steps.tag.outputs.name }}.zip"' not in workflow


def test_tag_push_release_verifies_source_and_refuses_asset_overwrite() -> None:
    workflow = (Path(__file__).resolve().parent.parent / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8"
    )

    assert 'git rev-list -n 1 "refs/tags/$tag"' in workflow
    assert "does not match tag" in workflow
    assert "releases/tags/$escapedTag" in workflow
    assert "already exists; refusing to overwrite its metadata or assets" in workflow
    assert '"release", "create", $env:RELEASE_TAG' in workflow
    assert "uses: softprops/action-gh-release" not in workflow
    assert "no existing release or asset was overwritten" in workflow
