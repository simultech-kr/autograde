// Windows GUI bootstrap; runs on the Windows-provided .NET Framework.
using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Text;
using System.Threading;
using System.Windows.Forms;

namespace Autograde.WslLauncher
{
    internal sealed class LauncherWindow : Form
    {
        private readonly LauncherConfig config;
        private bool running;
        private bool finished;
        private readonly Label status;

        public LauncherWindow(LauncherConfig value)
        {
            config = value;
            Text = "Autograde IDE";
            ClientSize = new Size(460, 136);
            FormBorderStyle = FormBorderStyle.FixedDialog;
            MaximizeBox = false;
            MinimizeBox = false;
            StartPosition = FormStartPosition.CenterScreen;
            Font = new Font("Segoe UI", 10);
            status = new Label { Left = 20, Top = 18, Width = 420, Height = 70,
                Text = "실습 환경을 준비하고 있습니다.\n잠시 후 Autograde IDE가 열립니다." };
            Controls.Add(status);
            Controls.Add(new ProgressBar { Left = 20, Top = 100, Width = 420, Height = 14, Style = ProgressBarStyle.Marquee });
        }

        protected override void OnShown(EventArgs args)
        {
            base.OnShown(args);
            running = true;
            ThreadPool.QueueUserWorkItem(delegate { RunSession(); });
        }

        private void OnUi(Action action)
        {
            if (!IsDisposed && IsHandleCreated) try { BeginInvoke(action); } catch (InvalidOperationException) { }
        }

        private void RunSession()
        {
            string failure = null;
            try
            {
                string wsl = FindWsl();
                if (wsl == null) throw new InvalidOperationException("WSL이 설치되어 있지 않습니다. 담당자가 WSL2와 WSLg를 설치한 뒤 다시 실행해 주세요.");
                var protocol = new SessionProtocol(false);
                var gate = new object();
                using (var process = new Process())
                {
                    process.StartInfo = new ProcessStartInfo {
                        FileName = wsl,
                        Arguments = WindowsArguments.Join(config.Arguments(false)),
                        UseShellExecute = false,
                        CreateNoWindow = true,
                        WindowStyle = ProcessWindowStyle.Hidden,
                        RedirectStandardOutput = true,
                        RedirectStandardError = true,
                        StandardOutputEncoding = new UTF8Encoding(false),
                        StandardErrorEncoding = new UTF8Encoding(false),
                        WorkingDirectory = AppDomain.CurrentDomain.BaseDirectory
                    };
                    // Remove inherited runtime switches, including WSLENV which
                    // otherwise forwards host values into the Linux process.
                    var environmentKeys = new System.Collections.Generic.List<string>();
                    foreach (string key in process.StartInfo.EnvironmentVariables.Keys) environmentKeys.Add(key);
                    foreach (string key in environmentKeys)
                        if (key == "WSLENV" || key.StartsWith("NODE_", StringComparison.OrdinalIgnoreCase) ||
                            key.StartsWith("ELECTRON_", StringComparison.OrdinalIgnoreCase) || key.StartsWith("VSCODE_", StringComparison.OrdinalIgnoreCase) ||
                            key.StartsWith("AUTOGRADE_", StringComparison.OrdinalIgnoreCase)) process.StartInfo.EnvironmentVariables.Remove(key);
                    process.OutputDataReceived += delegate(object sender, DataReceivedEventArgs data) {
                        if (data.Data == null) return;
                        lock (gate)
                        {
                            bool before = protocol.Started;
                            protocol.Accept(data.Data);
                            if (!before && protocol.Started) OnUi(delegate { Hide(); });
                        }
                    };
                    // Drain without writing any persistent log. Runtime returns
                    // sanitized user-facing failures as structured stdout events.
                    process.ErrorDataReceived += delegate { };
                    if (!process.Start()) throw new InvalidOperationException("WSL 실행기를 시작할 수 없습니다.");
                    process.BeginOutputReadLine();
                    process.BeginErrorReadLine();
                    var startup = Stopwatch.StartNew();
                    bool warned = false;
                    while (!process.WaitForExit(1000))
                    {
                        lock (gate)
                        {
                            if (!warned && !protocol.Started && protocol.Failure == null && startup.Elapsed.TotalSeconds >= 120)
                            {
                                warned = true;
                                OnUi(delegate {
                                    lock (gate) if (protocol.Started) return;
                                    status.Text = "실습 시작 확인이 지연되고 있습니다.\n담당자에게 WSL 상태 확인을 요청해 주세요.";
                                    MessageBox.Show(this, "2분 안에 IDE 시작을 확인하지 못했습니다. 담당자에게 WSL 상태 확인을 요청해 주세요. 실행기는 종료와 파일 정리 여부를 계속 확인하며, 임의로 WSL을 강제 종료하지 않습니다.",
                                        "실습 시작 확인 지연", MessageBoxButtons.OK, MessageBoxIcon.Warning);
                                });
                            }
                        }
                    }
                    process.WaitForExit(); // Drain redirected async output before judging cleanup.
                    lock (gate) failure = protocol.ResultError(process.ExitCode);
                }
            }
            catch (InvalidOperationException error) { failure = error.Message; }
            catch { failure = "Autograde IDE를 실행하지 못했습니다. 담당자에게 WSL 설치와 실행 설정을 확인해 달라고 요청해 주세요."; }
            OnUi(delegate {
                running = false; finished = true;
                if (failure != null) MessageBox.Show(this, failure, "Autograde IDE를 확인해 주세요", MessageBoxButtons.OK, MessageBoxIcon.Warning);
                Close();
            });
        }

        private static string FindWsl()
        {
            string windows = Environment.GetFolderPath(Environment.SpecialFolder.Windows);
            // Sysnative is required if a 32-bit Framework process runs on 64-bit Windows.
            foreach (string directory in new string[] { "Sysnative", "System32" })
            {
                string candidate = Path.Combine(windows, directory, "wsl.exe");
                if (File.Exists(candidate)) return candidate;
            }
            return null;
        }

        protected override void OnFormClosing(FormClosingEventArgs args)
        {
            if (running && !finished && args.CloseReason == CloseReason.UserClosing)
            {
                args.Cancel = true;
                MessageBox.Show(this, "실습은 Autograde IDE의 ‘공용 실습 종료’에서 종료해 주세요. 실행기는 IDE 종료 후 임시 파일 정리가 끝날 때까지 기다립니다.",
                    "실습 종료 안내", MessageBoxButtons.OK, MessageBoxIcon.Information);
            }
            base.OnFormClosing(args);
        }
    }

    internal static class Program
    {
        [STAThread]
        private static void Main()
        {
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
            try
            {
                var config = LauncherConfig.Parse(File.ReadAllText(Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "launcher.json"), Encoding.UTF8));
                bool created;
                using (var lease = new Mutex(true, config.MutexName(), out created))
                {
                    if (!created)
                    {
                        MessageBox.Show("이 사용자의 Autograde 실습이 이미 실행 중입니다. 작업 표시줄에서 기존 실습을 열어 주세요. 다음 학생은 ‘공용 실습 종료’ 후 새로 시작해야 합니다.",
                            "Autograde IDE", MessageBoxButtons.OK, MessageBoxIcon.Information);
                        return;
                    }
                    try { Application.Run(new LauncherWindow(config)); }
                    finally { lease.ReleaseMutex(); }
                }
            }
            catch (Exception error)
            {
                string message = error is InvalidDataException ? error.Message : "실행 설정을 읽을 수 없습니다. 담당자가 Autograde IDE 바로가기를 다시 설치해 주세요.";
                MessageBox.Show(message, "Autograde IDE를 확인해 주세요", MessageBoxButtons.OK, MessageBoxIcon.Warning);
            }
        }
    }
}
