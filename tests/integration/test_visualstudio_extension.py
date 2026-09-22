"""Optional cross-language contract checks. .NET 8 SDK must be on PATH."""
import io
import json
import re
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import xml.etree.ElementTree as ET

import pytest

from autograde.platform_bundle import BundleStore
from autograde.platform_bundle_worker import BundleSubmissionProcessor
from autograde.platform_grader import PilotLocalGrader
from autograde.workspace import WorkspaceBuilder
from test_course_portal import portal, claim, login, request, cookie, csrf, WEB

ROOT = Path(__file__).resolve().parents[2]
PROJECT = ROOT / "extensions/visualstudio/Autograde.Checks/Autograde.Checks.csproj"


def test_vsix_registration_and_packaging_contract():
    directory = ROOT / "extensions/visualstudio/Autograde.VisualStudio"
    manifest = ET.parse(directory / "source.extension.vsixmanifest")
    ns = {"v": "http://schemas.microsoft.com/developer/vsx-schema/2011"}
    target = manifest.find("v:Installation/v:InstallationTarget", ns)
    assert target.attrib == {"Id": "Microsoft.VisualStudio.Community", "Version": "[17.0,)"}
    assert target.find("v:ProductArchitecture", ns).text == "amd64"
    assert manifest.find("v:Assets/v:Asset", ns).attrib["Type"] == "Microsoft.VisualStudio.VsPackage"
    commands = ET.parse(directory / "Commands.vsct")
    c = {"c": "http://schemas.microsoft.com/VisualStudio/2005-10-18/CommandTable"}
    guid = commands.find("c:Symbols/c:GuidSymbol[@name='guidPackage']", c).attrib["value"].strip("{}")
    assert f'Guid("{guid}")' in (directory / "AutogradePackage.cs").read_text()
    project = ET.parse(directory / "Autograde.VisualStudio.csproj")
    assert any("Microsoft.VsSDK.targets" in item.attrib.get("Project", "") for item in project.findall("Import"))
    assert project.find("ItemGroup/VSCTCompile/ResourceName").text == "Menus.ctmenu"
    assert 'ProvideMenuResource("Menus.ctmenu", 1)' in (directory / "AutogradePackage.cs").read_text()


def test_compact_layout_keeps_results_separate_from_secondary_actions():
    """Layout wiring guard; native sizing still requires Windows visual QA."""
    directory = ROOT / "extensions/visualstudio/Autograde.VisualStudio"
    source = (directory / "AssignmentControl.cs").read_text()
    assert 'Tuple.Create("과제", taskPanel)' in source
    assert 'Tuple.Create("결과", resultPanel)' in source
    assert 'Tuple.Create("기록", historyPanel)' in source
    assert 'new GridLength(1, GridUnitType.Star)' in source
    assert 'Grid.SetRow(pages, 1)' in source and 'Grid.SetRow(footer, 2)' in source
    assert 'HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled' in source
    assert 'downloadButton.Visibility = authenticated && selected != null && !ready' in source
    assert 'submitButton.Visibility = authenticated && ready' in source
    assert 'TextWrapping = TextWrapping.Wrap' in source
    assert 'ThemeResources.Tabs(pages)' in source


def test_toolbar_alignment_and_signed_out_visibility_contract():
    """WPF wiring only; native layout at Windows DPI requires manual confirmation."""
    directory = ROOT / 'extensions/visualstudio/Autograde.VisualStudio'
    ui = (directory / 'AssignmentControl.cs').read_text()
    theme = (directory / 'ThemeResources.cs').read_text()
    assert 'var toolbar = new Grid()' in ui
    assert 'toolbar.ActualWidth < 300' in ui
    assert 'Grid.SetRow(chrome, narrow ? 1 : 0)' in ui
    assert 'button.Margin = new Thickness(8, 0, 0, 0)' in ui
    assert 'button.VerticalAlignment = VerticalAlignment.Center' in ui
    assert 'button.MinHeight = 32' in theme
    assert 'button.Padding = new Thickness(10, 4, 10, 4)' in theme
    assert 'logoutButton.Visibility = authenticated ? Visibility.Visible : Visibility.Collapsed' in ui
    assert 'assignmentManagement.Visibility = folderManagement.Visibility = authenticated' in ui
    assert 'output.TextChanged += (_, __) => UpdateOutputVisibility()' in ui
    assert 'string.IsNullOrWhiteSpace(output.Text) ? Visibility.Collapsed : Visibility.Visible' in ui


def test_logout_ui_does_not_require_valid_address():
    """Source contract only; Windows WPF interaction remains a manual test."""
    source = (ROOT / "extensions/visualstudio/Autograde.VisualStudio/AssignmentControl.cs").read_text()
    action = source.split('AddButton(chrome, "로그아웃"', 1)[1].split('var cancel =', 1)[0]
    assert "RequireClient()" not in action
    assert "client.LogoutAsync(ct)" in action
    assert "finally { ClearScreen();" in action


def test_vsix_explicitly_includes_json_dependency():
    """VSSDK's default suppression list includes Newtonsoft.Json.dll."""
    directory = ROOT / "extensions/visualstudio"
    extension = ET.parse(directory / "Autograde.VisualStudio/Autograde.VisualStudio.csproj")
    core = ET.parse(directory / "Autograde.Core/Autograde.Core.csproj")
    dependencies = extension.findall("ItemGroup/PackageReference[@Include='Newtonsoft.Json']")
    assert len(dependencies) == 1
    dependency = dependencies[0]
    assert dependency.attrib["GeneratePathProperty"] == "true"
    assert "ForceIncludeInVSIX" not in dependency.attrib
    assert dependency.attrib["Version"] == core.find(
        "ItemGroup/PackageReference[@Include='Newtonsoft.Json']"
    ).attrib["Version"]
    assert extension.find("PropertyGroup/CopyLocalLockFileAssemblies").text == "true"
    content = extension.find(
        "ItemGroup/Content[@Include='$(PkgNewtonsoft_Json)/lib/net45/Newtonsoft.Json.dll']"
    )
    assert content is not None
    assert content.find("Link").text == "Newtonsoft.Json.dll"
    assert content.find("IncludeInVSIX").text == "true"
    # Keep the final ZIP inspection: compiling alone cannot prove packaging worked.
    build = (directory / "build.ps1").read_text()
    assert "'Newtonsoft.Json.dll'" in build
    assert 'throw "VSIX dependency missing: $required"' in build


def test_theme_release_preserves_upgrade_identity_and_versions():
    directory = ROOT / 'extensions/visualstudio/Autograde.VisualStudio'
    ns = {'v': 'http://schemas.microsoft.com/developer/vsx-schema/2011'}
    identity = ET.parse(directory / 'source.extension.vsixmanifest').find('v:Metadata/v:Identity', ns)
    assert identity.attrib['Id'] == 'Autograde.VisualStudio.74db5571-a3ad-4451-a5f4-e8cc28d20536'
    assert identity.attrib['Version'] == '0.5.6'
    project = ET.parse(directory / 'Autograde.VisualStudio.csproj')
    assert project.find('PropertyGroup/Version').text == identity.attrib['Version']
    assert '"0.5.6")]' in (directory / 'AutogradePackage.cs').read_text()
    assert '["extension_version"] = "0.5.6"' in (directory.parent / 'Autograde.Core/ServiceClient.cs').read_text()
    build = (directory.parent / 'build.ps1').read_text()
    assert '$builtIdentity.Id -ne $expectedIdentity.Id' in build
    assert '$builtIdentity.Version -ne $expectedIdentity.Version' in build


def test_download_opens_installed_folder_without_deleting_files_on_open_failure():
    """Source contract; native save/trust dialogs still need a Windows IDE test."""
    directory = ROOT / 'extensions/visualstudio/Autograde.VisualStudio'
    ui = (directory / 'AssignmentControl.cs').read_text()
    download = ui.split('async Task DownloadAsync(', 1)[1].split('async Task ReportDownloadAsync', 1)[0]
    assert download.index('Bundle.ExtractStarter(') < download.index('folder.Text = target;') < download.index('await OpenDownloadedFolderAsync(target, ct, diagnostic)')
    opening = ui.split('async Task OpenDownloadedFolderAsync(', 1)[1].split('void RequireClient()', 1)[0]
    assert 'catch (OperationCanceledException ex)' in opening and 'catch (Exception ex)' in opening
    assert 'DownloadDiagnostic.OpenRecoveryGuidance(target)' in opening
    assert 'Delete(' not in opening
    adapter = (directory / 'WorkspaceOpener.cs').read_text()
    assert adapter.index('SwitchToMainThreadAsync(cancel)') < adapter.index('solution.OpenFolder(fullPath)')
    assert 'GetSolutionInfo' in adapter and 'StringComparison.OrdinalIgnoreCase' in adapter
    assert 'Directory.Exists(directory)' in adapter
    assert 'Process.Start' not in adapter and 'ExecuteCommand' not in adapter
    assert 'Bundle.VerifyWorkspace(folder.Text, client.BaseUrl, Id)' in ui


def test_extension_manager_description_and_open_recovery_are_connected():
    directory = ROOT / 'extensions/visualstudio/Autograde.VisualStudio'
    ns = {'v': 'http://schemas.microsoft.com/developer/vsx-schema/2011'}
    description = ET.parse(directory / 'source.extension.vsixmanifest').find('v:Metadata/v:Description', ns).text
    for phrase in ('도구 → Autograde 과제', 'API 주소', '수령 코드', '수강 등록', '이전 공개 최고점',
                   '파일 → 열기 → 폴더', '문의번호', '로그아웃', 'VS Code용 아님'):
        assert phrase in description
    assert len(description) <= 4000
    ui = (directory / 'AssignmentControl.cs').read_text()
    assert 'ShowDownloadDiagnostic(diagnostic, target)' in ui
    presentation = ui.split('void ShowDownloadDiagnostic(', 1)[1].split('async Task ReportDownloadAsync', 1)[0]
    assert 'OpenRecoveryGuidance(target)' in presentation and '저장 위치: ' in presentation
    retry = ui.split('folderButton = AddButton', 1)[1].split('AddButton(panel, "기존 과제 폴더 선택"', 1)[0]
    assert 'Bundle.VerifyWorkspace' in retry and 'await ReportDownloadAsync(Id, diagnostic)' in retry
    assert 'ShowDownloadDiagnostic(diagnostic, folder.Text)' in retry
    assert 'StarterAsync' not in retry and 'ExtractStarter' not in retry


def test_wpf_theme_uses_dynamic_resources_and_preserves_password_masking():
    """Source wiring regression only, not a Windows WPF rendering test."""
    directory = ROOT / 'extensions/visualstudio/Autograde.VisualStudio'
    theme = (directory / 'ThemeResources.cs').read_text()
    ui = (directory / 'AssignmentControl.cs').read_text()
    assert 'new DynamicResourceExtension(key)' in theme
    assert 'SetResourceReference' in theme
    for key in ('ToolWindowBackgroundBrushKey', 'ToolWindowTextBrushKey',
                'ThemedDialogTextBoxStyleKey', 'ThemedDialogButtonStyleKey', 'ThemedDialogListBoxStyleKey',
                'SelectedItemActiveBrushKey', 'SelectedItemActiveTextBrushKey',
                'SelectedItemInactiveBrushKey', 'SelectedItemInactiveTextBrushKey',
                'TextBoxBackgroundDisabledBrushKey', 'TextBoxTextDisabledBrushKey',
                'TextBoxBorderFocusedBrushKey', 'FocusVisualBorderBrushKey'):
        assert key in theme
    assert 'ThemeResources.Apply(this, claim, new[] { assignments, history }, address, folder, output, gradingOutput, diagnosticText)' in ui
    assert 'ThemeResources.Button(cancel)' in ui and 'ThemeResources.Button(button)' in ui
    assert 'ThemeResources.Label(status)' in ui and 'ThemeResources.Label(label)' in ui
    assert 'new PasswordBox { MaxLength = 256 }' in ui and 'claim.Clear()' in ui
    assert '"PART_ContentHost"' in theme and 'typeof(PasswordBox)' in theme
    assert 'VSColorTheme.ThemeChanged +=' not in theme  # No static event subscription to leak.
    assert 'Color.FromRgb' not in theme and 'Brushes.White' not in theme and 'Brushes.Black' not in theme


def test_acceptance_does_not_wait_for_grading_and_background_has_identity_fences():
    """Source wiring guard; runtime polling behavior is covered by C# checks."""
    source = (ROOT / 'extensions/visualstudio/Autograde.VisualStudio/AssignmentControl.cs').read_text()
    submit = source.split('AddButton(footerActions, "파일 확인 후 제출"', 1)[1].split('panel = resultPanel;', 1)[0]
    assert 'await client.SubmitAsync' in submit
    assert 'ResultAsync' not in submit and 'Task.Delay' not in submit
    assert 'receiptStatus.Text' in submit and 'PauseGradingWatch();' in submit
    assert 'else StartGradingWatch();' in source
    assert 'ReferenceEquals(receiptWatch, watch)' in source
    assert 'ReferenceEquals(client, current)' in source
    assert 'ReferenceEquals(gradingPoll, cancellation)' in source
    assert 'if (IsCurrent()) ShowResult(result, gradingOutput)' in source
    assert 'PauseGradingWatch(); receiptWatch = null;' in source
    assert 'while (busy) await Task.Delay(200, ct);' in source


def test_result_cards_are_themed_and_cleared_at_session_boundary():
    directory = ROOT / 'extensions/visualstudio/Autograde.VisualStudio'
    ui = (directory / 'AssignmentControl.cs').read_text()
    view = (directory / 'ResultView.cs').read_text()
    assert 'panel.Children.Add(liveResult)' in ui
    assert 'panel.Children.Add(inspectedResult)' in ui
    assert 'liveResult.Clear(); inspectedResult.Clear();' in ui
    assert '과거 제출 기록 · ' in ui
    assert 'ResultPresentation.From(result)' in view
    assert 'ThemeResources.Label(text)' in view
    assert 'SetResourceReference' in view
    assert 'AutomationProperties.SetName' in view
    assert 'model.Details' in view
    assert 'new Expander' in view


@pytest.fixture(scope="module")
def dotnet_client():
    dotnet = shutil.which("dotnet")
    if not dotnet:
        pytest.skip("Visual Studio protocol tests require the .NET 8 SDK on PATH")
    build = subprocess.run([dotnet, "build", str(PROJECT), "--nologo"], capture_output=True, text=True, timeout=180)
    assert build.returncode == 0, build.stdout + build.stderr
    return [dotnet, str(PROJECT.parent / "bin/Debug/net8.0/Autograde.Checks.dll")]


def run(client, *args):
    return subprocess.run([*client, *map(str, args)], capture_output=True, text=True, timeout=40)


def test_visualstudio_client_self_checks(dotnet_client):
    result = run(dotnet_client)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "checks passed" in result.stdout


def test_csharp_submission_accepted_by_python(dotnet_client, tmp_path):
    root = tmp_path.resolve() / "source"
    root.mkdir()
    (root / "main.cpp").write_text("int main(){return 0;}\n")
    (root / "한글").mkdir()
    (root / "한글" / "header.hpp").write_text("// unicode PAX\n")
    (root / ".vs").mkdir()
    (root / ".vs" / "secret.txt").write_text("excluded")
    archive = tmp_path / "submission.tar.gz"
    result = run(dotnet_client, "pack", root, archive)
    assert result.returncode == 0, result.stderr
    artifact = BundleStore(tmp_path / "bundles").ingest(archive, expected_kind="submission")
    assert artifact.metadata.file_count == 2
    assert {entry.path for entry in artifact.metadata.files} == {"main.cpp", "한글/header.hpp"}


def test_python_starter_accepted_by_csharp(dotnet_client, tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    (root / "main.c").write_text("int main(void){return 0;}\n")
    (root / "한글").mkdir()
    (root / "한글" / "설명.txt").write_text("Hello, World!")
    (root / "empty").mkdir()
    artifact = BundleStore(tmp_path / "bundles").create_from_directory(root, kind="starter")
    target = tmp_path.resolve() / "download"
    result = run(dotnet_client, "extract", artifact.path, target)
    assert result.returncode == 0, result.stderr
    assert (target / "한글" / "설명.txt").read_text() == "Hello, World!"
    assert (target / "empty").is_dir()
    assert json.loads((target / ".autograde/assignment.json").read_text())["assignmentId"] == "asn_test"
    # A repeated download must never replace the student's work.
    (target / "main.c").write_text("student edit")
    assert run(dotnet_client, "extract", artifact.path, target).returncode != 0
    assert (target / "main.c").read_text() == "student edit"


@pytest.mark.parametrize("attack", ["traversal", "symlink", "hardlink", "collision", "corrupt", "manifest", "reserved", "parent_collision"])
def test_hostile_starter_never_written(dotnet_client, tmp_path, attack):
    root = tmp_path / "source"
    root.mkdir()
    (root / "main.c").write_text("int main(void){return 0;}\n")
    artifact = BundleStore(tmp_path / "bundles").create_from_directory(root, kind="starter")
    archive = tmp_path / "malicious.tar.gz"
    with tarfile.open(artifact.path, "r:gz") as good, tarfile.open(archive, "w:gz", format=tarfile.PAX_FORMAT) as bad:
        for entry in good:
            content = good.extractfile(entry).read() if entry.isfile() else None
            if attack == "manifest" and entry.name == "main.c":
                content = b"tampered"
                entry.size = len(content)
            bad.addfile(entry, io.BytesIO(content) if content is not None else None)
        extra = tarfile.TarInfo({"traversal": "../outside.c", "reserved": "CON.txt", "collision": "MAIN.c", "parent_collision": "main.c/nested.c"}.get(attack, "evil.c"))
        if attack in ("symlink", "hardlink"):
            extra.type = tarfile.SYMTYPE if attack == "symlink" else tarfile.LNKTYPE
            extra.linkname = "../outside.c"
        if attack != "manifest":
            bad.addfile(extra, io.BytesIO(b""))
    if attack == "corrupt":
        archive.write_bytes(archive.read_bytes()[:20])
    target = tmp_path.resolve() / "download"
    assert run(dotnet_client, "extract", archive, target).returncode != 0
    assert not target.exists()
    assert not (tmp_path / "outside.c").exists()


def test_real_portal_csharp_login_download_submit_grade_logout(dotnet_client, portal, tmp_path):
    web, api, services, _, store = portal
    example = ROOT / "examples/hello-world"
    probe = subprocess.run([sys.executable, str(example / "assessment/grade.py"),
        "--submission", str(example / "c/solution"), "--data", str(example / "c/data")], capture_output=True, timeout=30)
    if probe.returncode == 78:
        pytest.skip("C17 toolchain is unavailable")
    assert probe.returncode == 0
    starter = store.create_from_directory(example / "c/windows/starter", kind="starter")
    builder = WorkspaceBuilder(tmp_path / "digests")
    services["come3105"].state.register_bundle_assignment_release(
        assignment_id="asn_hello_c", course_key="come3105", assignment_key="hello-c", release_id="c-v1", title="Hello World C17",
        starter_path=str(starter.path), starter_digest=starter.digest, starter_size_bytes=starter.compressed_bytes,
        assessment_path=str(example / "assessment"), assessment_digest=builder.digest_instructor_tree(example / "assessment").sha256,
        data_path=str(example / "c/data"), dataset_digest=builder.digest_instructor_tree(example / "c/data").sha256,
        runner_image="pilot-local:v1", rubric_version="c-v1", max_score=10, result_policy="immediate", ready=True)
    status, headers, body = login(web, "come3105")
    assert status == 200
    status, _, body = request(web, "/courses/come3105/claims", method="POST", cookie=cookie(headers), origin=WEB,
        data={"csrf": csrf(body), "assignment_id": "asn_hello_c"})
    assert status == 200
    code = re.search(rb"<code>(AK1-[A-Z0-9-]+)</code>", body)[1].decode()
    setup = {"url": f"http://127.0.0.1:{api.server_address[1]}", "claim": code,
             "target": str(tmp_path.resolve() / "download")}
    process = subprocess.Popen([*dotnet_client, "live"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True)
    try:
        process.stdin.write(json.dumps(setup) + "\n")
        process.stdin.flush()
        # Bound the complete interaction, including a client that never produces its receipt.
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=1) as pool:
            receipt_future = pool.submit(process.stdout.readline)
            try:
                receipt_line = receipt_future.result(timeout=40)
            except TimeoutError:
                process.kill()
                raise
        if not receipt_line:
            _, stderr = process.communicate(timeout=5)
            pytest.fail("C# client did not return a receipt: " + stderr)
        receipt = json.loads(receipt_line)
        with PilotLocalGrader() as grader:
            processor = BundleSubmissionProcessor(state=services["come3105"].state, course_key="come3105",
                workspace_builder=WorkspaceBuilder(tmp_path / "graded"), grader=grader)
            processor.process(receipt["submission_id"])
        stdout, stderr = process.communicate("graded\n", timeout=40)
        assert process.returncode == 0, stderr
        assert "LIVE PASS" in stdout
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
