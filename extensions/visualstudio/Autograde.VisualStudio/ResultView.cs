using System.Windows;
using System.Windows.Automation;
using System.Windows.Controls;
using Autograde.Core;
using Microsoft.VisualStudio.PlatformUI;
using Microsoft.VisualStudio.Shell;
using Newtonsoft.Json.Linq;

namespace Autograde.VisualStudio
{
    internal sealed class ResultView : UserControl
    {
        readonly StackPanel content = new StackPanel();
        public ResultView()
        {
            var border = new Border { Child = content, Padding = new Thickness(12), Margin = new Thickness(0, 8, 0, 8), BorderThickness = new Thickness(1), CornerRadius = new CornerRadius(5) };
            border.SetResourceReference(Border.BorderBrushProperty, CommonControlsColors.TextBoxBorderBrushKey);
            border.SetResourceReference(Border.BackgroundProperty, EnvironmentColors.ToolWindowBackgroundBrushKey);
            Content = border;
            Visibility = Visibility.Collapsed;
        }
        TextBlock Text(string value, double size = 13, bool strong = false)
        {
            var text = new TextBlock { Text = value, TextWrapping = TextWrapping.Wrap, FontSize = size,
                FontWeight = strong ? FontWeights.SemiBold : FontWeights.Normal, Margin = new Thickness(0, 3, 0, 5) };
            ThemeResources.Label(text); content.Children.Add(text); return text;
        }
        public void Clear() { content.Children.Clear(); Visibility = Visibility.Collapsed; }
        public void ShowNoSubmission(string title)
        {
            content.Children.Clear(); Visibility = Visibility.Visible;
            AutomationProperties.SetName(this, title + " · 아직 제출하지 않았습니다");
            Text(title, 13, true);
            Text("아직 제출하지 않았습니다", 20, true);
            Text("이 과제에 접수된 제출이 없습니다. 점수는 제출하고 채점 결과가 공개된 뒤 표시됩니다.");
            Text("다음 할 일", 14, true);
            Text("과제 파일을 준비하고 모두 저장한 뒤 ‘파일 확인 후 제출’을 누르세요.");
        }
        public void Show(JObject result, string context)
        {
            var model = ResultPresentation.From(result);
            content.Children.Clear(); Visibility = Visibility.Visible;
            AutomationProperties.SetName(this, context + " · " + model.Headline + " · " + model.Score);
            Text(context, 13, true);
            Text(model.Headline, 20, true);
            Text(model.Score, 28, true);
            Text(model.PreviousBest, 12);
            if (model.Percent.HasValue)
            {
                var progress = new ProgressBar { Minimum = 0, Maximum = 100, Value = model.Percent.Value, Height = 8, Margin = new Thickness(0, 4, 0, 8) };
                progress.SetResourceReference(Control.ForegroundProperty, CommonControlsColors.TextBoxBorderFocusedBrushKey);
                progress.SetResourceReference(Control.BackgroundProperty, CommonControlsColors.TextBoxBackgroundBrushKey);
                AutomationProperties.SetName(progress, "자동채점 총점 달성률");
                content.Children.Add(progress);
            }
            Text(model.Summary);
            Text("다음 할 일", 14, true); Text(model.NextStep);
            foreach (var item in model.Criteria)
            {
                var label = new TextBlock { Text = item.Status + " · " + item.Title + "  " + item.Score, TextWrapping = TextWrapping.Wrap };
                var feedback = new TextBlock { Text = item.Feedback, TextWrapping = TextWrapping.Wrap, Margin = new Thickness(8) };
                ThemeResources.Label(label); ThemeResources.Label(feedback);
                content.Children.Add(new Expander { Header = label, Content = feedback, Margin = new Thickness(0, 4, 0, 4) });
            }
            var details = new TextBox { Text = model.Details, IsReadOnly = true, TextWrapping = TextWrapping.Wrap,
                AcceptsReturn = true, MaxHeight = 180, VerticalScrollBarVisibility = ScrollBarVisibility.Auto, Padding = new Thickness(6) };
            details.SetResourceReference(FrameworkElement.StyleProperty, VsResourceKeys.ThemedDialogTextBoxStyleKey);
            content.Children.Add(new Expander { Header = "접수번호·상세 진단 (복사 가능)", Content = details });
        }
    }
}
