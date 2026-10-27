using System;
using System.Diagnostics;
using System.Threading;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Threading;
using Autograde.Core;
using Microsoft.VisualStudio.Shell;
using Microsoft.VisualStudio.Threading;

namespace Autograde.VisualStudio
{
    // A passive student view. The separate Agent and supervisor own all restriction commands.
    internal sealed class ExamModeControl : UserControl, IDisposable
    {
        readonly TextBox address = new TextBox();
        readonly PasswordBox code = new PasswordBox { MaxLength = 8 };
        readonly TextBlock summary = new TextBlock { Text = "일반 과제 모드 · 시험 연결 안 됨", TextWrapping = TextWrapping.Wrap };
        readonly TextBlock details = new TextBlock { TextWrapping = TextWrapping.Wrap };
        readonly TextBlock scope = new TextBlock { TextWrapping = TextWrapping.Wrap };
        readonly StackPanel inputs = new StackPanel();
        readonly Button connect = new Button { Content = "시험 연결" };
        readonly Button disconnect = new Button { Content = "화면 연결 끊기", Visibility = Visibility.Collapsed };
        readonly DispatcherTimer timer = new DispatcherTimer { Interval = TimeSpan.FromSeconds(1) };
        readonly JoinableTaskFactory jobs;
        readonly CancellationTokenSource lifetime = new CancellationTokenSource();
        CancellationTokenSource request;
        ExamControlClient client;
        ExamStatus status;
        Stopwatch sinceResponse;
        object loginIdentity;
        string origin, assignmentId;
        int generation;
        bool disposed, working, polling;
        DateTimeOffset nextPoll;

        public ExamModeControl(JoinableTaskFactory jobs)
        {
            this.jobs = jobs;
            ThemeResources.Apply(this, code, new ListBox[0], address);
            ThemeResources.Label(summary); ThemeResources.Label(details); ThemeResources.Label(scope);
            ThemeResources.Button(connect); ThemeResources.Button(disconnect);
            var body = new StackPanel { Margin = new Thickness(0, 6, 0, 6) }; Content = body;
            body.Children.Add(summary); body.Children.Add(scope); body.Children.Add(details); body.Children.Add(inputs);
            Label(inputs, "감독자가 안내한 시험 관리 서버 주소 (과제 서버와 별도)"); inputs.Children.Add(address);
            Label(inputs, "좌석 확인용 8자리 시험 연결 코드 · 과제 수령 코드와 다름"); inputs.Children.Add(code);
            connect.Margin = disconnect.Margin = new Thickness(0, 6, 0, 0);
            inputs.Children.Add(connect); body.Children.Add(disconnect);
            Label(body, "별도 Agent 설치가 필요합니다. 이 화면을 닫거나 제출·로그아웃해도 통신 제한은 해제되지 않습니다. 제한 시작·해제는 감독자가 관리합니다.");
            connect.Click += (_, __) => jobs.RunAsync(ConnectAsync).FileAndForget("Autograde/ExamConnect");
            disconnect.Click += (_, __) => jobs.RunAsync(DisconnectAsync).FileAndForget("Autograde/ExamDisconnect");
            address.TextChanged += (_, __) => { if (client != null || working) Reset("시험 주소 변경 · 다시 연결하세요."); };
            timer.Tick += (_, __) => Tick();
            Loaded += (_, __) => { if (!disposed) { timer.Start(); Tick(); } };
            Unloaded += (_, __) => timer.Stop();
            UpdateButtons();
        }

        static void Label(Panel panel, string text)
        {
            var label = new TextBlock { Text = text, TextWrapping = TextWrapping.Wrap, Margin = new Thickness(0, 6, 0, 4) };
            ThemeResources.Label(label); panel.Children.Add(label);
        }

        public void SetScope(object identity, string serviceOrigin, string selectedAssignment)
        {
            if (ReferenceEquals(identity, loginIdentity) && serviceOrigin == origin && selectedAssignment == assignmentId) return;
            loginIdentity = identity; origin = serviceOrigin; assignmentId = selectedAssignment;
            scope.Text = selectedAssignment == null ? "" : "현재 과제 ID: " + selectedAssignment + "\n채점 서버: " + serviceOrigin;
            Reset(identity == null || selectedAssignment == null ? "일반 과제 모드 · 먼저 로그인하고 수락한 과제를 선택하세요." : "일반 과제 모드 · 시험 연결 안 됨");
        }

        bool Current(int expected, ExamControlClient candidate) => !disposed && generation == expected && ReferenceEquals(client, candidate) && loginIdentity != null;

        async Task ConnectAsync()
        {
            if (disposed || working || loginIdentity == null || assignmentId == null) return;
            string entered = code.Password; code.Clear();
            Reset("시험 연결 중 · 상태 확인 전");
            if (entered.Length == 0) { summary.Text = "8자리 시험 연결 코드를 입력하세요."; return; }
            working = true; UpdateButtons();
            int expected = generation;
            ExamControlClient candidate = null;
            var cancellation = CancellationTokenSource.CreateLinkedTokenSource(lifetime.Token); request = cancellation;
            try
            {
                candidate = new ExamControlClient(address.Text, origin, assignmentId); client = candidate;
                var value = await candidate.ConnectAsync(entered, cancellation.Token);
                if (!Current(expected, candidate)) return;
                status = value; sinceResponse = Stopwatch.StartNew(); nextPoll = DateTimeOffset.UtcNow.AddSeconds(5);
                summary.Text = status.Label(TimeSpan.Zero); RenderDetails();
            }
            catch (Exception ex)
            {
                if (!disposed && generation == expected)
                {
                    candidate?.Dispose(); client = null; status = null;
                    summary.Text = Failure(ex); details.Text = "통신 제한 여부는 감독자에게 확인하세요.";
                }
            }
            finally
            {
                entered = null; cancellation.Dispose();
                if (ReferenceEquals(request, cancellation)) request = null;
                if (generation == expected) { working = false; UpdateButtons(); }
            }
        }

        async Task DisconnectAsync()
        {
            var previous = client;
            // Revoke only this passive IDE token. No release command exists in the client.
            client = null; Reset("화면 연결을 끊었습니다. 통신 제한은 유지될 수 있으므로 감독자에게 확인하세요.");
            if (previous == null) return;
            int expected = generation;
            try { await previous.DisconnectAsync(lifetime.Token); }
            catch (Exception) { if (!disposed && expected == generation) summary.Text = "로컬 연결 정보 삭제됨 · 서버 연결 해제 확인 실패. 통신 제한은 감독자에게 확인하세요."; }
            finally { previous.Dispose(); }
        }

        void Tick()
        {
            if (disposed || client == null || working) return;
            if (!client.HasSession) { Reset("시험 화면 연결 만료 · 새 연결 코드를 요청하세요. 통신 제한 해제를 의미하지 않습니다."); return; }
            if (status != null && sinceResponse != null) { summary.Text = status.Label(sinceResponse.Elapsed); RenderDetails(); }
            if (!polling && DateTimeOffset.UtcNow >= nextPoll) jobs.RunAsync(PollAsync).FileAndForget("Autograde/ExamStatus");
        }

        async Task PollAsync()
        {
            var current = client;
            if (disposed || polling || current == null || working) return;
            polling = true; int expected = generation;
            var cancellation = CancellationTokenSource.CreateLinkedTokenSource(lifetime.Token); request = cancellation;
            try
            {
                var value = await current.StatusAsync(cancellation.Token);
                if (!Current(expected, current)) return;
                status = value; sinceResponse = Stopwatch.StartNew(); summary.Text = value.Label(TimeSpan.Zero); RenderDetails();
            }
            catch (Exception ex)
            {
                if (Current(expected, current))
                {
                    // Do not redraw a previous installed result on the next timer tick after a failed request.
                    status = null; summary.Text = "상태 확인 불가 · " + Failure(ex); details.Text = "이전 설치 보고는 현재 상태가 아닙니다. 감독자에게 확인하세요.";
                    if (!current.HasSession) { current.Dispose(); client = null; UpdateButtons(); }
                }
            }
            finally
            {
                cancellation.Dispose(); if (ReferenceEquals(request, cancellation)) request = null;
                if (generation == expected) { polling = false; nextPoll = DateTimeOffset.UtcNow.AddSeconds(5); }
            }
        }

        void RenderDetails()
        {
            if (status == null) return;
            details.Text = "시험: " + status.Title + " · " + (status.ExamState == "ended" ? "종료" : "진행 중") +
                "\n좌석: " + status.SeatId + " · Agent: " + (status.Backend ?? "모의") +
                "\n제한 기한: " + (status.LeaseExpiresAt?.ToLocalTime().ToString("yyyy-MM-dd HH:mm:ss") ?? "보고 없음") +
                "\n화면 연결 만료: " + status.ExpiresAt.ToLocalTime().ToString("HH:mm:ss") +
                "\n마지막 갱신: " + status.ServerTime.ToLocalTime().ToString("HH:mm:ss") + " · 5초마다 확인";
        }

        static string Failure(Exception ex) => ex is ExamControlError ? ex.Message : ex is OperationCanceledException ? "요청 취소 또는 시간 초과. 다시 연결 상태를 확인하세요." :
            ex is ArgumentException ? "시험 주소·8자리 코드를 확인하세요. 외부 주소는 HTTPS가 필요합니다." : "시험 서버 응답을 확인할 수 없습니다. 주소·인증서·방화벽을 확인하세요.";

        public void Reset(string message = "일반 과제 모드 · 시험 연결 안 됨")
        {
            generation++; request?.Cancel(); client?.Dispose(); client = null; status = null; sinceResponse = null;
            working = polling = false; code.Clear(); summary.Text = message; details.Text = ""; UpdateButtons();
        }
        void UpdateButtons()
        {
            inputs.Visibility = client?.HasSession == true ? Visibility.Collapsed : Visibility.Visible;
            connect.IsEnabled = !working && loginIdentity != null && assignmentId != null;
            address.IsEnabled = code.IsEnabled = !working;
            disconnect.Visibility = client != null ? Visibility.Visible : Visibility.Collapsed;
        }
        public void Dispose()
        {
            if (disposed) return; disposed = true; timer.Stop(); Reset(); lifetime.Cancel();
        }
    }
}
