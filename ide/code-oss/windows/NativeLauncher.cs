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
        private readonly object gate = new object(); private NativeJob active; private bool finished;
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
            stop.Enabled = false; status.Text = "실습 프로그램과 자식 프로세스를 종료하고 임시 파일을 정리하고 있습니다.";
            try { await Task.Run(delegate { lock (gate) { if (active != null) active.StopAndDrain(); } }); }
            catch { MessageBox.Show("실습 프로그램의 종료를 확인하지 못했습니다. 다음 학생에게 넘기기 전에 담당자에게 확인을 요청해 주세요.", "정리 확인 필요", MessageBoxButtons.OK, MessageBoxIcon.Warning); }
        }
        protected override void OnFormClosing(FormClosingEventArgs e)
        { if (!finished) { e.Cancel = true; if (stop.Enabled) EndSession(); } base.OnFormClosing(e); }
        private async Task RunSession()
        {
            string errorMessage = null; int result = 0;
            try {
                result = await Task.Run(delegate {
                    string package = AppDomain.CurrentDomain.BaseDirectory.TrimEnd(Path.DirectorySeparatorChar);
                    NativePaths.NoLinks(package);
                    string node = Path.Combine(package, @"runtime\node.exe"), bootstrap = Path.Combine(package, @"autograde\windows-native-bootstrap.mjs");
                    NativePaths.NoLinks(node); NativePaths.NoLinks(bootstrap); NativePaths.NoLinks(Path.Combine(package, "windows-package.json"));
                    string local = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
                    using (var store = new NativeSessionStore(Path.Combine(local, "AutogradeNative"))) {
                        string session = store.CreateSession(); bool mayClean = true;
                        try {
                            string temporary = Path.Combine(session, "tmp"); NativePaths.PrivateDirectory(temporary, true);
                            var env = NativeToolchain.EnvironmentFor(temporary);
                            env["PATH"] = Path.Combine(package, "runtime") + ";" + env["PATH"];
                            var job = new NativeJob(NativeSessionStore.JobName(store.SessionId(session)));
                            lock (gate) { active = job; }
                            mayClean = false;
                            try {
                                BeginInvoke((Action)delegate { status.Text = "고정 작업 폴더에서 실습하세요. 종료할 때 정리하고, 남은 자료는 다음 실행 전에 다시 정리합니다.\n서버 저장 완료 여부를 확인하세요. 직접 내보낸 파일은 보존합니다."; stop.Enabled = true; });
                                return job.Run(node, new[] { bootstrap, session, Process.GetCurrentProcess().Id.ToString() }, package, env, Path.Combine(session, ".native-launch-pending.json"));
                            } finally {
                                lock (gate) { try { job.StopAndDrain(); mayClean = true; } finally { active = null; job.Dispose(); } }
                            }
                        } finally { if (mayClean) store.Cleanup(session); }
                    }
                });
            } catch (Exception error) {
                errorMessage = error is InvalidDataException && System.Text.RegularExpressions.Regex.IsMatch(error.Message, "[가-힣]") ? error.Message :
                    "이전 실습의 종료·임시 파일 정리 또는 Windows 개발 도구를 확인할 수 없습니다. 새 실습을 시작하지 않았습니다. 담당자에게 앱 설치와 사용자 권한을 확인해 달라고 요청해 주세요.";
            }
            finished = true; stop.Enabled = false;
            if (errorMessage != null) MessageBox.Show(errorMessage, "Autograde IDE 확인 필요", MessageBoxButtons.OK, MessageBoxIcon.Warning);
            else if (result != 0) MessageBox.Show("IDE가 종료되었습니다. 임시 실습 파일은 정리했습니다. 제출한 코드와 직접 내보낸 파일을 확인해 주세요.", "Autograde IDE", MessageBoxButtons.OK, MessageBoxIcon.Information);
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
