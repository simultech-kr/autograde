using System;
using System.ComponentModel.Design;
using System.Runtime.InteropServices;
using System.Threading;
using System.Threading.Tasks;
using Microsoft.VisualStudio.Shell;
using Task = System.Threading.Tasks.Task;

namespace Autograde.VisualStudio
{
    [PackageRegistration(UseManagedResourcesOnly = true, AllowsBackgroundLoading = true)]
    [InstalledProductRegistration("Autograde", "C/C++ 과제 수령·제출·서버 채점 결과 확인. 도구 → Autograde 과제에서 시작하세요.", "0.5.6")]
    [ProvideMenuResource("Menus.ctmenu", 1)]
    [ProvideToolWindow(typeof(AssignmentWindow))]
    [Guid("74db5571-a3ad-4451-a5f4-e8cc28d20536")]
    public sealed class AutogradePackage : AsyncPackage
    {
        protected override async Task InitializeAsync(CancellationToken cancellationToken, IProgress<ServiceProgressData> progress)
        {
            await JoinableTaskFactory.SwitchToMainThreadAsync(cancellationToken);
            var commands = await GetServiceAsync(typeof(IMenuCommandService)) as OleMenuCommandService;
            commands?.AddCommand(new MenuCommand(OpenWindow, new CommandID(new Guid("cd754a3d-73d8-47f1-bddc-429f9211fb89"), 0x0100)));
        }
        void OpenWindow(object sender, EventArgs args) => JoinableTaskFactory.RunAsync(async () =>
        {
            try { await ShowToolWindowAsync(typeof(AssignmentWindow), 0, true, DisposalToken); }
            catch (OperationCanceledException) { }
            catch (Exception) { System.Windows.MessageBox.Show("Autograde 창을 열 수 없습니다. 확장 설치 상태를 확인하세요."); }
        }).FileAndForget("Autograde/OpenWindow");
    }

    [Guid("5dc23b36-ea97-45ad-9e44-122d82728951")]
    public sealed class AssignmentWindow : ToolWindowPane
    {
        readonly AssignmentControl control;
        public AssignmentWindow() : base(null) { Caption = "Autograde 과제"; Content = control = new AssignmentControl(); }
        protected override void Dispose(bool disposing) { if (disposing) control.Dispose(); base.Dispose(disposing); }
    }
}
