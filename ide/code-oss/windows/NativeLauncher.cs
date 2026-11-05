using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Threading.Tasks;
using System.Windows.Forms;

namespace Autograde.NativeWindows
{
    internal sealed class NativeWindow : Form
    {
        private readonly Label status; private readonly Button stop;
        private readonly object gate = new object(); private NativeJob active; private bool finished, stopRequested;
        public NativeWindow()
        {
            Text = "Autograde IDE"; ClientSize = new Size(490, 155); StartPosition = FormStartPosition.CenterScreen;
            FormBorderStyle = FormBorderStyle.FixedDialog; MaximizeBox = false;
            status = new Label { Left = 18, Top = 20, Width = 455, Height = 66, Text = "Windows 개발 도구와 이전 실습 정리를 확인하고 있습니다." };
            stop = new Button { Left = 305, Top = 105, Width = 165, Height = 30, Text = "공용 실습 종료", Enabled = false };
            Controls.Add(status); Controls.Add(stop); stop.Click += delegate { EndSession(); };
            Shown += async delegate { await RunSession(); };
        }
        private async void EndSession()
        {
            if (MessageBox.Show("필요한 코드는 먼저 내보내거나 제출하세요. 실습 프로그램을 종료하고 이번 세션의 임시 파일을 정리할까요?", "공용 실습 종료", MessageBoxButtons.YesNo, MessageBoxIcon.Question) != DialogResult.Yes) return;
            stopRequested = true;
            stop.Enabled = false; status.Text = "실습 프로그램과 자식 프로세스를 종료하고 임시 파일을 정리하고 있습니다.";
            try { await Task.Run(delegate { NativeDiagnostics.Run("process-stop", delegate { lock (gate) { if (active != null) active.StopAndDrain(); } }); }); }
            catch (Exception error) { MessageBox.Show(DiagnosticMessage(error), "정리 확인 필요", MessageBoxButtons.OK, MessageBoxIcon.Warning); }
        }
        protected override void OnFormClosing(FormClosingEventArgs e)
        { if (!finished) { e.Cancel = true; if (stop.Enabled) EndSession(); } base.OnFormClosing(e); }
        private static string DiagnosticMessage(Exception error)
        {
            string report = NativeDiagnostics.Format(error), file = NativeDiagnosticLog.TryWrite(error);
            return report + "\n\n" + (file == null
                ? "진단 파일을 저장하지 못했습니다. 이 창의 실패 단계와 오류 코드를 담당자에게 전달해 주세요."
                : "진단 파일: " + file + "\n실패 단계와 오류 코드를 담당자에게 전달해 주세요.");
        }
        private int RunManagedSession()
        {
            string package = AppDomain.CurrentDomain.BaseDirectory.TrimEnd(Path.DirectorySeparatorChar);
            NativeDiagnostics.Run("package", delegate { NativePaths.NoLinks(package); });
            string node = Path.Combine(package, @"runtime\node.exe"), bootstrap = Path.Combine(package, @"autograde\windows-native-bootstrap.mjs");
            NativeDiagnostics.Run("package-node", delegate { NativePaths.NoLinks(node); });
            NativeDiagnostics.Run("package-bootstrap", delegate { NativePaths.NoLinks(bootstrap); });
            NativeDiagnostics.Run("package-manifest", delegate { NativePaths.NoLinks(Path.Combine(package, "windows-package.json")); });
            string local = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
            using (var store = NativeDiagnostics.Run("session-recovery", () => new NativeSessionStore(Path.Combine(local, "AutogradeNative")))) {
                string session = NativeDiagnostics.Run("session-create", () => store.CreateSession()); bool mayClean = true;
                return NativeDiagnostics.WithCleanup(delegate {
                    var env = NativeDiagnostics.Run("toolchain", delegate {
                        string temporary = Path.Combine(session, "tmp"); NativePaths.PrivateDirectory(temporary, true);
                        return NativeToolchain.EnvironmentFor(temporary);
                    });
                    env["PATH"] = Path.Combine(package, "runtime") + ";" + env["PATH"];
                    var job = NativeDiagnostics.Run("process-create", () => new NativeJob(NativeSessionStore.JobName(store.SessionId(session))));
                    lock (gate) { active = job; }
                    mayClean = false;
                    return NativeDiagnostics.WithCleanup(delegate {
                        BeginInvoke((Action)delegate { status.Text = "고정 작업 폴더에서 실습하세요. 종료할 때 정리하고, 남은 자료는 다음 실행 전에 다시 정리합니다.\n서버 저장 완료 여부를 확인하세요. 직접 내보낸 파일은 보존합니다."; stop.Enabled = true; });
                        return NativeDiagnostics.Run("runtime", () => job.Run(node, new[] { bootstrap, session, Process.GetCurrentProcess().Id.ToString() }, package, env, Path.Combine(session, ".native-launch-pending.json")));
                    }, delegate {
                        NativeDiagnostics.Run("process-stop", delegate {
                            lock (gate) { try { job.StopAndDrain(); mayClean = true; } finally { active = null; job.Dispose(); } }
                        });
                    });
                }, delegate { if (mayClean) NativeDiagnostics.Run("session-cleanup", () => store.Cleanup(session)); });
            }
        }
        private async Task RunSession()
        {
            string errorMessage = null; int result = 0;
            try { result = await Task.Run(() => RunManagedSession()); }
            catch (Exception error) { errorMessage = DiagnosticMessage(error); }
            finished = true; stop.Enabled = false;
            if (errorMessage != null) MessageBox.Show(errorMessage, "Autograde IDE 확인 필요", MessageBoxButtons.OK, MessageBoxIcon.Warning);
            else if (result != 0 && !stopRequested) MessageBox.Show("IDE 실행 프로세스가 오류로 종료되었습니다. 종료 코드: " + result + "\n임시 실습 파일은 정리했습니다. 설치 패키지와 IDE 실행 상태를 확인해 주세요.", "Autograde IDE", MessageBoxButtons.OK, MessageBoxIcon.Warning);
            Close();
        }
    }
    internal static class NativeProgram
    {
        [STAThread] private static int Main(string[] args)
        {
            if (args.Length != 0 || !Environment.Is64BitOperatingSystem || !Environment.Is64BitProcess) return 2;
            Application.EnableVisualStyles(); Application.SetCompatibleTextRenderingDefault(false);
            Application.Run(new NativeWindow()); return 0;
        }
    }
}
