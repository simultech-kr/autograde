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
        readonly TextBox output = new TextBox { IsReadOnly = true, AcceptsReturn = true, TextWrapping = TextWrapping.Wrap, MinHeight = 40, MaxHeight = 120, VerticalScrollBarVisibility = ScrollBarVisibility.Auto };
        readonly TabControl pages = new TabControl();
        readonly StackPanel loginPanel = new StackPanel(), settingsPanel = new StackPanel(), footerActions = new StackPanel();
        readonly TextBlock identity = new TextBlock { TextWrapping = TextWrapping.Wrap };
        readonly TextBlock downloadStatus = new TextBlock { TextWrapping = TextWrapping.Wrap };
        readonly TextBox diagnosticText = new TextBox { IsReadOnly = true, TextWrapping = TextWrapping.Wrap, AcceptsReturn = true, MaxHeight = 160, VerticalScrollBarVisibility = ScrollBarVisibility.Auto };
        readonly Expander diagnosticDetails = new Expander { Header = "다운로드 상세 · 문의번호" };
        readonly Expander settingsExpander = new Expander { Header = "서버 설정" };
        Button downloadButton, submitButton, folderButton, cancelButton;
        DownloadDiagnostic downloadDiagnostic;
        bool showingInspected;
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
            ThemeResources.Apply(this, claim, new[] { assignments, history }, address, folder, output, gradingOutput, diagnosticText);
            ThemeResources.Label(status);
            ThemeResources.Label(receiptStatus);
            ThemeResources.Tabs(pages);
            jobs = ThreadHelper.JoinableTaskContext.CreateFactory(pendingTasks);
            // Fixed chrome/actions, only the active tab scrolls. Never put all commands above the results.
            var layout = new Grid { Margin = new Thickness(8) };
            layout.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            layout.RowDefinitions.Add(new RowDefinition { Height = new GridLength(1, GridUnitType.Star) });
            layout.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            Content = layout;
            var header = new StackPanel(); layout.Children.Add(header);
            var chrome = new WrapPanel(); header.Children.Add(chrome);
            AddLabel(chrome, "Autograde " + typeof(AssignmentControl).Assembly.GetName().Version.ToString(3));
            var settings = new Button { Content = "설정", Margin = new Thickness(6, 0, 0, 0) };
            ThemeResources.Button(settings); settings.Click += (_, __) => { pages.SelectedIndex = 0; settingsExpander.IsExpanded = !settingsExpander.IsExpanded; };
            chrome.Children.Add(settings);
            header.Children.Add(status); ThemeResources.Label(identity); header.Children.Add(identity);
            Grid.SetRow(pages, 1); layout.Children.Add(pages);
            var taskPanel = new StackPanel(); var resultPanel = new StackPanel(); var historyPanel = new StackPanel();
            foreach (var item in new[] { Tuple.Create("과제", taskPanel), Tuple.Create("결과", resultPanel), Tuple.Create("기록", historyPanel) })
                pages.Items.Add(new TabItem { Header = item.Item1, Content = new ScrollViewer { Content = item.Item2,
                    HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled, VerticalScrollBarVisibility = ScrollBarVisibility.Auto, Padding = new Thickness(4) } });
            var footer = new StackPanel(); Grid.SetRow(footer, 2); layout.Children.Add(footer); footer.Children.Add(footerActions);
            ThemeResources.Label(downloadStatus); taskPanel.Children.Add(downloadStatus);
            taskPanel.Children.Add(diagnosticDetails);
            var diagPanel = new StackPanel(); diagnosticDetails.Content = diagPanel; diagPanel.Children.Add(diagnosticText);
            var copy = new Button { Content = "진단 정보 복사" }; ThemeResources.Button(copy);
            copy.Click += (_, __) => { try { Clipboard.SetText(diagnosticText.Text); } catch (Exception) { output.Text = "클립보드를 사용할 수 없습니다. 상세 내용을 선택해 복사하세요."; } };
            diagPanel.Children.Add(copy); diagnosticDetails.Visibility = Visibility.Collapsed;
            settingsExpander.Content = settingsPanel; taskPanel.Children.Add(settingsExpander);
            taskPanel.Children.Add(loginPanel);
            var panel = settingsPanel;
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
            panel = loginPanel;
            AddLabel(panel, "학생 웹에서 과제 수령 코드를 발급받으세요. 로그인 정보는 저장하지 않습니다.");
            AddLabel(panel, "과제 수령 코드 (비밀번호가 아님 / 저장하지 않음)"); panel.Children.Add(claim);
            AddButton(panel, "코드로 로그인", async ct =>
            {
                RequireClient(); var code = claim.Password; claim.Clear(); ClearScreen();
                string id = await client.LoginAsync(code, ct); code = null;
                await ReloadAsync(id, ct); settingsExpander.IsExpanded = false; pages.SelectedIndex = 0;
                output.Text = "수락 완료 · 아래 과제 다운로드를 눌러 새 폴더에 받으세요.";
            });
            var management = new StackPanel(); taskPanel.Children.Add(new Expander { Header = "과제 선택 · 새로고침", Content = management }); panel = management;
            AddButton(panel, "과제 새로고침", ct => ReloadAsync(null, ct));
            panel.Children.Add(assignments);
            assignments.SelectionChanged += (_, __) => { history.Items.Clear(); inspectedResult.Clear(); UpdateCompactState(); };
            downloadButton = AddButton(footerActions, "과제 다운로드 · 열기", DownloadAsync);
            var folderPanel = new StackPanel(); taskPanel.Children.Add(new Expander { Header = "폴더 관리 · 다시 다운로드", Content = folderPanel }); panel = folderPanel;
            AddButton(panel, "새 폴더에 다시 받기", DownloadAsync);
            AddLabel(panel, "제출 폴더 (다운로드 폴더를 사용)"); panel.Children.Add(folder);
            folderButton = AddButton(footerActions, "과제 폴더 열기", async ct =>
            {
                RequireClient();
                if (string.IsNullOrWhiteSpace(folder.Text)) throw new InvalidOperationException("먼저 과제를 다운로드하거나 기존 과제 폴더를 선택하세요.");
                Bundle.VerifyWorkspace(folder.Text, client.BaseUrl, Id);
                await OpenDownloadedFolderAsync(folder.Text, ct);
            });
            AddButton(panel, "기존 과제 폴더 선택", ct =>
            {
                RequireClient(); string id = Id;
                using (var dialog = new Forms.FolderBrowserDialog { Description = "이 확장으로 받은 과제 폴더를 선택하세요." })
                    if (dialog.ShowDialog() == Forms.DialogResult.OK) { Bundle.VerifyWorkspace(dialog.SelectedPath, client.BaseUrl, id); folder.Text = dialog.SelectedPath; }
                return Task.CompletedTask;
            });
            submitButton = AddButton(footerActions, "파일 확인 후 제출", async ct =>
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
                showingInspected = false;
                receiptWatch = new ReceiptWatch { SubmissionId = submission,
                    Title = (string)Selected["course_key"] + " / " + (string)Selected["title"],
                    Deadline = DateTimeOffset.UtcNow.AddMinutes(2), Stopped = GradingPoller.IsFinished((string)submitted["state"]) };
                receiptStatus.Text = "제출 접수 완료 · " + receiptWatch.Title + "\n접수번호: " + submission +
                    "\n서버 접수 시각: " + (string)submitted["received_at"];
                gradingOutput.Text = "채점 상태: " + GradingPoller.Label((string)submitted["state"]) + "\n다른 작업을 해도 서버 채점은 계속됩니다.";
                liveResult.Show(submitted, "마지막 접수 · " + receiptWatch.Title);
                pages.SelectedIndex = 1;
                liveResult.BringIntoView();
                output.Text = "제출이 접수되었습니다. 상단의 내 제출 결과를 확인하세요.";
            });
            panel = resultPanel;
            panel.Children.Add(receiptStatus); panel.Children.Add(liveResult); panel.Children.Add(inspectedResult);
            panel.Children.Add(new Expander { Header = "채점 처리 상태", Content = gradingOutput });
            AddButton(panel, "최신 결과 확인", async ct =>
            {
                RequireClient(); string id = Id; await ReloadAsync(id, ct);
                var submission = ServiceClient.Required(Selected["latest_submission"], "submission_id");
                var result = await client.ResultAsync(submission, ct);
                if (receiptWatch?.SubmissionId == submission)
                {
                    showingInspected = false;
                    inspectedResult.Clear();
                    liveResult.Show(result, "마지막 접수 · " + receiptWatch.Title);
                    liveResult.BringIntoView();
                }
                else ShowResult(result, context: "선택 과제의 최신 제출 · " + (string)Selected["title"]);
            });
            panel = historyPanel;
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
            var restorePanel = new StackPanel(); panel.Children.Add(new Expander { Header = "이전 코드 복원", Content = restorePanel });
            AddButton(restorePanel, "선택 기록을 새 폴더로 복원", async ct =>
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
            AddButton(chrome, "로그아웃", async ct =>
            {
                // Logout must work even when the address textbox is invalid or unapplied.
                ClearScreen();
                try { if (client != null) await client.LogoutAsync(ct); }
                finally { ClearScreen(); status.Text = "로그아웃됨 · 서버 연결은 별도 확인"; output.Text = "이 IDE의 로그인 정보를 지웠습니다."; }
            });
            var cancel = cancelButton = new Button { Content = "현재 작업 취소", Margin = new Thickness(0, 5, 0, 0), Visibility = Visibility.Collapsed };
            ThemeResources.Button(cancel);
            cancel.Click += (_, __) => operation?.Cancel(); footer.Children.Add(cancel);
            // Persistent, bounded status: errors are not lost in a disappearing notification.
            footer.Children.Add(output);
            try { if (File.Exists(SettingsPath) && new FileInfo(SettingsPath).Length < 2048) address.Text = ServiceClient.NormalizeAddress(File.ReadAllText(SettingsPath)); }
            catch (Exception) { }
            try { client = new ServiceClient(ServiceClient.NormalizeAddress(address.Text)); } catch (Exception) { settingsExpander.IsExpanded = true; }
            UpdateCompactState();
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
        Button AddButton(Panel panel, string title, Func<CancellationToken, Task> action)
        {
            var button = new Button { Content = new TextBlock { Text = title, TextWrapping = TextWrapping.Wrap }, Margin = new Thickness(0, 5, 0, 0), Padding = new Thickness(6) };
            ThemeResources.Button(button);
            button.Click += (_, __) => jobs.RunAsync(async () =>
            {
                if (busy || disposed) return;
                busy = true; actions.ForEach(b => b.IsEnabled = false); address.IsEnabled = claim.IsEnabled = assignments.IsEnabled = false;
                cancelButton.Visibility = Visibility.Visible;
                operation = CancellationTokenSource.CreateLinkedTokenSource(lifetime.Token);
                try { await action(operation.Token); }
                catch (OperationCanceledException) { output.Text = "작업 취소 또는 요청 시간 초과. 제출 중이었다면 최신 결과를 먼저 확인하세요."; }
                catch (ServiceError ex) { output.Text = ex.Message + "\n401이면 새 수령 코드로 로그인하세요. 제출 결과가 불명확하면 먼저 최신 결과를 확인하세요."; }
                catch (HttpRequestException) { output.Text = "서버에 연결할 수 없습니다. API 주소, 인증서, 방화벽을 확인하세요."; }
                catch (Exception ex) when (ex is ArgumentException || ex is InvalidDataException || ex is InvalidOperationException) { output.Text = ex.Message; }
                catch (DownloadFailure ex) { output.Text = ex.Code + "\n" + DownloadDiagnostic.Guidance(ex.Code); }
                catch (Exception) { output.Text = "작업 실패. 폴더 접근 권한과 서버 응답을 확인하세요. 다운로드 실패만으로 다시 수락할 필요는 없습니다."; }
                finally
                {
                    operation.Dispose(); operation = null; busy = false;
                    actions.ForEach(b => b.IsEnabled = true); address.IsEnabled = claim.IsEnabled = assignments.IsEnabled = true;
                    if (client?.HasSession != true) ClearScreen(false);
                    else StartGradingWatch();
                    cancelButton.Visibility = Visibility.Collapsed; UpdateCompactState();
                }
            }).FileAndForget("Autograde/Action");
            actions.Add(button); panel.Children.Add(button);
            return button;
        }
        void UpdateCompactState()
        {
            bool authenticated = client?.HasSession == true;
            loginPanel.Visibility = authenticated ? Visibility.Collapsed : Visibility.Visible;
            var selected = (assignments.SelectedItem as AssignmentItem)?.Value;
            identity.Text = selected == null ? "수령 코드로 과제를 시작하세요." : (string)selected["course_key"] + " · " + (string)selected["title"];
            bool ready = false;
            if (authenticated && selected != null && !string.IsNullOrWhiteSpace(folder.Text))
                try { Bundle.VerifyWorkspace(folder.Text, client.BaseUrl, (string)selected["assignment_id"]); ready = true; } catch (Exception) { }
            if (downloadButton != null) downloadButton.Visibility = authenticated && selected != null && !ready ? Visibility.Visible : Visibility.Collapsed;
            if (submitButton != null) submitButton.Visibility = authenticated && ready ? Visibility.Visible : Visibility.Collapsed;
            if (folderButton != null) folderButton.Visibility = authenticated && ready ? Visibility.Visible : Visibility.Collapsed;
            if (downloadDiagnostic == null) downloadStatus.Text = ready ? "파일 준비 완료 · 저장한 뒤 제출하세요." : authenticated ? "수락 완료 · 과제 파일을 다운로드하세요." : "로그인 필요";
        }
        async Task DownloadAsync(CancellationToken ct)
        {
            RequireClient(); var selected = Selected; string id = Id;
            using (var dialog = new Forms.FolderBrowserDialog { Description = "새 과제 폴더를 만들 상위 폴더를 선택하세요." })
            {
                if (dialog.ShowDialog() != Forms.DialogResult.OK) return;
                var diagnostic = downloadDiagnostic = new DownloadDiagnostic();
                pages.SelectedIndex = 0; diagnosticDetails.Visibility = Visibility.Visible;
                var target = Path.Combine(dialog.SelectedPath, "autograde-" + DateTime.Now.ToString("yyyyMMdd-HHmmss") + "-" + Guid.NewGuid().ToString("N").Substring(0, 6));
                IProgress<string> progress = new Progress<string>(value => { if (downloadDiagnostic == diagnostic) downloadStatus.Text = diagnostic.Summary; });
                Action<string> stage = value => {
                    diagnostic.Stage = value;
                    progress.Report(value);
                };
                try {
                    stage("requesting"); await ReportDownloadAsync(id, diagnostic);
                    var bytes = await client.StarterAsync(selected, ct, stage);
                    stage("installing");
                    await Task.Run(() => Bundle.ExtractStarter(bytes, target, client.BaseUrl, id, ct), ct);
                    folder.Text = target; diagnostic.Stage = "files_ready"; diagnostic.Outcome = "succeeded";
                    await ReportDownloadAsync(id, diagnostic);
                    diagnostic.Stage = "opening";
                    await OpenDownloadedFolderAsync(target, ct, diagnostic);
                }
                catch (Exception ex) { diagnostic.Fail(ex, ct.IsCancellationRequested); }
                await ReportDownloadAsync(id, diagnostic);
                if (downloadDiagnostic == diagnostic) {
                    downloadStatus.Text = diagnostic.Summary + "\n" + DownloadDiagnostic.Guidance(diagnostic.Code);
                    diagnosticText.Text = diagnostic.Details;
                    output.Text = diagnostic.Summary + " · " + diagnostic.Delivery +
                        (diagnostic.Code == null ? "" : "\n" + diagnostic.Code + " · " + DownloadDiagnostic.Guidance(diagnostic.Code));
                    diagnosticDetails.IsExpanded = diagnostic.Code != null;
                }
            }
        }
        async Task ReportDownloadAsync(string id, DownloadDiagnostic diagnostic)
        {
            diagnostic.Delivery = await client.ReportDownloadAsync(id, diagnostic.Payload(typeof(AssignmentControl).Assembly.GetName().Version.ToString(3)), lifetime.Token);
            if (downloadDiagnostic == diagnostic) diagnosticText.Text = diagnostic.Details;
        }
        async Task OpenDownloadedFolderAsync(string target, CancellationToken ct, DownloadDiagnostic diagnostic = null)
        {
            output.Text = "과제 파일 준비 완료: " + target;
            try
            {
                await WorkspaceOpener.OpenAsync(target, ct);
                if (diagnostic != null) diagnostic.OpenOutcome = "opened";
                output.AppendText("\n다운로드한 위치의 과제 폴더를 열었습니다. IDE의 저장·신뢰 확인이 표시되면 확인하세요.");
            }
            catch (OperationCanceledException ex)
            { diagnostic?.Fail(ex, true); output.AppendText("\n폴더 열기를 취소했습니다. 다운로드한 파일은 보존됩니다. ‘다운로드한 과제 폴더 열기’로 다시 여세요."); }
            catch (Exception ex)
            { diagnostic?.Fail(ex, false); output.AppendText("\n자동 열기가 완료되지 않았습니다. 파일은 보존됩니다. ‘다운로드한 과제 폴더 열기’로 재시도하거나 파일 → 열기 → 폴더에서 위 경로를 선택하세요."); }
        }
        void RequireClient()
        {
            if (client == null || ServiceClient.NormalizeAddress(address.Text) != client.BaseUrl)
                throw new InvalidOperationException("먼저 서버 주소를 적용하세요. 주소가 바뀌면 다시 로그인해야 합니다.");
        }
        void ClearScreen(bool clearResults = true)
        {
            PauseGradingWatch(); receiptWatch = null;
            showingInspected = false;
            liveResult.Clear(); inspectedResult.Clear();
            receiptStatus.Text = "이번 로그인에서 접수한 제출이 없습니다."; gradingOutput.Clear();
            assignments.Items.Clear(); history.Items.Clear(); folder.Clear(); claim.Clear(); if (clearResults) output.Clear();
            downloadDiagnostic = null; diagnosticText.Clear(); diagnosticDetails.Visibility = Visibility.Collapsed;
            UpdateCompactState();
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
                if (!showingInspected) liveResult.Show(result, "마지막 접수 · " + receiptWatch?.Title);
                gradingOutput.Text = "채점 상태 확인 · " + DateTime.Now.ToString("HH:mm:ss");
            }
            else
            {
                showingInspected = true;
                inspectedResult.Show(result, context ?? "직접 조회한 결과");
                liveResult.Clear(); pages.SelectedIndex = 1;
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
