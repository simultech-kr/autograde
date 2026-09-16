using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Net.Http;
using System.Threading;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Threading;
using Autograde.Core;
using Microsoft.VisualStudio.Shell;
using Microsoft.VisualStudio.Threading;
using Newtonsoft.Json.Linq;
using Forms = System.Windows.Forms;

namespace Autograde.VisualStudio
{
    public sealed class AssignmentControl : UserControl, IDisposable
    {
        readonly TextBox address = new TextBox { Text = "https://ai.cbchoi.info:20000" };
        readonly PasswordBox claim = new PasswordBox { MaxLength = 256 };
        readonly ListBox assignments = new ListBox { MinHeight = 85, MaxHeight = 180, DisplayMemberPath = "Label" };
        readonly ListBox history = new ListBox { MinHeight = 60, MaxHeight = 180, DisplayMemberPath = "Label" };
        sealed class HistoryItem
        {
            public JObject Value { get; set; }
            public string Label => (string)Value["received_at"] + " · " + (string)Value["state"] + " · " + ((string)Value["source_sha256"]).Substring(7, 12);
        }
        JObject SelectedVersion => (history.SelectedItem as HistoryItem)?.Value ?? throw new InvalidOperationException("제출 기록을 조회하고 복원할 항목을 선택하세요.");
        readonly TextBox folder = new TextBox { IsReadOnly = true };
        readonly TextBox output = new TextBox { IsReadOnly = true, AcceptsReturn = true, TextWrapping = TextWrapping.Wrap, MinHeight = 120, VerticalScrollBarVisibility = ScrollBarVisibility.Auto };
        readonly TextBox gradingOutput = new TextBox { IsReadOnly = true, AcceptsReturn = true, TextWrapping = TextWrapping.Wrap, MinHeight = 90, MaxHeight = 220, VerticalScrollBarVisibility = ScrollBarVisibility.Auto };
        readonly TextBlock receiptStatus = new TextBlock { Text = "이번 로그인에서 접수한 제출이 없습니다.", TextWrapping = TextWrapping.Wrap };
        readonly ResultView liveResult = new ResultView();
        readonly ResultView inspectedResult = new ResultView();
        sealed class ReceiptWatch
        {
            public string SubmissionId, Title;
            public DateTimeOffset Deadline;
            public bool Stopped;
        }
        ReceiptWatch receiptWatch;
        CancellationTokenSource gradingPoll;
        readonly TextBlock status = new TextBlock { Text = "연결 확인 전", TextWrapping = TextWrapping.Wrap };
        readonly List<Button> actions = new List<Button>();
        readonly DispatcherTimer health = new DispatcherTimer { Interval = TimeSpan.FromSeconds(30) };
        readonly CancellationTokenSource lifetime = new CancellationTokenSource();
        readonly JoinableTaskCollection pendingTasks = ThreadHelper.JoinableTaskContext.CreateCollection();
        readonly JoinableTaskFactory jobs;
        CancellationTokenSource operation;
        ServiceClient client;
        bool busy, probing, disposed;
        sealed class AssignmentItem
        {
            public JObject Value { get; set; }
            public string Label => (string)Value["course_key"] + " / " + (string)Value["title"];
        }
        string Id => ServiceClient.Required((assignments.SelectedItem as AssignmentItem)?.Value, "assignment_id");
        JObject Selected => (assignments.SelectedItem as AssignmentItem)?.Value ?? throw new InvalidOperationException("과제를 선택하세요.");
        static readonly string SettingsPath = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "AutogradeVS", "service.txt");

        public AssignmentControl()
        {
            ThemeResources.Apply(this, claim, new[] { assignments, history }, address, folder, output, gradingOutput);
            ThemeResources.Label(status);
            ThemeResources.Label(receiptStatus);
            jobs = ThreadHelper.JoinableTaskContext.CreateFactory(pendingTasks);
            var panel = new StackPanel { Margin = new Thickness(12), MaxWidth = 720 };
            Content = new ScrollViewer { Content = panel, VerticalScrollBarVisibility = ScrollBarVisibility.Auto };
            AddLabel(panel, "Autograde " + typeof(AssignmentControl).Assembly.GetName().Version.ToString(3) + " · C/C++ 실습 (VS2022 / VS2026)");
            AddLabel(panel, "내 제출 결과");
            panel.Children.Add(receiptStatus); panel.Children.Add(liveResult); panel.Children.Add(gradingOutput);
            panel.Children.Add(inspectedResult);
            gradingOutput.MinHeight = 40; gradingOutput.MaxHeight = 100;
            AddLabel(panel, "학생 웹 20010번에서 로그인 후 수령 코드를 받으세요. 아래 주소는 API 20000번입니다.");
            AddLabel(panel, "API 서버 주소 (HTTPS 또는 localhost HTTP)"); panel.Children.Add(address);
            AddButton(panel, "주소 적용 / 연결 확인", async ct =>
            {
                var normalized = ServiceClient.NormalizeAddress(address.Text);
                if (client != null && normalized != client.BaseUrl)
                {
                    ClearScreen();
                    try { await client.LogoutAsync(ct); } catch (Exception) { }
                }
                if (client == null || client.BaseUrl != normalized) { client?.Dispose(); client = new ServiceClient(normalized); }
                address.Text = normalized;
                Directory.CreateDirectory(Path.GetDirectoryName(SettingsPath));
                File.WriteAllText(SettingsPath, normalized); // Public address only, never credentials.
                await ProbeAsync(ct);
            });
            panel.Children.Add(status);
            AddLabel(panel, "과제 수령 코드 (비밀번호가 아님 / 저장하지 않음)"); panel.Children.Add(claim);
            AddButton(panel, "코드로 로그인", async ct =>
            {
                RequireClient(); var code = claim.Password; claim.Clear(); ClearScreen();
                string id = await client.LoginAsync(code, ct); code = null;
                await ReloadAsync(id, ct); output.Text = "로그인했습니다. 과제 다운로드를 눌러 새 폴더에 받으세요.";
            });
            AddButton(panel, "과제 새로고침", ct => ReloadAsync(null, ct));
            panel.Children.Add(assignments);
            assignments.SelectionChanged += (_, __) => { history.Items.Clear(); inspectedResult.Clear(); };
            AddButton(panel, "선택 과제 다운로드", async ct =>
            {
                RequireClient(); var selected = Selected; string id = Id;
                using (var dialog = new Forms.FolderBrowserDialog { Description = "새 과제 폴더를 만들 상위 폴더를 선택하세요." })
                {
                    if (dialog.ShowDialog() != Forms.DialogResult.OK) return;
                    var target = Path.Combine(dialog.SelectedPath, "autograde-" + DateTime.Now.ToString("yyyyMMdd-HHmmss") + "-" + Guid.NewGuid().ToString("N").Substring(0, 6));
                    var bytes = await client.StarterAsync(selected, ct);
                    await Task.Run(() => Bundle.ExtractStarter(bytes, target, client.BaseUrl, id, ct), ct);
                    folder.Text = target;
                    output.Text = "다운로드 완료: " + target + "\nVS의 파일 → 열기 → 폴더에서 여세요. 신뢰하는 과제만 빌드하세요.";
                }
            });
            AddLabel(panel, "제출 폴더 (다운로드 폴더를 사용)"); panel.Children.Add(folder);
            AddButton(panel, "기존 과제 폴더 선택", ct =>
            {
                RequireClient(); string id = Id;
                using (var dialog = new Forms.FolderBrowserDialog { Description = "이 확장으로 받은 과제 폴더를 선택하세요." })
                    if (dialog.ShowDialog() == Forms.DialogResult.OK) { Bundle.VerifyWorkspace(dialog.SelectedPath, client.BaseUrl, id); folder.Text = dialog.SelectedPath; }
                return Task.CompletedTask;
            });
            AddButton(panel, "파일 확인 후 제출", async ct =>
            {
                RequireClient(); string id = Id, root = folder.Text;
                if (string.IsNullOrWhiteSpace(root)) throw new InvalidOperationException("제출 폴더를 선택하세요.");
                if (MessageBox.Show("VS에서 모두 저장(Ctrl+Shift+S)했나요? 저장된 디스크 파일만 제출합니다.", "Autograde", MessageBoxButton.OKCancel) != MessageBoxResult.OK) return;
                var snapshot = await Task.Run(() => { Bundle.VerifyWorkspace(root, client.BaseUrl, id); return Bundle.CreateSubmission(root, ct); }, ct);
                output.Text = "제출 예정 (" + snapshot.SourceBytes + " bytes)\n" + string.Join("\n", snapshot.Files);
                if (MessageBox.Show("선택 과제: " + (string)Selected["title"] + "\n폴더: " + root + "\n" + snapshot.Files.Length + "개 파일을 서버에 제출할까요?\n창의 파일 목록을 확인하세요.", "제출 확인", MessageBoxButton.YesNo) != MessageBoxResult.Yes) return;
                var submitted = await client.SubmitAsync(id, snapshot.Archive, ct);
                Selected["latest_submission"] = submitted;
                var submission = ServiceClient.Required(submitted, "submission_id");
                PauseGradingWatch();
                inspectedResult.Clear();
                receiptWatch = new ReceiptWatch { SubmissionId = submission,
                    Title = (string)Selected["course_key"] + " / " + (string)Selected["title"],
                    Deadline = DateTimeOffset.UtcNow.AddMinutes(2), Stopped = GradingPoller.IsFinished((string)submitted["state"]) };
                receiptStatus.Text = "제출 접수 완료 · " + receiptWatch.Title + "\n접수번호: " + submission +
                    "\n서버 접수 시각: " + (string)submitted["received_at"];
                gradingOutput.Text = "채점 상태: " + GradingPoller.Label((string)submitted["state"]) + "\n다른 작업을 해도 서버 채점은 계속됩니다.";
                liveResult.Show(submitted, "마지막 접수 · " + receiptWatch.Title);
                liveResult.BringIntoView();
                output.Text = "제출이 접수되었습니다. 상단의 내 제출 결과를 확인하세요.";
            });
            AddLabel(panel, "마지막 접수 / 백그라운드 채점 확인 (이번 로그인)");
            AddLabel(panel, "자동 확인은 접수 후 최대 2분입니다. 조회 중단·로그아웃은 서버 채점을 취소하지 않습니다.");
            AddButton(panel, "최신 결과 확인", async ct =>
            {
                RequireClient(); string id = Id; await ReloadAsync(id, ct);
                var submission = ServiceClient.Required(Selected["latest_submission"], "submission_id");
                var result = await client.ResultAsync(submission, ct);
                if (receiptWatch?.SubmissionId == submission)
                {
                    inspectedResult.Clear();
                    liveResult.Show(result, "마지막 접수 · " + receiptWatch.Title);
                    liveResult.BringIntoView();
                }
                else ShowResult(result, context: "선택 과제의 최신 제출 · " + (string)Selected["title"]);
            });
            AddLabel(panel, "제출 기록 (서버 접수본 / 최신순)"); panel.Children.Add(history);
            AddButton(panel, "제출 기록 조회", async ct =>
            {
                RequireClient(); var response = await client.HistoryAsync(Id, ct);
                history.Items.Clear();
                foreach (JObject row in (JArray)response["submissions"]) history.Items.Add(new HistoryItem { Value = row });
                if (history.Items.Count > 0) history.SelectedIndex = 0;
                output.Text = history.Items.Count == 0 ? "접수된 제출 기록이 없습니다." :
                    ((bool)response["has_more"] ? "최근 100건만 표시합니다." : "제출 기록을 불러왔습니다.");
            });
            AddButton(panel, "선택 기록의 결과 확인", async ct =>
            {
                RequireClient(); var version = SelectedVersion; var title = (string)Selected["title"];
                ShowResult(await client.ResultAsync(ServiceClient.Required(version, "submission_id"), ct),
                    context: "과거 제출 기록 · " + title + " · " + (string)version["received_at"]);
            });
            AddButton(panel, "선택 기록을 새 폴더로 복원", async ct =>
            {
                RequireClient(); var version = SelectedVersion; string id = Id;
                if (ServiceClient.Required(version, "assignment_id") != id) throw new InvalidOperationException("과제의 제출 기록을 다시 조회하세요.");
                using (var dialog = new Forms.FolderBrowserDialog { Description = "현재 작업을 덮어쓰지 않고 새 폴더에 복원합니다. 상위 폴더를 선택하세요." })
                {
                    if (dialog.ShowDialog() != Forms.DialogResult.OK) return;
                    var target = Path.Combine(dialog.SelectedPath, "autograde-restore-" + Guid.NewGuid().ToString("N"));
                    var bytes = await client.SourceAsync(version, ct);
                    await Task.Run(() => Bundle.ExtractSubmission(bytes, target, client.BaseUrl, id, ct), ct);
                    folder.Text = target;
                    output.Text = "복원 완료: " + target + "\n자동 제출·빌드를 실행하지 않았습니다. 파일 → 열기 → 폴더에서 여세요.";
                }
            });
            AddButton(panel, "로그아웃 / 자리 비우기", async ct =>
            {
                // Logout must work even when the address textbox is invalid or unapplied.
                ClearScreen();
                try { if (client != null) await client.LogoutAsync(ct); }
                finally { ClearScreen(); status.Text = "로그아웃됨 · 서버 연결은 별도 확인"; output.Text = "이 IDE의 로그인 정보를 지웠습니다."; }
            });
            var cancel = new Button { Content = "현재 작업 취소 (서버 채점은 계속됨)", Margin = new Thickness(0, 5, 0, 0) };
            ThemeResources.Button(cancel);
            cancel.Click += (_, __) => operation?.Cancel(); panel.Children.Add(cancel);
            AddLabel(panel, "작업 안내 / 직접 조회한 결과"); panel.Children.Add(output);
            AddLabel(panel, "창을 숨겨도 로그인은 유지됩니다. 공용 PC에서는 반드시 로그아웃하세요.\nWindows 전용 API 서버 채점은 아직 지원하지 않습니다. 신뢰된 파일럿 과제만 사용하세요.");
            try { if (File.Exists(SettingsPath) && new FileInfo(SettingsPath).Length < 2048) address.Text = ServiceClient.NormalizeAddress(File.ReadAllText(SettingsPath)); }
            catch (Exception) { }
            health.Tick += HealthTick; health.Start();
            Unloaded += (_, __) => health.Stop();
            Loaded += (_, __) => { if (!disposed) health.Start(); };
        }

        static void AddLabel(Panel panel, string text)
        {
            var label = new TextBlock { Text = text, TextWrapping = TextWrapping.Wrap, Margin = new Thickness(0, 8, 0, 4) };
            ThemeResources.Label(label);
            panel.Children.Add(label);
        }
        void AddButton(Panel panel, string title, Func<CancellationToken, Task> action)
        {
            var button = new Button { Content = title, Margin = new Thickness(0, 5, 0, 0), Padding = new Thickness(6) };
            ThemeResources.Button(button);
            button.Click += (_, __) => jobs.RunAsync(async () =>
            {
                if (busy || disposed) return;
                busy = true; actions.ForEach(b => b.IsEnabled = false); address.IsEnabled = claim.IsEnabled = assignments.IsEnabled = false;
                operation = CancellationTokenSource.CreateLinkedTokenSource(lifetime.Token);
                try { await action(operation.Token); }
                catch (OperationCanceledException) { output.Text = "작업 취소 또는 요청 시간 초과. 제출 중이었다면 최신 결과를 먼저 확인하세요."; }
                catch (ServiceError ex) { output.Text = ex.Message + "\n401이면 새 수령 코드로 로그인하세요. 제출 결과가 불명확하면 먼저 최신 결과를 확인하세요."; }
                catch (HttpRequestException) { output.Text = "서버에 연결할 수 없습니다. API 주소, 인증서, 방화벽을 확인하세요."; }
                catch (Exception ex) when (ex is ArgumentException || ex is InvalidDataException || ex is InvalidOperationException) { output.Text = ex.Message; }
                catch (Exception) { output.Text = "작업 실패. 폴더 접근 권한과 서버 응답을 확인하세요. 수령 코드가 소비되었다면 웹에서 다시 발급하세요."; }
                finally
                {
                    operation.Dispose(); operation = null; busy = false;
                    actions.ForEach(b => b.IsEnabled = true); address.IsEnabled = claim.IsEnabled = assignments.IsEnabled = true;
                    if (client?.HasSession != true) ClearScreen(false);
                    else StartGradingWatch();
                }
            }).FileAndForget("Autograde/Action");
            actions.Add(button); panel.Children.Add(button);
        }
        void RequireClient()
        {
            if (client == null || ServiceClient.NormalizeAddress(address.Text) != client.BaseUrl)
                throw new InvalidOperationException("먼저 서버 주소를 적용하세요. 주소가 바뀌면 다시 로그인해야 합니다.");
        }
        void ClearScreen(bool clearResults = true)
        {
            PauseGradingWatch(); receiptWatch = null;
            liveResult.Clear(); inspectedResult.Clear();
            receiptStatus.Text = "이번 로그인에서 접수한 제출이 없습니다."; gradingOutput.Clear();
            assignments.Items.Clear(); history.Items.Clear(); folder.Clear(); claim.Clear(); if (clearResults) output.Clear();
        }
        void PauseGradingWatch()
        {
            var old = gradingPoll; gradingPoll = null;
            old?.Cancel(); // Its running task owns disposal.
        }
        void StartGradingWatch()
        {
            var watch = receiptWatch; var current = client;
            if (disposed || busy || watch == null || watch.Stopped || gradingPoll != null || current?.HasSession != true) return;
            var cancellation = CancellationTokenSource.CreateLinkedTokenSource(lifetime.Token);
            gradingPoll = cancellation;
            bool IsCurrent() => !disposed && !cancellation.IsCancellationRequested &&
                ReferenceEquals(gradingPoll, cancellation) && ReferenceEquals(receiptWatch, watch) && ReferenceEquals(client, current);
            jobs.RunAsync(async () =>
            {
                try
                {
                    var ended = await GradingPoller.WatchAsync(async ct =>
                        {
                            // Don't start a new read while a foreground operation needs the client.
                            // Don't abort an in-flight token rotation just because a button was clicked.
                            while (busy) await Task.Delay(200, ct);
                            return await current.ResultAsync(watch.SubmissionId, ct);
                        },
                        result => { if (IsCurrent()) ShowResult(result, gradingOutput); },
                        message => { if (IsCurrent()) gradingOutput.Text = message; }, watch.Deadline, cancellation.Token);
                    if (!IsCurrent()) return;
                    watch.Stopped = true;
                    if (ended == GradingWatchEnd.Expired)
                        gradingOutput.AppendText("\n자동 확인 종료 · 접수는 유지됩니다. 해당 과제를 선택하고 최신 결과를 확인하세요.");
                }
                catch (OperationCanceledException) when (cancellation.IsCancellationRequested) { }
                catch (Exception)
                {
                    if (IsCurrent()) { watch.Stopped = true; gradingOutput.Text = "접수는 유지됩니다. 자동 조회를 중단했습니다. 해당 과제의 최신 결과를 직접 확인하세요."; }
                }
                finally
                {
                    if (ReferenceEquals(gradingPoll, cancellation)) gradingPoll = null;
                    cancellation.Dispose();
                }
            }).FileAndForget("Autograde/GradingWatch");
        }
        async Task ReloadAsync(string id, CancellationToken ct)
        {
            id = id ?? (string)(assignments.SelectedItem as AssignmentItem)?.Value["assignment_id"];
            RequireClient(); var items = await client.AssignmentsAsync(ct);
            assignments.Items.Clear();
            foreach (var item in items.OfType<JObject>().Where(a => (string)a["delivery_mode"] == "bundle"))
            {
                var row = new AssignmentItem { Value = item }; assignments.Items.Add(row);
            }
            var selected = AssignmentSelection.Select(items, id);
            assignments.SelectedItem = assignments.Items.Cast<AssignmentItem>().FirstOrDefault(row => ReferenceEquals(row.Value, selected));
        }
        void ShowResult(JObject result, TextBox destination = null, string context = null)
        {
            if (destination == gradingOutput)
            {
                liveResult.Show(result, "마지막 접수 · " + receiptWatch?.Title);
                gradingOutput.Text = "채점 상태 확인 · " + DateTime.Now.ToString("HH:mm:ss");
            }
            else
            {
                inspectedResult.Show(result, context ?? "직접 조회한 결과");
                inspectedResult.BringIntoView();
                output.Text = "상단에 결과 요약·수정 필요 항목·다음 할 일을 표시했습니다.";
            }
        }
        async Task ProbeAsync(CancellationToken ct)
        {
            var current = client;
            try { await current.HealthAsync(ct); if (!disposed && current == client) status.Text = "서버 응답 정상 · " + DateTime.Now.ToString("HH:mm:ss") + (current.HasSession ? " · 로그인됨" : " · 로그인 필요"); }
            catch (Exception) { if (!disposed && current == client) status.Text = "서버 연결 실패 · " + DateTime.Now.ToString("HH:mm:ss") + " (인증 상태와는 별개)"; }
        }
        void HealthTick(object sender, EventArgs args) => jobs.RunAsync(async () =>
        {
            if (busy || probing || disposed || client == null) return;
            probing = true; try { await ProbeAsync(lifetime.Token); } finally { probing = false; }
        }).FileAndForget("Autograde/Health");
        public void Dispose()
        {
            if (disposed) return;
            disposed = true; health.Stop(); PauseGradingWatch(); lifetime.Cancel();
            ThreadHelper.JoinableTaskFactory.Run(async () => await pendingTasks.JoinTillEmptyAsync());
            client?.Dispose(); lifetime.Dispose();
        }
    }
}
